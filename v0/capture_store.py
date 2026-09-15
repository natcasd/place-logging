"""Durable private capture acceptance and atomic result materialization.

Processing and cache publication are separate worker responsibilities. No model
call, media download, or public cache write occurs in these transactions.
"""
from __future__ import annotations

import json
import re
import sqlite3
from typing import Any
from urllib.parse import urlsplit

from account_store import AccountConflict, AccountStore, RecordNotFound
from capture_result import DirectContext, OriginalMention, validate_result
from multi_user_migration import public_identity
from source_identity import canonical_source_url
from store import _identity_key


class CaptureStore(AccountStore):
    def _request_key(self, key: str) -> None:
        if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z0-9_.:-]{1,128}', key):
            raise ValueError('A stable request key of 1 to 128 characters is required')

    def _capture(self, con: sqlite3.Connection, capture_id: int) -> sqlite3.Row:
        row = con.execute('SELECT * FROM captures WHERE user_id = ? AND id = ?',
                          (self.user_id, capture_id)).fetchone()
        if row is None:
            raise RecordNotFound()
        return row

    def _accepted(self, con: sqlite3.Connection, run: sqlite3.Row) -> dict[str, Any]:
        return {'ingest_id': run['id'], 'item_id': run['item_id'], 'status': run['status'],
                'accepted_sequence': run['accepted_sequence'],
                'saved_entries': self._outcomes(con, run['item_id']) if run['status'] != 'cancelled' else []}

    def _new_run(self, con: sqlite3.Connection, capture_id: int, key: str, intent: str) -> dict[str, Any]:
        capture = self._capture(con, capture_id)
        sequence = self._sequence(con)
        run_id = con.execute('''
            INSERT INTO ingest_runs (user_id, item_id, source_url, source_platform, status, stage,
                idempotency_key, intent, accepted_sequence)
            VALUES (?, ?, ?, ?, 'queued', 'accepted', ?, ?, ?)
        ''', (self.user_id, capture_id, capture['source_url'], capture['source_platform'] or 'other',
              key, intent, sequence)).lastrowid
        con.execute('UPDATE captures SET last_submitted_at = CURRENT_TIMESTAMP WHERE user_id = ? AND id = ?',
                    (self.user_id, capture_id))
        con.execute('''
            INSERT INTO ingest_events (user_id, ingest_run_id, stage, status, message)
            VALUES (?, ?, 'accepted', 'queued', 'Save accepted')
        ''', (self.user_id, run_id))
        return self._accepted(con, self._run(con, run_id))

    def accept_public(self, source_url: str, request_key: str, *, channel: str = 'share_extension') -> dict[str, Any]:
        self._request_key(request_key)
        if channel not in {'share_extension', 'shortcut'}:
            raise ValueError('Unsupported capture channel')
        if not isinstance(source_url, str) or not 1 <= len(source_url) <= 4096:
            raise ValueError('A supported public post URL is required')
        parsed = urlsplit(source_url.strip())
        if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
            raise ValueError('A supported public post URL is required')
        identity = public_identity(source_url)
        if identity is None:
            raise ValueError('Resolve the supported public post URL before accepting it')
        platform, post_id = identity
        canonical = canonical_source_url(source_url)
        with self._transaction(write=True) as con:
            previous = con.execute('SELECT * FROM ingest_runs WHERE user_id = ? AND idempotency_key = ?',
                                   (self.user_id, request_key)).fetchone()
            if previous:
                capture = self._capture(con, previous['item_id']) if previous['item_id'] is not None else None
                if (capture is None or capture['input_kind'] != 'public_post'
                        or (capture['source_platform'], capture['source_post_id']) != identity
                        or previous['intent'] == 'legacy'):
                    raise AccountConflict('Request key was already used for different or legacy input')
                return self._accepted(con, previous)
            captures = con.execute('''
                SELECT * FROM captures WHERE user_id = ? AND source_platform = ? AND source_post_id = ?
            ''', (self.user_id, platform, post_id)).fetchall()
            if any(row['materialization_state'] == 'legacy_unverified' for row in captures):
                raise AccountConflict('This saved post requires legacy reconciliation before re-sharing')
            if captures:
                capture_id = captures[0]['id']
                intent = 'reshare'
            else:
                capture_id = con.execute('''
                    INSERT INTO captures (user_id, vertical, source_url, input_kind, capture_channel,
                        source_platform, source_post_id) VALUES (?, 'recommendation', ?, 'public_post',
                        ?, ?, ?)
                ''', (self.user_id, canonical, channel, platform, post_id)).lastrowid
                intent = 'capture'
            return self._new_run(con, capture_id, request_key, intent)

    def retry_public(self, ingest_id: int) -> dict[str, Any]:
        """Retry the same operation without granting new restoration intent."""
        with self._transaction(write=True) as con:
            run = self._run(con, ingest_id)
            capture = self._capture(con, run['item_id']) if run['item_id'] is not None else None
            if (capture is None or capture['input_kind'] != 'public_post'
                    or capture['materialization_state'] == 'legacy_unverified'
                    or run['intent'] == 'legacy' or run['accepted_sequence'] <= 0):
                raise AccountConflict('This saved post requires reconciliation before retrying')
            if run['status'] in {'queued', 'processing'}:
                return self._accepted(con, run)
            if run['status'] not in {'failed', 'retry_scheduled'}:
                raise AccountConflict('Only failed or scheduled saves can be retried')
            con.execute('''
                UPDATE ingest_runs SET status = 'queued', stage = 'accepted', error_type = NULL,
                    error_message = NULL, failure_kind = NULL, retryable = 0,
                    next_retry_at = NULL, completed_at = NULL, last_retry_at = CURRENT_TIMESTAMP,
                    updated_at = CURRENT_TIMESTAMP WHERE user_id = ? AND id = ?
            ''', (self.user_id, ingest_id))
            con.execute('''INSERT INTO ingest_events (user_id, ingest_run_id, stage, status, message)
                           VALUES (?, ?, 'accepted', 'queued', 'Retry accepted')''',
                        (self.user_id, ingest_id))
            return self._accepted(con, self._run(con, ingest_id))

    def accept_direct(self, text: str, request_key: str, *, channel: str = 'typed',
                      context: dict | None = None) -> dict[str, Any]:
        self._request_key(request_key)
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 10000 or channel not in {'typed', 'siri'}:
            raise ValueError('A private text input and supported channel are required')
        text = text.strip()
        context_json = json.dumps(DirectContext.model_validate(context or {}).model_dump(exclude_none=True), sort_keys=True)
        with self._transaction(write=True) as con:
            previous = con.execute('SELECT * FROM ingest_runs WHERE user_id = ? AND idempotency_key = ?',
                                   (self.user_id, request_key)).fetchone()
            if previous:
                capture = self._capture(con, previous['item_id']) if previous['item_id'] is not None else None
                if (capture is None or capture['input_kind'] != 'direct' or capture['input_text'] != text
                        or capture['capture_channel'] != channel or capture['context_json'] != context_json):
                    raise AccountConflict('Request key was already used for different input')
                return self._accepted(con, previous)
            capture_id = con.execute('''
                INSERT INTO captures (user_id, vertical, input_kind, capture_channel, input_text, context_json)
                VALUES (?, 'recommendation', 'direct', ?, ?, ?)
            ''', (self.user_id, channel, text, context_json)).lastrowid
            return self._new_run(con, capture_id, request_key, 'capture')

    def _place(self, con: sqlite3.Connection, place_id: str) -> int:
        con.execute('INSERT INTO locations (google_place_id) VALUES (?) ON CONFLICT(google_place_id) DO NOTHING',
                    (place_id,))
        return con.execute('SELECT id FROM locations WHERE google_place_id = ?', (place_id,)).fetchone()[0]

    def _candidate(self, con: sqlite3.Connection, place_id: str) -> dict:
        row = con.execute('SELECT * FROM locations WHERE google_place_id = ?', (place_id,)).fetchone()
        if row is None:
            return {'id': place_id}
        return {'id': place_id, 'displayName': {'text': row['display_name']},
                'location': {'latitude': row['lat'], 'longitude': row['lng']},
                'formattedAddress': row['formatted_address'], 'googleMapsUri': row['google_maps_url']}

    def _write_mention(self, con: sqlite3.Connection, capture_id: int, ordinal: int,
                       output: OriginalMention, sequence: int, removed_id: int | None) -> None:
        extracted = output.extracted.model_dump()
        key, normalized, kind = _identity_key(extracted, extracted['type_name'], output.place_id)
        existing = con.execute('SELECT id FROM recommendations WHERE user_id = ? AND identity_key = ?',
                               (self.user_id, key)).fetchone()
        if existing:
            entry_id = existing['id']
        else:
            location_id = self._place(con, output.place_id) if output.place_id else None
            entry_id = con.execute('''
                INSERT INTO recommendations (user_id, name, normalized_name, entry_type, type_key,
                    identity_key, location_id, starts_at, ends_at, recurrence_text)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (self.user_id, extracted['extracted_name'], normalized, extracted['type_name'], kind,
                  key, location_id, extracted['starts_at'], extracted['ends_at'], extracted['recurrence_text'])).lastrowid
        values = (entry_id, extracted['extracted_name'], extracted['type_name'], extracted['description'],
                  json.dumps(extracted['dishes']), extracted['why_its_cool'], json.dumps(extracted['tags']),
                  extracted['timestamp_seconds'], extracted['slide_index'], output.status,
                  json.dumps([self._candidate(con, candidate) for candidate in output.candidate_ids]),
                  extracted['location_query'], sequence)
        if removed_id is not None:
            con.execute('''
                UPDATE recommendation_mentions SET entry_id = ?, source_name = ?, source_type = ?, description = ?,
                    dishes_json = ?, why_its_cool = ?, tags_json = ?, timestamp_seconds = ?, slide_index = ?,
                    resolution_status = ?, resolution_candidates_json = ?, location_query = ?,
                    last_user_change_sequence = ?, removed_at = NULL
                WHERE user_id = ? AND id = ? AND item_id = ? AND output_key = ? AND removed_at IS NOT NULL
            ''', (*values, self.user_id, removed_id, capture_id, output.key))
        else:
            con.execute('''
                INSERT INTO recommendation_mentions (entry_id, source_name, source_type, description,
                    dishes_json, why_its_cool, tags_json, timestamp_seconds, slide_index, resolution_status,
                    resolution_candidates_json, location_query, last_user_change_sequence,
                    user_id, item_id, output_key, ordinal)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (*values, self.user_id, capture_id, output.key, ordinal))

    def materialize(self, ingest_id: int, *, public_cache_id: int | None = None,
                    private_result: dict | None = None) -> dict[str, Any]:
        with self._transaction(write=True) as con:
            run = self._run(con, ingest_id)
            capture = self._capture(con, run['item_id'])
            if run['accepted_sequence'] <= 0 or capture['materialization_state'] == 'legacy_unverified':
                raise AccountConflict('Legacy input requires explicit reconciliation')
            if run['status'] in {'completed', 'partial'}:
                return self._accepted(con, run)
            if run['status'] not in {'queued', 'processing', 'retry_scheduled'}:
                raise AccountConflict('This request is not eligible to save a result')
            complete = capture['materialization_state'] == 'complete'
            if capture['input_kind'] == 'public_post':
                if private_result is not None:
                    raise AccountConflict('Public captures cannot use private results')
                cache_id = capture['post_cache_id'] or public_cache_id
                cache = con.execute('''
                    SELECT * FROM post_processing_cache WHERE id = ? AND platform = ? AND post_id = ?
                        AND status = 'completed'
                ''', (cache_id, capture['source_platform'], capture['source_post_id'])).fetchone()
                if cache is None:
                    raise AccountConflict('A matching published post result is required')
                baseline = validate_result(cache['result_json'])
            elif capture['input_kind'] == 'direct':
                if public_cache_id is not None:
                    raise AccountConflict('Direct captures cannot use public results')
                value = capture['private_result_json'] if complete else private_result
                if value is None:
                    raise AccountConflict('A private original result is required')
                baseline = validate_result(value)
            else:
                raise AccountConflict('Legacy input requires explicit reconciliation')
            rows = con.execute('SELECT * FROM recommendation_mentions WHERE user_id = ? AND item_id = ?',
                               (self.user_id, capture['id'])).fetchall()
            existing = {row['output_key']: row for row in rows}
            keys = {output.key for output in baseline.mentions}
            if (complete and set(existing) != keys) or (not complete and existing):
                raise AccountConflict('Capture output identities require reconciliation')
            for ordinal, output in enumerate(baseline.mentions):
                row = existing.get(output.key)
                if row is not None:
                    if row['ordinal'] != ordinal:
                        raise AccountConflict('Capture output ordering does not match its pinned result')
                    if row['removed_at'] is None:
                        continue  # Active descriptions, location edits, and all other fields survive.
                    if run['intent'] != 'reshare' or run['accepted_sequence'] <= row['last_user_change_sequence']:
                        continue
                self._write_mention(con, capture['id'], ordinal, output, run['accepted_sequence'],
                                    row['id'] if row is not None else None)
            if not complete:
                if capture['input_kind'] == 'public_post':
                    con.execute('''
                        UPDATE captures SET post_cache_id = ?, materialization_state = 'complete'
                        WHERE user_id = ? AND id = ?
                    ''', (cache['id'], self.user_id, capture['id']))
                else:
                    con.execute('''
                        UPDATE captures SET private_result_json = ?, materialization_state = 'complete'
                        WHERE user_id = ? AND id = ?
                    ''', (baseline.model_dump_json(), self.user_id, capture['id']))
            outcomes = self._outcomes(con, capture['id'])
            final_status = 'partial' if any(r['resolution_status'] in {'needs_review', 'unresolved'} for r in outcomes) else 'completed'
            con.execute('''
                UPDATE ingest_runs SET status = ?, stage = 'completed', result_json = NULL,
                    error_type = NULL, error_message = NULL, failure_kind = NULL, retryable = 0,
                    next_retry_at = NULL, completed_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE user_id = ? AND id = ?
            ''', (final_status, self.user_id, ingest_id))
            con.execute('''
                INSERT INTO ingest_events (user_id, ingest_run_id, stage, status, message)
                VALUES (?, ?, 'completed', ?, 'Saved available recommendations')
            ''', (self.user_id, ingest_id, final_status))
            return self._accepted(con, self._run(con, ingest_id))
