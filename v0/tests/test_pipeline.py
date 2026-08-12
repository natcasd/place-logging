from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pipeline


class SourcePlatformTests(unittest.TestCase):
    def test_recognizes_supported_platforms(self) -> None:
        cases = {
            "https://youtu.be/abc": "youtube",
            "https://www.youtube.com/watch?v=abc": "youtube",
            "https://www.instagram.com/reel/abc/": "instagram",
            "https://vm.tiktok.com/abc/": "tiktok",
            "https://example.com/video": "other",
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                self.assertEqual(pipeline.source_platform(url), expected)


class YouTubeExtractionTests(unittest.TestCase):
    @patch("pipeline._client")
    def test_sends_youtube_url_directly_to_gemini(self, mock_client: MagicMock) -> None:
        response = SimpleNamespace(
            output_text=json.dumps(
                {"places": [{"extracted_name": "Mission Sandwich Social"}]}
            )
        )
        mock_client.return_value.interactions.create.return_value = response

        places = pipeline.extract_youtube_url(
            "https://www.youtube.com/watch?v=abc",
            "Focus on Brooklyn",
        )

        self.assertEqual(places[0]["extracted_name"], "Mission Sandwich Social")
        call = mock_client.return_value.interactions.create.call_args.kwargs
        self.assertEqual(
            call["input"][1],
            {
                "type": "video",
                "uri": "https://www.youtube.com/watch?v=abc",
            },
        )
        self.assertIn("Focus on Brooklyn", call["input"][0]["text"])
        self.assertFalse(call["store"])


class InstagramFetcherTests(unittest.TestCase):
    @patch("pipeline.subprocess.run")
    def test_uses_yt_dlp_from_running_python_environment(
        self,
        mock_run: MagicMock,
    ) -> None:
        mock_run.return_value = SimpleNamespace(
            returncode=1,
            stderr="download failed",
            stdout="",
        )

        with self.assertRaisesRegex(RuntimeError, "download failed"):
            pipeline.fetch(
                "https://www.instagram.com/reel/abc/",
                Path("/tmp/place-logging-fetch-test"),
            )

        command = mock_run.call_args.args[0]
        self.assertEqual(command[:3], [sys.executable, "-m", "yt_dlp"])


class ProcessIngestTests(unittest.TestCase):
    @patch("pipeline.resolve")
    @patch("pipeline.extract_youtube_url")
    def test_youtube_bypasses_downloader(
        self,
        mock_extract: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_extract.return_value = [{"extracted_name": "Test Place"}]
        mock_resolve.return_value = {"status": "unresolved", "reason": "test"}

        with patch("pipeline.fetch") as mock_fetch:
            result = pipeline.process_ingest(
                "https://youtu.be/abc",
                None,
                Path("/unused"),
            )

        mock_fetch.assert_not_called()
        self.assertEqual(result["metadata"]["source_platform"], "youtube")
        self.assertEqual(
            result["places_extracted"][0]["extracted_name"],
            "Test Place",
        )

    def test_tiktok_fails_with_clear_temporary_message(self) -> None:
        with self.assertRaisesRegex(NotImplementedError, "temporarily unavailable"):
            pipeline.process_ingest(
                "https://www.tiktok.com/@user/video/123",
                None,
                Path("/unused"),
            )

    @patch("pipeline.resolve")
    @patch("pipeline.extract")
    @patch("pipeline.fetch")
    def test_instagram_cleans_up_downloaded_files(
        self,
        mock_fetch: MagicMock,
        mock_extract: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "post.mp4"
            info = video.with_suffix(".info.json")
            video.touch()
            info.touch()
            mock_fetch.return_value = (video, {"webpage_url": "instagram"})
            mock_extract.return_value = [{"extracted_name": "Test Place"}]
            mock_resolve.return_value = {"status": "unresolved", "reason": "test"}

            pipeline.process_ingest(
                "https://www.instagram.com/reel/abc/",
                None,
                Path(temp_dir),
            )

            self.assertFalse(video.exists())
            self.assertFalse(info.exists())


if __name__ == "__main__":
    unittest.main()
