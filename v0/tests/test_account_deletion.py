from __future__ import annotations

import sqlite3
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock

from fastapi.testclient import TestClient
from firebase_admin import auth, exceptions

import store
from account_app import InvalidSession, RecentSignInRequired, create_account_app
from account_deletion import AccountDeletion
from account_store import AccountStore, AccountUnavailable
from capture_store import CaptureStore
from firebase_identity import FirebaseIdentity, FirebaseSessionVerifier, IdentityStore
from legacy_reconciliation import prepare_reconciled_copy
from multi_user_migration import prepare_copy


class AccountDeletionTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = self.root / 'original.sqlite'
        self.db = self.root / 'accounts.sqlite'
        store.init_db(source)
        store.save_ingest(source, {'source_url': 'https://youtu.be/public-post',
            'entries_extracted': [{'extracted_name': 'Cafe', 'type_name': 'Restaurant'}],
            'resolved_entries': [{'extracted': {'extracted_name': 'Cafe', 'type_name': 'Restaurant'},
                'status': 'resolved', 'place': {'id': 'shared-place'}}]})
        prepare_reconciled_copy(source, self.db, owner_id='owner', owner_name='Owner')
        self.accounts = IdentityStore(self.db)
        self.identity = FirebaseIdentity('project', 'uid-owner', 'Owner', int(time.time()))
        self.other = FirebaseIdentity('project', 'uid-other', 'Other', int(time.time()))
        self.accounts.bind_existing('owner', self.identity)
        self.other_id = self.accounts.resolve(self.other).user_id
        self.tokens = Mock(project_id='project')
        self.tokens.verify.return_value = self.identity
        self.deletion = AccountDeletion(self.accounts, self.tokens)

    def rows(self, table):
        with sqlite3.connect(self.db) as con:
            return con.execute('SELECT * FROM ' + table + ' ORDER BY 1').fetchall()

    def test_request_is_durable_idempotent_and_blocks_all_access(self):
        self.deletion.request('fresh-token')
        self.deletion.request('fresh-token')
        self.tokens.verify.assert_called_with('fresh-token', recent=True)
        self.tokens.client.delete_user.assert_not_called()
        with self.assertRaises(AccountUnavailable):
            AccountStore(self.db, 'owner').entries()
        with self.assertRaises(AccountUnavailable):
            self.accounts.resolve(self.identity)
        self.assertEqual(AccountStore(self.db, self.other_id).entries(), [])
        self.assertEqual(len(self.rows('recommendations')), 1)

    def test_worker_removes_only_target_private_data_and_keeps_shared_rows(self):
        other_store = CaptureStore(self.db, self.other_id)
        accepted = other_store.accept_direct('My own cafe', 'private-other')
        other_store.materialize(accepted['ingest_id'], private_result={'mentions': [{
            'key': 'other-cafe', 'extracted': {'extracted_name': 'My own cafe', 'type_name': 'Restaurant'},
            'status': 'resolved', 'place_id': 'shared-place'}]})
        owned = ['captures', 'recommendations', 'recommendation_mentions', 'movie_enrichments', 'ingest_runs', 'ingest_events']
        with sqlite3.connect(self.db) as con:
            other_before = {table: con.execute('SELECT * FROM '+table+' WHERE user_id=? ORDER BY 1',
                                              (self.other_id,)).fetchall() for table in owned}
        shared = self.rows('locations')
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO post_processing_cache (platform,post_id,canonical_url,processing_version) VALUES ('youtube','shared','https://youtu.be/shared','v1')")
        cache = self.rows('post_processing_cache')
        self.deletion.request('fresh')
        self.assertEqual(self.deletion.drain_once(), {'completed': 1, 'pending_retry': 0})
        self.tokens.client.delete_user.assert_called_once_with('uid-owner')
        for table in owned:
            self.assertEqual(self.rows(table), other_before[table], table)
        self.assertEqual(self.rows('locations'), shared)
        self.assertEqual(self.rows('post_processing_cache'), cache)
        self.assertEqual([r[0] for r in self.rows('users')], [self.other_id])
        self.assertEqual(self.deletion.drain_once()['completed'], 0)

    def test_provider_outage_keeps_private_records_hidden_and_retries_after_restart(self):
        self.deletion.request('fresh')
        self.tokens.client.delete_user.side_effect = exceptions.UnavailableError('private provider details')
        self.assertEqual(self.deletion.drain_once(), {'completed': 0, 'pending_retry': 1})
        self.assertEqual(len(self.rows('captures')), 1)
        with self.assertRaises(AccountUnavailable): self.accounts.resolve(self.identity)
        self.tokens.client.delete_user.side_effect = None
        restarted = AccountDeletion(IdentityStore(self.db), self.tokens)
        self.assertEqual(restarted.drain_once()['completed'], 1)

    def test_already_deleted_firebase_user_still_cleans_up_private_rows(self):
        self.deletion.request('fresh')
        self.tokens.client.delete_user.side_effect = auth.UserNotFoundError('gone')
        self.assertEqual(self.deletion.drain_once()['completed'], 1)
        self.assertEqual(self.rows('captures'), [])

    def test_first_request_can_delete_unused_identity_without_claiming_legacy(self):
        self.tokens.verify.return_value = FirebaseIdentity('project', 'new-uid', 'Owner', int(time.time()))
        self.deletion.request('fresh')
        self.deletion.drain_once()
        self.tokens.client.delete_user.assert_called_once_with('new-uid')
        self.assertEqual(len(AccountStore(self.db, 'owner').entries()), 1)

    def test_worker_does_not_delete_pending_identity_from_another_project(self):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE users SET status='deleting', firebase_project_id='other-project' WHERE id='owner'")
        self.assertEqual(self.deletion.drain_once()['completed'], 0)
        self.tokens.client.delete_user.assert_not_called()

    def test_api_requires_recent_login_and_accepts_retry_without_reactivating(self):
        sessions = FirebaseSessionVerifier(self.tokens, self.accounts)
        api = TestClient(create_account_app(db_path=self.db, verify_session=sessions, deletion=self.deletion))
        self.assertEqual(api.delete('/api/v1/account').status_code, 401)
        headers = {'Authorization': 'Bearer fresh'}
        self.tokens.verify.side_effect = RecentSignInRequired()
        response = api.delete('/api/v1/account', headers=headers)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()['code'], 'recent_sign_in_required')
        self.tokens.verify.side_effect = None
        self.assertEqual(len(AccountStore(self.db, 'owner').entries()), 1)
        self.assertEqual(api.delete('/api/v1/account', headers=headers).status_code, 202)
        self.assertEqual(api.delete('/api/v1/account', headers=headers).status_code, 202)
        self.assertEqual(api.get('/api/v1/entries', headers=headers).status_code, 401)

    def test_late_verified_request_cannot_recreate_deleted_account(self):
        self.deletion.request('fresh')
        self.deletion.drain_once()
        self.tokens.verify.side_effect = [self.identity, InvalidSession()]
        with self.assertRaises(InvalidSession):
            FirebaseSessionVerifier(self.tokens, self.accounts)('token-verified-before-deletion')
        self.assertEqual([r[0] for r in self.rows('users')], [self.other_id])

    def test_offline_schema_four_upgrade_preserves_every_existing_row(self):
        # The only schema-5 change is admitting the durable deleting status.
        # Create an actual schema-4 CHECK on a new copy, not the source library.
        with sqlite3.connect(self.db) as con:
            schema = con.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
            con.execute('PRAGMA writable_schema=ON')
            con.execute("UPDATE sqlite_master SET sql=? WHERE name='users'",
                        (schema.replace("'active', 'disabled', 'deleting'", "'active', 'disabled'"),))
            con.execute('PRAGMA writable_schema=OFF')
            con.execute('PRAGMA user_version=4')
        before = {t:self.rows(t) for t in ['users','captures','recommendations','recommendation_mentions','locations']}
        upgraded = self.root / 'schema5.sqlite'
        report = prepare_copy(self.db, upgraded, owner_id='owner', owner_name='Owner')
        self.assertTrue(report['original_data_preserved'])
        with sqlite3.connect(upgraded) as con:
            for table, rows in before.items():
                self.assertEqual(con.execute('SELECT * FROM '+table+' ORDER BY 1').fetchall(), rows)
            con.execute("UPDATE users SET status='deleting' WHERE id='owner'")
        for table, rows in before.items():
            self.assertEqual(self.rows(table), rows)
