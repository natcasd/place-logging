from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from backfill_location_names import apply_plan, create_plan, fetch_display_name
from store import init_db, save_ingest


class LocationNameBackfillTests(unittest.TestCase):
    @staticmethod
    def _response(
        status_code: int,
        payload: dict | None = None,
        *,
        text: str = "",
        headers: dict | None = None,
    ) -> Mock:
        response = Mock()
        response.status_code = status_code
        response.ok = 200 <= status_code < 300
        response.json.return_value = payload or {}
        response.text = text
        response.headers = headers or {}
        return response

    def test_fetch_display_name_retries_transient_failure(self) -> None:
        request_get = Mock(
            side_effect=[
                self._response(503, text="busy"),
                self._response(
                    200,
                    {"displayName": {"text": "The Metropolitan Museum of Art"}},
                ),
            ]
        )
        sleep = Mock()

        name = fetch_display_name(
            "ChIJmet",
            "test-key",
            request_get=request_get,
            sleep=sleep,
        )

        self.assertEqual(name, "The Metropolitan Museum of Art")
        self.assertEqual(request_get.call_count, 2)
        sleep.assert_called_once_with(1)
        self.assertEqual(
            request_get.call_args.kwargs["headers"]["X-Goog-FieldMask"],
            "id,displayName",
        )

    def test_plan_and_apply_backfill_locations_and_legacy_rows(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            db_path = root / "places.db"
            plan_path = root / "plan.json"
            init_db(db_path)
            save_ingest(
                db_path,
                {
                    "source_url": "https://www.instagram.com/reel/met/",
                    "metadata": {},
                    "resolved_things": [
                        {
                            "status": "resolved",
                            "extracted": {
                                "extracted_name": "Giacometti in the Temple of Dendur",
                                "type_name": "Exhibit",
                                "ends_at": "2026-09-08",
                            },
                            "place": {"id": "ChIJmet"},
                        }
                    ],
                },
            )
            request_get = Mock(
                return_value=self._response(
                    200,
                    {"displayName": {"text": "The Metropolitan Museum of Art"}},
                )
            )

            plan = create_plan(
                db_path,
                plan_path,
                "test-key",
                request_get=request_get,
                sleep=Mock(),
            )
            applied, backup_path = apply_plan(db_path, plan_path, root / "backups")

            self.assertTrue(plan["complete"])
            self.assertEqual(applied, 1)
            self.assertTrue(backup_path.exists())
            con = sqlite3.connect(db_path)
            try:
                self.assertEqual(
                    con.execute("SELECT display_name FROM locations").fetchone()[0],
                    "The Metropolitan Museum of Art",
                )
                self.assertEqual(
                    con.execute("SELECT location_name FROM places").fetchone()[0],
                    "The Metropolitan Museum of Art",
                )
            finally:
                con.close()
            saved_plan = json.loads(plan_path.read_text())
            self.assertEqual(saved_plan["candidate_count"], 1)


if __name__ == "__main__":
    unittest.main()
