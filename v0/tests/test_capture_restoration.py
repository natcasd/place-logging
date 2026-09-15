from __future__ import annotations

import copy
import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient
from pydantic import ValidationError

import store
from account_app import VerifiedAccount, create_account_app
from account_store import AccountConflict, AccountUnavailable, RecordNotFound
from capture_result import validate_result
from capture_store import CaptureStore
from multi_user_migration import prepare_copy, SCHEMA_VERSION, migrate_copy


def result(*places):
    return {'metadata': {'uploader': 'Public creator', 'caption_or_description': 'Public caption'},
            'mentions': [{'key': 'output-' + str(i),
                          'extracted': {'extracted_name': name, 'type_name': 'Restaurant',
                                        'description': 'Original ' + name},
                          'status': 'resolved', 'place_id': place}
                         for i, (name, place) in enumerate(places)]}


class CaptureRestorationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.legacy = Path(self.directory.name) / 'legacy.db'
        self.db = Path(self.directory.name) / 'accounts.db'
        store.init_db(self.legacy)
        prepare_copy(self.legacy, self.db, owner_id='a', owner_name='A')
        with self.connection() as con:
            con.execute("INSERT INTO users (id, display_name) VALUES ('b', 'B')")
        self.a = CaptureStore(self.db, 'a')
        self.b = CaptureStore(self.db, 'b')
        self.baseline = result(('Cafe', 'cafe'), ('Bakery', 'bakery'))
        self.cache_id = self.cache(self.baseline)

    def connection(self):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys = ON')
        self.addCleanup(con.close)
        return con

    def cache(self, body, post='post', version='v1'):
        with self.connection() as con:
            return con.execute('''
                INSERT INTO post_processing_cache (platform, post_id, canonical_url, processing_version,
                    status, result_json, completed_at)
                VALUES ('youtube', ?, ?, ?, 'completed', ?, CURRENT_TIMESTAMP)
            ''', (post, 'https://youtu.be/' + post, version, json.dumps(body))).lastrowid

    def save(self, account=None, key='first', post='post', cache_id=None):
        account = account or self.a
        accepted = account.accept_public('https://youtu.be/' + post, key)
        return account.materialize(accepted['ingest_id'], public_cache_id=cache_id or self.cache_id)

    def mentions(self, user='a', capture=None):
        sql = 'SELECT * FROM recommendation_mentions WHERE user_id = ?'
        params = [user]
        if capture is not None:
            sql += ' AND item_id = ?'; params.append(capture)
        return [dict(r) for r in self.connection().execute(sql + ' ORDER BY id', params)]

    def snapshot(self):
        return list(self.connection().iterdump())

    def test_shared_result_creates_independent_private_libraries_without_baseline_copies(self):
        a = self.save()
        b = self.save(self.b)
        self.assertEqual(a['status'], 'completed')
        self.assertTrue(set(r['entry_id'] for r in a['saved_entries']).isdisjoint(r['entry_id'] for r in b['saved_entries']))
        con = self.connection()
        captures = con.execute('SELECT * FROM captures').fetchall()
        self.assertEqual(len(captures), 2)
        self.assertTrue(all(r['post_cache_id'] == self.cache_id for r in captures))
        self.assertTrue(all(r['raw_payload_json'] is None and r['llm_output_json'] is None and r['private_result_json'] is None for r in captures))
        self.assertEqual(self.a.sources()[0]['creator'], 'Public creator')
        self.assertEqual(self.a.activity()[0]['caption'], 'Public caption')

    def test_same_request_key_retries_are_noops_and_different_input_is_rejected(self):
        saved = self.save()
        before = self.snapshot()
        retried = self.a.accept_public('https://www.youtube.com/watch?v=post&tracking=1', 'first')
        self.assertEqual(retried, saved)
        self.assertEqual(self.a.materialize(saved['ingest_id']), saved)
        self.assertEqual(before, self.snapshot())
        with self.assertRaises(AccountConflict):
            self.a.accept_public('https://youtu.be/another-post', 'first')
        with self.assertRaises(AccountConflict):
            self.a.accept_direct('Cafe', 'first')
        self.assertEqual(before, self.snapshot())

    def test_deliberate_reshare_restores_only_removed_outputs_and_preserves_active_edits(self):
        saved = self.save()
        first, second = self.mentions()
        self.a.delete_mention(first['id'])
        with self.connection() as con:
            con.execute("UPDATE recommendation_mentions SET description = 'My edit', tags_json = '[\"private\"]' WHERE id = ?", (second['id'],))
        active_before = self.mentions()[1]
        accepted = self.a.accept_public('https://youtu.be/post', 'intentional-reshare')
        restored = self.a.materialize(accepted['ingest_id'])
        self.assertEqual(restored['item_id'], saved['item_id'])
        rows = self.mentions()
        self.assertEqual(rows[0]['id'], first['id'])
        self.assertEqual(rows[0]['description'], 'Original Cafe')
        self.assertIsNone(rows[0]['removed_at'])
        self.assertEqual(rows[1], active_before)
        self.assertNotEqual(rows[0]['entry_id'], first['entry_id'])

    def test_completed_retry_does_not_undo_deletion(self):
        saved = self.save()
        self.a.delete_entries([r['entry_id'] for r in saved['saved_entries']])
        before = self.snapshot()
        self.assertEqual(self.a.materialize(saved['ingest_id'])['saved_entries'], [])
        self.a.accept_public('https://youtu.be/post', 'first')
        self.assertEqual(before, self.snapshot())

    def test_accepted_share_before_delete_cannot_resurrect_it_on_late_completion(self):
        self.save()
        old = self.a.accept_public('https://youtu.be/post', 'accepted-before-delete')
        mention = self.mentions()[0]
        self.a.delete_mention(mention['id'])
        removal = self.mentions()[0]
        self.a.materialize(old['ingest_id'])
        self.assertEqual(self.mentions()[0], removal)
        newer = self.a.accept_public('https://youtu.be/post', 'accepted-after-delete')
        self.a.materialize(newer['ingest_id'])
        self.assertIsNone(self.mentions()[0]['removed_at'])

    def test_out_of_order_shares_do_not_overwrite_later_restoration_or_edits(self):
        self.save()
        self.a.delete_mention(self.mentions()[0]['id'])
        old = self.a.accept_public('https://youtu.be/post', 'older')
        newer = self.a.accept_public('https://youtu.be/post', 'newer')
        self.a.materialize(newer['ingest_id'])
        with self.connection() as con:
            con.execute("UPDATE recommendation_mentions SET description = 'New edit' WHERE user_id = 'a'")
        before = self.mentions()
        self.a.materialize(old['ingest_id'])
        self.assertEqual(self.mentions(), before)

    def test_map_delete_then_one_post_reshare_does_not_restore_other_posts_or_siri(self):
        a = self.save()
        other_cache = self.cache(result(('Cafe', 'cafe')), 'other')
        b = self.save(key='another', post='other', cache_id=other_cache)
        direct = self.a.accept_direct('Save Cafe', 'siri', channel='siri')
        self.a.materialize(direct['ingest_id'], private_result=result(('Cafe', 'cafe')))
        cafe = a['saved_entries'][0]['entry_id']
        self.a.delete_entries([cafe])
        share = self.a.accept_public('https://youtu.be/post', 'reshare')
        self.a.materialize(share['ingest_id'])
        self.assertIsNone(self.mentions(capture=a['item_id'])[0]['removed_at'])
        self.assertIsNotNone(self.mentions(capture=b['item_id'])[0]['removed_at'])
        self.assertIsNotNone(self.mentions(capture=direct['item_id'])[0]['removed_at'])

    def test_new_post_can_recreate_a_deleted_recommendation(self):
        a = self.save()
        old = a['saved_entries'][0]['entry_id']
        self.a.delete_entries([old])
        cache = self.cache(result(('Cafe', 'cafe')), 'new')
        new = self.save(key='new', post='new', cache_id=cache)
        self.assertNotEqual(new['saved_entries'][0]['entry_id'], old)
        self.assertIsNotNone(self.mentions(capture=a['item_id'])[0]['removed_at'])

    def test_mention_delete_keeps_parent_until_last_mention_and_clears_private_fields(self):
        self.save()
        cache = self.cache(result(('Cafe', 'cafe')), 'other')
        self.save(key='other', post='other', cache_id=cache)
        first = self.mentions()[0]
        self.assertEqual(self.a.delete_mention(first['id'])['deleted_entries'], 0)
        removed = self.mentions()[0]
        self.assertEqual(removed['description'], '')
        self.assertEqual(removed['source_name'], '')
        self.assertIsNone(removed['resolution_candidates_json'])
        self.assertIsNone(removed['tags_json'])
        final = next(r for r in self.mentions() if r['entry_id'] == first['entry_id'])
        self.assertEqual(self.a.delete_mention(final['id'])['deleted_entries'], 1)
        with self.assertRaises(RecordNotFound):
            self.a.delete_mention(first['id'])

    def test_result_versions_do_not_replace_a_pinned_capture(self):
        saved = self.save()
        v2 = self.cache(result(('New extraction', 'new-place')), version='v2')
        self.a.delete_entries([r['entry_id'] for r in saved['saved_entries']])
        request = self.a.accept_public('https://youtu.be/post', 'reshare')
        self.a.materialize(request['ingest_id'], public_cache_id=v2)
        self.assertEqual({r['source_name'] for r in self.mentions()}, {'Cafe', 'Bakery'})
        self.assertEqual(self.connection().execute('SELECT post_cache_id FROM captures WHERE id = ?', (saved['item_id'],)).fetchone()[0], self.cache_id)

    def test_direct_requests_are_private_retryable_and_new_invocations_get_new_captures(self):
        request = self.a.accept_direct('Save my cafe', 'direct-1', channel='siri', context={'latitude': 40.0, 'longitude': -74.0})
        self.a.materialize(request['ingest_id'], private_result=result(('Cafe', 'cafe')))
        self.assertEqual(self.a.accept_direct('Save my cafe', 'direct-1', channel='siri', context={'longitude': -74.0, 'latitude': 40.0})['item_id'], request['item_id'])
        with self.assertRaises(AccountConflict):
            self.a.accept_direct('Different input', 'direct-1', channel='siri')
        second = self.a.accept_direct('Save my cafe', 'direct-2', channel='siri')
        self.a.materialize(second['ingest_id'], private_result=result(('Cafe', 'cafe')))
        self.assertNotEqual(request['item_id'], second['item_id'])
        self.assertEqual(len(self.a.entries()), 1)
        self.assertEqual(len(self.a.entries()[0]['sources']), 2)
        self.assertEqual(self.b.sources(), [])
        self.assertEqual(self.connection().execute('SELECT COUNT(*) FROM post_processing_cache').fetchone()[0], 1)
        direct = self.connection().execute('SELECT * FROM captures WHERE id = ?', (request['item_id'],)).fetchone()
        self.assertIsNone(direct['source_url']); self.assertIsNone(direct['post_cache_id'])
        self.assertIn('latitude', direct['context_json'])
        self.assertIsNotNone(direct['private_result_json'])

    def test_direct_retry_uses_pinned_original_result(self):
        req = self.a.accept_direct('Cafe', 'direct')
        self.a.materialize(req['ingest_id'], private_result=result(('Cafe', 'cafe')))
        before = self.snapshot()
        self.a.materialize(req['ingest_id'], private_result=result(('Other', 'other')))
        self.assertEqual(before, self.snapshot())

    def test_empty_success_is_complete_without_recommendations(self):
        cache = self.cache(result(), 'empty')
        req = self.save(key='empty', post='empty', cache_id=cache)
        self.assertEqual(req['status'], 'completed')
        self.assertEqual(req['saved_entries'], [])
        self.assertEqual(self.a.activity()[0]['status'], 'completed')
        self.assertEqual(self.connection().execute('SELECT materialization_state FROM captures').fetchone()[0], 'complete')

    def test_wrong_cache_unpublished_cache_and_mixed_baseline_modes_are_rejected(self):
        req = self.a.accept_public('https://youtu.be/post', 'first')
        wrong = self.cache(self.baseline, 'wrong')
        with self.connection() as con:
            pending = con.execute("INSERT INTO post_processing_cache (platform, post_id, canonical_url, processing_version) VALUES ('youtube', 'post', 'https://youtu.be/post', 'pending')").lastrowid
        before = self.snapshot()
        for cache_id in (wrong, pending, 99999):
            with self.assertRaises(AccountConflict):
                self.a.materialize(req['ingest_id'], public_cache_id=cache_id)
        with self.assertRaises(AccountConflict):
            self.a.materialize(req['ingest_id'], private_result=self.baseline)
        self.assertEqual(before, self.snapshot())
        direct = self.a.accept_direct('Cafe', 'direct')
        with self.assertRaises(AccountConflict):
            self.a.materialize(direct['ingest_id'], public_cache_id=self.cache_id)

    def test_invalid_result_keys_and_unexpected_payloads_never_partially_save(self):
        req = self.a.accept_direct('Cafe', 'direct')
        invalids = []
        duplicate = copy.deepcopy(self.baseline); duplicate['mentions'][1]['key'] = duplicate['mentions'][0]['key']; invalids.append(duplicate)
        for key in ('media', 'user_id', 'private_context', 'raw_provider_response'):
            bad = copy.deepcopy(self.baseline); bad[key] = 'not allowed'; invalids.append(bad)
        bad = copy.deepcopy(self.baseline); bad['mentions'][0]['place'] = {'id': 'cafe'}; invalids.append(bad)
        bad = copy.deepcopy(self.baseline); bad['mentions'][0]['key'] = ''; invalids.append(bad)
        bad = copy.deepcopy(self.baseline); bad['mentions'][0]['extracted']['timestamp_seconds'] = float('nan'); invalids.append(bad)
        before = self.snapshot()
        for invalid in invalids:
            with self.assertRaises((ValidationError, ValueError)):
                self.a.materialize(req['ingest_id'], private_result=invalid)
            self.assertEqual(before, self.snapshot())
        with self.assertRaises(ValueError):
            validate_result(' ' * 1_000_001)

    def test_membership_corruption_is_rejected_before_restoration(self):
        self.save()
        with self.connection() as con:
            con.execute("UPDATE recommendation_mentions SET output_key = 'unknown' WHERE user_id = 'a' AND ordinal = 0")
        req = self.a.accept_public('https://youtu.be/post', 'new')
        before = self.snapshot()
        with self.assertRaises(AccountConflict):
            self.a.materialize(req['ingest_id'])
        self.assertEqual(before, self.snapshot())

    def test_mid_materialization_failure_rolls_back_all_output_and_completion(self):
        req = self.a.accept_public('https://youtu.be/post', 'first')
        before = self.snapshot()
        original = CaptureStore._write_mention
        def fail_after_write(instance, *args):
            original(instance, *args)
            raise RuntimeError('injected failure')
        with patch.object(CaptureStore, '_write_mention', fail_after_write):
            with self.assertRaisesRegex(RuntimeError, 'injected failure'):
                self.a.materialize(req['ingest_id'], public_cache_id=self.cache_id)
        self.assertEqual(before, self.snapshot())

    def test_cross_account_completion_and_deletion_are_rejected(self):
        req = self.save()
        before = self.snapshot()
        with self.assertRaises(RecordNotFound):
            self.b.materialize(req['ingest_id'])
        with self.assertRaises(RecordNotFound):
            self.b.delete_mention(self.mentions()[0]['id'])
        self.assertEqual(before, self.snapshot())

    def test_disabled_or_deleted_account_wins_over_late_worker(self):
        req = self.a.accept_public('https://youtu.be/post', 'first')
        with self.connection() as con:
            con.execute("UPDATE users SET status = 'disabled' WHERE id = 'a'")
        with self.assertRaises(AccountUnavailable):
            self.a.materialize(req['ingest_id'], public_cache_id=self.cache_id)
        with self.connection() as con:
            con.execute("DELETE FROM users WHERE id = 'a'")
        with self.assertRaises(AccountUnavailable):
            self.a.materialize(req['ingest_id'], public_cache_id=self.cache_id)
        self.assertEqual(self.mentions(), [])

    def test_cancelled_request_stays_hidden_and_old_key_cannot_become_new_intent(self):
        req = self.save()
        self.a.delete_entries([r['entry_id'] for r in req['saved_entries']])
        retry = self.a.accept_public('https://youtu.be/post', 'failed-reshare')
        with self.connection() as con:
            con.execute("UPDATE ingest_runs SET status = 'failed' WHERE id = ?", (retry['ingest_id'],))
        self.a.delete_failed_activity(retry['ingest_id'])
        before = self.snapshot()
        cancelled = self.a.accept_public('https://youtu.be/post', 'failed-reshare')
        self.assertEqual(cancelled['status'], 'cancelled')
        with self.assertRaises(RecordNotFound):
            self.a.materialize(retry['ingest_id'])
        self.assertEqual(before, self.snapshot())
        self.assertNotIn(retry['ingest_id'], [r['id'] for r in self.a.activity()])
        newer = self.a.accept_public('https://youtu.be/post', 'genuinely-new-share')
        self.a.materialize(newer['ingest_id'])
        self.assertTrue(all(r['removed_at'] is None for r in self.mentions()))

    def test_concurrent_duplicate_delivery_creates_one_request_and_one_capture(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            rows = list(pool.map(lambda _: self.a.accept_public('https://youtu.be/post', 'same-key'), range(2)))
        self.assertEqual(rows[0]['ingest_id'], rows[1]['ingest_id'])
        self.assertEqual(self.connection().execute('SELECT COUNT(*) FROM captures').fetchone()[0], 1)
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _: self.a.materialize(rows[0]['ingest_id'], public_cache_id=self.cache_id), range(2)))
        self.assertEqual(len(self.mentions()), 2)

    def test_legacy_capture_is_not_silently_reprocessed_or_restored(self):
        with self.connection() as con:
            con.execute("INSERT INTO captures (user_id, vertical, source_url, input_kind, capture_channel, source_platform, source_post_id, materialization_state) VALUES ('a', 'recommendation', 'https://youtu.be/post', 'public_post', 'legacy', 'youtube', 'post', 'legacy_unverified')")
        before = self.snapshot()
        with self.assertRaises(AccountConflict):
            self.a.accept_public('https://youtu.be/post', 'first')
        self.assertEqual(before, self.snapshot())
        self.save(self.b)

    def test_multiple_outputs_for_same_place_preserve_individual_lifecycle(self):
        cache = self.cache(result(('Cafe intro', 'cafe'), ('Cafe later', 'cafe')), 'twice')
        req = self.save(key='twice', post='twice', cache_id=cache)
        self.assertEqual(len(self.mentions()), 2)
        self.assertEqual(len(self.a.entries()), 1)
        self.assertEqual([r['source_count'] for r in req['saved_entries']], [1, 1])
        first, second = self.mentions()
        self.assertEqual(self.a.delete_mention(first['id'])['deleted_entries'], 0)
        again = self.a.accept_public('https://youtu.be/twice', 'again')
        self.a.materialize(again['ingest_id'])
        self.assertEqual(self.mentions()[1], second)
        self.assertEqual(len(self.a.entries()[0]['sources']), 2)

    def test_location_edit_then_reshare_can_restore_into_same_place_and_capture(self):
        body = result(('Cafe', 'cafe'), ('Mystery cafe', 'unused'))
        body['mentions'][1].update(status='needs_review', place_id=None, candidate_ids=['cafe'])
        cache = self.cache(body, 'review')
        req = self.save(key='review', post='review', cache_id=cache)
        first, second = self.mentions()
        self.a.delete_mention(first['id'])
        with self.connection() as con:
            con.execute("UPDATE locations SET lat = 40.0, lng = -74.0 WHERE google_place_id = 'cafe'")
            con.execute('UPDATE recommendation_mentions SET resolution_candidates_json = ? WHERE id = ?',
                        (json.dumps([{'id': 'cafe', 'location': {'latitude': 40.0, 'longitude': -74.0}}]), second['id']))
        self.a.confirm_activity_location(req['ingest_id'], second['entry_id'], 'cafe', mention_id=second['id'])
        edited = self.mentions()[1]
        again = self.a.accept_public('https://youtu.be/review', 'again')
        self.a.materialize(again['ingest_id'])
        self.assertEqual(self.mentions()[1], edited)
        self.assertEqual(self.mentions()[0]['entry_id'], edited['entry_id'])

    def test_confirming_one_grouped_mention_preserves_other_mentions_location(self):
        body = result(('Cafe', 'unused'), ('Cafe', 'unused'))
        for output in body['mentions']:
            output.update(status='needs_review', place_id=None, candidate_ids=['new-place'])
        with self.connection() as con:
            con.execute("INSERT INTO locations (google_place_id, lat, lng) VALUES ('new-place', 40, -74)")
        cache = self.cache(body, 'ambiguous')
        req = self.save(key='ambiguous', post='ambiguous', cache_id=cache)
        first, second = self.mentions()
        self.assertEqual(first['entry_id'], second['entry_id'])
        before = self.snapshot()
        with self.assertRaises(AccountConflict):
            self.a.confirm_activity_location(req['ingest_id'], first['entry_id'], 'new-place')
        self.assertEqual(before, self.snapshot())
        self.a.confirm_activity_location(req['ingest_id'], first['entry_id'], 'new-place', mention_id=first['id'])
        changed, sibling = self.mentions()
        self.assertEqual(sibling, second)
        self.assertNotEqual(changed['entry_id'], sibling['entry_id'])
        with self.connection() as con:
            self.assertIsNone(con.execute('SELECT location_id FROM recommendations WHERE id = ?',
                                         (sibling['entry_id'],)).fetchone()[0])
        self.a.confirm_activity_location(req['ingest_id'], sibling['entry_id'], 'new-place', mention_id=sibling['id'])
        self.assertEqual(len(self.a.entries()), 1)
        self.assertTrue(all(r['entry_id'] == changed['entry_id'] for r in self.mentions()))

    def test_mention_delete_api_auth_and_ownership(self):
        self.save(); self.save(self.b)
        app = create_account_app(db_path=self.db, verify_session=lambda _token: VerifiedAccount('a'))
        with TestClient(app) as client:
            own = self.mentions()[0]['id']; foreign = self.mentions('b')[0]['id']
            self.assertEqual(client.delete('/api/v1/mentions/' + str(own)).status_code, 401)
            self.assertEqual(client.delete('/api/v1/mentions/' + str(foreign), headers={'Authorization': 'Bearer test'}).status_code, 404)
            self.assertEqual(client.delete('/api/v1/mentions/' + str(own), headers={'Authorization': 'Bearer test'}).status_code, 200)

    def test_upgrade_v1_copy_preserves_records_and_rolls_back_on_wrong_owner(self):
        self.save()
        with self.connection() as con:
            con.execute('CREATE UNIQUE INDEX idx_mentions_active_recommendation_capture ON recommendation_mentions(entry_id, item_id) WHERE removed_at IS NULL')
            con.execute('PRAGMA user_version = 1')
        before = self.snapshot()
        with self.assertRaises(ValueError):
            migrate_copy(self.connection(), owner_id='missing', owner_name='Missing')
        self.assertEqual(before, self.snapshot())
        target = Path(self.directory.name) / 'upgraded.db'
        report = prepare_copy(self.db, target, owner_id='a', owner_name='A')
        self.assertEqual(report['upgraded_from'], 1)
        self.assertEqual(before, self.snapshot())
        con = sqlite3.connect(target); self.addCleanup(con.close)
        self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
        self.assertEqual(con.execute('SELECT COUNT(*) FROM recommendation_mentions').fetchone()[0], 2)
        self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])


if __name__ == '__main__':
    unittest.main()
