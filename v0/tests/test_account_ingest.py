from __future__ import annotations

import base64
import hashlib
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from unittest.mock import MagicMock, patch

import requests
from fastapi.testclient import TestClient

import store
from account_app import InvalidSession, VerifiedAccount, create_account_app
from account_ingest import SourceResolutionUnavailable, resolve_public_url
from capture_store import CaptureStore
from multi_user_migration import prepare_copy
from post_processing_store import PostProcessingStore
from post_processing_worker import PostProcessingWorker


BASELINE = {'mentions': [{'key': 'one', 'extracted': {'extracted_name': 'Cafe',
    'type_name': 'Restaurant', 'description': 'Original'}, 'status': 'resolved', 'place_id': 'place'}]}


class AccountIngestTests(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temp)
        self.legacy = self.root / 'legacy.sqlite'
        self.db = self.root / 'accounts.sqlite'
        store.init_db(self.legacy)
        self.original_hash = hashlib.sha256(self.legacy.read_bytes()).hexdigest()
        prepare_copy(self.legacy, self.db, owner_id='a', owner_name='A')
        with sqlite3.connect(self.db) as con:
            con.execute("INSERT INTO users (id, display_name) VALUES ('b', 'B')")
        self.a = CaptureStore(self.db, 'a')
        self.b = CaptureStore(self.db, 'b')
        self.queue = PostProcessingStore(self.db, 'v1')
        self.client = self.enterContext(TestClient(create_account_app(db_path=self.db, verify_session=self.verify)))

    def tearDown(self):
        self.assertEqual(hashlib.sha256(self.legacy.read_bytes()).hexdigest(), self.original_hash)

    def verify(self, token):
        if token not in {'a', 'b'}:
            raise InvalidSession()
        return VerifiedAccount(token)

    def post(self, path='/ingests', user='a', **body):
        return self.client.post('/api/v1' + path, headers={'Authorization': 'Bearer ' + user}, json=body)

    def accept(self, key='first', user='a', url='https://youtu.be/post'):
        response = self.post(user=user, source_url=url, request_key=key)
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_result_tracks_completion_and_never_exposes_other_accounts(self):
        accepted = self.accept()
        path = f"/api/v1/ingests/{accepted['ingest_id']}"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer b'}).status_code, 404)
        queued = self.client.get(path, headers={'Authorization': 'Bearer a'}).json()
        self.assertEqual(queued['status'], 'queued')
        self.assertEqual(queued['saved_entries'], [])
        PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE).run_once()
        saved = self.client.get(path, headers={'Authorization': 'Bearer a'}).json()
        self.assertEqual(saved['status'], 'completed')
        self.assertEqual(saved['saved_entries'][0]['name'], 'Cafe')
        self.a.delete_entries([saved['saved_entries'][0]['entry_id']])
        self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer a'}).json()['saved_entries'], [])

    def test_result_wait_returns_completion_without_accepting_another_save(self):
        accepted = self.accept()
        path = f"/api/v1/ingests/{accepted['ingest_id']}?wait_seconds=2"
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.client.get, path, headers={'Authorization': 'Bearer a'})
            PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE).run_once()
            result = pending.result(timeout=5)
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.json()['status'], 'completed')
        self.assertEqual(len(self.a.activity()), 1)

    def test_result_exposes_failure_details_and_rejects_unbounded_wait(self):
        accepted = self.accept()
        lease = self.queue.claim()
        self.queue.fail(lease, ValueError('invalid extraction'), stage='extracting')
        path = f"/api/v1/ingests/{accepted['ingest_id']}"
        result = self.client.get(path, headers={'Authorization': 'Bearer a'}).json()
        self.assertIn(result['status'], {'failed', 'retry_scheduled'})
        self.assertTrue(result['error_message'])
        self.assertEqual(result['saved_entries'], [])
        self.assertEqual(self.client.get(path + '?wait_seconds=26', headers={'Authorization': 'Bearer a'}).status_code, 422)
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE users SET status='deleting' WHERE id='a'")
        self.assertEqual(self.client.get(path, headers={'Authorization': 'Bearer a'}).status_code, 401)

    def test_share_request_waits_for_actual_result_without_duplicate_acceptance(self):
        accepted = self.accept()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self.post, '/ingests?wait_seconds=2',
                                  source_url='https://youtu.be/post', request_key='first')
            PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE).run_once()
            result = pending.result(timeout=5)
        self.assertEqual(result.status_code, 200, result.text)
        self.assertEqual(result.json()['ingest_id'], accepted['ingest_id'])
        self.assertEqual(result.json()['status'], 'completed')
        self.assertEqual(result.json()['saved_entries'][0]['name'], 'Cafe')
        self.assertEqual(len(self.a.activity()), 1)

    def test_waiting_share_reports_processing_error_and_preserves_queued_work_on_timeout(self):
        result = self.post('/ingests?wait_seconds=1', source_url='https://youtu.be/post', request_key='first')
        self.assertEqual(result.status_code, 202)
        self.assertEqual(result.json()['status'], 'queued')
        lease = self.queue.claim()
        self.queue.fail(lease, ValueError('bad result'), stage='saving')
        failure = self.post('/ingests?wait_seconds=1', source_url='https://youtu.be/post', request_key='first')
        self.assertEqual(failure.status_code, 200)
        self.assertEqual(failure.json()['status'], 'failed')
        self.assertEqual(failure.json()['failure_kind'], 'save_failed')
        self.assertTrue(failure.json()['error_message'])
        self.assertEqual(len(self.a.activity()), 1)
        self.assertEqual(self.post('/ingests?wait_seconds=151', source_url='https://youtu.be/post', request_key='other').status_code, 422)

    def test_waiting_share_does_not_report_scheduled_retry_as_a_final_outcome(self):
        self.accept()
        lease = self.queue.claim()
        self.queue.fail(lease, requests.Timeout('temporary'), stage='fetching')
        pending = self.post('/ingests?wait_seconds=1', source_url='https://youtu.be/post', request_key='first')
        self.assertEqual(pending.status_code, 202)
        self.assertEqual(pending.json()['status'], 'retry_scheduled')
        self.assertEqual(pending.json()['saved_entries'], [])

    def test_acceptance_is_durable_and_processing_occurs_outside_request(self):
        with patch('ingest_service.IngestService.ingest', side_effect=AssertionError('legacy called')):
            accepted = self.accept()
        self.assertEqual(accepted['status'], 'queued')
        self.assertEqual(self.a.entries(), [])
        self.assertEqual(self.a.activity()[0]['id'], accepted['ingest_id'])
        self.assertEqual(self.queue.deliver_ready(), 0)
        # A newly constructed worker discovers the request after restart.
        worker = PostProcessingWorker(PostProcessingStore(self.db, 'v1'), self.root, lambda *_: BASELINE)
        worker.run_once()
        self.assertEqual(len(self.a.entries()), 1)
        self.assertEqual(self.b.entries(), [])

    def legacy_failure(self):
        with sqlite3.connect(self.db) as con:
            return con.execute('''INSERT INTO ingest_runs (user_id, source_url, source_platform,
                status, stage, idempotency_key, intent, failure_kind, retryable)
                VALUES ('a', 'https://youtu.be/post', 'youtube', 'failed', 'extracting',
                    'legacy-failure', 'legacy', 'analysis_failed', 1)''').lastrowid

    def test_legacy_failure_retries_same_activity_without_exposing_other_accounts(self):
        ingest_id = self.legacy_failure()
        self.assertEqual(self.post(f'/activity/{ingest_id}/retry', user='b').status_code, 404)
        response = self.post(f'/activity/{ingest_id}/retry')
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()['ingest_id'], ingest_id)
        PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE).run_once()
        self.assertEqual(self.a.activity()[0]['status'], 'completed')
        self.assertEqual(len(self.a.entries()), 1)
        self.assertEqual(self.b.entries(), [])
        self.assertEqual(len(self.a.activity()), 1)

    def test_legacy_retry_does_not_restore_removed_mentions(self):
        self.accept()
        worker = PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE)
        worker.run_once()
        self.a.delete_entries([self.a.entries()[0]['id']])
        ingest_id = self.legacy_failure()
        self.assertEqual(self.post(f'/activity/{ingest_id}/retry').status_code, 202)
        worker.run_once()
        self.assertEqual(self.a.entries(), [])
        self.assertEqual(len(self.a.sources()), 1)

    def test_legacy_retry_is_durable_and_duplicate_retry_reuses_capture(self):
        ingest_id = self.legacy_failure()
        first = self.post(f'/activity/{ingest_id}/retry').json()
        second = self.post(f'/activity/{ingest_id}/retry').json()
        self.assertEqual(first, second)
        self.assertEqual(len(self.a.sources()), 1)
        self.assertEqual(len(self.a.activity()), 1)

    def test_legacy_short_link_resolution_checks_owner_and_preserves_failure_on_outage(self):
        ingest_id = self.legacy_failure()
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE ingest_runs SET source_url = 'https://www.tiktok.com/t/short/' WHERE id = ?", (ingest_id,))
        with patch('capture_store.resolve_public_url', side_effect=SourceResolutionUnavailable()) as resolve:
            self.assertEqual(self.post(f'/activity/{ingest_id}/retry', user='b').status_code, 404)
            resolve.assert_not_called()
            self.assertEqual(self.post(f'/activity/{ingest_id}/retry').status_code, 503)
        self.assertEqual(self.a.activity()[0]['status'], 'failed')
        self.assertEqual(self.a.sources(), [])
        with patch('capture_store.resolve_public_url', return_value='https://www.tiktok.com/@creator/video/123456'):
            self.assertEqual(self.post(f'/activity/{ingest_id}/retry').status_code, 202)
        self.assertEqual(self.a.sources()[0]['source_url'], 'https://www.tiktok.com/@_/video/123456')

    def test_replay_returns_same_operation_and_new_share_restores_only_deliberately(self):
        first = self.accept()
        worker = PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE)
        worker.run_once()
        self.a.delete_entries([self.a.entries()[0]['id']])
        replay = self.accept(url='https://www.youtube.com/shorts/post?tracking=yes')
        self.assertEqual(replay['ingest_id'], first['ingest_id'])
        self.assertEqual(replay['accepted_sequence'], first['accepted_sequence'])
        worker.run_once()
        self.assertEqual(self.a.entries(), [])
        self.accept('deliberate-share')
        worker.run_once()
        self.assertEqual(len(self.a.entries()), 1)

    def test_two_users_same_key_have_separate_operations_and_private_results(self):
        a = self.accept()
        b = self.accept(user='b')
        self.assertNotEqual(a['ingest_id'], b['ingest_id'])
        calls = []
        def process(*args):
            calls.append(args[0])
            return BASELINE
        PostProcessingWorker(self.queue, self.root, process).run_once()
        self.assertEqual(len(calls), 1)
        self.assertNotEqual(self.a.entries()[0]['id'], self.b.entries()[0]['id'])

    def test_concurrent_network_replays_create_one_run(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            rows = list(pool.map(lambda _: self.accept(), range(8)))
        self.assertEqual(len({row['ingest_id'] for row in rows}), 1)
        self.assertEqual(len(self.a.activity()), 1)

    def test_key_reuse_for_different_post_is_conflict(self):
        self.accept()
        response = self.post(source_url='https://youtu.be/other', request_key='first')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(len(self.a.sources()), 1)

    def test_invalid_credentials_never_trigger_resolution(self):
        with patch('account_app.resolve_public_url', side_effect=AssertionError('network before auth')):
            self.assertEqual(self.post(user='invalid', source_url='https://vm.tiktok.com/short/', request_key='x').status_code, 401)

    def test_owner_injection_and_missing_or_invalid_keys_fail(self):
        for extra in ({'user_id': 'b', 'request_key': 'x'}, {}, {'request_key': 'bad key'}, {'request_key': 'x' * 129}):
            with self.subTest(extra=extra):
                self.assertEqual(self.post(source_url='https://youtu.be/post', **extra).status_code, 422)
        self.assertEqual(self.a.activity(), [])

    def test_shortcut_uses_same_durable_acceptance_and_records_channel(self):
        url = 'https://youtu.be/post'
        body = {'source_url_base64': base64.b64encode(url.encode()).decode(), 'request_key': 'shortcut'}
        response = self.post('/shortcut/ingests', **body)
        self.assertEqual(response.status_code, 202)
        with sqlite3.connect(self.db) as con:
            self.assertEqual(con.execute('SELECT capture_channel FROM captures').fetchone()[0], 'shortcut')
        self.assertEqual(self.post('/shortcut/ingests', **body).json(), response.json())
        for value in ('not base64!', '🚫🚫🚫🚫', base64.b64encode(b'\xff\xff\xff').decode()):
            self.assertEqual(self.post('/shortcut/ingests', source_url_base64=value, request_key='bad').status_code, 422)

    def test_resolution_errors_do_not_accept_a_partial_request_or_leak_details(self):
        with patch('account_app.resolve_public_url', side_effect=SourceResolutionUnavailable('private token')):
            response = self.post(source_url='https://vm.tiktok.com/short/', request_key='x')
        self.assertEqual(response.status_code, 503)
        self.assertNotIn('private token', response.text)
        self.assertEqual(self.a.activity(), [])

    def test_manual_retry_bypasses_backoff_without_a_new_operation_or_sequence(self):
        accepted = self.accept()
        lease = self.queue.claim()
        self.queue.fail(lease, requests.Timeout(), stage='fetching')
        self.assertIsNone(self.queue.claim())
        response = self.post(f"/activity/{accepted['ingest_id']}/retry")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()['accepted_sequence'], accepted['accepted_sequence'])
        self.assertEqual(len(self.a.activity()), 1)
        lease = self.queue.claim()
        self.assertIsNotNone(lease)
        self.assertEqual(lease.attempt, 1)
        # A replay while processing must not steal the live lease.
        self.assertEqual(self.post(f"/activity/{accepted['ingest_id']}/retry").json()['status'], 'processing')
        self.queue.publish(lease, BASELINE)
        self.queue.deliver_ready()
        self.assertEqual(self.post(f"/activity/{accepted['ingest_id']}/retry").status_code, 409)

    def test_retry_cannot_access_foreign_cancelled_or_missing_run(self):
        accepted = self.accept()
        path = f"/activity/{accepted['ingest_id']}/retry"
        self.assertEqual(self.post(path, user='b').status_code, 404)
        self.assertEqual(self.post('/activity/999999/retry').status_code, 404)
        lease = self.queue.claim()
        self.queue.fail(lease, requests.Timeout(), stage='fetching')
        self.a.delete_failed_activity(accepted['ingest_id'])
        self.assertEqual(self.post(path).status_code, 404)
        self.assertEqual(self.accept()['status'], 'cancelled')

    def test_retry_of_failed_reshare_does_not_restore_a_later_deletion(self):
        self.accept()
        worker = PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE)
        worker.run_once()
        reshare = self.accept('reshare')
        with sqlite3.connect(self.db) as con:
            con.execute("UPDATE ingest_runs SET status = 'failed' WHERE id = ?", (reshare['ingest_id'],))
        self.a.delete_entries([self.a.entries()[0]['id']])
        response = self.post(f"/activity/{reshare['ingest_id']}/retry")
        self.assertEqual(response.status_code, 202)
        worker.run_once()
        self.assertEqual(self.a.entries(), [])

    def test_lifespan_runs_and_drains_explicit_worker(self):
        started, finished = Event(), Event()
        worker = PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE)
        def run(stop):
            started.set()
            stop.wait(5)
            finished.set()
        with patch.object(worker, 'run', side_effect=run):
            with TestClient(create_account_app(db_path=self.db, verify_session=self.verify, worker=worker)):
                self.assertTrue(started.wait(2))
                self.assertFalse(finished.is_set())
            self.assertTrue(finished.is_set())

    def test_worker_rejects_mismatched_or_unmigrated_database(self):
        worker = PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE)
        with self.assertRaises(ValueError):
            create_account_app(db_path=self.legacy, verify_session=self.verify, worker=worker)
        worker = PostProcessingWorker(PostProcessingStore(self.legacy, 'v1'), self.root, lambda *_: BASELINE)
        with self.assertRaisesRegex(RuntimeError, 'explicitly migrated'):
            with TestClient(create_account_app(db_path=self.legacy, verify_session=self.verify, worker=worker)):
                pass


