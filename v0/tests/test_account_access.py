from __future__ import annotations

import base64
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import store
from account_app import InvalidSession, VerifiedAccount, create_account_app
from account_store import AccountStore, AccountUnavailable, RecordNotFound
from multi_user_migration import prepare_copy


class AccountAccessTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.legacy = Path(self.directory.name) / 'legacy.db'
        self.db = Path(self.directory.name) / 'accounts.db'
        store.init_db(self.legacy)
        extracted = {'extracted_name': 'Nathan private cafe', 'type_name': 'Restaurant',
                     'description': 'Nathan private description'}
        self.capture = store.save_ingest(self.legacy, {
            'source_url': 'https://youtu.be/shared',
            'metadata': {'uploader': 'Nathan creator', 'caption_or_description': 'Nathan caption'},
            'entries_extracted': [extracted],
            'resolved_entries': [{'extracted': extracted, 'status': 'resolved',
                                 'place': {'id': 'shared-place', 'displayName': {'text': 'Public cafe'}}}],
        })
        self.entry = store.list_entries(self.legacy)[0]['id']
        self.run = store.start_ingest_run(self.legacy, 'https://youtu.be/shared', 'youtube')
        store.finish_ingest_run(self.legacy, self.run, status='completed', stage='completed',
                                message='Nathan event', item_id=self.capture,
                                outcomes=store.saved_entry_outcomes(self.legacy, self.capture))
        self.expected = {'entries': store.list_entries(self.legacy),
                         'sources': store.list_sources(self.legacy),
                         'activity': store.list_ingest_runs(self.legacy)}
        prepare_copy(self.legacy, self.db, owner_id='nathan', owner_name='Nathan')
        with self.connection() as con:
            con.execute("INSERT INTO users (id, display_name) VALUES ('friend', 'Friend'), ('empty', 'Empty')")
            key = con.execute('SELECT identity_key FROM recommendations WHERE id = ?', (self.entry,)).fetchone()[0]
            location_id = con.execute('SELECT location_id FROM recommendations WHERE id = ?', (self.entry,)).fetchone()[0]
            self.friend_entry = con.execute('''
                INSERT INTO recommendations (user_id, name, normalized_name, entry_type, type_key, identity_key, location_id)
                VALUES ('friend', 'Friend private cafe', 'friend cafe', 'Restaurant', 'food', ?, ?)
            ''', (key, location_id)).lastrowid
            self.cache = con.execute('''
                INSERT INTO post_processing_cache (platform, post_id, canonical_url, processing_version,
                    status, result_json, completed_at)
                VALUES ('youtube', 'shared', 'https://youtu.be/shared', 'v1', 'completed', '{"mentions":[]}', CURRENT_TIMESTAMP)
            ''').lastrowid
            con.execute("UPDATE captures SET post_cache_id = ?, materialization_state = 'complete' WHERE id = ?", (self.cache, self.capture))
            self.friend_capture = con.execute('''
                INSERT INTO captures (user_id, vertical, source_url, raw_payload_json, input_kind,
                    capture_channel, source_platform, source_post_id, post_cache_id, materialization_state)
                VALUES ('friend', 'recommendation', 'https://youtu.be/shared', ?, 'public_post',
                    'share_extension', 'youtube', 'shared', ?, 'complete')
            ''', (json.dumps({'uploader': 'Friend creator', 'caption_or_description': 'Friend caption'}), self.cache)).lastrowid
            self.friend_mention = con.execute('''
                INSERT INTO recommendation_mentions (user_id, entry_id, item_id, ordinal, source_name,
                    source_type, description, resolution_status, output_key)
                VALUES ('friend', ?, ?, 0, 'Friend private cafe', 'Restaurant', 'Friend private description', 'resolved', 'cafe')
            ''', (self.friend_entry, self.friend_capture)).lastrowid
            self.friend_run = self.insert_run(con, 'friend', self.friend_capture)
            con.execute("INSERT INTO ingest_events (user_id, ingest_run_id, stage, status, message) VALUES ('friend', ?, 'completed', 'completed', 'Friend event')", (self.friend_run,))
        self.nathan = AccountStore(self.db, 'nathan')
        self.friend = AccountStore(self.db, 'friend')
        self.revoked = set()

        def verify(token):
            # Test-only stand-in for the eventual provider/session adapter.
            identities = {'nathan-session': 'nathan', 'friend-session': 'friend', 'empty-session': 'empty',
                          'apple-session': 'nathan', 'google-session': 'nathan', 'email-session': 'nathan'}
            if token in self.revoked or token not in identities:
                raise InvalidSession()
            return VerifiedAccount(identities[token])

        self.app = create_account_app(db_path=self.db, verify_session=verify)
        self.client = self.enterContext(TestClient(self.app))

    def connection(self):
        con = sqlite3.connect(self.db)
        con.execute('PRAGMA foreign_keys = ON')
        self.addCleanup(con.close)
        return con

    def insert_run(self, con, user, capture=None, status='completed'):
        key = str(con.execute('SELECT COUNT(*) FROM ingest_runs').fetchone()[0])
        return con.execute('''
            INSERT INTO ingest_runs (user_id, item_id, source_url, source_platform, status, stage, idempotency_key, intent)
            VALUES (?, ?, 'https://youtu.be/shared', 'youtube', ?, 'completed', ?, 'capture')
        ''', (user, capture, status, 'request-' + key)).lastrowid

    def headers(self, user='nathan'):
        return {'Authorization': f'Bearer {user}-session'}

    def request(self, method, path, user='nathan', **kwargs):
        return self.client.request(method, '/api/v1' + path, headers=self.headers(user), **kwargs)

    def snapshot(self):
        return list(self.connection().iterdump())

    def seed_review(self, user='nathan', candidate='shared-place'):
        with self.connection() as con:
            capture = con.execute('''
                INSERT INTO captures (user_id, vertical, input_kind, capture_channel, input_text)
                VALUES (?, 'recommendation', 'direct', 'siri', 'Save this cafe')
            ''', (user,)).lastrowid
            entry = con.execute('''
                INSERT INTO recommendations (user_id, name, normalized_name, entry_type, type_key, identity_key)
                VALUES (?, 'Review cafe', 'review cafe', 'Restaurant', 'restaurant', ?)
            ''', (user, f'review-{capture}')).lastrowid
            candidates = [{'id': candidate, 'displayName': {'text': 'Private candidate label'},
                           'location': {'latitude': 40.7, 'longitude': -74.0}}]
            mention = con.execute('''
                INSERT INTO recommendation_mentions (user_id, entry_id, item_id, ordinal, source_name,
                    source_type, resolution_status, resolution_candidates_json, output_key)
                VALUES (?, ?, ?, 0, 'Review cafe', 'Restaurant', 'needs_review', ?, 'cafe')
            ''', (user, entry, capture, json.dumps(candidates))).lastrowid
            run = self.insert_run(con, user, capture, 'partial')
        return capture, entry, mention, run

    def test_reads_preserve_existing_library_and_isolate_all_nested_data(self):
        for path, expected in self.expected.items():
            response = self.request('GET', '/' + path)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()[path], expected)
            self.assertNotIn('Friend', response.text)
            other = self.request('GET', '/' + path, 'friend')
            self.assertEqual(other.status_code, 200)
            self.assertNotIn('Nathan', other.text)
            self.assertIn('Friend', other.text)
            empty = self.request('GET', '/' + path, 'empty')
            self.assertEqual(empty.json()[path], [])

    def test_all_data_routes_require_a_verified_session(self):
        operations = [
            ('GET', '/entries', None), ('GET', '/sources', None), ('GET', '/activity', None),
            ('DELETE', f'/entries/{self.entry}', None), ('DELETE', '/entries', {'entry_ids': [self.entry]}),
            ('DELETE', f'/activity/{self.run}', None), ('POST', f'/activity/{self.run}/retry', None),
            ('POST', f'/activity/{self.run}/entries/{self.entry}/location', {'candidate_id': 'shared-place'}),
            ('POST', '/ingests', {'source_url': 'https://youtu.be/shared'}),
            ('POST', '/shortcut/ingests', {'source_url_base64': base64.b64encode(b'https://youtu.be/shared').decode()}),
        ]
        before = self.snapshot()
        for method, path, body in operations:
            for headers in ({}, {'Authorization': 'Bearer api-secret'}, {'Authorization': 'Bearer nathan'},
                            {'Authorization': 'Basic nathan-session'}, {'X-User-ID': 'nathan'}):
                response = self.client.request(method, '/api/v1' + path, json=body, headers=headers)
                self.assertEqual(response.status_code, 401, (method, path, response.text))
                self.assertEqual(response.headers['www-authenticate'], 'Bearer')
        self.assertEqual(before, self.snapshot())
        self.assertEqual(self.client.get('/healthz').status_code, 200)

    def test_client_supplied_owner_cannot_override_identity(self):
        response = self.client.get('/api/v1/entries?user_id=friend',
                                   headers={**self.headers(), 'X-User-ID': 'friend'})
        self.assertEqual([r['id'] for r in response.json()['entries']], [self.entry])
        before = self.snapshot()
        response = self.request('DELETE', '/entries', json={'entry_ids': [self.friend_entry], 'user_id': 'friend'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(before, self.snapshot())

    def test_foreign_ids_match_nonexistent_ids_and_leave_database_unchanged(self):
        before = self.snapshot()
        pairs = [
            ('DELETE', f'/entries/{self.friend_entry}', '/entries/99999', None),
            ('DELETE', f'/activity/{self.friend_run}', '/activity/99999', None),
            ('POST', f'/activity/{self.friend_run}/retry', '/activity/99999/retry', None),
            ('POST', f'/activity/{self.friend_run}/entries/{self.friend_entry}/location',
             '/activity/99999/entries/99999/location', {'candidate_id': 'shared-place'}),
            ('POST', f'/activity/{self.run}/entries/{self.friend_entry}/location',
             f'/activity/{self.run}/entries/99999/location', {'candidate_id': 'shared-place'}),
        ]
        for method, foreign, missing, body in pairs:
            a = self.request(method, foreign, json=body)
            b = self.request(method, missing, json=body)
            self.assertEqual((a.status_code, a.json()), (404, b.json()))
        self.assertEqual(before, self.snapshot())

    def test_mixed_owner_batch_delete_is_atomic(self):
        before = self.snapshot()
        response = self.request('DELETE', '/entries', json={'entry_ids': [self.entry, self.friend_entry]})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(before, self.snapshot())

    def test_map_delete_hides_mentions_preserves_other_user_and_shared_cache(self):
        other = {'entries': self.friend.entries(), 'sources': self.friend.sources(), 'activity': self.friend.activity()}
        with self.connection() as con:
            cache = con.execute('SELECT * FROM post_processing_cache').fetchall()
            locations = con.execute('SELECT * FROM locations').fetchall()
        response = self.request('DELETE', f'/entries/{self.entry}')
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['deleted_sources'], 0)
        self.assertEqual(self.nathan.entries(), [])
        self.assertEqual(self.nathan.sources()[0]['entry_count'], 0)
        self.assertEqual(self.nathan.activity()[0]['results'], [])
        self.assertEqual(other, {'entries': self.friend.entries(), 'sources': self.friend.sources(), 'activity': self.friend.activity()})
        con = self.connection()
        removed = con.execute("SELECT entry_id, removed_at, last_user_change_sequence FROM recommendation_mentions WHERE user_id = 'nathan'").fetchone()
        self.assertIsNone(removed[0]); self.assertIsNotNone(removed[1]); self.assertEqual(removed[2], 1)
        self.assertEqual(con.execute('SELECT * FROM post_processing_cache').fetchall(), cache)
        self.assertEqual(con.execute('SELECT * FROM locations').fetchall(), locations)
        self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])

    def test_duplicate_ids_in_batch_are_one_mutation(self):
        response = self.request('DELETE', '/entries', json={'entry_ids': [self.entry, self.entry]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['deleted_entries'], 1)
        self.assertEqual(self.connection().execute("SELECT mutation_sequence FROM users WHERE id = 'nathan'").fetchone()[0], 1)

    def test_delete_rolls_back_on_late_failure(self):
        before = self.snapshot()
        with patch.object(AccountStore, '_refresh_activity', side_effect=RuntimeError('injected failure')):
            with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                self.nathan.delete_entries([self.entry])
        self.assertEqual(before, self.snapshot())

    def test_concurrent_deletes_do_not_duplicate_mutations(self):
        def delete():
            try:
                self.nathan.delete_entries([self.entry])
                return 'deleted'
            except RecordNotFound:
                return 'missing'
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual(sorted(pool.map(lambda _: delete(), range(2))), ['deleted', 'missing'])
        self.assertEqual(self.connection().execute("SELECT mutation_sequence FROM users WHERE id = 'nathan'").fetchone()[0], 1)

    def test_failed_activity_delete_preserves_other_submissions_and_removal_markers(self):
        self.nathan.delete_entries([self.entry])
        with self.connection() as con:
            failed = self.insert_run(con, 'nathan', self.capture, 'retry_scheduled')
            con.execute("INSERT INTO ingest_events (user_id, ingest_run_id, stage, status, message) VALUES ('nathan', ?, 'fetching', 'failed', 'Failed')", (failed,))
        response = self.request('DELETE', f'/activity/{failed}')
        self.assertEqual(response.status_code, 200)
        con = self.connection()
        self.assertIsNotNone(con.execute('SELECT 1 FROM captures WHERE id = ?', (self.capture,)).fetchone())
        self.assertIsNotNone(con.execute('SELECT 1 FROM ingest_runs WHERE id = ?', (self.run,)).fetchone())
        self.assertEqual(con.execute('SELECT COUNT(*) FROM ingest_events WHERE ingest_run_id = ?', (failed,)).fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM recommendation_mentions WHERE user_id = 'nathan' AND removed_at IS NOT NULL").fetchone()[0], 1)

    def test_completed_or_saved_activity_cannot_be_deleted(self):
        before = self.snapshot()
        self.assertEqual(self.request('DELETE', f'/activity/{self.run}').status_code, 409)
        self.assertEqual(before, self.snapshot())
        with self.connection() as con:
            failed = self.insert_run(con, 'nathan', self.capture, 'failed')
        before = self.snapshot()
        self.assertEqual(self.request('DELETE', f'/activity/{failed}').status_code, 409)
        self.assertEqual(before, self.snapshot())

    def test_location_confirmation_merges_only_with_own_recommendation(self):
        capture, entry, mention, run = self.seed_review()
        other = self.friend.entries()
        locations = self.connection().execute('SELECT * FROM locations').fetchall()
        response = self.request('POST', f'/activity/{run}/entries/{entry}/location', json={'candidate_id': 'shared-place'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['entry']['entry_id'], self.entry)
        self.assertEqual(self.friend.entries(), other)
        self.assertEqual(self.connection().execute('SELECT * FROM locations').fetchall(), locations)
        self.assertIsNone(self.connection().execute('SELECT id FROM recommendations WHERE id = ?', (entry,)).fetchone())
        self.assertEqual(self.connection().execute('SELECT last_user_change_sequence FROM recommendation_mentions WHERE id = ?', (mention,)).fetchone()[0], 1)

    def test_location_confirmation_does_not_merge_into_another_users_place(self):
        # Leave only Friend's recommendation at the matching shared place.
        self.nathan.delete_entries([self.entry])
        _, entry, _, run = self.seed_review()
        response = self.request('POST', f'/activity/{run}/entries/{entry}/location', json={'candidate_id': 'shared-place'})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['entry']['entry_id'], entry)
        self.assertEqual(self.friend.entries()[0]['id'], self.friend_entry)

    def test_invalid_location_confirmation_has_no_side_effects(self):
        _, entry, _, run = self.seed_review()
        before = self.snapshot()
        response = self.request('POST', f'/activity/{run}/entries/{entry}/location', json={'candidate_id': 'made-up-place'})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(before, self.snapshot())

    def test_direct_capture_reads_allow_null_source_urls(self):
        capture, _, _, run = self.seed_review()
        with self.connection() as con:
            con.execute('UPDATE ingest_runs SET source_url = NULL WHERE id = ?', (run,))
        for path in ('entries', 'sources', 'activity'):
            response = self.request('GET', '/' + path)
            self.assertEqual(response.status_code, 200, response.text)
            rows = response.json()[path]
            row = next(r for r in rows if r.get('item_id', r.get('id')) == capture)
            self.assertIsNone(row['source_url'])
            self.assertNotIn('Friend', response.text)

    def test_removed_mentions_are_absent_from_every_read(self):
        self.nathan.delete_entries([self.entry])
        for path in ('entries', 'sources', 'activity'):
            response = self.request('GET', '/' + path)
            self.assertEqual(response.status_code, 200)
            self.assertNotIn('Nathan private description', response.text)

    def test_activity_does_not_trust_stale_result_json(self):
        with self.connection() as con:
            orphan_run = self.insert_run(con, 'nathan', status='failed')
            con.execute('UPDATE ingest_runs SET result_json = ? WHERE id = ?',
                        (json.dumps([{'entry_id': self.friend_entry, 'name': 'Friend stale secret'}]), orphan_run))
        response = self.request('GET', '/activity')
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('Friend', response.text)

    def test_disabled_deleted_and_revoked_accounts_lose_access(self):
        self.revoked.add('nathan-session')
        self.assertEqual(self.request('GET', '/entries').status_code, 401)
        self.revoked.clear()
        with self.connection() as con:
            con.execute("UPDATE users SET status = 'disabled' WHERE id = 'nathan'")
        before = self.snapshot()
        self.assertEqual(self.request('GET', '/entries').status_code, 401)
        self.assertEqual(self.request('DELETE', f'/entries/{self.entry}').status_code, 401)
        self.assertEqual(before, self.snapshot())
        with self.connection() as con:
            con.execute("DELETE FROM users WHERE id = 'nathan'")
        self.assertEqual(self.request('GET', '/entries').status_code, 401)
        self.assertEqual(self.request('GET', '/entries', 'friend').status_code, 200)

    def test_account_status_is_rechecked_inside_mutation(self):
        original = AccountStore.delete_entries
        def disable_then_delete(account, ids):
            with closing(sqlite3.connect(self.db)) as con:
                con.execute("UPDATE users SET status = 'disabled' WHERE id = 'nathan'")
                con.commit()
            return original(account, ids)
        with patch.object(AccountStore, 'delete_entries', disable_then_delete):
            self.assertEqual(self.request('DELETE', f'/entries/{self.entry}').status_code, 401)
        self.assertIsNotNone(self.connection().execute('SELECT id FROM recommendations WHERE id = ?', (self.entry,)).fetchone())

    def test_concurrent_accounts_do_not_share_request_context(self):
        def read(user):
            response = self.request('GET', '/entries', user)
            self.assertEqual(response.status_code, 200)
            self.assertEqual([r['id'] for r in response.json()['entries']],
                             [self.entry] if user == 'nathan' else [self.friend_entry])
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(read, ['nathan', 'friend'] * 6))

    def test_linked_login_methods_use_the_same_internal_account(self):
        for token in ('apple-session', 'google-session', 'email-session'):
            response = self.client.get('/api/v1/entries', headers={'Authorization': 'Bearer ' + token})
            self.assertEqual([r['id'] for r in response.json()['entries']], [self.entry])

    def test_authentication_verifier_is_required_and_raw_identity_is_rejected(self):
        with self.assertRaises(ValueError):
            create_account_app(db_path=self.db, verify_session=None)
        with TestClient(create_account_app(db_path=self.db, verify_session=lambda _token: 'nathan')) as client:
            self.assertEqual(client.get('/api/v1/entries', headers=self.headers()).status_code, 401)

    def test_movie_enrichment_stays_with_its_owner(self):
        for user, private_title in (('nathan', 'Nathan movie'), ('friend', 'Friend movie')):
            _, entry, _, _ = self.seed_review(user)
            with self.connection() as con:
                con.execute("UPDATE recommendations SET entry_type = 'Movie', name = ? WHERE id = ?", (private_title, entry))
                con.execute('''
                    INSERT INTO movie_enrichments (entry_id, user_id, provider, resolved_title, match_status)
                    VALUES (?, ?, 'wikidata', ?, 'matched')
                ''', (entry, user, private_title))
        for user, own, other in (('nathan', 'Nathan movie', 'Friend movie'), ('friend', 'Friend movie', 'Nathan movie')):
            response = self.request('GET', '/entries', user)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn(own, response.text)
            self.assertNotIn(other, response.text)
            movie = next(r for r in response.json()['entries'] if r['type'] == 'Movie')
            self.assertEqual(movie['movie_enrichment']['resolved_title'], own)

    def test_deleting_group_removes_mentions_from_all_own_captures(self):
        _, entry, _, run = self.seed_review()
        self.nathan.confirm_activity_location(run, entry, 'shared-place')
        self.assertEqual(len(self.nathan.entries()[0]['sources']), 2)
        self.nathan.delete_entries([self.entry])
        self.assertEqual(self.nathan.entries(), [])
        self.assertTrue(all(not r['results'] for r in self.nathan.activity()))
        con = self.connection()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM recommendation_mentions WHERE user_id = 'nathan' AND removed_at IS NOT NULL").fetchone()[0], 2)
        self.assertEqual(len(self.friend.entries()[0]['sources']), 1)

    def test_ingest_requires_request_keys_and_legacy_retry_cannot_fall_back(self):
        before = self.snapshot()
        with patch('ingest_service.IngestService.ingest', side_effect=AssertionError('legacy called')):
            self.assertEqual(self.request('POST', '/ingests', json={'source_url': 'https://youtu.be/shared'}).status_code, 422)
            encoded = base64.b64encode(b'https://youtu.be/shared').decode()
            self.assertEqual(self.request('POST', '/shortcut/ingests', json={'source_url_base64': encoded}).status_code, 422)
            self.assertEqual(self.request('POST', f'/activity/{self.run}/retry').status_code, 409)
        self.assertEqual(before, self.snapshot())

    def test_query_limits_are_validated(self):
        for path in ('entries', 'sources', 'activity'):
            self.assertEqual(self.request('GET', '/' + path + '?limit=0').status_code, 422)
            self.assertEqual(self.request('GET', '/' + path + '?limit=1001').status_code, 422)
        self.assertEqual(len(self.request('GET', '/entries?limit=1').json()['entries']), 1)

    def test_store_has_no_legacy_or_missing_account_fallback(self):
        with self.assertRaisesRegex(RuntimeError, 'explicitly migrated'):
            AccountStore(self.legacy, 'nathan').entries()
        for user in ('', 'missing', "' OR 1=1 --"):
            with self.assertRaises(AccountUnavailable):
                AccountStore(self.db, user).entries()
        missing = Path(self.directory.name) / 'does-not-exist.db'
        with self.assertRaises(sqlite3.OperationalError):
            AccountStore(missing, 'nathan').entries()
        self.assertFalse(missing.exists())


if __name__ == '__main__':
    unittest.main()
