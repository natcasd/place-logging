from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from ingest_service import IngestService


class IngestServiceTests(unittest.TestCase):
    @patch("ingest_service.list_thing_types", return_value=["Place", "Book"])
    @patch("ingest_service.save_ingest", return_value=42)
    @patch("ingest_service.process_ingest")
    def test_processes_and_persists_canonical_result(
        self,
        mock_process,
        mock_save,
        mock_types,
    ) -> None:
        processed = {
            "source_url": "https://youtu.be/test",
            "user_prompt": None,
            "metadata": {},
            "places_extracted": [],
            "resolved_places": [],
        }
        mock_process.return_value = processed
        service = IngestService(Path("/tmp/test.db"), Path("/tmp/downloads"))

        result = service.ingest("https://youtu.be/test")

        mock_process.assert_called_once_with(
            "https://youtu.be/test",
            None,
            Path("/tmp/downloads"),
            ["Place", "Book"],
        )
        mock_types.assert_called_once_with(Path("/tmp/test.db"))
        mock_save.assert_called_once_with(Path("/tmp/test.db"), processed)
        self.assertEqual(result["item_id"], 42)
        self.assertEqual(result["source_url"], "https://youtu.be/test")

    @patch(
        "ingest_service.delete_place",
        return_value={"deleted_places": 2, "deleted_items": 1},
    )
    def test_deletes_logical_place(self, mock_delete) -> None:
        service = IngestService(Path("/tmp/test.db"), Path("/tmp/downloads"))

        result = service.delete_place(7)

        mock_delete.assert_called_once_with(Path("/tmp/test.db"), 7)
        self.assertEqual(result, {"deleted_places": 2, "deleted_items": 1})

    @patch(
        "ingest_service.delete_thing",
        return_value={"deleted_things": 1, "deleted_sources": 0},
    )
    def test_deletes_thing_without_deleting_source(self, mock_delete) -> None:
        service = IngestService(Path("/tmp/test.db"), Path("/tmp/downloads"))

        result = service.delete_thing(8)

        mock_delete.assert_called_once_with(Path("/tmp/test.db"), 8)
        self.assertEqual(result, {"deleted_things": 1, "deleted_sources": 0})


if __name__ == "__main__":
    unittest.main()
