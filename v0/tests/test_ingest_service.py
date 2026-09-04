from __future__ import annotations

import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from ingest_service import IngestService
from store import list_ingest_runs


class IngestServiceTests(unittest.TestCase):
    @patch("ingest_service.process_ingest")
    def test_processes_and_persists_canonical_result(
        self,
        mock_process,
    ) -> None:
        processed = {
            "source_url": "https://youtu.be/test",
            "user_prompt": None,
            "metadata": {"source_platform": "youtube"},
            "places_extracted": [],
            "resolved_places": [
                {
                    "status": "not_applicable",
                    "extracted": {
                        "extracted_name": "The Creative Act",
                        "type_name": "Book",
                    },
                }
            ],
        }
        def process(*args, **kwargs):
            kwargs["progress"]("extracting")
            return processed

        mock_process.side_effect = process
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            service = IngestService(db_path, Path(temp_dir) / "downloads")
            service.initialize()

            result = service.ingest("https://youtu.be/test")

            self.assertEqual(result["source_url"], "https://youtu.be/test")
            self.assertEqual(result["saved_things"][0]["name"], "The Creative Act")
            self.assertTrue(result["saved_things"][0]["is_new"])
            self.assertFalse(result["already_logged"])
            activity = list_ingest_runs(db_path)
            self.assertEqual(activity[0]["status"], "completed")
            self.assertEqual(
                [event["stage"] for event in activity[0]["events"]],
                ["accepted", "extracting", "saving", "completed"],
            )

    @patch("ingest_service.process_ingest", side_effect=RuntimeError("boom"))
    def test_persists_failed_processing_run(self, mock_process) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            service = IngestService(db_path, Path(temp_dir) / "downloads")
            service.initialize()

            with self.assertRaisesRegex(RuntimeError, "boom"):
                service.ingest("https://youtu.be/test")

            activity = list_ingest_runs(db_path)
            self.assertEqual(activity[0]["status"], "failed")
            self.assertEqual(activity[0]["stage"], "accepted")
            self.assertEqual(activity[0]["error_message"], "boom")

    @patch("ingest_service.process_ingest")
    def test_same_processed_post_is_not_processed_or_saved_again(self, mock_process) -> None:
        processed = {
            "source_url": "https://www.instagram.com/reel/duplicate/",
            "user_prompt": None,
            "metadata": {"source_platform": "instagram", "extraction_status": "complete"},
            "places_extracted": [],
            "resolved_places": [],
        }
        mock_process.return_value = processed
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            service = IngestService(db_path, Path(temp_dir) / "downloads")
            service.initialize()

            first = service.ingest("https://www.instagram.com/reel/duplicate/")
            second = service.ingest(
                "https://instagram.com/reel/duplicate/?igsh=tracking"
            )

            self.assertFalse(first["already_logged"])
            self.assertTrue(second["already_logged"])
            self.assertEqual(second["item_id"], first["item_id"])
            self.assertEqual(second["ingest_id"], first["ingest_id"])
            self.assertEqual(mock_process.call_count, 1)
            self.assertEqual(len(list_ingest_runs(db_path)), 1)

    @patch("ingest_service.process_ingest")
    def test_retries_source_whose_saved_extraction_failed(self, mock_process) -> None:
        failed = {
            "source_url": "https://www.instagram.com/reel/retry/",
            "user_prompt": None,
            "metadata": {"source_platform": "instagram", "extraction_status": "failed"},
            "places_extracted": [],
            "resolved_places": [],
        }
        completed = {
            **failed,
            "metadata": {"source_platform": "instagram", "extraction_status": "complete"},
        }
        mock_process.side_effect = [failed, completed]
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "test.db"
            service = IngestService(db_path, Path(temp_dir) / "downloads")
            service.initialize()

            first = service.ingest(failed["source_url"])
            second = service.ingest(failed["source_url"])

            self.assertFalse(first["already_logged"])
            self.assertFalse(second["already_logged"])
            self.assertNotEqual(second["item_id"], first["item_id"])
            self.assertEqual(mock_process.call_count, 2)
            self.assertEqual(len(list_ingest_runs(db_path)), 2)

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

    @patch(
        "ingest_service.delete_things",
        return_value={"deleted_things": 3, "deleted_sources": 0},
    )
    def test_deletes_logical_thing_card_without_deleting_sources(self, mock_delete) -> None:
        service = IngestService(Path("/tmp/test.db"), Path("/tmp/downloads"))

        result = service.delete_things([8, 9, 10])

        mock_delete.assert_called_once_with(Path("/tmp/test.db"), [8, 9, 10])
        self.assertEqual(result, {"deleted_things": 3, "deleted_sources": 0})


if __name__ == "__main__":
    unittest.main()
