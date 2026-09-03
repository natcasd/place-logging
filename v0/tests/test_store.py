from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from store import (
    delete_place,
    delete_thing,
    delete_things,
    init_db,
    list_places,
    list_sources,
    list_thing_types,
    list_things,
    save_ingest,
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
            self.assertEqual(places[0]["ends_at"], "2026-09-30")
            canonical = list_things(db_path)[0]
            self.assertEqual(len(canonical["sources"]), 1)
            self.assertEqual(
                canonical["sources"][0]["description"],
                "A bakery with great bread and sandwiches.",
            )
            self.assertEqual(list_thing_types(db_path), ["Restaurant"])

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
            self.assertIn("thing_type", columns)
            self.assertIn("description", columns)
            self.assertIn("starts_at", columns)
            self.assertIn("ends_at", columns)
            self.assertIn("location_name", columns)
            con = sqlite3.connect(db_path)
            try:
                legacy = con.execute(
                    "SELECT google_place_id, thing_type FROM places WHERE id = 1"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(legacy, ("legacy", "Unknown"))
            self.assertEqual(len(list(Path(temp_dir).glob("*.pre-things-*.bak"))), 1)

    def test_normalized_migration_backs_up_and_backfills_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/legacy/",
                    "metadata": {},
                    "resolved_things": [
                        self.resolved_place("Legacy Restaurant", "places/legacy")
                    ],
                },
            )
            con = sqlite3.connect(db_path)
            con.executescript(
                "DROP TABLE thing_sources; DROP TABLE things; DROP TABLE locations;"
            )
            con.commit()
            con.close()

            init_db(db_path)

            self.assertEqual([thing["name"] for thing in list_things(db_path)], ["Legacy Restaurant"])
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
            for suffix, name, thing_type, ends_at, description in examples:
                extracted = {
                    "extracted_name": name,
                    "type_name": thing_type,
                    "description": description,
                }
                if ends_at:
                    extracted["ends_at"] = ends_at
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://www.instagram.com/reel/{suffix}/",
                        "metadata": {"source_platform": "instagram"},
                        "resolved_things": [
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

            things = list_things(db_path)

            self.assertEqual(len(things), 2)
            venue = next(thing for thing in things if thing["type"] == "Restaurant")
            exhibit = next(thing for thing in things if thing["type"] == "Exhibit")
            self.assertEqual(len(venue["sources"]), 2)
            self.assertEqual(venue["description"], "Most recent description")
            self.assertEqual(exhibit["ends_at"], "2026-09-08")
            self.assertEqual(venue["location_id"], exhibit["location_id"])

    def test_temporary_things_with_different_dates_remain_separate(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            for suffix, ends_at in (("one", "2026-09-08"), ("two", "2027-09-08")):
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://www.instagram.com/reel/{suffix}/",
                        "metadata": {},
                        "resolved_things": [
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

            self.assertEqual(len(list_things(db_path)), 2)

    def test_non_location_things_match_only_on_normalized_name_and_type(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            for suffix, name, thing_type in (
                ("one", "The Creative Act", "Book"),
                ("two", "the creative act", "Book"),
                ("three", "The Creative Act", "Movie"),
            ):
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://www.instagram.com/reel/{suffix}/",
                        "metadata": {},
                        "resolved_things": [
                            {
                                "status": "not_applicable",
                                "extracted": {
                                    "extracted_name": name,
                                    "type_name": thing_type,
                                },
                            }
                        ],
                    },
                )

            things = list_things(db_path)
            self.assertEqual(len(things), 2)
            book = next(thing for thing in things if thing["type"] == "Book")
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
                    "resolved_things": [
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

            self.assertEqual(list_thing_types(db_path), ["Unknown"])

    def test_saves_non_location_thing_and_preserves_source(self) -> None:
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
                    "things_extracted": [
                        {
                            "extracted_name": "The Creative Act",
                            "type_name": "Book",
                            "description": "A book about creativity.",
                        }
                    ],
                    "resolved_things": [
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

            things = list_things(db_path)
            self.assertEqual(things[0]["type"], "Book")
            self.assertIsNone(things[0]["latitude"])
            self.assertEqual(things[0]["resolution_status"], "not_applicable")

            sources = list_sources(db_path)
            self.assertEqual(sources[0]["thing_count"], 1)
            self.assertTrue(sources[0]["media_preserved"])
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
                    "things_extracted": [],
                    "resolved_things": [],
                },
            )

            self.assertEqual(list_things(db_path), [])
            sources = list_sources(db_path)
            self.assertEqual(len(sources), 1)
            self.assertTrue(sources[0]["needs_review"])

    def test_delete_thing_preserves_its_source(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/book/",
                    "metadata": {},
                    "resolved_things": [
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
            thing = list_things(db_path)[0]

            result = delete_thing(db_path, thing["id"])

            self.assertEqual(result, {"deleted_things": 1, "deleted_sources": 0})
            self.assertEqual(list_things(db_path), [])
            self.assertEqual(len(list_sources(db_path)), 1)
            self.assertTrue(list_sources(db_path)[0]["needs_review"])

    def test_delete_things_removes_exact_card_rows_and_preserves_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            for suffix, name in (("one", "S&P Lunch"), ("two", "S&P Lunch")):
                save_ingest(
                    db_path,
                    {
                        "source_url": f"https://www.instagram.com/reel/{suffix}/",
                        "metadata": {},
                        "resolved_things": [self.resolved_place(name, "places/shared")],
                    },
                )
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/exhibit/",
                    "metadata": {},
                    "resolved_things": [
                        self.resolved_place("Guest Pop-Up", "places/shared")
                    ],
                },
            )
            things = list_things(db_path)
            restaurant_ids = [thing["id"] for thing in things if thing["name"] == "S&P Lunch"]

            result = delete_things(db_path, restaurant_ids)

            self.assertEqual(result, {"deleted_things": 1, "deleted_sources": 0})
            self.assertEqual([thing["name"] for thing in list_things(db_path)], ["Guest Pop-Up"])
            self.assertEqual(len(list_sources(db_path)), 3)

    def test_delete_things_is_atomic_when_any_id_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/one/",
                    "metadata": {},
                    "resolved_things": [self.resolved_place("Keep Me", "places/keep")],
                },
            )
            thing = list_things(db_path)[0]

            self.assertIsNone(delete_things(db_path, [thing["id"], 999]))
            self.assertEqual([saved["name"] for saved in list_things(db_path)], ["Keep Me"])

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
