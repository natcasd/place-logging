from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pipeline


class FakeGeminiError(RuntimeError):
    def __init__(self, code: int) -> None:
        super().__init__(f"Gemini error {code}")
        self.code = code


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
    def test_prompt_requires_main_intent_and_uses_fixed_types(self) -> None:
        prompt = pipeline._extraction_prompt(
            {"caption_or_description": "An exhibit at a museum."},
            existing_types=["Place", "Unknown", "Restaurant", "Restaurant", "Exhibit"],
        )

        self.assertIn("part of the post's main intent", prompt)
        self.assertIn("Do not also save the host venue as a separate thing", prompt)
        self.assertIn("supplier, neighboring business, collaborator, or partner", prompt)
        self.assertIn("closing call-to-action", prompt)
        self.assertIn("Never use a generic class as its name", prompt)
        self.assertIn("Restaurant, Café, Bar, Bakery", prompt)
        self.assertIn("Use Exhibit for a museum or gallery exhibition", prompt)
        self.assertNotIn("Existing specific type names", prompt)

    def test_schema_allows_only_controlled_types(self) -> None:
        type_schema = pipeline.EXTRACTION_RESPONSE_SCHEMA["properties"]["things"]["items"]["properties"]["type_name"]

        self.assertEqual(type_schema["enum"], list(pipeline.THING_TYPES))

    def test_removes_generic_unnamed_things(self) -> None:
        things = pipeline._remove_generic_thing_names(
            [
                {"extracted_name": "Cafe", "type_name": "Café"},
                {"extracted_name": "Theodora", "type_name": "Restaurant"},
                {"extracted_name": "Place", "type_name": "Unknown"},
            ]
        )

        self.assertEqual(things, [{"extracted_name": "Theodora", "type_name": "Restaurant"}])

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
        properties = pipeline.EXTRACTION_RESPONSE_SCHEMA["properties"]["things"]["items"]["properties"]
        self.assertIn("timestamp_seconds", properties)
        self.assertIn("type_name", properties)
        self.assertIn("description", properties)
        self.assertIn("location_query", properties)
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


