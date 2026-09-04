from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
import sqlite3

from backfill_controlled_types import apply_plan, create_plan
from store import init_db, list_things, save_ingest


class ControlledTypeBackfillTests(unittest.TestCase):
    def _save(self, db_path: Path, suffix: str, name: str, thing_type: str) -> None:
        save_ingest(
            db_path,
            {
                "source_url": f"https://www.instagram.com/reel/{suffix}/",
                "metadata": {"source_platform": "instagram"},
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

    def test_rebuilds_controlled_types_and_keeps_sources(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "places.db"
            init_db(db_path)
            self._save(db_path, "deli", "S&P Lunch", "Deli")
            self._save(db_path, "dessert", "Run On Clouds Slushie", "Dessert")
            self._save(db_path, "city", "Oslo", "City")
            con = sqlite3.connect(db_path)
            con.execute("UPDATE places SET thing_type = 'Deli' WHERE extracted_name = 'S&P Lunch'")
            con.commit()
            con.close()

            plan = create_plan(db_path)

            self.assertEqual(plan["summary"]["delete_count"], 1)
            self.assertEqual(plan["summary"]["update_count"], 2)
            self.assertEqual(plan["summary"]["updates_by_target_type"], {
                "Pop-up": 1,
                "Restaurant": 1,
            })

            result = apply_plan(db_path, plan)

            self.assertEqual(result["deleted_occurrences"], 1)
            self.assertEqual(result["updated_occurrences"], 2)
            self.assertTrue(Path(result["backup_path"]).exists())
            things = list_things(db_path)
            self.assertEqual(
                [(thing["name"], thing["type"]) for thing in things],
                [("Run On Clouds Slushie", "Pop-up"), ("S&P Lunch", "Restaurant")],
            )


if __name__ == "__main__":
    unittest.main()
