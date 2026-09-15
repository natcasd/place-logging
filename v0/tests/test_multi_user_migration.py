from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import multi_user_migration as migration
import store


class MultiUserMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.source = Path(self.directory.name) / "original.db"
        self.output = Path(self.directory.name) / "multi-user.db"
        store.init_db(self.source)
        self.capture_a = self.capture("https://www.instagram.com/reel/A/", "place-1")
        self.capture_b = self.capture("https://youtu.be/B", "place-1")
        # Original extraction survives, but its removed recommendation does not.
        self.capture_deleted = self.capture("https://youtu.be/C", "place-deleted")
        with sqlite3.connect(self.source) as con:
            self.entry_id = con.execute("SELECT entry_id FROM recommendation_mentions WHERE item_id = ?", (self.capture_a,)).fetchone()[0]
            con.execute("UPDATE recommendation_mentions SET description = 'My private correction' WHERE item_id = ?", (self.capture_a,))
            deleted = con.execute("SELECT entry_id FROM recommendation_mentions WHERE item_id = ?", (self.capture_deleted,)).fetchone()[0]
        store.delete_entry(self.source, deleted)
        with sqlite3.connect(self.source) as con:
            # Enrichment records are preserved and acquire matching ownership too.
            con.execute("INSERT INTO movie_enrichments (entry_id, provider, match_status) VALUES (?, 'test-provider', 'unmatched')", (self.entry_id,))
            # Exercise a high-water mark above all remaining IDs.
            con.execute("UPDATE sqlite_sequence SET seq = 1000 WHERE name = 'recommendations'")

    def capture(self, url: str, place_id: str) -> int:
        capture_id = store.save_ingest(self.source, {
            "source_url": url,
            "metadata": {"caption_or_description": "Public original caption"},
            "entries_extracted": [{"extracted_name": "Cafe", "type_name": "Restaurant", "description": "Original description"}],
            "resolved_entries": [{
                "extracted": {"extracted_name": "Cafe", "type_name": "Restaurant", "description": "Original description"},
                "status": "resolved", "place": {"id": place_id},
            }],
        })
        run = store.start_ingest_run(self.source, url, "instagram" if "instagram" in url else "youtube")
        with sqlite3.connect(self.source) as con:
            con.execute("UPDATE ingest_runs SET item_id = ?, status = 'completed', stage = 'completed' WHERE id = ?", (capture_id, run))
            con.execute("INSERT INTO ingest_events (ingest_run_id, stage, status, message) VALUES (?, 'completed', 'completed', 'Saved')", (run,))
        return capture_id

    def migrate(self) -> dict:
        return migration.prepare_copy(self.source, self.output, owner_id="nathan", owner_name="Nathan")

    def connection(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.output)
        con.execute("PRAGMA foreign_keys = ON")
        self.addCleanup(con.close)
        return con

    def insert_recommendation(self, con: sqlite3.Connection, user: str, key: str = "a-new-key") -> int:
        return con.execute("""INSERT INTO recommendations
            (user_id, name, normalized_name, entry_type, type_key, identity_key)
            VALUES (?, 'Cafe', 'cafe', 'Restaurant', 'restaurant', ?)""", (user, key)).lastrowid

    def insert_direct_capture(self, con: sqlite3.Connection, user: str) -> int:
        return con.execute("""INSERT INTO captures
            (user_id, vertical, input_kind, capture_channel, input_text,
             private_result_json, materialization_state)
            VALUES (?, 'recommendation', 'direct', 'siri', 'Save this cafe', ?, 'complete')""",
            (user, json.dumps({"mentions": [{"key": "cafe"}]}))).lastrowid

    def assert_rejected(self, con: sqlite3.Connection, sql: str, params: tuple = ()) -> None:
        with self.assertRaises(sqlite3.IntegrityError):
            try:
                con.execute(sql, params)
                con.commit()
            except sqlite3.IntegrityError:
                con.rollback()
                raise

    def test_copy_preserves_every_original_field_relationship_and_deleted_ids(self) -> None:
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        report = self.migrate()
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash)
        self.assertTrue(report["original_data_preserved"])
        self.assertFalse(report["legacy_api_compatible"])
        self.assertEqual(report["shared_cache_rows"], 0)
        self.assertEqual(self.output.stat().st_mode & 0o777, 0o600)
        con = self.connection()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM users").fetchone()[0], 1)
        for table in migration.OWNED_TABLES:
            self.assertEqual(con.execute(f"SELECT DISTINCT user_id FROM {table}").fetchall(), [("nathan",)])
        self.assertEqual(con.execute("SELECT description FROM recommendation_mentions WHERE item_id = ?", (self.capture_a,)).fetchone()[0], "My private correction")
        self.assertEqual(con.execute("SELECT COUNT(*) FROM recommendation_mentions WHERE item_id = ?", (self.capture_deleted,)).fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT DISTINCT materialization_state FROM captures").fetchall(), [("legacy_unverified",)])
        self.assertEqual(con.execute("SELECT COUNT(*) FROM post_processing_cache").fetchone()[0], 0)
        self.assertGreater(self.insert_recommendation(con, "nathan"), 1000)

    def test_reapplying_is_noop_and_different_owner_is_rejected(self) -> None:
        self.migrate()
        con = self.connection()
        before = list(con.iterdump())
        result = migration.migrate_copy(con, owner_id="nathan", owner_name="Nathan")
        self.assertTrue(result["already_migrated"])
        self.assertEqual(before, list(con.iterdump()))
        with self.assertRaisesRegex(ValueError, "owner"):
            migration.migrate_copy(con, owner_id="someone-else", owner_name="Other")

    def test_legacy_store_and_api_initialization_refuse_migrated_file(self) -> None:
        self.migrate()
        with self.assertRaisesRegex(RuntimeError, "legacy unscoped"):
            store.init_db(self.output)
        with self.assertRaisesRegex(RuntimeError, "legacy unscoped"):
            store.list_entries(self.output)
        # The unchanged app still works against the original database.
        self.assertEqual(len(store.list_entries(self.source)), 1)

    def test_legacy_guard_survives_sql_dump_restore_without_header_markers(self) -> None:
        self.migrate()
        with sqlite3.connect(self.output) as con:
            con.execute("PRAGMA application_id = 0")
            con.execute("PRAGMA user_version = 0")
        with self.assertRaisesRegex(RuntimeError, "legacy unscoped"):
            store.list_entries(self.output)

    def test_original_library_views_match_on_isolated_migrated_copy(self) -> None:
        expected = {
            "entries": store.list_entries(self.source),
            "sources": store.list_sources(self.source),
            "activity": store.list_ingest_runs(self.source),
        }
        self.migrate()

        def read_only_copy(path: Path) -> sqlite3.Connection:
            self.assertEqual(path, self.output)
            return sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)

        # Only in this one-owner fixture: replay existing read projections to
        # prove visible library preservation. Never bypass the guard in the app.
        with patch.object(store, "_connect", side_effect=read_only_copy):
            actual = {
                "entries": store.list_entries(self.output),
                "sources": store.list_sources(self.output),
                "activity": store.list_ingest_runs(self.output),
            }
        self.assertEqual(actual, expected)

    def test_no_overwrite_or_in_place_migration(self) -> None:
        with self.assertRaisesRegex(ValueError, "new file"):
            migration.prepare_copy(self.source, self.source, owner_id="nathan", owner_name="Nathan")
        self.output.write_bytes(b"do not overwrite")
        with self.assertRaisesRegex(ValueError, "new file"):
            self.migrate()
        self.assertEqual(self.output.read_bytes(), b"do not overwrite")

    def test_failure_rolls_back_schema_and_never_publishes_output(self) -> None:
        original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()
        with patch.object(migration, "_execute_schema", side_effect=RuntimeError("injected failure")):
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                self.migrate()
        self.assertFalse(self.output.exists())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original_hash)
        self.assertFalse(list(Path(self.directory.name).glob(".multi-user-*")))
        # Exercise rollback on the connection itself, not only deletion of the copy.
        con = sqlite3.connect(self.source)
        self.addCleanup(con.close)
        before = list(con.iterdump())
        with patch.object(migration, "_execute_schema", side_effect=RuntimeError("injected failure")):
            with self.assertRaises(RuntimeError):
                migration.migrate_copy(con, owner_id="nathan", owner_name="Nathan")
        self.assertEqual(before, list(con.iterdump()))

    def test_unrecognized_schema_and_duplicate_sources_require_review(self) -> None:
        with sqlite3.connect(self.source) as con:
            con.execute("ALTER TABLE captures ADD COLUMN unreviewed_private_field TEXT")
        with self.assertRaisesRegex(ValueError, "Unexpected columns"):
            self.migrate()
        self.assertFalse(self.output.exists())

    def test_final_validation_failure_rolls_back_rows_and_version_markers(self) -> None:
        con = sqlite3.connect(self.source)
        self.addCleanup(con.close)
        before = list(con.iterdump())
        with patch.object(migration, "_validate_target", side_effect=ValueError("verification failed")):
            with self.assertRaisesRegex(ValueError, "verification failed"):
                migration.migrate_copy(con, owner_id="nathan", owner_name="Nathan")
        self.assertEqual(list(con.iterdump()), before)
        self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(con.execute("PRAGMA application_id").fetchone()[0], 0)

    def test_equivalent_instagram_forms_preserve_and_report_historical_duplicates(self) -> None:
        duplicate = self.capture("https://www.instagram.com/p/A/?tracking=123", "different-place")
        report = self.migrate()
        self.assertEqual(report["legacy_duplicate_capture_groups"], [[self.capture_a, duplicate]])
        con = self.connection()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM captures WHERE source_platform = 'instagram' AND source_post_id = 'A'").fetchone()[0], 2)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM recommendation_mentions WHERE item_id = ?", (duplicate,)).fetchone()[0], 1)
        con.execute("UPDATE captures SET materialization_state = 'pending' WHERE id = ?", (self.capture_a,))
        con.commit()
        self.assert_rejected(con, "UPDATE captures SET materialization_state = 'pending' WHERE id = ?", (duplicate,))

    def test_invalid_foreign_keys_block_migration(self) -> None:
        with sqlite3.connect(self.source) as con:
            con.execute("UPDATE recommendation_mentions SET entry_id = 99999")
        with self.assertRaisesRegex(ValueError, "Foreign key check"):
            self.migrate()
        self.assertFalse(self.output.exists())

    def test_sqlite_backup_includes_committed_wal_data(self) -> None:
        writer = sqlite3.connect(self.source)
        self.addCleanup(writer.close)
        writer.execute("PRAGMA journal_mode = WAL")
        writer.execute("UPDATE recommendation_mentions SET description = 'Committed in WAL'")
        writer.commit()
        self.migrate()
        self.assertEqual(self.connection().execute("SELECT DISTINCT description FROM recommendation_mentions").fetchall(), [("Committed in WAL",)])

    def test_historically_added_columns_can_have_a_different_order(self) -> None:
        self.source = Path(self.directory.name) / "column-order.db"
        schema = store.RECOMMENDATION_SCHEMA.replace(
            "  source_url      TEXT NOT NULL,\n", "",
        ).replace(
            "  completed_at    TIMESTAMP\n", "  completed_at    TIMESTAMP,\n  source_url TEXT NOT NULL\n",
        )
        with sqlite3.connect(self.source) as con:
            con.executescript(store.CAPTURE_SCHEMA + schema)
        capture = self.capture("https://youtu.be/column-order", "another-place")
        self.migrate()
        con = self.connection()
        self.assertEqual(con.execute("SELECT source_url FROM ingest_runs WHERE item_id = ?", (capture,)).fetchone()[0], "https://youtu.be/column-order")

    def test_recommendation_identity_is_per_user(self) -> None:
        self.migrate()
        con = self.connection()
        con.execute("INSERT INTO users (id, display_name) VALUES ('friend', 'Friend')")
        key = con.execute("SELECT identity_key FROM recommendations WHERE id = ?", (self.entry_id,)).fetchone()[0]
        other = self.insert_recommendation(con, "friend", key)
        con.commit()
        self.assertNotEqual(other, self.entry_id)
        self.assert_rejected(con, """INSERT INTO recommendations
            (user_id, name, normalized_name, entry_type, type_key, identity_key)
            VALUES ('nathan', 'Cafe', 'cafe', 'Restaurant', 'restaurant', ?)""", (key,))

    def test_cross_owner_references_are_rejected_by_database(self) -> None:
        self.migrate()
        con = self.connection()
        con.execute("INSERT INTO users (id, display_name) VALUES ('friend', 'Friend')")
        other = self.insert_recommendation(con, "friend")
        other_capture = self.insert_direct_capture(con, "friend")
        con.commit()
        self.assert_rejected(con, "UPDATE recommendation_mentions SET entry_id = ? WHERE item_id = ?", (other, self.capture_a))
        self.assert_rejected(con, "UPDATE recommendation_mentions SET item_id = ? WHERE item_id = ?", (other_capture, self.capture_a))
        self.assert_rejected(con, "UPDATE ingest_runs SET item_id = ?", (other_capture,))
        self.assert_rejected(con, "UPDATE ingest_events SET user_id = 'friend'")
        self.assert_rejected(con, "UPDATE movie_enrichments SET user_id = 'friend'")

    def test_siri_capture_requires_no_public_url_or_cache(self) -> None:
        self.migrate()
        con = self.connection()
        capture_id = self.insert_direct_capture(con, "nathan")
        con.execute("""INSERT INTO recommendation_mentions
            (user_id, entry_id, item_id, ordinal, source_name, source_type, resolution_status, output_key)
            VALUES ('nathan', ?, ?, 0, 'Cafe', 'Restaurant', 'resolved', 'cafe')""", (self.entry_id, capture_id))
        con.commit()
        self.assertEqual(con.execute("SELECT source_url, post_cache_id FROM captures WHERE id = ?", (capture_id,)).fetchone(), (None, None))
        self.assertEqual(con.execute("SELECT COUNT(*) FROM recommendation_mentions WHERE entry_id = ?", (self.entry_id,)).fetchone()[0], 3)

    def test_removed_mentions_can_survive_parent_deletion_and_retain_unique_identity(self) -> None:
        self.migrate()
        con = self.connection()
        con.execute("UPDATE recommendation_mentions SET removed_at = CURRENT_TIMESTAMP, entry_id = NULL, last_user_change_sequence = 1 WHERE entry_id = ?", (self.entry_id,))
        con.execute("DELETE FROM recommendations WHERE id = ?", (self.entry_id,))
        con.commit()
        self.assertEqual(con.execute("SELECT COUNT(*) FROM recommendation_mentions WHERE removed_at IS NOT NULL").fetchone()[0], 2)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM captures").fetchone()[0], 3)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM movie_enrichments").fetchone()[0], 0)
        self.assert_rejected(con, "UPDATE recommendation_mentions SET removed_at = NULL")

    def test_repeated_user_shares_can_have_separate_history_but_network_retry_key_is_unique(self) -> None:
        self.migrate()
        con = self.connection()
        sql = """INSERT INTO ingest_runs
            (user_id, source_url, status, stage, item_id, idempotency_key, intent)
            VALUES ('nathan', 'https://youtu.be/B', 'queued', 'accepted', ?, ?, 'reshare')"""
        con.execute(sql, (self.capture_b, "share-1"))
        con.execute(sql, (self.capture_b, "share-2"))
        con.commit()
        self.assert_rejected(con, sql, (self.capture_b, "share-1"))

    def test_cache_results_are_versioned_immutable_and_not_private_library_state(self) -> None:
        self.migrate()
        con = self.connection()
        cache = con.execute("""INSERT INTO post_processing_cache
            (platform, post_id, canonical_url, processing_version, status, result_json, completed_at)
            VALUES ('instagram', 'A', 'https://www.instagram.com/reel/A/', 'v1',
                'completed', '{"mentions":[]}', CURRENT_TIMESTAMP)""").lastrowid
        con.execute("UPDATE captures SET post_cache_id = ?, materialization_state = 'complete' WHERE id = ?", (cache, self.capture_a))
        con.commit()
        self.assert_rejected(con, "UPDATE post_processing_cache SET result_json = ? WHERE id = ?", ('{"mentions":[{"key":"added"}]}', cache))
        self.assert_rejected(con, "UPDATE captures SET post_cache_id = NULL WHERE id = ?", (self.capture_a,))
        self.assert_rejected(con, "DELETE FROM post_processing_cache WHERE id = ?", (cache,))
        self.assert_rejected(con, """INSERT INTO post_processing_cache
            (platform, post_id, canonical_url, processing_version)
            VALUES ('instagram', 'A', 'https://www.instagram.com/p/A/', 'v1')""")
        con.execute("""INSERT INTO post_processing_cache
            (platform, post_id, canonical_url, processing_version)
            VALUES ('instagram', 'A', 'https://www.instagram.com/p/A/', 'v2')""")
        con.commit()

    def test_capture_cannot_reference_wrong_post_or_unpublished_result(self) -> None:
        self.migrate()
        con = self.connection()
        cache = con.execute("""INSERT INTO post_processing_cache
            (platform, post_id, canonical_url, processing_version)
            VALUES ('instagram', 'A', 'https://www.instagram.com/reel/A/', 'v1')""").lastrowid
        con.commit()
        self.assert_rejected(con, "UPDATE captures SET post_cache_id = ? WHERE id = ?", (cache, self.capture_b))
        self.assert_rejected(con, "UPDATE captures SET post_cache_id = ?, materialization_state = 'complete' WHERE id = ?", (cache, self.capture_a))
        self.assert_rejected(con, """INSERT INTO post_processing_cache
            (platform, post_id, canonical_url, processing_version, status, result_json, completed_at)
            VALUES ('youtube', 'invalid', 'https://youtu.be/invalid', 'v1', 'completed', '{}', CURRENT_TIMESTAMP)""")

    def test_account_deletion_cascades_private_data_but_preserves_other_user(self) -> None:
        self.migrate()
        con = self.connection()
        con.execute("INSERT INTO users (id, display_name) VALUES ('friend', 'Friend')")
        other = self.insert_recommendation(con, "friend")
        capture = self.insert_direct_capture(con, "friend")
        con.execute("""INSERT INTO recommendation_mentions
            (user_id, entry_id, item_id, ordinal, source_name, source_type, resolution_status, output_key)
            VALUES ('friend', ?, ?, 0, 'Cafe', 'Restaurant', 'resolved', 'cafe')""", (other, capture))
        con.commit()
        con.execute("DELETE FROM users WHERE id = 'nathan'")
        con.commit()
        for table in migration.OWNED_TABLES:
            self.assertEqual(con.execute(f"SELECT COUNT(*) FROM {table} WHERE user_id = 'nathan'").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT COUNT(*) FROM recommendation_mentions WHERE user_id = 'friend'").fetchone()[0], 1)
        self.assertEqual(con.execute("PRAGMA foreign_key_check").fetchall(), [])


if __name__ == "__main__":
    unittest.main()
