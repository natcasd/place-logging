from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import store
from store import (
    confirm_activity_location,
    delete_entry,
    delete_entries,
    init_db,
    list_ingest_runs,
    list_sources,
    list_entry_types,
    list_entries,
    save_ingest,
    saved_entry_outcomes,
    start_ingest_run,
    finish_ingest_run,
    get_ingest_run,
    prepare_ingest_retry,
    record_ingest_failure,
    recover_interrupted_ingests,
)


LEGACY_PLACES_SCHEMA = """
CREATE TABLE places (
  id                         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id                    INTEGER NOT NULL REFERENCES items(id),
  ordinal                    INTEGER NOT NULL,
  extracted_name             TEXT NOT NULL,
  google_place_id            TEXT,
  lat                        REAL,
  lng                        REAL,
  formatted_address          TEXT,
  google_maps_url            TEXT,
  location_name              TEXT,
  dishes_json                TEXT,
  why_its_cool               TEXT,
  tags_json                  TEXT,
  timestamp_seconds          REAL,
  slide_index                INTEGER,
  resolution_status          TEXT NOT NULL,
  resolution_candidates_json TEXT,
  entry_type                 TEXT NOT NULL DEFAULT 'Unknown',
  description                TEXT NOT NULL DEFAULT '',
  starts_at                  TEXT,
  ends_at                    TEXT,
  recurrence_text            TEXT,
  location_query             TEXT
);
CREATE INDEX idx_places_item ON places(item_id);
CREATE INDEX idx_places_google_id ON places(google_place_id);
"""


def pre_104_capture_schema() -> str:
    return store.CAPTURE_SCHEMA.replace("captures", "items") + LEGACY_PLACES_SCHEMA


def pre_105_recommendation_schema() -> str:
    return store.RECOMMENDATION_SCHEMA.replace(
        "  ordinal                    INTEGER NOT NULL,",
        "  legacy_place_id            INTEGER UNIQUE REFERENCES places(id),\n"
        "  ordinal                    INTEGER NOT NULL,",
    )


