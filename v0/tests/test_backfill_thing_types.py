from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import backfill_thing_types as backfill
from store import init_db


class BackfillThingTypesTests(unittest.TestCase):
    def make_db(self, root: Path) -> Path:
        db_path = root / "places.db"
        init_db(db_path)
        con = sqlite3.connect(db_path)
        try:
            con.execute(
                """INSERT INTO items
                   (id, vertical, source_url, raw_payload_json)
                   VALUES (1, 'thing', 'https://example.com/post', ?)""",
                (
                    json.dumps(
                        {
                            "uploader": "creator",
                            "caption_or_description": "A restaurant and a museum.",
                            "source_content": {"summary": "Two recommendations."},
                        }
                    ),
                ),
            )
            con.execute(
                """INSERT INTO places
                   (id, item_id, ordinal, extracted_name, resolution_status,
                    thing_type, description, formatted_address)
                   VALUES (10, 1, 0, 'Dinner', 'auto', 'Place',
                           'A restaurant serving dinner.', 'New York, NY')"""
            )
            con.execute(
                """INSERT INTO places
                   (id, item_id, ordinal, extracted_name, resolution_status,
                    thing_type, description)
                   VALUES (11, 1, 1, 'Museum', 'auto', 'Museum', 'An art museum.')"""
            )
            con.commit()
        finally:
            con.close()
        return db_path

    def test_finds_only_generic_places_with_source_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = self.make_db(Path(temp_dir))
            con = sqlite3.connect(db_path)
            try:
                groups = backfill.find_candidates(con)
            finally:
                con.close()

            self.assertEqual(len(groups), 1)
            self.assertEqual(groups[0]["source_context"]["creator"], "creator")
            self.assertEqual(
                [thing["thing_id"] for thing in groups[0]["things"]],
                [10],
            )

    @patch("backfill_thing_types._client")
    def test_classifies_every_id_with_specific_type(self, mock_client: MagicMock) -> None:
        mock_client.return_value.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps(
                {
                    "classifications": [
                        {
                            "thing_id": 10,
                            "type_name": "restaurant",
                            "reason": "It serves dinner.",
                        }
                    ]
                }
            )
        )
        groups = [
            {
                "item_id": 1,
                "source_context": {},
                "things": [{"thing_id": 10, "name": "Dinner"}],
            }
        ]

        updates = backfill._classify_batch(groups, ["Place", "Restaurant"])

        self.assertEqual(updates[0]["type_name"], "Restaurant")
        prompt = mock_client.return_value.models.generate_content.call_args.kwargs[
            "contents"
        ][0]
        self.assertIn('Existing specific categories:\n["Restaurant"]', prompt)

    def test_apply_plan_backs_up_and_removes_generic_place(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = self.make_db(root)
            plan_path = root / "plan.json"
            plan_path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "candidate_count": 1,
                        "complete": True,
                        "results": [
                            {
                                "method": "gemini_classification",
                                "updates": [
                                    {
                                        "thing_id": 10,
                                        "type_name": "Restaurant",
                                        "reason": "It serves dinner.",
                                    }
                                ],
                            }
                        ],
                    }
                )
            )

            applied, backup_path = backfill.apply_plan(
                db_path,
                plan_path,
                root / "backups",
            )

            self.assertEqual(applied, 1)
            self.assertTrue(backup_path.exists())
            con = sqlite3.connect(db_path)
            try:
                rows = con.execute(
                    "SELECT id, thing_type FROM places ORDER BY id"
                ).fetchall()
            finally:
                con.close()
            self.assertEqual(rows, [(10, "Restaurant"), (11, "Museum")])


if __name__ == "__main__":
    unittest.main()
