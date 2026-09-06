from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

import store
from store import (
    delete_place,
    delete_entry,
    delete_entries,
    init_db,
    list_places,
    list_ingest_runs,
    list_sources,
    list_entry_types,
    list_entries,
    save_ingest,
    saved_entry_outcomes,
    start_ingest_run,
    finish_ingest_run,
)


class StoreTests(unittest.TestCase):
    @staticmethod
    def resolved_place(name: str, google_place_id: str) -> dict:
        return {
            "status": "resolved",
            "extracted": {"extracted_name": name},
            "place": {"id": google_place_id},
        }

    def test_lists_saved_places_newest_first(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/test/",
                    "user_prompt": None,
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

            places = list_places(db_path, 10)

            self.assertEqual(len(places), 1)
            self.assertEqual(places[0]["name"], "La Once Mil")
            self.assertEqual(places[0]["dishes"], ["sandwich"])
            self.assertEqual(places[0]["tags"], ["bakery"])
            self.assertEqual(places[0]["source_url"], "https://www.instagram.com/reel/test/")
            self.assertEqual(places[0]["latitude"], 19.42)
            self.assertEqual(places[0]["location_name"], "Google Location Name")
            self.assertEqual(places[0]["timestamp_seconds"], 12.5)
            self.assertEqual(places[0]["slide_index"], 3)
            self.assertEqual(places[0]["type"], "Restaurant")
            self.assertEqual(
                places[0]["description"],
                "A bakery with great bread and sandwiches.",
            )
            self.assertIsNone(places[0]["starts_at"])
            self.assertIsNone(places[0]["ends_at"])
            self.assertIsNone(places[0]["recurrence_text"])
            canonical = list_entries(db_path)[0]
            self.assertIsNone(canonical["starts_at"])
            self.assertIsNone(canonical["ends_at"])
            self.assertIsNone(canonical["recurrence_text"])
            self.assertEqual(len(canonical["sources"]), 1)
            self.assertEqual(
                canonical["sources"][0]["description"],
                "A bakery with great bread and sandwiches.",
            )
            self.assertEqual(list_entry_types(db_path), ["Restaurant"])

    def test_init_db_migrates_existing_places_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            con = sqlite3.connect(db_path)
            con.execute(
                """CREATE TABLE places (
                     id INTEGER PRIMARY KEY,
                     item_id INTEGER NOT NULL,
                     google_place_id TEXT
                )"""
            )
            con.execute(
                "INSERT INTO places (id, item_id, google_place_id) VALUES (1, 9, 'legacy')"
            )
            con.commit()
            con.close()

            init_db(db_path)

            con = sqlite3.connect(db_path)
            try:
                columns = {
                    row[1]
                    for row in con.execute("PRAGMA table_info(places)").fetchall()
                }
            finally:
                con.close()
            self.assertIn("timestamp_seconds", columns)
            self.assertIn("slide_index", columns)
            self.assertIn("entry_type", columns)
            self.assertIn("description", columns)
            self.assertIn("starts_at", columns)
            self.assertIn("ends_at", columns)
            self.assertIn("location_name", columns)
            con = sqlite3.connect(db_path)
            try:
                legacy = con.execute(
                    "SELECT google_place_id, entry_type FROM places WHERE id = 1"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(legacy, ("legacy", "Unknown"))
            self.assertEqual(len(list(Path(temp_dir).glob("*.pre-entries-*.bak"))), 1)

    def test_init_db_renames_existing_entry_model_without_changing_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "places.db"
            legacy_schema = store.SCHEMA.replace("entry_type", "thing_type")
            legacy_normalized = (
                store.NORMALIZED_SCHEMA
                .replace("entry_sources", "thing_sources")
                .replace("entry_id", "thing_id")
                .replace("entry_type", "thing_type")
                .replace("entries", "things")
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
                    """SELECT e.id, e.entry_type, e.identity_key,
                              es.id, es.entry_id, i.vertical
                         FROM entries AS e
                         JOIN entry_sources AS es ON es.entry_id = e.id
                         JOIN items AS i ON i.id = es.item_id"""
                ).fetchone()
                place_type = con.execute(
                    "SELECT entry_type FROM places WHERE id = 5"
                ).fetchone()[0]
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
                    "entry|query:|name:s p lunch|type:restaurant|starts:|ends:",
                    8,
                    7,
                    "entry",
                ),
            )
            self.assertEqual(place_type, "Restaurant")
            self.assertEqual(
                activity_results,
                [{"entry_id": 7, "name": "S&P Lunch"}],
            )
            self.assertEqual(len(list(root.glob("*.pre-entry-rename-*.bak"))), 1)

    def test_normalized_migration_backs_up_and_backfills_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/legacy/",
                    "metadata": {},
                    "resolved_entries": [
                        self.resolved_place("Legacy Restaurant", "places/legacy")
                    ],
                },
            )
            con = sqlite3.connect(db_path)
            con.executescript(
                "DROP TABLE entry_sources; DROP TABLE entries; DROP TABLE locations;"
            )
            con.commit()
            con.close()

            init_db(db_path)

            self.assertEqual([entry["name"] for entry in list_entries(db_path)], ["Legacy Restaurant"])
            self.assertEqual(
                len(list(Path(temp_dir).glob("*.pre-normalized-*.bak"))),
                1,
            )

    def test_conservative_matching_merges_venue_aliases_but_not_exhibits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            examples = (
                ("one", "S&P Lunch", "Restaurant", None, "First description"),
                ("two", "S & P Lunch", "Deli", None, "Most recent description"),
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

    def test_normalizes_legacy_type_aliases_to_controlled_types(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            for ordinal, entry_type in enumerate(
                ("Deli", "Coffee Shop", "Exhibit", "Fitness Exercise", "Food Pop-Up"),
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
                item_ids.append(
                    save_ingest(
                        db_path,
                        {
                            "source_url": f"https://www.instagram.com/reel/{suffix}/",
                            "metadata": {"source_platform": "instagram"},
                            "resolved_entries": [
                                self.resolved_place("S&P Lunch", "places/shared")
                            ],
                        },
                    )
                )

            first = saved_entry_outcomes(db_path, item_ids[0])[0]
            second = saved_entry_outcomes(db_path, item_ids[1])[0]
            self.assertTrue(first["is_new"])
            self.assertEqual(first["source_count"], 1)
            self.assertFalse(second["is_new"])
            self.assertEqual(second["source_count"], 2)

    def test_failed_run_preserves_stage_and_readable_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            run_id = start_ingest_run(
                db_path,
                "https://www.instagram.com/reel/fail/",
                None,
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

    def test_delete_place_removes_all_references_but_preserves_post_siblings(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/multiple/",
                    "metadata": {},
                    "places_extracted": [],
                    "resolved_places": [
                        self.resolved_place("Delete Me", "places/delete"),
                        self.resolved_place("Keep Me", "places/keep"),
                    ],
                },
            )
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/delete-only/",
                    "metadata": {},
                    "places_extracted": [],
                    "resolved_places": [
                        self.resolved_place("Delete Me", "places/delete"),
                    ],
                },
            )
            selected = next(
                place
                for place in list_places(db_path, 10)
                if place["google_place_id"] == "places/delete"
            )

            result = delete_place(db_path, selected["id"])

            self.assertEqual(result, {"deleted_places": 2, "deleted_items": 0})
            remaining = list_places(db_path, 10)
            self.assertEqual([place["name"] for place in remaining], ["Keep Me"])
            self.assertEqual(
                remaining[0]["source_url"],
                "https://www.instagram.com/reel/multiple/",
            )
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 2)
            finally:
                con.close()

    def test_delete_place_returns_none_for_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)

            self.assertIsNone(delete_place(db_path, 999))


if __name__ == "__main__":
    unittest.main()
