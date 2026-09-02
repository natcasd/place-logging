from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from store import (
    delete_place,
    delete_thing,
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
            self.assertEqual(places[0]["timestamp_seconds"], 12.5)
            self.assertEqual(places[0]["slide_index"], 3)
            self.assertEqual(places[0]["type"], "Restaurant")
            self.assertEqual(
                places[0]["description"],
                "A bakery with great bread and sandwiches.",
            )
            self.assertEqual(places[0]["ends_at"], "2026-09-30")
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
            con = sqlite3.connect(db_path)
            try:
                legacy = con.execute(
                    "SELECT google_place_id, thing_type FROM places WHERE id = 1"
                ).fetchone()
            finally:
                con.close()
            self.assertEqual(legacy, ("legacy", "Unknown"))
            self.assertEqual(len(list(Path(temp_dir).glob("*.pre-things-*.bak"))), 1)

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

            self.assertEqual(result, {"deleted_places": 2, "deleted_items": 1})
            remaining = list_places(db_path, 10)
            self.assertEqual([place["name"] for place in remaining], ["Keep Me"])
            self.assertEqual(
                remaining[0]["source_url"],
                "https://www.instagram.com/reel/multiple/",
            )
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
            finally:
                con.close()

    def test_delete_place_returns_none_for_unknown_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)

            self.assertIsNone(delete_place(db_path, 999))


if __name__ == "__main__":
    unittest.main()
