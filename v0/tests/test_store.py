from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from store import delete_place, init_db, list_places, save_ingest


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
