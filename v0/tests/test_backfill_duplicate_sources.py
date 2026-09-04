from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from backfill_duplicate_sources import apply_plan, create_plan
from store import init_db, list_things, save_ingest


class DuplicateSourceBackfillTests(unittest.TestCase):
    @staticmethod
    def result(url: str, things: list[tuple[str, str | None]]) -> dict:
        resolved = []
        for name, google_id in things:
            resolved.append(
                {
                    "status": "resolved" if google_id else "needs_review",
                    "extracted": {
                        "extracted_name": name,
                        "type_name": "Restaurant",
                        "description": f"Try {name}.",
                    },
                    "place": (
                        {
                            "id": google_id,
                            "displayName": {"text": name},
                        }
                        if google_id
                        else {}
                    ),
                }
            )
        return {
            "source_url": url,
            "metadata": {"source_platform": "instagram", "extraction_status": "complete"},
            "resolved_things": resolved,
        }

    def test_keeps_newest_source_and_removes_redundant_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "places.db"
            init_db(db_path)
            save_ingest(db_path, self.result("https://instagram.com/reel/same/", [("Cafe", "g1")]))
            save_ingest(
                db_path,
                self.result(
                    "https://www.instagram.com/reel/same/?igsh=tracking",
                    [("Cafe", "g1")],
                ),
            )
            init_db(db_path)
            plan_path = root / "plan.json"

            plan = create_plan(db_path, plan_path)
            summary, backup = apply_plan(db_path, plan_path, root / "backups")

            self.assertEqual(plan["group_count"], 1)
            self.assertEqual(plan["groups"][0]["keeper"]["item_id"], 2)
            self.assertEqual(summary["items_removed"], 1)
            self.assertTrue(backup.exists())
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM items").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0], 1)
                self.assertFalse(con.execute("PRAGMA foreign_key_check").fetchall())
            finally:
                con.close()

    def test_preserves_recommendation_found_only_by_older_processing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                self.result(
                    "https://instagram.com/p/union/",
                    [("Main", "g-main"), ("Bonus", "g-bonus")],
                ),
            )
            save_ingest(
                db_path,
                self.result("https://instagram.com/p/union/?utm_source=x", [("Main", "g-main")]),
            )
            init_db(db_path)
            plan_path = root / "plan.json"
            create_plan(db_path, plan_path)

            apply_plan(db_path, plan_path, root / "backups")

            self.assertEqual({thing["name"] for thing in list_things(db_path)}, {"Main", "Bonus"})
            self.assertEqual(
                {source["item_id"] for thing in list_things(db_path) for source in thing["sources"]},
                {2},
            )

    def test_prefers_resolved_copy_of_same_thing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "places.db"
            init_db(db_path)
            save_ingest(
                db_path,
                self.result("https://instagram.com/p/resolved/", [("Court Street", None)]),
            )
            save_ingest(
                db_path,
                self.result("https://instagram.com/p/resolved/", [("Court Street", "g-court")]),
            )
            init_db(db_path)
            plan_path = root / "plan.json"
            create_plan(db_path, plan_path)

            summary, _ = apply_plan(db_path, plan_path, root / "backups")

            things = list_things(db_path)
            self.assertEqual(summary["things_removed"], 1)
            self.assertEqual(len(things), 1)
            self.assertEqual(things[0]["google_place_id"], "g-court")


if __name__ == "__main__":
    unittest.main()
