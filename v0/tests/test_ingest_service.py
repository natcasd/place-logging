from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from ingest_service import IngestService


class IngestServiceTests(unittest.TestCase):
    @patch("ingest_service.save_ingest", return_value=42)
    @patch("ingest_service.process_ingest")
    def test_processes_and_persists_canonical_result(
        self,
        mock_process,
        mock_save,
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
        )
        mock_save.assert_called_once_with(Path("/tmp/test.db"), processed)
        self.assertEqual(result["item_id"], 42)
        self.assertEqual(result["source_url"], "https://youtu.be/test")


if __name__ == "__main__":
    unittest.main()