class StoreTests(unittest.TestCase):
    @staticmethod
    def resolved_place(name: str, google_place_id: str) -> dict:
        return {
            "status": "resolved",
            "extracted": {"extracted_name": name},
            "place": {"id": google_place_id},
        }

    def test_fresh_database_uses_only_canonical_persistence_model(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            init_db(db_path)

            con = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                mention_columns = {
                    row[1]
                    for row in con.execute(
                        "PRAGMA table_info(recommendation_mentions)"
                    )
                }
                foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                con.close()

            self.assertTrue(
                {"captures", "recommendations", "recommendation_mentions", "locations"}
                <= tables
            )
            self.assertNotIn("places", tables)
            self.assertNotIn("legacy_place_id", mention_columns)
            self.assertEqual(foreign_key_errors, [])

    def test_ingest_retry_state_is_durable_and_reuses_one_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            run_id = start_ingest_run(
                db_path,
                "https://www.instagram.com/reel/retry/",
                "instagram",
            )

            status = record_ingest_failure(
                db_path,
                run_id,
                stage="fetching",
                error=RuntimeError("HTTP Error 429; Retry-After: 10"),
                failure_kind="media_fetch_failed",
                user_message="Instagram media could not be downloaded.",
                retryable=True,
                retry_delay_seconds=10,
            )
            claimed = prepare_ingest_retry(db_path, run_id, automatic=False)

            self.assertEqual(status, "retry_scheduled")
            self.assertIsNotNone(claimed)
            self.assertEqual(claimed["attempt_count"], 2)
            self.assertEqual(get_ingest_run(db_path, run_id)["status"], "processing")
            self.assertEqual(len(list_ingest_runs(db_path)), 1)

    def test_recovers_interrupted_processing_after_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            run_id = start_ingest_run(
                db_path,
                "https://youtu.be/interrupted",
                "youtube",
            )

            recovered = recover_interrupted_ingests(db_path)
            run = get_ingest_run(db_path, run_id)

            self.assertEqual(recovered, 1)
            self.assertEqual(run["status"], "retry_scheduled")
            self.assertEqual(run["failure_kind"], "interrupted")
            self.assertIsNotNone(run["next_retry_at"])

    def test_lists_saved_recommendations_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/test/",
                    "metadata": {},
                    "places_extracted": [],
                    "resolved_places": [
                        {
                            "status": "resolved",
                            "extracted": {
                                "extracted_name": "La Once Mil",
                                "type_name": "Restaurant",
                                "description": "A bakery with great bread and sandwiches.",
                                "starts_at": "2026-09-01",
                                "ends_at": "2026-09-30",
                                "recurrence_text": "Thursday - Sunday",
                                "dishes": ["sandwich"],
                                "why_its_cool": "Great bread.",
                                "tags": ["bakery"],
                                "timestamp_seconds": 12.5,
                                "slide_index": 3,
                            },
                            "place": {
                                "id": "places/abc",
                                "displayName": {"text": "Google Location Name"},
                                "location": {
                                    "latitude": 19.42,
                                    "longitude": -99.21,
                                },
                                "formattedAddress": "Mexico City",
                                "googleMapsUri": "https://maps.google.com/abc",
                            },
                        }
                    ],
                },
            )

            recommendations = list_entries(db_path, 10)

            self.assertEqual(len(recommendations), 1)
            self.assertEqual(recommendations[0]["name"], "La Once Mil")
            self.assertEqual(recommendations[0]["dishes"], ["sandwich"])
            self.assertEqual(recommendations[0]["tags"], ["bakery"])
            self.assertEqual(
                recommendations[0]["source_url"],
                "https://www.instagram.com/reel/test/",
            )
            self.assertEqual(recommendations[0]["latitude"], 19.42)
            self.assertEqual(recommendations[0]["location_name"], "Google Location Name")
            self.assertEqual(recommendations[0]["timestamp_seconds"], 12.5)
            self.assertEqual(recommendations[0]["slide_index"], 3)
            self.assertEqual(recommendations[0]["type"], "Restaurant")
            self.assertEqual(
                recommendations[0]["description"],
                "A bakery with great bread and sandwiches.",
            )
            self.assertEqual(recommendations[0]["starts_at"], "2026-09-01")
            self.assertEqual(recommendations[0]["ends_at"], "2026-09-30")
            self.assertEqual(
                recommendations[0]["recurrence_text"], "Thursday - Sunday"
            )
            self.assertEqual(len(recommendations[0]["sources"]), 1)
            self.assertEqual(
                recommendations[0]["sources"][0]["description"],
                "A bakery with great bread and sandwiches.",
            )
            self.assertEqual(list_entry_types(db_path), ["Restaurant"])

    def test_init_db_backfills_and_removes_legacy_places_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "places.db"
            con = sqlite3.connect(db_path)
            con.executescript(pre_104_capture_schema())
            con.execute(
                """INSERT INTO items (id, vertical, source_url)
                   VALUES (9, 'entry', 'https://example.com/legacy')"""
            )
            con.execute(
                """INSERT INTO places
                   (id, item_id, ordinal, extracted_name, google_place_id,
                    resolution_status, entry_type, description)
                   VALUES (1, 9, 0, 'Legacy Cafe', 'legacy', 'auto', 'Café',
                           'A preserved recommendation')"""
            )
            con.commit()
            con.close()

            init_db(db_path)

            con = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                migrated = con.execute(
                    """SELECT r.name, r.entry_type, rm.description, c.id
                         FROM recommendations AS r
                         JOIN recommendation_mentions AS rm ON rm.entry_id = r.id
                         JOIN captures AS c ON c.id = rm.item_id"""
                ).fetchone()
                foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                con.close()
            self.assertNotIn("places", tables)
            self.assertEqual(
                migrated,
                ("Legacy Cafe", "Café", "A preserved recommendation", 9),
            )
            self.assertEqual(foreign_key_errors, [])
            self.assertEqual(
                len(list(root.glob("*.pre-legacy-places-removal-*.bak"))), 1
            )

    def test_init_db_removes_user_prompt_columns_without_losing_data(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "places.db"
            init_db(db_path)
            item_id = save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/legacy-prompt/",
                    "metadata": {"source_platform": "instagram"},
                    "resolved_entries": [
                        self.resolved_place("Legacy Restaurant", "places/legacy-prompt")
                    ],
                },
            )
            run_id = start_ingest_run(
                db_path,
                "https://www.instagram.com/reel/processing/",
                "instagram",
            )
            con = sqlite3.connect(db_path)
            con.execute("ALTER TABLE captures ADD COLUMN user_prompt TEXT")
            con.execute("ALTER TABLE ingest_runs ADD COLUMN user_prompt TEXT")
            con.execute(
                "UPDATE captures SET user_prompt = 'legacy source prompt' WHERE id = ?",
                (item_id,),
            )
            con.execute(
                "UPDATE ingest_runs SET user_prompt = 'legacy run prompt' WHERE id = ?",
                (run_id,),
            )
            con.commit()
            con.close()

            init_db(db_path)
            init_db(db_path)

            con = sqlite3.connect(db_path)
            try:
                item_columns = {
                    row[1] for row in con.execute("PRAGMA table_info(captures)").fetchall()
                }
                run_columns = {
                    row[1]
                    for row in con.execute("PRAGMA table_info(ingest_runs)").fetchall()
                }
                source_url = con.execute(
                    "SELECT source_url FROM captures WHERE id = ?", (item_id,)
                ).fetchone()[0]
                run = con.execute(
                    "SELECT source_url, source_platform, status FROM ingest_runs WHERE id = ?",
                    (run_id,),
                ).fetchone()
                foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                con.close()

            self.assertNotIn("user_prompt", item_columns)
            self.assertNotIn("user_prompt", run_columns)
            self.assertEqual(
                source_url,
                "https://www.instagram.com/reel/legacy-prompt/",
            )
            self.assertEqual(
                run,
                (
                    "https://www.instagram.com/reel/processing/",
                    "instagram",
                    "processing",
                ),
            )
            self.assertEqual(foreign_key_errors, [])
            backup_paths = list(root.glob("*.pre-user-prompt-removal-*.bak"))
            self.assertEqual(len(backup_paths), 1)
            backup = sqlite3.connect(backup_paths[0])
            try:
                self.assertEqual(
                    backup.execute(
                        "SELECT user_prompt FROM captures WHERE id = ?", (item_id,)
                    ).fetchone()[0],
                    "legacy source prompt",
                )
                self.assertEqual(
                    backup.execute(
                        "SELECT user_prompt FROM ingest_runs WHERE id = ?", (run_id,)
                    ).fetchone()[0],
                    "legacy run prompt",
                )
            finally:
                backup.close()

    def test_init_db_renames_existing_entry_model_without_changing_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "places.db"
            legacy_schema = pre_104_capture_schema().replace(
                "entry_type", "thing_type"
            )
            legacy_normalized = (
                pre_105_recommendation_schema()
                .replace("recommendation_mentions", "thing_sources")
                .replace("entry_id", "thing_id")
                .replace("entry_type", "thing_type")
                .replace("recommendations", "things")
                .replace("captures", "items")
            )
            con = sqlite3.connect(db_path)
            con.executescript(legacy_schema)
            con.executescript(legacy_normalized)
            con.execute(
                """INSERT INTO items (id, vertical, source_url)
                   VALUES (4, 'thing', 'https://example.com/source')"""
            )
            con.execute(
                """INSERT INTO places
                   (id, item_id, ordinal, extracted_name, resolution_status,
                    thing_type)
                   VALUES (5, 4, 0, 'S&P Lunch', 'auto', 'Restaurant')"""
            )
            con.execute(
                """INSERT INTO locations (id, google_place_id, display_name)
                   VALUES (6, 'google-id', 'S&P Lunch')"""
            )
            con.execute(
                """INSERT INTO things
                   (id, name, normalized_name, thing_type, type_key,
                    identity_key, location_id)
                   VALUES (7, 'S&P Lunch', 's p lunch', 'Restaurant', 'food',
                           'thing|query:|name:s p lunch|type:restaurant|starts:|ends:',
                           6)"""
            )
            con.execute(
                """INSERT INTO thing_sources
                   (id, thing_id, item_id, legacy_place_id, ordinal,
                    source_name, source_type, resolution_status)
                   VALUES (8, 7, 4, 5, 0, 'S&P Lunch', 'Restaurant', 'auto')"""
            )
            con.execute(
                """INSERT INTO ingest_runs
                   (id, source_url, source_platform, status, stage, item_id,
                    result_json)
                   VALUES (9, 'https://example.com/source', 'instagram',
                           'completed', 'completed', 4,
                           '[{"thing_id": 7, "name": "S&P Lunch"}]')"""
            )
            con.commit()
            con.close()

            init_db(db_path)

            con = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                row = con.execute(
                    """SELECT r.id, r.entry_type, r.identity_key,
                              rm.id, rm.entry_id, c.vertical
                         FROM recommendations AS r
                         JOIN recommendation_mentions AS rm ON rm.entry_id = r.id
                         JOIN captures AS c ON c.id = rm.item_id"""
                ).fetchone()
                activity_results = json.loads(
                    con.execute(
                        "SELECT result_json FROM ingest_runs WHERE id = 9"
                    ).fetchone()[0]
                )
            finally:
                con.close()

            self.assertNotIn("things", tables)
            self.assertNotIn("thing_sources", tables)
            self.assertEqual(
                row,
                (
                    7,
                    "Restaurant",
                    "recommendation|query:|name:s p lunch|type:restaurant|starts:|ends:",
                    8,
                    7,
                    "recommendation",
                ),
            )
            self.assertNotIn("places", tables)
            self.assertEqual(
                activity_results,
                [{"entry_id": 7, "name": "S&P Lunch"}],
            )
            self.assertEqual(len(list(root.glob("*.pre-entry-rename-*.bak"))), 1)

    def test_init_db_renames_current_core_tables_once_without_data_loss(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "places.db"
            legacy_capture_schema = pre_104_capture_schema()
            legacy_recommendation_schema = (
                pre_105_recommendation_schema()
                .replace("recommendation_mentions", "entry_sources")
                .replace("recommendations", "entries")
                .replace("captures", "items")
            )
            con = sqlite3.connect(db_path)
            con.executescript(legacy_capture_schema)
            con.executescript(legacy_recommendation_schema)
            con.execute(
                """INSERT INTO items (id, vertical, source_url)
                   VALUES (41, 'entry', 'https://example.com/capture')"""
            )
            con.execute(
                """INSERT INTO places
                   (id, item_id, ordinal, extracted_name, resolution_status,
                    entry_type)
                   VALUES (51, 41, 0, 'Migration Movie', 'auto', 'Movie')"""
            )
            con.execute(
                """INSERT INTO locations (id, google_place_id, display_name)
                   VALUES (61, 'google-migration', 'Migration Theater')"""
            )
            con.execute(
                """INSERT INTO entries
                   (id, name, normalized_name, entry_type, type_key,
                    identity_key, location_id)
                   VALUES (71, 'Migration Movie', 'migration movie', 'Movie',
                           'movie',
                           'entry|query:migration|name:migration movie|type:movie|starts:|ends:',
                           61)"""
            )
            con.execute(
                """INSERT INTO entry_sources
                   (id, entry_id, item_id, legacy_place_id, ordinal,
                    source_name, source_type, resolution_status)
                   VALUES (81, 71, 41, 51, 0, 'Migration Movie', 'Movie', 'auto')"""
            )
            con.execute(
                """INSERT INTO movie_enrichments
                   (entry_id, provider, match_status)
                   VALUES (71, 'wikidata', 'matched')"""
            )
            con.execute(
                """INSERT INTO ingest_runs
                   (id, source_url, source_platform, status, stage, item_id)
                   VALUES (91, 'https://example.com/capture', 'other',
                           'completed', 'completed', 41)"""
            )
            con.commit()
            con.close()

            init_db(db_path)
            init_db(db_path)

            con = sqlite3.connect(db_path)
            try:
                tables = {
                    row[0]
                    for row in con.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                }
                migrated = con.execute(
                    """SELECT r.id, r.identity_key, rm.id, c.id, c.vertical,
                              me.entry_id, ir.item_id
                         FROM recommendations AS r
                         JOIN recommendation_mentions AS rm ON rm.entry_id = r.id
                         JOIN captures AS c ON c.id = rm.item_id
                         JOIN movie_enrichments AS me ON me.entry_id = r.id
                         JOIN ingest_runs AS ir ON ir.item_id = c.id"""
                ).fetchone()
                foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                con.close()

            self.assertTrue(
                {"captures", "recommendations", "recommendation_mentions", "locations"}
                <= tables
            )
            self.assertTrue({"items", "entries", "entry_sources"}.isdisjoint(tables))
            self.assertNotIn("places", tables)
            self.assertEqual(
                migrated,
                (
                    71,
                    "recommendation|query:migration|name:migration movie|type:movie|starts:|ends:",
                    81,
                    41,
                    "recommendation",
                    71,
                    41,
                ),
            )
            self.assertEqual(foreign_key_errors, [])
            self.assertEqual(
                len(list(root.glob("*.pre-capture-recommendation-rename-*.bak"))),
                1,
            )
            cleanup_backups = list(
                root.glob("*.pre-legacy-places-removal-*.bak")
            )
            self.assertEqual(len(cleanup_backups), 1)
            backup = sqlite3.connect(cleanup_backups[0])
            try:
                self.assertEqual(
                    backup.execute("SELECT COUNT(*) FROM places").fetchone()[0],
                    1,
                )
                self.assertIn(
                    "legacy_place_id",
                    {
                        row[1]
                        for row in backup.execute(
                            "PRAGMA table_info(recommendation_mentions)"
                        )
                    },
                )
            finally:
                backup.close()

    def test_conservative_matching_merges_venue_aliases_but_not_exhibits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            examples = (
                ("one", "S&P Lunch", "Restaurant", None, "First description"),
                ("two", "S & P Lunch", "Restaurant", None, "Most recent description"),
                (
                    "three",
                    "Giacometti in the Temple of Dendur",
                    "Exhibit",
                    "2026-09-08",
                    "Exhibit description",
                ),
            )
            for suffix, name, entry_type, ends_at, description in examples:
                extracted = {
                    "extracted_name": name,
                    "type_name": entry_type,
                    "description": description,
                }
                if ends_at:
                    extracted["ends_at"] = ends_at
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://www.instagram.com/reel/{suffix}/",
                        "metadata": {"source_platform": "instagram"},
                        "resolved_entries": [
                            {
                                "status": "resolved",
                                "extracted": extracted,
                                "place": {
                                    "id": "places/shared",
                                    "displayName": {"text": "Shared Venue"},
                                },
                            }
                        ],
                    },
                )

            entries = list_entries(db_path)

            self.assertEqual(len(entries), 2)
            venue = next(entry for entry in entries if entry["type"] == "Restaurant")
            exhibit = next(entry for entry in entries if entry["type"] == "Exhibit")
            self.assertEqual(len(venue["sources"]), 2)
            self.assertEqual(venue["description"], "Most recent description")
            self.assertEqual(exhibit["ends_at"], "2026-09-08")
            self.assertEqual(venue["location_id"], exhibit["location_id"])

    def test_temporary_entries_with_different_dates_remain_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            for suffix, ends_at in (("one", "2026-09-08"), ("two", "2027-09-08")):
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://www.instagram.com/reel/{suffix}/",
                        "metadata": {},
                        "resolved_entries": [
                            {
                                "status": "resolved",
                                "extracted": {
                                    "extracted_name": "Annual Exhibition",
                                    "type_name": "Exhibit",
                                    "ends_at": ends_at,
                                },
                                "place": {"id": "places/museum"},
                            }
                        ],
                    },
                )

            self.assertEqual(len(list_entries(db_path)), 2)

    def test_non_location_entries_match_only_on_normalized_name_and_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            for suffix, name, entry_type in (
                ("one", "The Creative Act", "Book"),
                ("two", "the creative act", "Book"),
                ("three", "The Creative Act", "Movie"),
            ):
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://www.instagram.com/reel/{suffix}/",
                        "metadata": {},
                        "resolved_entries": [
                            {
                                "status": "not_applicable",
                                "extracted": {
                                    "extracted_name": name,
                                    "type_name": entry_type,
                                },
                            }
                        ],
                    },
                )

            entries = list_entries(db_path)
            self.assertEqual(len(entries), 2)
            book = next(entry for entry in entries if entry["type"] == "Book")
            self.assertEqual(len(book["sources"]), 2)

    def test_never_persists_generic_place_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/generic/",
                    "metadata": {},
                    "resolved_entries": [
                        {
                            "status": "not_applicable",
                            "extracted": {
                                "extracted_name": "Ambiguous recommendation",
                                "type_name": "Place",
                            },
                        }
                    ],
                },
            )

            self.assertEqual(list_entry_types(db_path), ["Unknown"])

    def test_normalizes_case_and_accents_to_catalog_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            for ordinal, entry_type in enumerate(
                ("restaurant", "Cafe", "EXHIBIT", "fitness", "POP-UP"),
                start=1,
            ):
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://www.instagram.com/reel/type-{ordinal}/",
                        "metadata": {},
                        "resolved_entries": [
                            {
                                "status": "not_applicable",
                                "extracted": {
                                    "extracted_name": f"Named entry {ordinal}",
                                    "type_name": entry_type,
                                },
                            }
                        ],
                    },
                )

            self.assertEqual(
                list_entry_types(db_path),
                ["Café", "Exhibit", "Fitness", "Pop-up", "Restaurant"],
            )

    def test_saves_non_location_entry_and_preserves_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/book/",
                    "metadata": {
                        "source_platform": "instagram",
                        "uploader": "reader",
                        "caption_or_description": "A book recommendation",
                        "source_content": {"summary": "A creator recommends a book."},
                        "media_count": 1,
                        "media_preserved": True,
                        "archived_media": [{"path": "/data/source.mp4"}],
                    },
                    "entries_extracted": [
                        {
                            "extracted_name": "The Creative Act",
                            "type_name": "Book",
                            "description": "A book about creativity.",
                        }
                    ],
                    "resolved_entries": [
                        {
                            "status": "not_applicable",
                            "extracted": {
                                "extracted_name": "The Creative Act",
                                "type_name": "Book",
                                "description": "A book about creativity.",
                            },
                        }
                    ],
                },
            )

            entries = list_entries(db_path)
            self.assertEqual(entries[0]["type"], "Book")
            self.assertIsNone(entries[0]["latitude"])
            self.assertEqual(entries[0]["resolution_status"], "not_applicable")

            sources = list_sources(db_path)
            self.assertEqual(sources[0]["entry_count"], 1)
            self.assertFalse(sources[0]["media_preserved"])
            self.assertEqual(sources[0]["summary"], "A creator recommends a book.")
            self.assertFalse(sources[0]["needs_review"])

    def test_source_survives_empty_extraction_for_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/unclear/",
                    "metadata": {"source_platform": "instagram"},
                    "entries_extracted": [],
                    "resolved_entries": [],
                },
            )

            self.assertEqual(list_entries(db_path), [])
            sources = list_sources(db_path)
            self.assertEqual(len(sources), 1)
            self.assertTrue(sources[0]["needs_review"])

    def test_activity_backfills_existing_sources_without_changing_them(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            item_id = save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/old/",
                    "metadata": {"source_platform": "instagram"},
                    "resolved_entries": [
                        self.resolved_place("Old Restaurant", "places/old")
                    ],
                },
            )

            init_db(db_path)

            activity = list_ingest_runs(db_path)
            self.assertEqual(len(activity), 1)
            self.assertEqual(activity[0]["item_id"], item_id)
            self.assertEqual(activity[0]["results"][0]["name"], "Old Restaurant")
            self.assertEqual(activity[0]["status"], "completed")
            self.assertEqual(len(list_sources(db_path)), 1)
            self.assertEqual(len(list_entries(db_path)), 1)

    def test_saved_entry_outcome_reports_new_then_added_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            item_ids = []
            for suffix in ("one", "two"):
                resolved = self.resolved_place("S&P Lunch", "places/shared")
                resolved["extracted"]["description"] = (
                    f"Description from source {suffix}."
                )
                item_ids.append(
                    save_ingest(
                        db_path,
                        {
                            "source_url": f"https://www.instagram.com/reel/{suffix}/",
                            "metadata": {"source_platform": "instagram"},
                            "resolved_entries": [resolved],
                        },
                    )
                )

            first = saved_entry_outcomes(db_path, item_ids[0])[0]
            second = saved_entry_outcomes(db_path, item_ids[1])[0]
            self.assertTrue(first["is_new"])
            self.assertEqual(first["source_count"], 1)
            self.assertEqual(first["description"], "Description from source one.")
            self.assertFalse(second["is_new"])
            self.assertEqual(second["source_count"], 2)
            self.assertEqual(second["description"], "Description from source two.")

    def test_failed_run_preserves_stage_and_readable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            run_id = start_ingest_run(
                db_path,
                "https://www.instagram.com/reel/fail/",
                "instagram",
            )
            finish_ingest_run(
                db_path,
                run_id,
                status="failed",
                stage="extracting",
                message="Failed while finding recommendations",
                error=RuntimeError("Gemini overloaded"),
            )

            run = list_ingest_runs(db_path)[0]
            self.assertEqual(run["status"], "failed")
            self.assertEqual(run["stage"], "extracting")
            self.assertEqual(run["error_type"], "RuntimeError")
            self.assertEqual(run["error_message"], "Gemini overloaded")

    def test_confirms_stored_activity_location_candidate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            source_url = "https://www.instagram.com/reel/review/"
            run_id = start_ingest_run(db_path, source_url, "instagram")
            candidates = [
                {
                    "id": "places/penny-east-village",
                    "displayName": {"text": "Penny"},
                    "formattedAddress": "90 E 10th St, New York, NY",
                    "googleMapsUri": "https://maps.google.com/penny",
                    "location": {"latitude": 40.731, "longitude": -73.989},
                },
                {
                    "id": "places/penny-brooklyn",
                    "displayName": {"text": "Penny Williamsburg"},
                    "formattedAddress": "2 Water St, Brooklyn, NY",
                    "location": {"latitude": 40.703, "longitude": -73.995},
                },
            ]
            item_id = save_ingest(
                db_path,
                {
                    "source_url": source_url,
                    "metadata": {"source_platform": "instagram"},
                    "resolved_entries": [
                        {
                            "status": "needs_review",
                            "extracted": {
                                "extracted_name": "Penny",
                                "type_name": "Restaurant",
                                "timestamp_seconds": 66,
                            },
                            "candidates": candidates,
                        }
                    ],
                },
            )
            before = saved_entry_outcomes(db_path, item_id)[0]
            self.assertEqual(
                [candidate["id"] for candidate in before["review_candidates"]],
                ["places/penny-east-village", "places/penny-brooklyn"],
            )
            finish_ingest_run(
                db_path,
                run_id,
                status="partial",
                stage="completed",
                message="Source saved with results needing review",
                item_id=item_id,
                outcomes=[before],
            )

            result = confirm_activity_location(
                db_path,
                run_id,
                before["entry_id"],
                "places/penny-east-village",
            )

            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(result["resolution_status"], "user_confirmed")
            self.assertEqual(result["location_name"], "Penny")
            self.assertEqual(result["formatted_address"], "90 E 10th St, New York, NY")
            self.assertEqual(result["review_candidates"], [])
            self.assertEqual(result["timestamp_seconds"], 66)
            activity = list_ingest_runs(db_path)[0]
            self.assertEqual(activity["status"], "completed")
            self.assertEqual(activity["results"][0]["resolution_status"], "user_confirmed")

    def test_rejects_candidate_not_stored_for_activity_entry(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            run_id = start_ingest_run(
                db_path,
                "https://www.instagram.com/reel/review/",
                "instagram",
            )
            item_id = save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/review/",
                    "metadata": {},
                    "resolved_entries": [
                        {
                            "status": "needs_review",
                            "extracted": {
                                "extracted_name": "Penny",
                                "type_name": "Restaurant",
                            },
                            "candidates": [
                                {
                                    "id": "places/allowed",
                                    "displayName": {"text": "Penny"},
                                }
                            ],
                        }
                    ],
                },
            )
            entry_id = saved_entry_outcomes(db_path, item_id)[0]["entry_id"]
            finish_ingest_run(
                db_path,
                run_id,
                status="partial",
                stage="completed",
                message="Needs review",
                item_id=item_id,
                outcomes=saved_entry_outcomes(db_path, item_id),
            )

            with self.assertRaisesRegex(ValueError, "available location candidates"):
                confirm_activity_location(
                    db_path,
                    run_id,
                    entry_id,
                    "places/not-offered",
                )

    def test_delete_entry_preserves_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/book/",
                    "metadata": {},
                    "resolved_entries": [
                        {
                            "status": "not_applicable",
                            "extracted": {
                                "extracted_name": "A Book",
                                "type_name": "Book",
                            },
                        }
                    ],
                },
            )
            entry = list_entries(db_path)[0]

            result = delete_entry(db_path, entry["id"])

            self.assertEqual(result, {"deleted_entries": 1, "deleted_sources": 0})
            self.assertEqual(list_entries(db_path), [])
            self.assertEqual(len(list_sources(db_path)), 1)
            self.assertTrue(list_sources(db_path)[0]["needs_review"])

    def test_deleting_last_activity_recommendation_completes_review(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            source_url = "https://www.instagram.com/reel/delete-review/"
            run_id = start_ingest_run(db_path, source_url, "instagram")
            item_id = save_ingest(
                db_path,
                {
                    "source_url": source_url,
                    "metadata": {"source_platform": "instagram"},
                    "resolved_entries": [
                        {
                            "status": "unresolved",
                            "extracted": {
                                "extracted_name": "Unknown Restaurant",
                                "type_name": "Restaurant",
                            },
                        }
                    ],
                },
            )
            outcomes = saved_entry_outcomes(db_path, item_id)
            finish_ingest_run(
                db_path,
                run_id,
                status="partial",
                stage="completed",
                message="Needs review",
                item_id=item_id,
                outcomes=outcomes,
            )

            delete_entry(db_path, outcomes[0]["entry_id"])

            activity = list_ingest_runs(db_path)[0]
            self.assertEqual(activity["status"], "completed")
            self.assertEqual(activity["results"], [])
            self.assertEqual(len(list_sources(db_path)), 1)

    def test_delete_entries_removes_exact_card_rows_and_preserves_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            for suffix, name in (("one", "S&P Lunch"), ("two", "S&P Lunch")):
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://www.instagram.com/reel/{suffix}/",
                        "metadata": {},
                        "resolved_entries": [self.resolved_place(name, "places/shared")],
                    },
                )
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/exhibit/",
                    "metadata": {},
                    "resolved_entries": [
                        self.resolved_place("Guest Pop-Up", "places/shared")
                    ],
                },
            )
            entries = list_entries(db_path)
            restaurant_ids = [entry["id"] for entry in entries if entry["name"] == "S&P Lunch"]

            result = delete_entries(db_path, restaurant_ids)

            self.assertEqual(result, {"deleted_entries": 1, "deleted_sources": 0})
            self.assertEqual([entry["name"] for entry in list_entries(db_path)], ["Guest Pop-Up"])
            self.assertEqual(len(list_sources(db_path)), 3)

    def test_delete_entries_is_atomic_when_any_id_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/one/",
                    "metadata": {},
                    "resolved_entries": [self.resolved_place("Keep Me", "places/keep")],
                },
            )
            entry = list_entries(db_path)[0]

            self.assertIsNone(delete_entries(db_path, [entry["id"], 999]))
            self.assertEqual([saved["name"] for saved in list_entries(db_path)], ["Keep Me"])

if __name__ == "__main__":
    unittest.main()
