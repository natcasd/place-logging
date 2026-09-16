import hashlib
import json
import logging
import sqlite3
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock, patch
from fastapi.testclient import TestClient
import store
from account_app import VerifiedAccount, create_account_app
from account_deletion import AccountDeletion
from account_operations import CaptureLimits, PipelineUsageOnly, SaveLimitExceeded, SavesPaused, queue_metrics
from account_recovery import DeletionJournal, prepare_backup
from account_store import AccountStore, AccountUnavailable
from capture_store import CaptureStore
from firebase_identity import FirebaseIdentity, IdentityStore
from legacy_reconciliation import prepare_reconciled_copy
from post_processing_store import PostProcessingStore

class OperationalTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        source = self.root / 'legacy.sqlite'
        self.db = self.root / 'accounts.sqlite'
        store.init_db(source)
        prepare_reconciled_copy(source, self.db, owner_id='a', owner_name='A')
        self.identities = IdentityStore(self.db)
        self.identity = FirebaseIdentity('project', 'uid-a', 'A', int(time.time()))
        self.identities.bind_existing('a', self.identity)
        self.b = self.identities.resolve(FirebaseIdentity('project', 'uid-b', 'B', int(time.time()))).user_id
        self.pause = self.root / 'pause'
        self.limits = CaptureLimits(user_pending=2, total_pending=3, user_daily=3, total_daily=5, manual_retries=2, pause_file=self.pause)
        self.a = CaptureStore(self.db, 'a', self.limits)
        self.journal = DeletionJournal.initialize(self.root / 'deletions.sqlite')
        self.tokens = Mock(project_id='project')
        self.tokens.verify.return_value = self.identity

    def save(self, key):
        return self.a.accept_public('https://youtu.be/'+key, key)

    def fail(self, run):
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE ingest_runs SET status='failed' WHERE id=?", (run['ingest_id'],))

    def test_concurrent_capacity(self):
        def attempt(i):
            try: self.save('post'+str(i)); return True
            except SaveLimitExceeded: return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(attempt, range(6))), 2)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT COUNT(*) FROM captures').fetchone()[0], 2)

    def test_replay_at_capacity_and_while_paused(self):
        first = self.save('one'); self.save('two'); self.pause.touch()
        self.assertEqual(self.save('one'), first)
        with self.assertRaises(SavesPaused): self.save('three')
        self.assertEqual(len(self.a.activity()), 2)

    def test_cancelled_failures_still_count_toward_daily_limit(self):
        for key in ('one', 'two', 'three'):
            run = self.save(key); self.fail(run); self.a.delete_failed_activity(run['ingest_id'])
        with self.assertRaises(SaveLimitExceeded): self.save('four')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE ingest_runs SET started_at=datetime('now','-2 days')")
        self.save('four')

    def test_per_user_and_global_capacity(self):
        self.save('one'); self.save('two')
        other = CaptureStore(self.db, self.b, self.limits)
        other.accept_public('https://youtu.be/three', 'three')
        with self.assertRaises(SaveLimitExceeded): other.accept_public('https://youtu.be/four', 'four')

    def test_retry_limit_and_scheduled_slot_reuse(self):
        run = self.save('one'); self.save('two')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE ingest_runs SET status='retry_scheduled' WHERE id=?", (run['ingest_id'],))
        self.a.retry_public(run['ingest_id']); self.fail(run)
        self.a.retry_public(run['ingest_id']); self.fail(run)
        with self.assertRaises(SaveLimitExceeded): CaptureStore(self.db, 'a', self.limits).retry_public(run['ingest_id'])

    def test_pause_keeps_reads_and_blocks_new_provider_jobs(self):
        self.save('one'); self.pause.touch()
        queue = PostProcessingStore(self.db, 'v1', pause_file=self.pause)
        self.assertIsNone(queue.claim())
        self.assertEqual(len(self.a.activity()), 1)
        self.pause.unlink()
        self.assertIsNotNone(queue.claim())

    def test_http_limits_keep_session(self):
        client = TestClient(create_account_app(db_path=self.db, verify_session=lambda _:VerifiedAccount('a'), limits=self.limits))
        headers = {'Authorization': 'Bearer valid'}
        def post(key): return client.post('/api/v1/ingests', headers=headers, json={'source_url':'https://youtu.be/'+key,'request_key':key})
        self.assertEqual(post('one').status_code, 202); self.assertEqual(post('two').status_code, 202)
        limited = post('three')
        self.assertEqual(limited.status_code, 429); self.assertIn('Retry-After', limited.headers)
        self.pause.touch()
        self.assertEqual(post('three').status_code, 503)
        self.assertEqual(client.get('/api/v1/entries', headers=headers).status_code, 200)

    def test_metrics_exclude_private_identifiers(self):
        self.save('secret-source')
        metrics = queue_metrics(PostProcessingStore(self.db, 'v1'))
        self.assertEqual(metrics['pending_saves'], 1)
        self.assertTrue(all(type(v) is int and v >= 0 for v in metrics.values()))
        self.assertNotIn('secret-source', json.dumps(metrics))

    def test_pipeline_logs_only_numeric_usage(self):
        filter_ = PipelineUsageOnly()
        self.assertFalse(filter_.filter(logging.LogRecord('pipeline', logging.ERROR, '', 1, 'token=secret', (), None)))
        usage = logging.LogRecord('pipeline', logging.INFO, '', 1, 'Gemini usage %s',
                                  (json.dumps({'input_tokens':23,'total_tokens':27,'model':'secret','source_url':'secret'}),), None)
        self.assertTrue(filter_.filter(usage)); self.assertNotIn('secret', usage.getMessage()); self.assertIn('23', usage.getMessage())

    def test_restore_reapplies_later_deletion_and_preserves_other_user(self):
        self.a.accept_direct('Private cafe', 'private')
        CaptureStore(self.db, self.b).accept_direct('Other cafe', 'other')
        backup = self.root / 'backup.sqlite'; prepare_backup(self.db, backup)
        before = hashlib.sha256(backup.read_bytes()).hexdigest()
        deletion = AccountDeletion(self.identities, self.tokens, self.journal)
        deletion.request('fresh'); deletion.drain_once()
        restored = self.root / 'restored.sqlite'
        report = prepare_backup(backup, restored, journal=self.journal, project_id='project')
        self.assertEqual(report['accounts_blocked_by_deletion_journal'], 1)
        with self.assertRaises(AccountUnavailable): AccountStore(restored, 'a').activity()
        self.assertEqual(len(AccountStore(restored, self.b).activity()), 1)
        AccountDeletion(IdentityStore(restored), self.tokens, self.journal).drain_once()
        with sqlite3.connect(restored) as con:
            self.assertEqual(con.execute('SELECT user_id FROM captures').fetchall(), [(self.b,)])
        self.assertEqual(hashlib.sha256(backup.read_bytes()).hexdigest(), before)
        self.assertEqual(restored.stat().st_mode & 0o777, 0o600)

    def test_journal_survives_crash_before_status_commit(self):
        self.save('one'); self.journal.record('project','uid-a')
        self.assertEqual(len(self.a.activity()), 1)
        AccountDeletion(self.identities, self.tokens, self.journal).drain_once()
        self.tokens.client.delete_user.assert_called_once_with('uid-a')
        with self.assertRaises(AccountUnavailable): self.a.activity()

    def test_journal_failure_does_not_accept_deletion(self):
        with patch.object(self.journal, 'record', side_effect=OSError('disk unavailable')):
            with self.assertRaises(OSError): AccountDeletion(self.identities, self.tokens, self.journal).request('fresh')
        self.a.require_active(); self.tokens.client.delete_user.assert_not_called()

    def test_backup_includes_committed_wal_and_never_overwrites(self):
        con = sqlite3.connect(self.db)
        try:
            con.execute('PRAGMA journal_mode=WAL')
            con.execute("UPDATE users SET display_name='Latest WAL value' WHERE id='a'"); con.commit()
            output = self.root / 'wal.sqlite'; prepare_backup(self.db, output)
            with sqlite3.connect(output) as copy:
                self.assertEqual(copy.execute("SELECT display_name FROM users WHERE id='a'").fetchone()[0], 'Latest WAL value')
            with self.assertRaises(ValueError): prepare_backup(self.db, output)
        finally: con.close()

    def test_recovery_fails_closed_for_missing_journal_or_wrong_project(self):
        with self.assertRaises(FileNotFoundError): DeletionJournal(self.root/'missing.sqlite')
        output = self.root/'wrong.sqlite'
        with self.assertRaises(ValueError): prepare_backup(self.db, output, journal=self.journal, project_id='different')
        self.assertFalse(output.exists())
        with self.assertRaises(FileExistsError): DeletionJournal.initialize(self.journal.path)

    def test_journal_export_is_independent_and_keeps_deletions(self):
        self.journal.record('project', 'uid-a')
        output = self.root / 'journal-backup.sqlite'
        self.journal.backup(output)
        exported = DeletionJournal(output)
        with self.identities.transaction() as con:
            self.assertEqual(exported.apply(con, 'project'), 1)
        with self.assertRaises(AccountUnavailable): self.a.require_active()
        self.assertEqual(output.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(ValueError): self.journal.backup(output)

    def test_release_factory_replays_journal_before_serving(self):
        from firebase_service import create_app
        import os
        self.journal.record('project', 'uid-a')
        env = {'JOT_ACCOUNT_DB_PATH': str(self.db), 'FIREBASE_PROJECT_ID': 'project',
               'JOT_PROCESSING_VERSION': 'v1', 'JOT_DELETION_JOURNAL_PATH': str(self.journal.path),
               'JOT_PROCESSING_PAUSE_FILE': str(self.pause)}
        with patch.dict(os.environ, env), patch('firebase_service.create_token_verifier', return_value=self.tokens), patch('firebase_service.configure_private_logging'):
            create_app()
        with self.assertRaises(AccountUnavailable): self.a.require_active()
        AccountStore(self.db, self.b).require_active()
