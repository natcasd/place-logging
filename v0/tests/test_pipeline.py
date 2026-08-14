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
    def test_prefers_full_720p_video_near_target_bitrate(self) -> None:
        info = {
            "formats": [
                {
                    "format_id": "audio",
                    "vcodec": "none",
                    "acodec": "aac",
                    "abr": 76,
                },
                {
                    "format_id": "video-low",
                    "vcodec": "vp9",
                    "acodec": "none",
                    "width": 720,
                    "height": 1280,
                    "vbr": 1496,
                },
                {
                    "format_id": "video-target",
                    "vcodec": "vp9",
                    "acodec": "none",
                    "width": 720,
                    "height": 1280,
                    "vbr": 2064,
                },
                {
                    "format_id": "video-too-large",
                    "vcodec": "vp9",
                    "acodec": "none",
                    "width": 720,
                    "height": 1280,
                    "vbr": 2883,
                },
                {
                    "format_id": "video-1080p",
                    "vcodec": "vp9",
                    "acodec": "none",
                    "width": 1080,
                    "height": 1920,
                    "vbr": 5600,
                },
            ]
        }

        self.assertEqual(
            pipeline._preferred_instagram_format(info),
            "video-target+audio",
        )

    def test_falls_back_when_separate_tracks_are_unavailable(self) -> None:
        info = {
            "formats": [
                {
                    "format_id": "progressive",
                    "vcodec": "unknown",
                    "acodec": "unknown",
                }
            ]
        }

        self.assertIsNone(pipeline._preferred_instagram_format(info))

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

        command = mock_run.call_args_list[-1].args[0]
        self.assertEqual(command[:3], [sys.executable, "-m", "yt_dlp"])

    @patch("pipeline.subprocess.run")
    def test_downloads_selected_video_and_audio_tracks(
        self,
        mock_run: MagicMock,
    ) -> None:
        probe_info = {
            "formats": [
                {
                    "format_id": "audio",
                    "vcodec": "none",
                    "acodec": "aac",
                    "abr": 76,
                },
                {
                    "format_id": "video",
                    "vcodec": "vp9",
                    "acodec": "none",
                    "width": 720,
                    "height": 1280,
                    "vbr": 2000,
                },
            ]
        }
        mock_run.side_effect = [
            SimpleNamespace(
                returncode=0,
                stderr="",
                stdout=json.dumps(probe_info),
            ),
            SimpleNamespace(
                returncode=1,
                stderr="download failed",
                stdout="",
            ),
        ]

        with self.assertRaisesRegex(RuntimeError, "download failed"):
            pipeline.fetch(
                "https://www.instagram.com/reel/abc/",
                Path("/tmp/place-logging-fetch-test"),
            )

        command = mock_run.call_args_list[1].args[0]
        self.assertIn("-f", command)
        self.assertEqual(command[command.index("-f") + 1], "video+audio")
        self.assertIn("--merge-output-format", command)


class InstagramExtractionTests(unittest.TestCase):
    @patch("pipeline.types.Part.from_bytes")
    @patch("pipeline._client")
    def test_sends_video_inline_to_gemini(
        self,
        mock_client: MagicMock,
        mock_from_bytes: MagicMock,
    ) -> None:
        video_part = object()
        mock_from_bytes.return_value = video_part
        mock_client.return_value.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps({"places": [{"extracted_name": "Test Place"}]})
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "post.mp4"
            video.write_bytes(b"test-video")

            places = pipeline.extract(video, {"webpage_url": "instagram"})

        mock_from_bytes.assert_called_once_with(
            data=b"test-video",
            mime_type="video/mp4",
        )
        call = mock_client.return_value.models.generate_content.call_args.kwargs
        self.assertIs(call["contents"][0], video_part)
        self.assertEqual(places[0]["extracted_name"], "Test Place")
        mock_client.return_value.files.upload.assert_not_called()

    @patch("pipeline._client")
    def test_rejects_video_above_safe_inline_limit(
        self,
        mock_client: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            video = Path(temp_dir) / "large.mp4"
            with video.open("wb") as handle:
                handle.truncate(pipeline.MAX_INLINE_VIDEO_BYTES + 1)

            with self.assertRaisesRegex(ValueError, "too large"):
                pipeline.extract(video, {})

        mock_client.return_value.models.generate_content.assert_not_called()


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
