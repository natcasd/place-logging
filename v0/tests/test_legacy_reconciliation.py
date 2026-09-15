from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import store
from account_store import AccountStore
from capture_store import CaptureStore
from legacy_reconciliation import prepare_reconciled_copy, reconcile_account
from multi_user_migration import prepare_copy, SCHEMA_VERSION
from post_processing_store import PostProcessingStore
from post_processing_worker import PostProcessingWorker


class LegacyReconciliationTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.source = self.root / 'original.sqlite'
        self.target = self.root / 'accounts.sqlite'
        store.init_db(self.source)
        self.capture = self.seed('https://youtu.be/post', ['Cafe', 'Bakery'])
        entries = store.list_entries(self.source)
        removed = next(e['id'] for e in entries if e['name'] == 'Bakery')
        store.delete_entry(self.source, removed)
        with sqlite3.connect(self.source) as con:
            con.execute("UPDATE recommendation_mentions SET description = 'My private edit', source_type = 'Café'")
            con.execute("UPDATE recommendations SET name = 'My private name'")
        self.source_hash = self.hash(self.source)
        self.expected = [store.list_entries(self.source), store.list_sources(self.source), store.list_ingest_runs(self.source)]

    def hash(self, path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def seed(self, url, names):
        originals = [{'extracted_name': name, 'type_name': 'Restaurant', 'description': 'Original ' + name} for name in names]
        return store.save_ingest(self.source, {'source_url': url, 'entries_extracted': originals,
            'metadata': {'uploader': 'Creator'}, 'resolved_entries': [
                {'extracted': e, 'status': 'resolved', 'place': {'id': e['extracted_name'],
                 'displayName': {'text': e['extracted_name']}}} for e in originals]})

    def migrate(self):
        report = prepare_reconciled_copy(self.source, self.target, owner_id='nathan', owner_name='Nathan')
        self.assertEqual(self.hash(self.source), self.source_hash)
        return report

    def rows(self, table):
        with sqlite3.connect(self.target) as con:
            con.row_factory = sqlite3.Row
            return [dict(r) for r in con.execute('SELECT * FROM ' + table + ' ORDER BY id')]

    def worker(self):
        def no_processing(*_):
            self.fail('Historical private baselines must not invoke public extraction')
        return PostProcessingWorker(PostProcessingStore(self.target, 'v1'), self.root, no_processing)

    def test_copy_keeps_original_library_and_absences_and_never_publishes_private_data(self):
        report = self.migrate()
        account = AccountStore(self.target, 'nathan')
        self.assertEqual([account.entries(), account.sources(), account.activity()], self.expected)
        self.assertEqual(report['reconciliation']['preserved_removed_outputs'], 1)
        self.assertEqual(self.rows('post_processing_cache'), [])
        mentions = self.rows('recommendation_mentions')
        self.assertEqual(sum(r['removed_at'] is not None for r in mentions), 1)
        self.assertEqual(mentions[0]['description'], 'My private edit')
        self.assertIsNotNone(self.rows('captures')[0]['private_result_json'])

    def test_deliberate_reshare_preserves_active_edits_and_restores_missing_output_unresolved(self):
        self.migrate()
        account = CaptureStore(self.target, 'nathan')
        old = self.rows('recommendation_mentions')[0]
        request = account.accept_public('https://youtu.be/post', 'new-share')
        self.worker().run_once()
        rows = self.rows('recommendation_mentions')
        self.assertEqual(rows[0], old)
        self.assertTrue(all(r['removed_at'] is None for r in rows))
        restored = next(r for r in rows if r['source_name'] == 'Bakery')
        self.assertEqual(restored['description'], 'Original Bakery')
        self.assertEqual(restored['resolution_status'], 'unresolved')
        self.assertEqual(account.activity()[0]['status'], 'partial')
        self.assertEqual(account.activity()[0]['item_id'], self.capture)
        self.assertEqual(account.accept_public('https://youtu.be/post', 'new-share')['ingest_id'], request['ingest_id'])
        self.assertEqual(self.rows('post_processing_cache'), [])

    def test_deleting_after_acceptance_wins_over_late_legacy_delivery(self):
        self.migrate()
        account = CaptureStore(self.target, 'nathan')
        account.accept_public('https://youtu.be/post', 'before-delete')
        old = self.rows('recommendation_mentions')[0]
        account.delete_mention(old['id'])
        self.worker().run_once()
        self.assertIsNotNone(next(r for r in self.rows('recommendation_mentions') if r['id'] == old['id'])['removed_at'])
        account.accept_public('https://youtu.be/post', 'later-deliberate-share')
        self.worker().run_once()
        self.assertIsNone(next(r for r in self.rows('recommendation_mentions') if r['id'] == old['id'])['removed_at'])

    def test_other_user_uses_fresh_public_result_without_private_edits(self):
        self.migrate()
        with sqlite3.connect(self.target) as con:
            con.execute("INSERT INTO users (id, display_name) VALUES ('friend', 'Friend')")
        friend = CaptureStore(self.target, 'friend')
        friend.accept_public('https://youtu.be/post', 'first')
        public = {'mentions': [{'key': 'fresh', 'extracted': {'extracted_name': 'Public Cafe',
                  'type_name': 'Restaurant'}, 'status': 'resolved', 'place_id': 'Cafe'}]}
        queue = PostProcessingStore(self.target, 'v1')
        PostProcessingWorker(queue, self.root, lambda *_: public).run_once()
        account = CaptureStore(self.target, 'nathan')
        account.accept_public('https://youtu.be/post', 'nathan-share')
        self.worker().run_once()
        self.assertEqual(friend.entries()[0]['name'], 'Public Cafe')
        self.assertEqual(len(friend.entries()), 1)
        self.assertNotIn('My private', self.rows('post_processing_cache')[0]['result_json'])
        self.assertEqual(next(r for r in self.rows('recommendation_mentions') if r['source_name'] == 'Cafe')['description'], 'My private edit')

    def test_duplicate_historical_captures_keep_ids_and_private_mentions(self):
        second = self.seed('https://www.youtube.com/watch?v=post', ['Other Cafe'])
        self.source_hash = self.hash(self.source)
        self.migrate()
        account = CaptureStore(self.target, 'nathan')
        ids = {r['id'] for r in self.rows('captures')}
        mentions = self.rows('recommendation_mentions')
        account.accept_public('https://youtu.be/post', 'group-share')
        self.worker().run_once()
        self.assertEqual({r['id'] for r in self.rows('captures')}, {self.capture, second})
        self.assertEqual(ids, {r['id'] for r in self.rows('captures')})
        self.assertTrue({r['id'] for r in mentions}.issubset({r['id'] for r in self.rows('recommendation_mentions')}))
        self.assertEqual(len(account.entries()), 3)

    def test_ambiguous_correspondence_fails_without_publishing_or_changing_source(self):
        with sqlite3.connect(self.source) as con:
            con.execute("UPDATE recommendation_mentions SET source_name = 'Unmatched' WHERE item_id = ?", (self.capture,))
        before = self.hash(self.source)
        with self.assertRaisesRegex(ValueError, 'ambiguous'):
            prepare_reconciled_copy(self.source, self.target, owner_id='nathan', owner_name='Nathan')
        self.assertFalse(self.target.exists())
        self.assertEqual(self.hash(self.source), before)

    def test_invalid_original_json_rolls_back_and_does_not_publish(self):
        with sqlite3.connect(self.source) as con:
            con.execute("UPDATE captures SET llm_output_json = 'invalid'")
        before = self.hash(self.source)
        with self.assertRaises(ValueError):
            prepare_reconciled_copy(self.source, self.target, owner_id='nathan', owner_name='Nathan')
        self.assertFalse(self.target.exists())
        self.assertEqual(self.hash(self.source), before)

    def test_reconciliation_is_idempotent_and_baseline_is_pinned(self):
        self.migrate()
        before = self.rows('captures'), self.rows('recommendation_mentions')
        self.assertEqual(reconcile_account(self.target, 'nathan')['reconciled_captures'], 0)
        self.assertEqual(before, (self.rows('captures'), self.rows('recommendation_mentions')))
        with sqlite3.connect(self.target) as con:
            with self.assertRaises(sqlite3.IntegrityError):
                con.execute("UPDATE captures SET private_result_json = '{\"mentions\":[]}'")

    def test_reconciliation_refuses_existing_output(self):
        self.migrate()
        before = self.hash(self.target)
        with self.assertRaises(ValueError):
            prepare_reconciled_copy(self.source, self.target, owner_id='nathan', owner_name='Nathan')
        self.assertEqual(self.hash(self.target), before)

    def test_v2_upgrade_preserves_every_field_and_high_water_marks(self):
        prepare_copy(self.source, self.target, owner_id='nathan', owner_name='Nathan')
        with sqlite3.connect(self.target) as con:
            con.execute('PRAGMA user_version = 2')
            con.execute("UPDATE sqlite_sequence SET seq = 9000 WHERE name = 'recommendation_mentions'")
        before = self.rows('captures'), self.rows('recommendation_mentions')
        upgraded = self.root / 'upgraded.sqlite'
        report = prepare_reconciled_copy(self.target, upgraded, owner_id='nathan', owner_name='Nathan')
        self.assertEqual(report['migration']['upgraded_from'], 2)
        self.assertEqual(before, (self.rows('captures'), self.rows('recommendation_mentions')))
        with sqlite3.connect(upgraded) as con:
            self.assertEqual(con.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(con.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertGreater(con.execute('SELECT MAX(id) FROM recommendation_mentions').fetchone()[0], 9000)

    def test_unknown_index_requires_review_and_leaves_source_unchanged(self):
        prepare_copy(self.source, self.target, owner_id='nathan', owner_name='Nathan')
        with sqlite3.connect(self.target) as con:
            con.execute('PRAGMA user_version = 2')
            con.execute('CREATE INDEX custom_library_index ON recommendations(name)')
        before = self.hash(self.target)
        output = self.root / 'unexpected.sqlite'
        with self.assertRaisesRegex(ValueError, 'indexes'):
            prepare_reconciled_copy(self.target, output, owner_id='nathan', owner_name='Nathan')
        self.assertFalse(output.exists())
        self.assertEqual(self.hash(self.target), before)
