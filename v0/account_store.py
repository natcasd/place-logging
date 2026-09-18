"""Account-scoped access to the multi-user schema; never opens a legacy database.

The caller supplies a server-verified internal account ID. Every operation checks
that account inside the same transaction as its queries or mutations.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Any

from multi_user_migration import APPLICATION_ID, SCHEMA_VERSION
from store import (
    _activity_payload, _decode_json_list, _entries_payload, _identity_key,
    _outcomes_payload, _sources_payload,
)


class AccountUnavailable(Exception):
    pass


class RecordNotFound(Exception):
    pass


class AccountConflict(Exception):
    pass


@dataclass(frozen=True)
class AccountStore:
    db_path: Path
    user_id: str

    @contextmanager
    def _transaction(self, *, write: bool = False) -> Iterator[sqlite3.Connection]:
        # mode=rw prevents an incorrect path from silently creating a database.
        con = sqlite3.connect(self.db_path.resolve().as_uri() + '?mode=rw', uri=True)
        con.row_factory = sqlite3.Row
        try:
            con.execute('PRAGMA foreign_keys = ON')
            if (con.execute('PRAGMA application_id').fetchone()[0] != APPLICATION_ID
                    or con.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION):
                raise RuntimeError('Account storage requires an explicitly migrated database')
            con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            if not self.user_id or con.execute(
                "SELECT 1 FROM users WHERE id = ? AND status = 'active'", (self.user_id,),
            ).fetchone() is None:
                raise AccountUnavailable()
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def require_active(self) -> None:
        with self._transaction():
            pass

    def _sequence(self, con: sqlite3.Connection) -> int:
        return con.execute(
            'UPDATE users SET mutation_sequence = mutation_sequence + 1 WHERE id = ? '
            'RETURNING mutation_sequence', (self.user_id,),
        ).fetchone()[0]

    def _run(self, con: sqlite3.Connection, ingest_id: int) -> sqlite3.Row:
        row = con.execute("SELECT * FROM ingest_runs WHERE user_id = ? AND id = ? AND status != 'cancelled'",
                          (self.user_id, ingest_id)).fetchone()
        if row is None:
            raise RecordNotFound()
        return row

    def require_run(self, ingest_id: int) -> None:
        with self._transaction() as con:
            self._run(con, ingest_id)

    def entries(self, limit: int = 200) -> list[dict[str, Any]]:
        if not 1 <= limit <= 1000:
            raise ValueError('limit must be between 1 and 1000')
        with self._transaction() as con:
            selected = con.execute('''
                SELECT t.id, MAX(i.created_at) AS latest_saved_at
                FROM recommendations t
                JOIN recommendation_mentions ts ON ts.entry_id = t.id AND ts.user_id = t.user_id
                JOIN captures i ON i.id = ts.item_id AND i.user_id = ts.user_id
                LEFT JOIN post_processing_cache pc ON pc.id = i.post_cache_id
                WHERE t.user_id = ? AND ts.removed_at IS NULL
                GROUP BY t.id ORDER BY latest_saved_at DESC, t.id DESC LIMIT ?
            ''', (self.user_id, limit)).fetchall()
            entry_ids = [row['id'] for row in selected]
            if not entry_ids:
                return []
            placeholders = ','.join('?' for _ in entry_ids)
            rows = con.execute(f'''
                SELECT t.id, t.name, t.entry_type, t.starts_at, t.ends_at,
                    t.recurrence_text, t.location_id,
                    me.provider AS movie_provider, me.provider_id AS movie_provider_id,
                    me.resolved_title AS movie_resolved_title, me.release_year AS movie_release_year,
                    me.letterboxd_url AS movie_letterboxd_url, me.match_status AS movie_match_status,
                    me.match_confidence AS movie_match_confidence,
                    l.google_place_id, l.display_name AS location_name, l.lat, l.lng,
                    l.formatted_address, l.google_maps_url,
                    ts.id AS source_connection_id, ts.item_id, ts.ordinal, ts.source_name,
                    ts.source_type, ts.description, ts.dishes_json, ts.why_its_cool, ts.tags_json,
                    ts.timestamp_seconds, ts.slide_index, ts.resolution_status, ts.location_query,
                    i.source_url, COALESCE(i.raw_payload_json, json_set(COALESCE(
                        json_extract(pc.result_json, '$.metadata'),
                        json_extract(i.private_result_json, '$.metadata'), '{{}}'),
                        '$.source_platform', COALESCE(i.source_platform, 'other'))) AS raw_payload_json, i.created_at
                FROM recommendations t
                LEFT JOIN locations l ON l.id = t.location_id
                LEFT JOIN movie_enrichments me ON me.entry_id = t.id AND me.user_id = t.user_id
                JOIN recommendation_mentions ts ON ts.entry_id = t.id AND ts.user_id = t.user_id
                JOIN captures i ON i.id = ts.item_id AND i.user_id = ts.user_id
                LEFT JOIN post_processing_cache pc ON pc.id = i.post_cache_id
                WHERE t.user_id = ? AND t.id IN ({placeholders}) AND ts.removed_at IS NULL
                ORDER BY i.created_at DESC, ts.id DESC
            ''', [self.user_id, *entry_ids]).fetchall()
            return _entries_payload(rows, entry_ids)

    def sources(self, limit: int = 200) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError('limit must be between 1 and 500')
        with self._transaction() as con:
            rows = con.execute('''
                SELECT i.id, i.source_url, COALESCE(i.raw_payload_json, json_set(COALESCE(
                        json_extract(pc.result_json, '$.metadata'),
                        json_extract(i.private_result_json, '$.metadata'), '{}'),
                        '$.source_platform', COALESCE(i.source_platform, 'other'))) AS raw_payload_json, i.created_at,
                    COUNT(DISTINCT ts.entry_id) AS entry_count
                FROM captures i
                LEFT JOIN post_processing_cache pc ON pc.id = i.post_cache_id
                LEFT JOIN recommendation_mentions ts ON ts.item_id = i.id
                    AND ts.user_id = i.user_id AND ts.removed_at IS NULL
                WHERE i.user_id = ? GROUP BY i.id
                ORDER BY i.created_at DESC, i.id DESC LIMIT ?
            ''', (self.user_id, limit)).fetchall()
            return _sources_payload(rows)

    def _outcomes(self, con: sqlite3.Connection, item_id: int) -> list[dict[str, Any]]:
        rows = con.execute('''
            SELECT t.id AS entry_id, ts.source_name AS name, ts.source_type AS entry_type,
                ts.description, t.location_id, l.display_name AS location_name, l.lat, l.lng,
                l.formatted_address, l.google_maps_url, ts.ordinal, ts.timestamp_seconds,
                ts.slide_index, ts.resolution_status, ts.resolution_candidates_json,
                ts.id AS source_connection_id,
                (SELECT MIN(m.id) FROM recommendation_mentions m
                 WHERE m.user_id = ts.user_id AND m.entry_id = t.id AND m.removed_at IS NULL)
                    AS first_source_id,
                (SELECT COUNT(DISTINCT m.item_id) FROM recommendation_mentions m
                 WHERE m.user_id = ts.user_id AND m.entry_id = t.id
                    AND m.removed_at IS NULL AND m.id <= ts.id) AS source_count
            FROM recommendation_mentions ts
            JOIN recommendations t ON t.id = ts.entry_id AND t.user_id = ts.user_id
            JOIN captures i ON i.id = ts.item_id AND i.user_id = ts.user_id
            LEFT JOIN locations l ON l.id = t.location_id
            WHERE ts.user_id = ? AND ts.item_id = ? AND ts.removed_at IS NULL
            ORDER BY ts.ordinal, ts.id
        ''', (self.user_id, item_id)).fetchall()
        return _outcomes_payload(rows)

    def activity(self, limit: int = 200) -> list[dict[str, Any]]:
        if not 1 <= limit <= 500:
            raise ValueError('limit must be between 1 and 500')
        with self._transaction() as con:
            # Keep every request key durably, but present same-source requests
            # accepted before an earlier run finished as one user operation.
            # Event IDs provide an exact transaction order without timestamp
            # precision assumptions; a later deliberate re-share remains visible.
            rows = con.execute('''
                SELECT r.*, COALESCE(i.raw_payload_json, json_set(COALESCE(
                        json_extract(pc.result_json, '$.metadata'),
                        json_extract(i.private_result_json, '$.metadata'), '{}'),
                        '$.source_platform', COALESCE(i.source_platform, 'other'))) AS raw_payload_json FROM ingest_runs r
                LEFT JOIN captures i ON i.id = r.item_id AND i.user_id = r.user_id
                LEFT JOIN post_processing_cache pc ON pc.id = i.post_cache_id
                WHERE r.user_id = ? AND r.status != 'cancelled'
                    AND NOT EXISTS (
                        SELECT 1
                        FROM ingest_runs earlier
                        JOIN ingest_events current_accept
                          ON current_accept.user_id = r.user_id
                         AND current_accept.ingest_run_id = r.id
                         AND current_accept.stage = 'accepted'
                         AND current_accept.status = 'queued'
                        JOIN ingest_events earlier_accept
                          ON earlier_accept.user_id = earlier.user_id
                         AND earlier_accept.ingest_run_id = earlier.id
                         AND earlier_accept.stage = 'accepted'
                         AND earlier_accept.status = 'queued'
                        WHERE r.item_id IS NOT NULL
                          AND earlier.user_id = r.user_id
                          AND earlier.item_id = r.item_id
                          AND earlier.id != r.id
                          AND earlier.status != 'cancelled'
                          AND earlier_accept.id < current_accept.id
                          AND NOT EXISTS (
                              SELECT 1 FROM ingest_events earlier_terminal
                              WHERE earlier_terminal.user_id = earlier.user_id
                                AND earlier_terminal.ingest_run_id = earlier.id
                                AND earlier_terminal.id < current_accept.id
                                AND earlier_terminal.status IN ('completed', 'partial', 'failed')
                          )
                    )
                ORDER BY r.updated_at DESC, r.id DESC LIMIT ?
            ''', (self.user_id, limit)).fetchall()
            activity = []
            for row in rows:
                events = [dict(event) for event in con.execute('''
                    SELECT id, stage, status, message, created_at FROM ingest_events
                    WHERE user_id = ? AND ingest_run_id = ? ORDER BY id
                ''', (self.user_id, row['id']))]
                # Only live owned mentions may supply library IDs. Old JSON
                # snapshots must not restore removed or detached recommendations.
                results = self._outcomes(con, row['item_id']) if row['item_id'] is not None else []
                activity.append(_activity_payload(row, results, events))
            return activity

    def _refresh_activity(self, con: sqlite3.Connection, item_ids: list[int], message: str) -> None:
        for item_id in set(item_ids):
            outcomes = self._outcomes(con, item_id)
            run_status = 'partial' if any(
                r['resolution_status'] in {'needs_review', 'unresolved'} for r in outcomes
            ) else 'completed'
            runs = con.execute('''
                SELECT id FROM ingest_runs WHERE user_id = ? AND item_id = ?
                    AND status IN ('completed', 'partial')
            ''', (self.user_id, item_id)).fetchall()
            for run in runs:
                con.execute('''
                    UPDATE ingest_runs SET status = ?, result_json = NULL, updated_at = CURRENT_TIMESTAMP
                    WHERE user_id = ? AND id = ?
                ''', (run_status, self.user_id, run['id']))
                con.execute('''
                    INSERT INTO ingest_events (user_id, ingest_run_id, stage, status, message)
                    VALUES (?, ?, 'reviewing', 'completed', ?)
                ''', (self.user_id, run['id'], message))

    def delete_entries(self, entry_ids: list[int]) -> dict[str, int]:
        ids = list(dict.fromkeys(entry_ids))
        if not 1 <= len(ids) <= 100:
            raise ValueError('Supply between 1 and 100 recommendation IDs')
        placeholders = ','.join('?' for _ in ids)
        with self._transaction(write=True) as con:
            found = {row[0] for row in con.execute(
                f'SELECT id FROM recommendations WHERE user_id = ? AND id IN ({placeholders})',
                [self.user_id, *ids],
            )}
            if found != set(ids):
                raise RecordNotFound()
            item_ids = [row[0] for row in con.execute(f'''
                SELECT DISTINCT item_id FROM recommendation_mentions
                WHERE user_id = ? AND entry_id IN ({placeholders}) AND removed_at IS NULL
            ''', [self.user_id, *ids])]
            sequence = self._sequence(con)
            mention_ids = [row[0] for row in con.execute(f'''
                SELECT id FROM recommendation_mentions
                WHERE user_id = ? AND entry_id IN ({placeholders}) AND removed_at IS NULL
            ''', [self.user_id, *ids])]
            self._remove_mentions(con, mention_ids, sequence)
            con.execute(f'DELETE FROM recommendations WHERE user_id = ? AND id IN ({placeholders})',
                        [self.user_id, *ids])
            self._refresh_activity(con, item_ids, 'Deleted recommendation')
            return {'deleted_entries': len(ids), 'deleted_sources': 0}

    def _remove_mentions(self, con: sqlite3.Connection, ids: list[int], sequence: int) -> None:
        for mention_id in ids:
            con.execute('''
                UPDATE recommendation_mentions SET entry_id = NULL, removed_at = CURRENT_TIMESTAMP,
                    last_user_change_sequence = ?, source_name = '', source_type = '', description = '',
                    dishes_json = NULL, why_its_cool = NULL, tags_json = NULL, timestamp_seconds = NULL,
                    slide_index = NULL, resolution_status = 'removed', resolution_candidates_json = NULL,
                    location_query = NULL WHERE user_id = ? AND id = ? AND removed_at IS NULL
            ''', (sequence, self.user_id, mention_id))

    def delete_mention(self, mention_id: int) -> dict[str, int]:
        with self._transaction(write=True) as con:
            row = con.execute('''
                SELECT entry_id, item_id FROM recommendation_mentions
                WHERE user_id = ? AND id = ? AND removed_at IS NULL
            ''', (self.user_id, mention_id)).fetchone()
            if row is None:
                raise RecordNotFound()
            self._remove_mentions(con, [mention_id], self._sequence(con))
            deleted = con.execute('''
                DELETE FROM recommendations WHERE user_id = ? AND id = ? AND NOT EXISTS
                (SELECT 1 FROM recommendation_mentions WHERE user_id = ? AND entry_id = ? AND removed_at IS NULL)
            ''', (self.user_id, row['entry_id'], self.user_id, row['entry_id'])).rowcount
            self._refresh_activity(con, [row['item_id']], 'Deleted mention')
            return {'mention_id': mention_id, 'deleted_entries': deleted}

    def delete_failed_activity(self, ingest_id: int) -> None:
        with self._transaction(write=True) as con:
            run = self._run(con, ingest_id)
            if run['status'] not in {'failed', 'retry_scheduled'}:
                raise AccountConflict('Only failed or scheduled Activity can be deleted')
            if run['item_id'] is not None and con.execute('''
                SELECT 1 FROM recommendation_mentions
                WHERE user_id = ? AND item_id = ? AND removed_at IS NULL
            ''', (self.user_id, run['item_id'])).fetchone():
                raise AccountConflict('Activity with saved recommendations cannot be deleted here')
            self._sequence(con)
            con.execute('''
                UPDATE ingest_runs SET status = 'cancelled', stage = 'cancelled', result_json = NULL,
                    error_type = NULL, error_message = NULL, failure_kind = NULL, retryable = 0,
                    next_retry_at = NULL, updated_at = CURRENT_TIMESTAMP, completed_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND id = ?
            ''', (self.user_id, ingest_id))
            con.execute('DELETE FROM ingest_events WHERE user_id = ? AND ingest_run_id = ?',
                        (self.user_id, ingest_id))
            # A Capture may have other submission histories and removed mentions.
            # Deleting one failed Activity must not cascade through those records.

    def confirm_activity_location(self, ingest_id: int, entry_id: int, candidate_id: str,
                                  *, mention_id: int | None = None) -> dict[str, Any]:
        with self._transaction(write=True) as con:
            rows = con.execute('''
                SELECT ts.*, t.starts_at, t.ends_at, t.recurrence_text
                FROM ingest_runs r
                JOIN recommendation_mentions ts ON ts.item_id = r.item_id AND ts.user_id = r.user_id
                JOIN recommendations t ON t.id = ts.entry_id AND t.user_id = ts.user_id
                WHERE r.user_id = ? AND r.id = ? AND t.id = ? AND ts.removed_at IS NULL
                    AND r.status != 'cancelled' AND (? IS NULL OR ts.id = ?)
            ''', (self.user_id, ingest_id, entry_id, mention_id, mention_id)).fetchall()
            if not rows:
                raise RecordNotFound()
            if len(rows) > 1:
                raise AccountConflict('Select the specific mention to confirm')
            row = rows[0]
            if row['resolution_status'] != 'needs_review':
                raise AccountConflict('This recommendation no longer needs location review')
            candidate = next((value for value in _decode_json_list(row['resolution_candidates_json'])
                              if isinstance(value, dict) and value.get('id') == candidate_id), None)
            if candidate is None:
                raise AccountConflict('Select one of the available location candidates')
            location = candidate.get('location') or {}
            if location.get('latitude') is None or location.get('longitude') is None:
                raise AccountConflict('The selected location is incomplete')
            extracted = {key: row[key] for key in ('starts_at', 'ends_at', 'recurrence_text', 'location_query')}
            extracted.update(extracted_name=row['source_name'])
            key, normalized, kind = _identity_key(extracted, row['source_type'], candidate_id)
            existing = con.execute('''
                SELECT id FROM recommendations WHERE user_id = ? AND identity_key = ? AND id != ?
            ''', (self.user_id, key, entry_id)).fetchone()
            # Private review cannot overwrite public place metadata used by others.
            con.execute('''
                INSERT INTO locations (google_place_id, display_name, lat, lng, formatted_address, google_maps_url)
                VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT(google_place_id) DO NOTHING
            ''', (candidate_id, (candidate.get('displayName') or {}).get('text'),
                  location['latitude'], location['longitude'], candidate.get('formattedAddress'),
                  candidate.get('googleMapsUri')))
            location_id = con.execute('SELECT id FROM locations WHERE google_place_id = ?', (candidate_id,)).fetchone()[0]
            sequence = self._sequence(con)
            siblings = con.execute('''
                SELECT 1 FROM recommendation_mentions WHERE user_id = ? AND entry_id = ?
                    AND id != ? AND removed_at IS NULL LIMIT 1
            ''', (self.user_id, entry_id, row['id'])).fetchone()
            target_id = existing['id'] if existing else entry_id
            if not existing and siblings:
                target_id = con.execute('''
                    INSERT INTO recommendations (user_id, name, normalized_name, entry_type, type_key,
                        identity_key, location_id, starts_at, ends_at, recurrence_text)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (self.user_id, row['source_name'], normalized, row['source_type'], kind, key,
                      location_id, row['starts_at'], row['ends_at'], row['recurrence_text'])).lastrowid
            if target_id != entry_id:
                con.execute('''
                    UPDATE recommendation_mentions SET entry_id = ? WHERE user_id = ? AND id = ?
                ''', (target_id, self.user_id, row['id']))
                con.execute('''
                    DELETE FROM recommendations WHERE user_id = ? AND id = ? AND NOT EXISTS
                    (SELECT 1 FROM recommendation_mentions WHERE user_id = ? AND entry_id = ? AND removed_at IS NULL)
                ''', (self.user_id, entry_id, self.user_id, entry_id))
            else:
                con.execute('''
                    UPDATE recommendations SET location_id = ?, identity_key = ?, normalized_name = ?,
                        type_key = ?, updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND id = ?
                ''', (location_id, key, normalized, kind, self.user_id, entry_id))
            con.execute('''
                UPDATE recommendation_mentions SET resolution_status = 'user_confirmed',
                    resolution_candidates_json = NULL, last_user_change_sequence = ?
                WHERE user_id = ? AND id = ? AND removed_at IS NULL
            ''', (sequence, self.user_id, row['id']))
            # Changing a grouped recommendation can affect its other captures too.
            item_ids = [r[0] for r in con.execute('''
                SELECT DISTINCT item_id FROM recommendation_mentions
                WHERE user_id = ? AND entry_id IN (?, ?) AND removed_at IS NULL
            ''', (self.user_id, entry_id, target_id))]
            self._refresh_activity(con, item_ids, 'Confirmed location')
            return next(r for r in self._outcomes(con, row['item_id']) if r['source_connection_id'] == row['id'])