class PublicURLResolutionTests(unittest.TestCase):
    def test_canonical_supported_urls_do_not_use_network(self):
        with patch('account_ingest.requests.Session', side_effect=AssertionError('network')):
            self.assertEqual(resolve_public_url('https://youtu.be/abc?tracking=yes'), 'https://www.youtube.com/watch?v=abc')

    def test_bad_urls_are_rejected_before_network(self):
        with patch('account_ingest.requests.Session', side_effect=AssertionError('network')):
            for url in ('file:///etc/passwd', 'https://localhost/video/123', 'https://user:pass@vm.tiktok.com/x',
                        'https://youtu.be:8888/abc', 'https://youtube.com.evil.test/watch?v=abc',
                        'https://vm.tiktok.com\\@localhost/x', 'https://youtu.be/ab\nc'):
                with self.subTest(url=url), self.assertRaises(ValueError):
                    resolve_public_url(url)

    def session(self, locations):
        session = MagicMock()
        session.__enter__.return_value = session
        responses = []
        for location in locations:
            response = MagicMock(status_code=302, headers={'Location': location})
            response.__enter__.return_value = response
            responses.append(response)
        session.get.side_effect = responses
        return session

    def test_short_link_resolves_to_canonical_video(self):
        session = self.session(['https://www.tiktok.com/@name/video/123?tracking=yes'])
        with patch('account_ingest.requests.Session', return_value=session):
            self.assertEqual(resolve_public_url('https://vm.tiktok.com/short/'), 'https://www.tiktok.com/@_/video/123')
        self.assertFalse(session.trust_env)
        self.assertFalse(session.get.call_args.kwargs['allow_redirects'])

    def test_redirect_to_private_or_unrelated_host_is_never_requested(self):
        for destination in ('http://127.0.0.1/secret', 'https://evil.test/', 'https://user:secret@www.tiktok.com/x'):
            session = self.session([destination])
            with patch('account_ingest.requests.Session', return_value=session), self.assertRaises(ValueError):
                resolve_public_url('https://vm.tiktok.com/short/')
            self.assertEqual(session.get.call_count, 1)

    def test_redirect_loop_has_a_bound(self):
        session = self.session(['https://vm.tiktok.com/short/'] * 5)
        with patch('account_ingest.requests.Session', return_value=session), self.assertRaises(SourceResolutionUnavailable):
            resolve_public_url('https://vm.tiktok.com/short/')
        self.assertEqual(session.get.call_count, 5)