class GeminiRetryTests(unittest.TestCase):
    @patch("pipeline.time.sleep")
    def test_retries_transient_errors_with_exponential_backoff(
        self,
        mock_sleep: MagicMock,
    ) -> None:
        operation = MagicMock(
            side_effect=[FakeGeminiError(503), FakeGeminiError(429), "ok"]
        )

        result = pipeline._call_gemini_with_retry(operation, "test extraction")

        self.assertEqual(result, "ok")
        self.assertEqual(operation.call_count, 3)
        self.assertEqual(
            [call.args[0] for call in mock_sleep.call_args_list],
            [3.0, 6.0],
        )

    @patch("pipeline.time.sleep")
    def test_does_not_retry_permanent_errors(self, mock_sleep: MagicMock) -> None:
        operation = MagicMock(side_effect=FakeGeminiError(400))

        with self.assertRaises(FakeGeminiError):
            pipeline._call_gemini_with_retry(operation, "test extraction")

        operation.assert_called_once_with()
        mock_sleep.assert_not_called()


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
    @patch.dict("pipeline.os.environ", {"GOOGLE_PLACES_API_KEY": "test"})
    def test_temporary_thing_rejects_unmatched_single_google_candidate(self) -> None:
        thing = {
            "extracted_name": "HiFi Pursuit Listening Room Dream No. 3",
            "type_name": "Exhibit",
            "location_query": "HiFi Pursuit Listening Room Dream No. 3, New York City",
            "ends_at": "2026-09-08",
        }
        response = MagicMock(ok=True)
        response.json.return_value = {
            "places": [{"displayName": {"text": "OJAS Listening Room"}}]
        }

        with patch("pipeline.requests.post", return_value=response):
            result = pipeline.resolve(thing)

        self.assertEqual(result["status"], "needs_review")
        self.assertIn("does not match", result["reason"])

    @patch.dict("pipeline.os.environ", {"GOOGLE_PLACES_API_KEY": "test"})
    def test_temporary_thing_accepts_matching_host_venue(self) -> None:
        thing = {
            "extracted_name": "HiFi Pursuit Listening Room Dream No. 3",
            "type_name": "Exhibit",
            "location_query": "Cooper Hewitt, Smithsonian Design Museum, New York City",
            "ends_at": "2026-09-08",
        }
        candidate = {"displayName": {"text": "Cooper Hewitt, Smithsonian Design Museum"}}
        response = MagicMock(ok=True)
        response.json.return_value = {"places": [candidate]}

        with patch("pipeline.requests.post", return_value=response):
            result = pipeline.resolve(thing)

        self.assertEqual(result, {"status": "auto", "place": candidate})

    @patch("pipeline.resolve")
    @patch("pipeline.extract_youtube_bundle")
    def test_youtube_bypasses_downloader(
        self,
        mock_extract: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        mock_extract.return_value = {
            "source_content": {"summary": "A test post."},
            "things": [{"extracted_name": "Test Place"}],
        }
        mock_resolve.return_value = {"status": "unresolved", "reason": "test"}
        stages = []

        with patch("pipeline.fetch") as mock_fetch:
            result = pipeline.process_ingest(
                "https://youtu.be/abc",
                None,
                Path("/unused"),
                progress=stages.append,
            )

        mock_fetch.assert_not_called()
        self.assertEqual(result["metadata"]["source_platform"], "youtube")
        self.assertEqual(result["metadata"]["source_content"]["summary"], "A test post.")
        self.assertEqual(
            result["places_extracted"][0]["extracted_name"],
            "Test Place",
        )
        self.assertEqual(stages, ["extracting", "resolving"])

    def test_tiktok_fails_with_clear_temporary_message(self) -> None:
        with self.assertRaisesRegex(NotImplementedError, "temporarily unavailable"):
            pipeline.process_ingest(
                "https://www.tiktok.com/@user/video/123",
                None,
                Path("/unused"),
            )

    def test_non_location_thing_skips_google_places(self) -> None:
        thing = {
            "extracted_name": "The Creative Act",
            "type_name": "Book",
            "description": "A book to read.",
            "location_query": "",
        }

        with patch("pipeline.requests.post") as mock_post:
            result = pipeline.resolve(thing)

        self.assertEqual(result["status"], "not_applicable")
        mock_post.assert_not_called()

    def test_new_thing_without_location_query_skips_google_places(self) -> None:
        thing = {
            "extracted_name": "A Song",
            "type_name": "Song",
            "description": "A song from the Reel.",
        }

        with patch("pipeline.requests.post") as mock_post:
            result = pipeline.resolve(thing)

        self.assertEqual(result["status"], "not_applicable")
        mock_post.assert_not_called()

    @patch("pipeline.resolve")
    @patch("pipeline.extract_bundle")
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
            mock_extract.return_value = {
                "source_content": {"summary": "A test post."},
                "things": [{"extracted_name": "Test Place"}],
            }
            mock_resolve.return_value = {"status": "unresolved", "reason": "test"}
            stages = []

            pipeline.process_ingest(
                "https://www.instagram.com/reel/abc/",
                None,
                Path(temp_dir),
                progress=stages.append,
            )

            self.assertFalse(video.exists())
            self.assertFalse(cleanup_dir.exists())
            self.assertTrue(Path(temp_dir).exists())
            source_dirs = list((Path(temp_dir) / "sources").iterdir())
            self.assertEqual(len(source_dirs), 1)
            self.assertTrue((source_dirs[0] / "001-post.mp4").exists())
            self.assertEqual(
                stages,
                ["fetching", "archiving", "extracting", "resolving"],
            )

    @patch("pipeline.resolve")
    @patch("pipeline.archive_media", side_effect=OSError("disk full"))
    @patch("pipeline.extract_bundle")
    @patch("pipeline.fetch")
    def test_archive_failure_still_preserves_source_record_payload(
        self,
        mock_fetch: MagicMock,
        mock_extract: MagicMock,
        _mock_archive: MagicMock,
        mock_resolve: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cleanup_dir = Path(temp_dir) / "instagram-ingest"
            cleanup_dir.mkdir()
            video = cleanup_dir / "post.mp4"
            video.touch()
            mock_fetch.return_value = pipeline.InstagramFetch(
                [video],
                {"source_platform": "instagram"},
                cleanup_dir,
            )
            mock_extract.return_value = {
                "source_content": {"summary": "Preserved analysis"},
                "things": [{"extracted_name": "Test Thing"}],
            }
            mock_resolve.return_value = {"status": "not_applicable"}

            with self.assertLogs("pipeline", level="ERROR"):
                result = pipeline.process_ingest(
                    "https://www.instagram.com/reel/abc/",
                    None,
                    Path(temp_dir),
                )

            self.assertFalse(result["metadata"]["media_preserved"])
            self.assertEqual(
                result["metadata"]["source_content"]["summary"],
                "Preserved analysis",
            )
            self.assertFalse(cleanup_dir.exists())

    @patch("pipeline.archive_media")
    @patch("pipeline.extract_bundle", side_effect=FakeGeminiError(503))
    @patch("pipeline.fetch")
    def test_exhausted_extraction_preserves_archived_source_for_review(
        self,
        mock_fetch: MagicMock,
        _mock_extract: MagicMock,
        mock_archive: MagicMock,
    ) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            cleanup_dir = Path(temp_dir) / "instagram-ingest"
            cleanup_dir.mkdir()
            video = cleanup_dir / "post.mp4"
            video.touch()
            mock_fetch.return_value = pipeline.InstagramFetch(
                [video],
                {
                    "source_platform": "instagram",
                    "caption_or_description": "Saved caption",
                },
                cleanup_dir,
            )
            mock_archive.return_value = [
                {"path": "/data/downloads/sources/post.mp4", "bytes": 100}
            ]

            with self.assertLogs("pipeline", level="ERROR"):
                result = pipeline.process_ingest(
                    "https://www.instagram.com/reel/abc/",
                    None,
                    Path(temp_dir),
                )

            self.assertEqual(result["things_extracted"], [])
            self.assertEqual(result["resolved_things"], [])
            self.assertEqual(result["metadata"]["extraction_status"], "failed")
            self.assertEqual(
                result["metadata"]["extraction_error"]["type"],
                "FakeGeminiError",
            )
            self.assertTrue(result["metadata"]["media_preserved"])
            self.assertEqual(
                result["metadata"]["caption_or_description"],
                "Saved caption",
            )
            self.assertFalse(cleanup_dir.exists())


if __name__ == "__main__":
    unittest.main()
