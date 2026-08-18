from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backfill_media_references as backfill
from store import init_db


class BackfillMediaReferencesTests(unittest.TestCase):
    def _database(self, root: Path) -> Path:
        db_path = root / "places.db"
        init_db(db_path)
        con = sqlite3.connect(db_path)
        con.execute(
            """INSERT INTO items
               (id, vertical, source_url, raw_payload_json, llm_output_json)
               VALUES (1, 'place', 'https://www.instagram.com/reel/example/', '{}', '[]')"""
        )
        for ordinal, name in enumerate(("First Place", "Second Place")):
            con.execute(
                """INSERT INTO places
                   (item_id, ordinal, extracted_name, resolution_status)
                   VALUES (1, ?, ?, 'auto')""",
                (ordinal, name),
            )
        con.commit()
        con.close()
        return db_path

    def test_finds_only_multi_place_media_without_references(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self._database(Path(temp_dir))
            con = sqlite3.connect(db_path)
            candidates = backfill.find_candidates(con)
            con.close()

            self.assertEqual([candidate["item_id"] for candidate in candidates], [1])
            self.assertEqual(len(candidates[0]["places"]), 2)

    def test_apply_plan_backs_up_and_only_sets_reference_columns(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = self._database(root)
            con = sqlite3.connect(db_path)
            place_rows = con.execute(
                "SELECT id, item_id, extracted_name FROM places ORDER BY ordinal"
            ).fetchall()
            con.close()
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "results": [
                            {
                                "updates": [
                                    {
                                        "place_id": place_rows[0][0],
                                        "item_id": place_rows[0][1],
                                        "extracted_name": place_rows[0][2],
                                        "timestamp_seconds": 8,
                                        "slide_index": None,
                                    }
                                ]
                            }
                        ],
                    }
                )
            )

            applied, backup_path = backfill.apply_plan(
                db_path, plan_path, root / "backups"
            )

            self.assertEqual(applied, 1)
            self.assertTrue(backup_path.exists())
            con = sqlite3.connect(db_path)
            row = con.execute(
                "SELECT extracted_name, timestamp_seconds, slide_index FROM places WHERE id = ?",
                (place_rows[0][0],),
            ).fetchone()
            con.close()
            self.assertEqual(row, ("First Place", 8.0, None))

    @patch("backfill_media_references._extract_for_candidate")
    def test_resume_skips_checkpointed_items(self, extract_mock) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = self._database(root)
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "database": str(db_path),
                        "candidate_count": 1,
                        "complete": False,
                        "results": [
                            {
                                "item_id": 1,
                                "source_url": "https://www.instagram.com/reel/example/",
                                "method": "gemini_extraction",
                                "updates": [],
                                "unresolved": ["First Place", "Second Place"],
                            }
                        ],
                    }
                )
            )

            plan = backfill.create_plan(
                db_path,
                root / "downloads",
                plan_path,
                resume=True,
            )

            extract_mock.assert_not_called()
            self.assertTrue(plan["complete"])
            self.assertEqual(len(plan["results"]), 1)


if __name__ == "__main__":
    unittest.main()
