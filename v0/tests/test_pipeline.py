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
        properties = pipeline.EXTRACTION_RESPONSE_SCHEMA["properties"]["places"]["items"]["properties"]
        self.assertIn("timestamp_seconds", properties)
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

    @patch("pipeline.requests.get")
    @patch("pipeline.subprocess.run")
    def test_downloads_single_image_from_best_thumbnail(
        self,
        mock_run: MagicMock,
        mock_get: MagicMock,
    ) -> None:
        mock_run.return_value = SimpleNamespace(
            returncode=0,
            stderr="",
            stdout=json.dumps(
                {
                    "id": "image-post",
                    "description": "Caption names Test Cafe",
                    "webpage_url": "https://www.instagram.com/p/image-post/",
                    "formats": [],
                    "thumbnails": [
                        {"url": "https://cdn.example/small.jpg"},
                        {"url": "https://cdn.example/original.jpg"},
                    ],
                }
            ),
        )
        mock_get.return_value.content = b"full-size-image"

        with tempfile.TemporaryDirectory() as temp_dir:
            fetched = pipeline.fetch(
                "https://www.instagram.com/p/image-post/",
                Path(temp_dir),
            )
            try:
                self.assertEqual(len(fetched.media_paths), 1)
                self.assertEqual(fetched.media_paths[0].read_bytes(), b"full-size-image")
                self.assertEqual(
                    fetched.metadata["caption_or_description"],
                    "Caption names Test Cafe",
                )
                self.assertEqual(fetched.metadata["media_types"], ["image"])
            finally:
                pipeline.shutil.rmtree(fetched.cleanup_dir)

        mock_get.assert_called_once()
        self.assertEqual(
            mock_get.call_args.args[0],
            "https://cdn.example/original.jpg",
        )

    @patch("pipeline.requests.get")
    @patch("pipeline.subprocess.run")
    def test_downloads_mixed_carousel_in_slide_order(
        self,
        mock_run: MagicMock,
        mock_get: MagicMock,
    ) -> None:
        probe = {
            "_type": "playlist",
            "id": "carousel",
            "description": "Slide one caption\n\nSlide two caption",
            "entries": [
                {
                    "id": "image-slide",
                    "formats": [],
                    "thumbnail": "https://cdn.example/image.jpg",
                },
                {
                    "id": "video-slide",
                    "formats": [{"format_id": "video"}],
                },
            ],
        }

        def run_command(command: list[str], **_: object) -> SimpleNamespace:
            if "--skip-download" in command:
                return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps(probe))
            template = Path(command[command.index("-o") + 1])
            video = Path(
                str(template)
                .replace("%(playlist_index)03d", "002")
                .replace("%(id)s", "video-slide")
                .replace("%(ext)s", "mp4")
            )
            video.write_bytes(b"video")
            return SimpleNamespace(
                returncode=1,
                stderr="No video formats found for image slide",
                stdout=str(video),
            )

        mock_run.side_effect = run_command
        mock_get.return_value.content = b"image"

        with tempfile.TemporaryDirectory() as temp_dir:
            fetched = pipeline.fetch(
                "https://www.instagram.com/p/carousel/",
                Path(temp_dir),
            )
            try:
                self.assertEqual(
                    [path.name for path in fetched.media_paths],
                    ["001-image-slide.jpg", "002-video-slide.mp4"],
                )
                self.assertEqual(fetched.metadata["media_count"], 2)
                self.assertEqual(fetched.metadata["media_types"], ["image", "video"])
            finally:
                pipeline.shutil.rmtree(fetched.cleanup_dir)


class InstagramExtractionTests(unittest.TestCase):
    def test_normalizes_media_references_against_carousel_shape(self) -> None:
        places = [
            {
                "extracted_name": "Video Place",
                "slide_index": 2,
                "timestamp_seconds": 4.5,
            },
            {
                "extracted_name": "Image Place",
                "slide_index": 1,
                "timestamp_seconds": 9,
            },
            {
                "extracted_name": "Bad Slide",
                "slide_index": 8,
                "timestamp_seconds": -2,
            },
        ]

        normalized = pipeline._normalize_media_references(
            places,
            {
                "source_platform": "instagram",
                "media_types": ["image", "video"],
            },
        )

        self.assertEqual(normalized[0]["slide_index"], 2)
        self.assertEqual(normalized[0]["timestamp_seconds"], 4.5)
        self.assertNotIn("timestamp_seconds", normalized[1])
        self.assertNotIn("slide_index", normalized[2])
        self.assertNotIn("timestamp_seconds", normalized[2])

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

    @patch("pipeline.types.Part.from_bytes")
    @patch("pipeline._client")
    def test_sends_all_carousel_media_and_combined_caption_to_gemini(
        self,
        mock_client: MagicMock,
        mock_from_bytes: MagicMock,
    ) -> None:
        image_part = object()
        video_part = object()
        mock_from_bytes.side_effect = [image_part, video_part]
        mock_client.return_value.models.generate_content.return_value = SimpleNamespace(
            text=json.dumps({"places": []})
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            image = Path(temp_dir) / "001-slide.jpg"
            video = Path(temp_dir) / "002-slide.mp4"
            image.write_bytes(b"image")
            video.write_bytes(b"video")
            pipeline.extract(
                [image, video],
                {
                    "caption_or_description": "First caption\n\nSecond caption",
                    "media_types": ["image", "video"],
                },
            )

        call = mock_client.return_value.models.generate_content.call_args.kwargs
        self.assertEqual(call["contents"][:2], [image_part, video_part])
        self.assertIn("First caption", call["contents"][2])
        self.assertIn("Second caption", call["contents"][2])
        self.assertIn("combined carousel captions", call["contents"][2])
        self.assertIn("slide_index", call["contents"][2])
        self.assertIn("1-based slide number", call["contents"][2])

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
            cleanup_dir = Path(temp_dir) / "instagram-ingest"
            cleanup_dir.mkdir()
            video = cleanup_dir / "post.mp4"
            info = video.with_suffix(".info.json")
            video.touch()
            info.touch()
            mock_fetch.return_value = pipeline.InstagramFetch(
                [video],
                {"webpage_url": "instagram"},
                cleanup_dir,
            )
            mock_extract.return_value = [{"extracted_name": "Test Place"}]
            mock_resolve.return_value = {"status": "unresolved", "reason": "test"}

            pipeline.process_ingest(
                "https://www.instagram.com/reel/abc/",
                None,
                Path(temp_dir),
            )

            self.assertFalse(video.exists())
            self.assertFalse(cleanup_dir.exists())
            self.assertTrue(Path(temp_dir).exists())


if __name__ == "__main__":
    unittest.main()
