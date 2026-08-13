from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from store import init_db, list_places, save_ingest


class StoreTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
