"""Trusted worker operations on shared public-post jobs.

This store is never exposed as an account API. Private delivery always delegates
to CaptureStore, which rechecks the owner and deletion order transactionally.
"""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from account_store import AccountConflict, AccountUnavailable, RecordNotFound
from capture_result import OriginalResult, validate_result
from capture_store import CaptureStore
from multi_user_migration import APPLICATION_ID, SCHEMA_VERSION
from public_processing_adapter import PlaceLookup
from retry_policy import classify_failure, retry_delay_seconds


class LeaseLost(Exception):
    """A replacement worker owns this job; the old result must be discarded."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')


@dataclass(frozen=True)
class PostLease:
    id: int
    token: str
    canonical_url: str
    platform: str
    attempt: int


@dataclass(frozen=True)
class PostProcessingStore:
    db_path: Path
    processing_version: str
    lease_seconds: int = 120
    max_attempts: int = 4
    max_active: int = 1
    now: Callable[[], datetime] = utc_now

    def __post_init__(self):
        if (not self.processing_version.strip() or self.lease_seconds < 3
                or not 1 <= self.max_attempts <= 20 or not 1 <= self.max_active <= 10):
            raise ValueError('Invalid worker configuration')

    @contextmanager
    def _transaction(self, *, write=False):
        con = sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=rw', uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA foreign_keys = ON')
            if (con.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or con.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                raise RuntimeError('Worker requires an explicitly migrated database')
            con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def _members(self, con, job, *, due_only=False):
        return con.execute('''
            SELECT r.* FROM ingest_runs r
            JOIN captures c ON c.id = r.item_id AND c.user_id = r.user_id
            JOIN users u ON u.id = r.user_id AND u.status = 'active'
            WHERE c.input_kind = 'public_post' AND c.materialization_state != 'legacy_unverified'
                AND c.source_platform = ? AND c.source_post_id = ?
                AND (c.post_cache_id IS NULL OR c.post_cache_id = ?)
                AND r.intent != 'legacy' AND r.accepted_sequence > 0
                AND r.status IN ('queued', 'processing', 'retry_scheduled')
                AND (? = 0 OR r.status != 'retry_scheduled' OR r.next_retry_at <= ?)
            ORDER BY r.id
        ''', (job['platform'], job['post_id'], job['id'], int(due_only), timestamp(self.now()))).fetchall()

    def _event(self, con, run, stage, state, message):
        con.execute('''INSERT INTO ingest_events (user_id, ingest_run_id, stage, status, message)
                       VALUES (?, ?, ?, ?, ?)''', (run['user_id'], run['id'], stage, state, message))

    def _leased(self, con, lease):
        job = con.execute('''
            SELECT * FROM post_processing_cache WHERE id = ? AND lease_token = ?
                AND status = 'processing' AND lease_expires_at > ?
        ''', (lease.id, lease.token, timestamp(self.now()))).fetchone()
        if job is None:
            raise LeaseLost()
        return job

    def claim(self) -> PostLease | None:
        with self._transaction(write=True) as con:
            now = timestamp(self.now())
            # Jobs are derived from durable private requests, so acceptance and
            # process crashes never leave an in-memory-only subscription.
            con.execute('''
                INSERT INTO post_processing_cache (platform, post_id, canonical_url, processing_version)
                SELECT DISTINCT c.source_platform, c.source_post_id, c.source_url, ?
                FROM captures c JOIN ingest_runs r ON r.item_id = c.id AND r.user_id = c.user_id
                JOIN users u ON u.id = c.user_id AND u.status = 'active'
                WHERE c.input_kind = 'public_post' AND c.materialization_state = 'pending'
                    AND c.post_cache_id IS NULL AND r.status = 'queued'
                    AND r.intent != 'legacy' AND r.accepted_sequence > 0
                ON CONFLICT(platform, post_id, processing_version) DO NOTHING
            ''', (self.processing_version,))
            if con.execute("SELECT COUNT(*) FROM post_processing_cache WHERE status = 'processing' AND lease_expires_at > ?",
                           (now,)).fetchone()[0] >= self.max_active:
                return None
            jobs = con.execute('''
                SELECT * FROM post_processing_cache WHERE processing_version = ? AND (
                    status IN ('queued', 'failed') OR (status = 'retry_scheduled' AND next_retry_at <= ?)
                    OR (status = 'processing' AND lease_expires_at <= ?)) ORDER BY id
            ''', (self.processing_version, now, now)).fetchall()
            for job in jobs:
                members = self._members(con, job, due_only=True)
                if not members or (job['status'] == 'failed' and not any(r['status'] == 'queued' for r in members)):
                    continue
                if job['status'] == 'processing' and job['attempt_count'] >= self.max_attempts:
                    self._fail(con, job, 'processing', 'interrupted', 'Processing was interrupted.', False, None)
                    continue
                token = uuid.uuid4().hex
                attempt = 1 if job['status'] == 'failed' else job['attempt_count'] + 1
                con.execute('''
                    UPDATE post_processing_cache SET status = 'processing', lease_token = ?, lease_expires_at = ?,
                        attempt_count = ?, next_retry_at = NULL, error_type = NULL, error_message = NULL,
                        updated_at = ? WHERE id = ?
                ''', (token, timestamp(self.now() + timedelta(seconds=self.lease_seconds)), attempt, now, job['id']))
                for run in members:
                    con.execute('''
                        UPDATE ingest_runs SET status = 'processing', stage = 'processing', attempt_count = ?,
                            next_retry_at = NULL, error_type = NULL, error_message = NULL, failure_kind = NULL,
                            retryable = 0, updated_at = ?, completed_at = NULL WHERE user_id = ? AND id = ?
                    ''', (attempt, now, run['user_id'], run['id']))
                    self._event(con, run, 'processing', 'processing', 'Processing shared post')
                return PostLease(job['id'], token, job['canonical_url'], job['platform'], attempt)
            return None

    def renew(self, lease: PostLease) -> None:
        with self._transaction(write=True) as con:
            self._leased(con, lease)
            con.execute('UPDATE post_processing_cache SET lease_expires_at = ?, updated_at = ? WHERE id = ?',
                        (timestamp(self.now() + timedelta(seconds=self.lease_seconds)), timestamp(self.now()), lease.id))

    def progress(self, lease: PostLease, stage: str) -> None:
        messages = {'fetching': 'Downloading source media', 'extracting': 'Finding recommendations',
                    'resolving': 'Resolving locations', 'saving': 'Saving results'}
        if stage not in messages:
            raise ValueError('Unknown processing stage')
        with self._transaction(write=True) as con:
            job = self._leased(con, lease)
            for run in self._members(con, job):
                if run['stage'] == stage and run['status'] == 'processing':
                    continue
                con.execute('''UPDATE ingest_runs SET status = 'processing', stage = ?, attempt_count = ?,
                               next_retry_at = NULL, updated_at = ? WHERE user_id = ? AND id = ?''',
                            (stage, lease.attempt, timestamp(self.now()), run['user_id'], run['id']))
                self._event(con, run, stage, 'processing', messages[stage])

    def publish(self, lease: PostLease, result: OriginalResult | dict, *, locations: tuple[PlaceLookup, ...] = ()) -> None:
        baseline = validate_result(result.model_dump() if isinstance(result, OriginalResult) else result)
        ids = {place_id for mention in baseline.mentions for place_id in
               ([mention.place_id] if mention.place_id else []) + mention.candidate_ids}
        if len(locations) > len(ids):
            raise ValueError('Unexpected place lookups')
        places = [PlaceLookup.model_validate(place.model_dump()) for place in locations]
        if any(place.google_place_id not in ids for place in places):
            raise ValueError('Place lookup is not part of this public result')
        with self._transaction(write=True) as con:
            self._leased(con, lease)
            for place in places:
                con.execute('''
                    INSERT INTO locations (google_place_id, display_name, lat, lng, formatted_address, google_maps_url, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(google_place_id) DO UPDATE SET
                        display_name = excluded.display_name, lat = excluded.lat, lng = excluded.lng,
                        formatted_address = excluded.formatted_address, google_maps_url = excluded.google_maps_url,
                        updated_at = excluded.updated_at
                ''', (place.google_place_id, place.display_name, place.lat, place.lng,
                      place.formatted_address, place.google_maps_url, timestamp(self.now())))
            con.execute('''
                UPDATE post_processing_cache SET status = 'completed', result_json = ?, completed_at = ?,
                    lease_token = NULL, lease_expires_at = NULL, next_retry_at = NULL,
                    error_type = NULL, error_message = NULL, updated_at = ? WHERE id = ?
            ''', (baseline.model_dump_json(), timestamp(self.now()), timestamp(self.now()), lease.id))

    def _fail(self, con, job, stage, kind, message, retryable, due):
        state = 'retry_scheduled' if due is not None else 'failed'
        con.execute('''
            UPDATE post_processing_cache SET status = ?, lease_token = NULL, lease_expires_at = NULL,
                next_retry_at = ?, error_type = ?, error_message = ?, updated_at = ? WHERE id = ?
        ''', (state, due, kind, message, timestamp(self.now()), job['id']))
        for run in self._members(con, job):
            con.execute('''
                UPDATE ingest_runs SET status = ?, stage = ?, error_type = ?, error_message = ?,
                    failure_kind = ?, retryable = ?, attempt_count = ?, next_retry_at = ?,
                    updated_at = ?, completed_at = ? WHERE user_id = ? AND id = ?
            ''', (state, stage, kind, message, kind, int(retryable), job['attempt_count'], due,
                  timestamp(self.now()), None if due else timestamp(self.now()), run['user_id'], run['id']))
            self._event(con, run, stage, state, message)

    def fail(self, lease: PostLease, error: Exception, *, stage: str) -> None:
        decision = classify_failure(error, stage=stage, platform=lease.platform)
        # Raw provider exception strings can contain URLs, response bodies, or
        # credentials. Only fixed public messages enter shared/private storage.
        messages = {'analysis_failed': 'Analysis failed before recommendations could be extracted.',
                    'media_fetch_failed': 'Source media could not be downloaded.',
                    'save_failed': 'Extracted recommendations could not be saved.',
                    'processing_failed': 'This post could not be processed.'}
        with self._transaction(write=True) as con:
            job = self._leased(con, lease)
            due = None
            if decision.retryable and lease.attempt < self.max_attempts:
                delay = retry_delay_seconds(error, attempt=lease.attempt, base_seconds=30, maximum_seconds=1800)
                due = timestamp(self.now() + timedelta(seconds=delay))
            self._fail(con, job, stage, decision.failure_kind, messages[decision.failure_kind], decision.retryable, due)

    def deliver_ready(self, limit: int = 100) -> int:
        if not 1 <= limit <= 1000:
            raise ValueError('Invalid delivery batch size')
        with self._transaction() as con:
            rows = con.execute('''
                SELECT r.id, r.user_id, pc.id AS cache_id FROM ingest_runs r
                JOIN users u ON u.id = r.user_id AND u.status = 'active'
                JOIN captures c ON c.id = r.item_id AND c.user_id = r.user_id
                JOIN post_processing_cache pc ON pc.platform = c.source_platform AND pc.post_id = c.source_post_id
                    AND ((c.post_cache_id IS NULL AND pc.processing_version = ?) OR c.post_cache_id = pc.id)
                WHERE pc.status = 'completed' AND c.input_kind = 'public_post'
                    AND c.materialization_state != 'legacy_unverified' AND r.intent != 'legacy'
                    AND r.accepted_sequence > 0 AND r.status IN ('queued', 'processing', 'retry_scheduled')
                ORDER BY r.id LIMIT ?
            ''', (self.processing_version, limit)).fetchall()
        delivered = 0
        for row in rows:
            try:
                CaptureStore(self.db_path, row['user_id']).materialize(row['id'], public_cache_id=row['cache_id'])
                delivered += 1
            except (AccountUnavailable, RecordNotFound):
                continue  # Account removal/cancellation wins after selection.
            except (AccountConflict, ValueError):
                # One inconsistent private capture must not block other users.
                account = CaptureStore(self.db_path, row['user_id'])
                try:
                    with account._transaction(write=True) as con:
                        run = account._run(con, row['id'])
                        if run['status'] not in {'queued', 'processing', 'retry_scheduled'}:
                            continue
                        con.execute('''UPDATE ingest_runs SET status = 'failed', stage = 'saving',
                            failure_kind = 'save_failed', error_message = 'This saved post needs reconciliation.',
                            retryable = 0, next_retry_at = NULL, completed_at = ?, updated_at = ?
                            WHERE user_id = ? AND id = ?''',
                                    (timestamp(self.now()), timestamp(self.now()), row['user_id'], row['id']))
                        self._event(con, run, 'saving', 'failed', 'This saved post needs reconciliation.')
                except (AccountUnavailable, RecordNotFound):
                    continue
        return delivered

    def active_tokens(self) -> set[str]:
        with self._transaction() as con:
            return {r[0] for r in con.execute("SELECT lease_token FROM post_processing_cache WHERE status = 'processing' AND lease_expires_at > ?",
                                           (timestamp(self.now()),))}
