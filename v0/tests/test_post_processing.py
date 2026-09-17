from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from unittest.mock import Mock

import requests
import store
from account_movie_enrichment import AccountMovieEnricher
from capture_store import CaptureStore
from multi_user_migration import prepare_copy
from post_processing_store import LeaseLost, PostProcessingStore
from post_processing_worker import PostProcessingWorker
from public_processing_adapter import adapt_public_result, PlaceLookup, process_public_post


BASELINE = {'mentions': [{'key': 'one', 'extracted': {'extracted_name': 'Cafe',
              'type_name': 'Restaurant', 'description': 'Original'},
              'status': 'resolved', 'place_id': 'place'}]}


class PostProcessingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.db = self.root / 'accounts.sqlite'
        legacy = self.root / 'legacy.sqlite'
        store.init_db(legacy)
        prepare_copy(legacy, self.db, owner_id='a', owner_name='A')
        with self.connection() as con:
            con.execute("INSERT INTO users (id, display_name) VALUES ('b', 'B')")
        self.a = CaptureStore(self.db, 'a')
        self.b = CaptureStore(self.db, 'b')
        self.time = datetime(2026, 9, 15, tzinfo=timezone.utc)
        self.queue = PostProcessingStore(self.db, 'v1', now=lambda: self.time)

    def connection(self):
        con = sqlite3.connect(self.db)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys = ON')
        self.addCleanup(con.close)
        return con

    def accept(self, user=None, post='post', key='request'):
        return (user or self.a).accept_public('https://youtu.be/' + post, key)

    def rows(self, table):
        return [dict(r) for r in self.connection().execute('SELECT * FROM ' + table + ' ORDER BY id')]

    def test_two_users_share_one_processing_call_and_get_private_copies(self):
        self.accept(); self.accept(self.b)
        calls = []
        def process(url, path, progress):
            calls.append(url)
            self.assertTrue(path.is_dir())
            (path / 'temporary-video.mp4').write_bytes(b'test')
            progress('extracting')
            return BASELINE
        worker = PostProcessingWorker(self.queue, self.root, process)
        self.assertTrue(worker.run_once())
        self.assertFalse(worker.run_once())
        self.assertEqual(calls, ['https://www.youtube.com/watch?v=post'])
        self.assertEqual(len(self.rows('post_processing_cache')), 1)
        self.assertEqual(len(self.a.entries()), 1)
        self.assertEqual(len(self.b.entries()), 1)
        self.assertNotEqual(self.a.entries()[0]['id'], self.b.entries()[0]['id'])
        self.assertEqual(list(worker.workdir.iterdir()), [])

    def seed_movie(self, account=None):
        account = account or self.a
        accepted = self.accept(account)
        baseline = {'mentions': [{'key': 'movie', 'extracted': {
            'extracted_name': 'Arrival', 'type_name': 'Movie', 'description': 'A linguist'},
            'status': 'not_applicable'}]}
        PostProcessingWorker(self.queue, self.root, lambda *_: baseline).run_once()
        return account.entries()[0]['id']

    def test_movie_enrichment_restores_links_and_stays_private(self):
        a_id = self.seed_movie()
        b_id = self.seed_movie(self.b)
        provider = Mock(name='provider')
        provider.lookup.return_value = {'provider': 'wikidata', 'provider_id': 'Q123',
            'resolved_title': 'Arrival', 'release_year': 2016, 'match_status': 'matched',
            'letterboxd_url': 'https://letterboxd.com/imdb/tt2543164/'}
        enrichment = AccountMovieEnricher(self.queue, provider)
        self.assertTrue(enrichment.run_once())
        self.assertTrue(enrichment.run_once())
        self.assertFalse(enrichment.run_once())
        self.assertEqual(provider.lookup.call_count, 2)
        rows = self.connection().execute('SELECT entry_id, user_id FROM movie_enrichments').fetchall()
        self.assertEqual({tuple(r) for r in rows}, {(a_id, 'a'), (b_id, 'b')})
        self.assertEqual(self.a.entries()[0]['movie_enrichment']['release_year'], 2016)
        self.assertNotIn('letterboxd', self.rows('post_processing_cache')[0]['result_json'])

    def test_movie_lookup_failure_preserves_saved_recommendation(self):
        entry_id = self.seed_movie()
        provider = Mock()
        provider.name = 'wikidata'
        provider.lookup.side_effect = RuntimeError('private provider response')
        enrichment = AccountMovieEnricher(self.queue, provider)
        self.assertTrue(enrichment.run_once())
        self.assertFalse(enrichment.run_once())
        self.assertEqual(self.a.entries()[0]['id'], entry_id)
        self.assertEqual(self.a.entries()[0]['movie_enrichment']['match_status'], 'error')
        self.assertNotIn('private provider response', '\n'.join(self.connection().iterdump()))

    def test_movie_enrichment_does_not_backfill_untouched_historical_library(self):
        # A pending historical capture is sufficient to check selection; no
        # baseline mutation or migration of the original library is needed.
        accepted = self.a.accept_public('https://youtu.be/old-film', 'old-film')
        with self.connection() as con:
            con.execute("UPDATE captures SET capture_channel = 'legacy' WHERE id = ?", (accepted['item_id'],))
        baseline = {'mentions': [{'key': 'film', 'extracted': {
            'extracted_name': 'Arrival', 'type_name': 'Movie'}, 'status': 'not_applicable'}]}
        PostProcessingWorker(self.queue, self.root, lambda *_: baseline).run_once()
        provider = Mock()
        self.assertFalse(AccountMovieEnricher(self.queue, provider).run_once())
        provider.lookup.assert_not_called()

    def test_movie_lookup_cannot_recreate_deleted_recommendation(self):
        entry_id = self.seed_movie()
        provider = Mock()
        def lookup(*_):
            self.a.delete_entries([entry_id])
            return {'provider': 'wikidata', 'match_status': 'unmatched'}
        provider.lookup.side_effect = lookup
        self.assertTrue(AccountMovieEnricher(self.queue, provider).run_once())
        self.assertEqual(self.a.entries(), [])
        self.assertEqual(self.connection().execute('SELECT COUNT(*) FROM movie_enrichments').fetchone()[0], 0)

    def test_movie_lookup_cannot_recreate_deleted_account(self):
        self.seed_movie()
        provider = Mock()
        def lookup(*_):
            with self.connection() as con:
                con.execute("DELETE FROM users WHERE id = 'a'")
            return {'provider': 'wikidata', 'match_status': 'unmatched'}
        provider.lookup.side_effect = lookup
        self.assertTrue(AccountMovieEnricher(self.queue, provider).run_once())
        self.assertEqual(self.connection().execute('SELECT COUNT(*) FROM movie_enrichments').fetchone()[0], 0)

    def test_concurrent_claims_have_one_winner_and_global_capacity(self):
        self.accept(); self.accept(self.b, post='other')
        with ThreadPoolExecutor(max_workers=2) as pool:
            leases = list(pool.map(lambda _: self.queue.claim(), range(2)))
        claimed = [r for r in leases if r]
        self.assertEqual(len(claimed), 1)
        self.queue.publish(claimed[0], BASELINE)
        self.assertIsNotNone(self.queue.claim())

    def test_expired_worker_cannot_renew_publish_fail_or_report(self):
        self.accept()
        old = self.queue.claim()
        self.time += timedelta(seconds=121)
        replacement = self.queue.claim()
        self.assertEqual(old.id, replacement.id)
        self.assertNotEqual(old.token, replacement.token)
        for operation in (lambda: self.queue.renew(old), lambda: self.queue.publish(old, BASELINE),
                          lambda: self.queue.fail(old, RuntimeError('private error'), stage='fetching'),
                          lambda: self.queue.progress(old, 'extracting')):
            with self.assertRaises(LeaseLost):
                operation()
        self.queue.publish(replacement, BASELINE)
        self.queue.deliver_ready()
        self.assertEqual(len(self.a.entries()), 1)

    def test_renewal_prevents_early_takeover(self):
        self.accept()
        lease = self.queue.claim()
        self.time += timedelta(seconds=100)
        self.queue.renew(lease)
        self.time += timedelta(seconds=100)
        self.assertIsNone(self.queue.claim())
        self.queue.publish(lease, BASELINE)

    def test_publication_survives_restart_before_delivery_and_late_subscriber(self):
        self.accept()
        lease = self.queue.claim()
        self.queue.publish(lease, BASELINE)
        self.accept(self.b)
        replacement = PostProcessingStore(self.db, 'v1', now=lambda: self.time)
        self.assertEqual(replacement.deliver_ready(), 2)
        self.assertIsNone(replacement.claim())
        self.assertEqual(len(self.rows('recommendation_mentions')), 2)

    def test_cache_hit_does_not_redownload_and_reshare_obeys_deletion_order(self):
        req = self.accept()
        lease = self.queue.claim(); self.queue.publish(lease, BASELINE); self.queue.deliver_ready()
        older = self.accept(key='before-delete')
        self.a.delete_entries([self.a.entries()[0]['id']])
        worker = PostProcessingWorker(self.queue, self.root, lambda *_: self.fail('Cache hit must not process'))
        worker.run_once()
        self.assertEqual(self.a.entries(), [])
        self.accept(key='after-delete')
        worker.run_once()
        self.assertEqual(len(self.a.entries()), 1)
        self.assertEqual(len(self.rows('captures')), 1)
        self.assertEqual(len(self.rows('post_processing_cache')), 1)

    def test_deleting_account_during_work_does_not_cancel_other_user_or_resurrect(self):
        self.accept(); self.accept(self.b)
        lease = self.queue.claim()
        with self.connection() as con:
            con.execute("DELETE FROM users WHERE id = 'a'")
        self.queue.progress(lease, 'extracting')
        self.queue.publish(lease, BASELINE)
        self.assertEqual(self.queue.deliver_ready(), 1)
        self.assertEqual({r['user_id'] for r in self.rows('recommendation_mentions')}, {'b'})

    def test_disabled_user_and_private_input_never_enter_shared_jobs(self):
        self.accept()
        self.b.accept_direct('My private restaurant', 'private', channel='siri')
        with self.connection() as con:
            con.execute("UPDATE users SET status = 'disabled' WHERE id = 'a'")
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.rows('post_processing_cache'), [])

    def test_transient_failure_backoff_is_durable_and_does_not_leak_exception(self):
        self.accept(); self.accept(self.b)
        lease = self.queue.claim()
        self.queue.fail(lease, requests.Timeout('secret-token private-url'), stage='fetching')
        self.assertEqual({r['status'] for r in self.rows('ingest_runs')}, {'retry_scheduled'})
        self.assertNotIn('secret-token', '\n'.join(self.connection().iterdump()))
        self.assertIsNone(self.queue.claim())
        self.time += timedelta(seconds=30)
        retry = self.queue.claim()
        self.assertEqual(retry.attempt, 2)
        self.queue.publish(retry, BASELINE)
        self.assertEqual(self.queue.deliver_ready(), 2)

    def test_retry_after_and_max_attempts_and_new_deliberate_share(self):
        self.accept()
        for i in range(4):
            lease = self.queue.claim()
            error = requests.Timeout('opaque')
            error.retry_after = 100
            self.queue.fail(lease, error, stage='fetching')
            self.time += timedelta(seconds=99)
            self.assertIsNone(self.queue.claim())
            self.time += timedelta(seconds=1)
        self.assertEqual(self.rows('post_processing_cache')[0]['status'], 'failed')
        self.assertEqual(self.rows('ingest_runs')[0]['status'], 'failed')
        self.assertIsNone(self.queue.claim())
        self.accept(key='new-user-share')
        fresh = self.queue.claim()
        self.assertEqual(fresh.attempt, 1)
        self.queue.publish(fresh, BASELINE)
        self.assertEqual(self.queue.deliver_ready(), 1)
        self.assertEqual(self.rows('ingest_runs')[0]['status'], 'failed')

    def test_cancelled_subscriber_remains_hidden_across_shared_retry(self):
        req = self.accept(); self.accept(self.b)
        lease = self.queue.claim()
        self.queue.fail(lease, requests.Timeout(), stage='fetching')
        self.a.delete_failed_activity(req['ingest_id'])
        self.time += timedelta(seconds=31)
        retry = self.queue.claim(); self.queue.publish(retry, BASELINE)
        self.assertEqual(self.queue.deliver_ready(), 1)
        self.assertEqual(self.a.activity(), [])
        self.assertEqual(self.a.entries(), [])

    def test_repeated_crashes_eventually_fail_instead_of_retrying_forever(self):
        self.accept()
        for _ in range(4):
            self.assertIsNotNone(self.queue.claim())
            self.time += timedelta(seconds=121)
        self.assertIsNone(self.queue.claim())
        self.assertEqual(self.rows('ingest_runs')[0]['status'], 'failed')
        self.assertEqual(self.rows('post_processing_cache')[0]['lease_token'], None)

    def test_media_cleanup_on_failure_and_abandoned_job_recovery(self):
        self.accept()
        def failure(url, directory, progress):
            (directory / 'media').write_bytes(b'test')
            raise ValueError('bad result')
        worker = PostProcessingWorker(self.queue, self.root, failure)
        worker.run_once()
        self.assertEqual(list(worker.workdir.iterdir()), [])
        self.accept(key='retry')
        old = self.queue.claim()
        path = worker.workdir / ('job-' + old.token)
        path.mkdir(); (path / 'media').write_bytes(b'test')
        keep = worker.workdir / 'unrelated'; keep.mkdir()
        self.assertEqual(worker.clean_abandoned_media(), 0)
        self.time += timedelta(seconds=121)
        self.assertEqual(worker.clean_abandoned_media(), 1)
        self.assertTrue(keep.exists())

    def test_cleanup_does_not_remove_a_job_started_during_scan(self):
        self.accept()
        worker = PostProcessingWorker(self.queue, self.root, lambda *_: BASELINE)
        worker.workdir.mkdir()
        original = self.queue.active_tokens
        def new_claim():
            lease = self.queue.claim()
            (worker.workdir / ('job-' + lease.token)).mkdir()
            return original()
        with patch.object(PostProcessingStore, 'active_tokens', lambda _: new_claim()):
            self.assertEqual(worker.clean_abandoned_media(), 0)
        self.assertEqual(len(list(worker.workdir.iterdir())), 1)

    def test_invalid_result_is_never_published_or_saved(self):
        self.accept()
        worker = PostProcessingWorker(self.queue, self.root, lambda *_: {'mentions': [], 'media': 'secret'})
        worker.run_once()
        self.assertEqual(self.rows('post_processing_cache')[0]['status'], 'failed')
        self.assertIsNone(self.rows('post_processing_cache')[0]['result_json'])
        self.assertEqual(self.a.entries(), [])

    def test_private_corruption_does_not_block_another_users_delivery(self):
        self.accept(); self.accept(self.b)
        lease = self.queue.claim(); self.queue.publish(lease, BASELINE)
        with self.connection() as con:
            rec = con.execute("INSERT INTO recommendations (user_id, name, normalized_name, entry_type, type_key, identity_key) VALUES ('a','Bad','bad','Restaurant','restaurant','bad')").lastrowid
            capture = con.execute("SELECT id FROM captures WHERE user_id = 'a'").fetchone()[0]
            con.execute("INSERT INTO recommendation_mentions (user_id, entry_id, item_id, ordinal, source_name, source_type, resolution_status, output_key) VALUES ('a',?,?,0,'Bad','Restaurant','unresolved','bad')", (rec, capture))
        self.assertEqual(self.queue.deliver_ready(), 1)
        self.assertEqual(self.rows('ingest_runs')[0]['status'], 'failed')
        self.assertEqual(len(self.b.entries()), 1)

    def test_pinned_old_version_is_used_without_reprocessing(self):
        self.accept()
        lease = self.queue.claim(); self.queue.publish(lease, BASELINE); self.queue.deliver_ready()
        self.a.delete_entries([self.a.entries()[0]['id']])
        self.accept(key='again')
        new = PostProcessingStore(self.db, 'v2', now=lambda: self.time)
        self.assertEqual(new.deliver_ready(), 1)
        self.assertIsNone(new.claim())
        self.assertEqual(len(self.rows('post_processing_cache')), 1)

    def test_worker_refuses_legacy_database(self):
        legacy = self.root / 'legacy.sqlite'
        with self.assertRaises(RuntimeError):
            PostProcessingStore(legacy, 'v1').claim()

    def test_adapter_keeps_original_evidence_and_only_allowlisted_place_data(self):
        extracted = {'extracted_name': 'Real Cafe', 'type_name': 'Restaurant', 'description': 'Source evidence',
                     'location_query': 'Real Cafe NYC', 'location_hints': {'city': 'NYC'},
                     'timestamp_seconds': 10, 'extraction_confidence': 'high'}
        place = {'id': 'place', 'displayName': {'text': 'Provider Name'},
                 'location': {'latitude': 40.0, 'longitude': -74.0}, 'formattedAddress': 'Provider Address',
                 'photos': [{'secret': 'provider-media'}], 'raw_provider_field': 'not retained'}
        value = {'metadata': {'uploader': 'Creator', 'source_content': {'summary': 'Summary', 'transcript': 'large raw text'},
                             'native_location': {'name': 'Real Cafe', 'address': '1 Main St',
                                                 'city': 'New York', 'latitude': 40.1, 'longitude': -73.9,
                                                 'provider_id': 'not retained'},
                             'raw_media': 'not retained'},
                 'resolved_entries': [{'extracted': extracted, 'status': 'auto', 'place': place,
                                       'location_query_used': 'Real Cafe NYC',
                                       'resolution_code': 'single_candidate_match'}]}
        adapted = adapt_public_result(value)
        self.assertEqual(adapted.original.mentions[0].place_id, 'place')
        self.assertEqual(adapted.original.mentions[0].key, 'output-0000')
        self.assertEqual(adapted.original.mentions[0].location_query_used, 'Real Cafe NYC')
        self.assertEqual(adapted.original.mentions[0].resolution_code, 'single_candidate_match')
        self.assertEqual(adapted.original.metadata.native_location.name, 'Real Cafe')
        self.assertEqual(adapted.original.metadata.native_location.address, '1 Main St')
        self.accept()
        worker = PostProcessingWorker(self.queue, self.root, lambda *_: adapted)
        worker.run_once()
        self.assertEqual(self.a.entries()[0]['latitude'], 40.0)
        self.assertEqual(self.a.entries()[0]['description'], 'Source evidence')
        cache = self.rows('post_processing_cache')[0]['result_json']
        self.assertNotIn('Provider Address', cache)
        self.assertNotIn('provider_id', cache)
        self.assertNotIn('latitude', cache)
        self.assertNotIn('provider-media', '\n'.join(self.connection().iterdump()))
        self.assertNotIn('not retained', '\n'.join(self.connection().iterdump()))

    def test_adapter_review_candidates_and_non_location_outputs(self):
        value = {'resolved_entries': [
            {'extracted': {'extracted_name': 'Book', 'type_name': 'Book', 'location_query': ''}, 'status': 'not_applicable'},
            {'extracted': {'extracted_name': 'Cafe', 'type_name': 'Restaurant'}, 'status': 'needs_review',
             'candidates': [{'id': 'one'}, {'id': 'two'}, {'id': 'one'}]}]}
        adapted = adapt_public_result(value)
        self.assertIsNone(adapted.original.mentions[0].extracted.location_query)
        self.assertEqual(adapted.original.mentions[1].candidate_ids, ['one', 'two'])
        self.assertEqual(len(adapted.locations), 2)

    def test_adapter_rejects_failed_or_missing_outputs_but_accepts_explicit_empty_success(self):
        for invalid in ({}, {'metadata': {'extraction_status': 'failed'}, 'resolved_entries': []}):
            with self.assertRaises(ValueError):
                adapt_public_result(invalid)
        self.assertEqual(adapt_public_result({'resolved_entries': []}).original.mentions, [])

    def test_stale_publisher_cannot_modify_shared_locations(self):
        self.accept()
        lease = self.queue.claim()
        self.time += timedelta(seconds=121)
        self.queue.claim()
        with self.assertRaises(LeaseLost):
            self.queue.publish(lease, BASELINE, locations=(PlaceLookup(google_place_id='place', lat=40, lng=-74),))
        self.assertEqual(self.rows('locations'), [])

    def test_publication_rejects_unrelated_place_and_rolls_back_write_failure(self):
        self.accept()
        lease = self.queue.claim()
        with self.assertRaises(ValueError):
            self.queue.publish(lease, BASELINE, locations=(PlaceLookup(google_place_id='unrelated'),))
        with self.connection() as con:
            con.execute("CREATE TRIGGER fail_publication BEFORE UPDATE ON post_processing_cache WHEN NEW.status = 'completed' BEGIN SELECT RAISE(ABORT, 'injected'); END")
        with self.assertRaises(sqlite3.IntegrityError):
            self.queue.publish(lease, BASELINE, locations=(PlaceLookup(google_place_id='place'),))
        self.assertEqual(self.rows('locations'), [])
        self.assertIsNone(self.rows('post_processing_cache')[0]['result_json'])

    def test_live_adapter_calls_pipeline_with_public_inputs_only(self):
        with patch('pipeline.process_ingest', return_value={'resolved_entries': []}) as process:
            report = lambda _: None
            result = process_public_post('https://www.youtube.com/watch?v=post', self.root, report)
        process.assert_called_once_with('https://www.youtube.com/watch?v=post', self.root, progress=report)
        self.assertEqual(result.original.mentions, [])

    def test_available_result_delivers_retry_scheduled_subscriber_without_waiting(self):
        self.accept()
        lease = self.queue.claim(); self.queue.publish(lease, BASELINE)
        with self.connection() as con:
            con.execute("UPDATE ingest_runs SET status = 'retry_scheduled', next_retry_at = '2099-01-01' WHERE user_id = 'a'")
        self.assertEqual(self.queue.deliver_ready(), 1)
        self.assertEqual(self.rows('ingest_runs')[0]['status'], 'completed')

    def test_account_deleted_between_delivery_selection_and_save_is_not_recreated(self):
        self.accept(); self.accept(self.b)
        lease = self.queue.claim(); self.queue.publish(lease, BASELINE)
        real = CaptureStore.materialize
        def delete_then_save(account, *args, **kwargs):
            if account.user_id == 'a':
                with self.connection() as con:
                    con.execute("DELETE FROM users WHERE id = 'a'")
            return real(account, *args, **kwargs)
        with patch.object(CaptureStore, 'materialize', delete_then_save):
            self.assertEqual(self.queue.deliver_ready(), 1)
        self.assertEqual({r['user_id'] for r in self.rows('recommendation_mentions')}, {'b'})


if __name__ == '__main__':
    unittest.main()
