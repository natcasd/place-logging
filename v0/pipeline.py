"""
Ingest pipeline: source URL → platform ingest → Gemini extract → optional location resolve.

Stays synchronous for simplicity; bot.py offloads to a thread via asyncio.to_thread.
"""
from __future__ import annotations

import json
import logging
import math
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from google import genai
from google.genai import types

from entry_types import ENTRY_TYPES, normalized_type_label, type_name_guidance

log = logging.getLogger(__name__)

GEMINI_MAX_ATTEMPTS = 3
GEMINI_BACKOFF_SECONDS = 3.0
TRANSIENT_GEMINI_STATUS_CODES = {429, 500, 502, 503, 504}


# ---------- Gemini client (lazy) ----------

_CLIENT: genai.Client | None = None


def _client() -> genai.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _CLIENT


def _gemini_status_code(exc: Exception) -> int | None:
    for value in (
        getattr(exc, "code", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _call_gemini_with_retry(
    operation: Callable[[], Any],
    operation_name: str,
) -> Any:
    """Retry only temporary Gemini capacity/service failures with backoff."""
    for attempt in range(1, GEMINI_MAX_ATTEMPTS + 1):
        try:
            return operation()
        except Exception as exc:
            status_code = _gemini_status_code(exc)
            if (
                status_code not in TRANSIENT_GEMINI_STATUS_CODES
                or attempt == GEMINI_MAX_ATTEMPTS
            ):
                raise
            delay = GEMINI_BACKOFF_SECONDS * (2 ** (attempt - 1))
            log.warning(
                "Gemini %s temporarily unavailable status=%s attempt=%d/%d; "
                "retrying in %.1fs",
                operation_name,
                status_code,
                attempt,
                GEMINI_MAX_ATTEMPTS,
                delay,
            )
            time.sleep(delay)
    raise AssertionError("unreachable")


# ---------- Fetcher ----------

YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "youtu.be",
}
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "m.tiktok.com",
    "vm.tiktok.com",
    "vt.tiktok.com",
    "tiktokv.com",
    "www.tiktokv.com",
}

INSTAGRAM_MAX_VIDEO_HEIGHT = 720
INSTAGRAM_TARGET_VIDEO_KBPS = 2500
TIKTOK_MAX_VIDEO_HEIGHT = 720
TIKTOK_FETCH_ATTEMPTS = 2
TIKTOK_FETCH_RETRY_SECONDS = 2.0
TRANSIENT_TIKTOK_ERRORS = (
    "http error 403",
    "http error 429",
    "unexpected response from webpage request",
    "unable to extract universal data",
    "unable to download video data",
    "remote end closed connection",
    "timed out",
)


@dataclass(frozen=True)
class MediaFetch:
    media_paths: list[Path]
    metadata: dict[str, Any]
    cleanup_dir: Path


def source_platform(source_url: str) -> str:
    """Return the supported source platform for a URL."""
    host = (urlparse(source_url).hostname or "").lower()
    if host in YOUTUBE_HOSTS:
        return "youtube"
    if host in INSTAGRAM_HOSTS:
        return "instagram"
    if host in TIKTOK_HOSTS:
        return "tiktok"
    return "other"


def _preferred_instagram_format(info: dict[str, Any]) -> str | None:
    """Choose a complete, efficient Instagram DASH rendition when available."""
    formats = info.get("formats") or []
    video_candidates = []
    audio_candidates = []
    for item in formats:
        if not isinstance(item, dict):
            continue
        vcodec = item.get("vcodec")
        acodec = item.get("acodec")
        if vcodec and vcodec != "none" and acodec == "none":
            width = item.get("width")
            height = item.get("height")
            bitrate = item.get("vbr") or item.get("tbr")
            if (
                isinstance(width, (int, float))
                and isinstance(height, (int, float))
                and min(width, height) <= INSTAGRAM_MAX_VIDEO_HEIGHT
                and isinstance(bitrate, (int, float))
            ):
                video_candidates.append(item)
        elif vcodec == "none" and acodec and acodec != "none":
            audio_candidates.append(item)

    if not video_candidates or not audio_candidates:
        return None

    under_target = [
        item
        for item in video_candidates
        if (item.get("vbr") or item.get("tbr")) <= INSTAGRAM_TARGET_VIDEO_KBPS
    ]
    if under_target:
        video = max(
            under_target,
            key=lambda item: (
                min(item.get("width") or 0, item.get("height") or 0),
                item.get("vbr") or item.get("tbr") or 0,
            ),
        )
    else:
        video = min(
            video_candidates,
            key=lambda item: item.get("vbr") or item.get("tbr") or float("inf"),
        )
    audio = max(
        audio_candidates,
        key=lambda item: item.get("abr") or item.get("tbr") or 0,
    )
    video_id = video.get("format_id")
    audio_id = audio.get("format_id")
    if not video_id or not audio_id:
        return None

    log.info(
        "Selected Instagram formats video=%s resolution=%sx%s bitrate_kbps=%s audio=%s",
        video_id,
        video.get("width"),
        video.get("height"),
        video.get("vbr") or video.get("tbr"),
        audio_id,
    )
    return f"{video_id}+{audio_id}"


def _probe_instagram(source_url: str) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--ignore-no-formats-error",
        "--skip-download",
        "--dump-single-json",
        source_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp metadata probe failed: {result.stderr.strip()[-1000:]}")
    try:
        info = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError, ValueError):
        raise RuntimeError("yt-dlp metadata probe returned invalid JSON")
    if not isinstance(info, dict):
        raise RuntimeError("yt-dlp metadata probe returned an invalid payload")
    return info


def _instagram_entries(info: dict[str, Any]) -> list[dict[str, Any]]:
    if info.get("_type") != "playlist":
        return [info]
    return [entry for entry in info.get("entries") or [] if isinstance(entry, dict)]


def _instagram_metadata(
    info: dict[str, Any],
    source_url: str,
    entries: list[dict[str, Any]],
) -> dict[str, Any]:
    media_types = ["video" if entry.get("formats") else "image" for entry in entries]
    tagged_accounts_by_media = [
        {
            "media_index": index,
            "accounts": entry["instagram_tagged_accounts"],
        }
        for index, entry in enumerate(entries, start=1)
        if isinstance(entry.get("instagram_tagged_accounts"), list)
        and entry["instagram_tagged_accounts"]
    ]
    creator_display_name = info.get("uploader")
    source_account_handle = info.get("channel")
    native_location = info.get("instagram_location")
    if not isinstance(native_location, dict):
        native_location = None
    native_location_tag = (
        (native_location or {}).get("name") or info.get("location")
    )
    return {
        "source_platform": "instagram",
        "caption_or_description": info.get("description"),
        # Keep uploader for released clients while exposing the two meanings
        # separately to Gemini and newer consumers.
        "uploader": creator_display_name or source_account_handle,
        "creator_display_name": creator_display_name,
        "source_account_handle": source_account_handle,
        "upload_date": info.get("upload_date"),
        "duration_seconds": info.get("duration"),
        "native_location": native_location,
        "native_location_tag": native_location_tag,
        "hashtags": info.get("tags"),
        "webpage_url": info.get("webpage_url") or source_url,
        "media_count": len(entries),
        "media_types": media_types,
        "tagged_accounts_by_media": tagged_accounts_by_media,
    }


def _best_instagram_image_url(entry: dict[str, Any]) -> str | None:
    # yt-dlp exposes image-only Instagram posts as ordered thumbnails, with the
    # original/full-size rendition last. `thumbnail` points at the same choice
    # when the extractor provides it.
    if entry.get("thumbnail"):
        return str(entry["thumbnail"])
    urls = [
        thumbnail.get("url")
        for thumbnail in entry.get("thumbnails") or []
        if isinstance(thumbnail, dict) and thumbnail.get("url")
    ]
    return str(urls[-1]) if urls else None


def _download_instagram_image(
    entry: dict[str, Any],
    destination: Path,
    source_url: str,
) -> None:
    image_url = _best_instagram_image_url(entry)
    if not image_url:
        raise RuntimeError("Instagram image did not include a downloadable image URL")
    response = requests.get(
        image_url,
        headers={
            "Referer": source_url,
            "User-Agent": "Mozilla/5.0 (compatible; PlaceLogger/1.0)",
        },
        timeout=30,
    )
    response.raise_for_status()
    destination.write_bytes(response.content)


def _fetch_instagram(source_url: str, workdir: Path) -> MediaFetch:
    """Download all media from an Instagram image, carousel, or Reel."""
    workdir.mkdir(parents=True, exist_ok=True)
    cleanup_dir = Path(tempfile.mkdtemp(prefix="instagram-", dir=workdir))
    try:
        info = _probe_instagram(source_url)
        entries = _instagram_entries(info)
        if not entries:
            raise RuntimeError("Instagram post did not contain any downloadable media")

        media_paths: list[Path] = []
        video_entries = [entry for entry in entries if entry.get("formats")]
        if video_entries:
            is_carousel = info.get("_type") == "playlist"
            output_template = (
                f"{cleanup_dir}/%(playlist_index)03d-%(id)s.%(ext)s"
                if is_carousel
                else f"{cleanup_dir}/%(id)s.%(ext)s"
            )
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--write-info-json",
                "-o", output_template,
                "--print", "after_move:filepath",
            ]
            if is_carousel:
                # Image entries intentionally have no yt-dlp video format. Let
                # yt-dlp continue through them; they are downloaded below.
                cmd.extend(["--ignore-errors", "--ignore-no-formats-error"])
            else:
                preferred_format = _preferred_instagram_format(info)
                if preferred_format:
                    cmd.extend(["-f", preferred_format, "--merge-output-format", "mp4"])
                else:
                    cmd.append("--no-playlist")
            cmd.append(source_url)
            result = subprocess.run(cmd, capture_output=True, text=True, check=False)
            downloaded_videos = [
                path
                for line in result.stdout.splitlines()
                if (path := Path(line.strip())).is_file()
            ]
            if result.returncode != 0 and not downloaded_videos:
                raise RuntimeError(f"yt-dlp failed: {result.stderr.strip()[-1000:]}")
            if result.returncode != 0:
                log.info(
                    "yt-dlp skipped non-video Instagram carousel entries as expected"
                )
            media_paths.extend(downloaded_videos)

        for index, entry in enumerate(entries, start=1):
            if entry.get("formats"):
                continue
            image_path = cleanup_dir / f"{index:03d}-{entry.get('id') or 'image'}.jpg"
            _download_instagram_image(entry, image_path, source_url)
            media_paths.append(image_path)

        media_paths.sort(key=lambda path: path.name)
        if not media_paths:
            raise RuntimeError("Instagram post did not produce any downloadable media")
        metadata = _instagram_metadata(info, source_url, entries)
        log.info(
            "Downloaded Instagram post media_count=%d media_types=%s",
            len(media_paths),
            metadata["media_types"],
        )
        return MediaFetch(media_paths, metadata, cleanup_dir)
    except Exception:
        shutil.rmtree(cleanup_dir, ignore_errors=True)
        raise


def _preferred_tiktok_format(info: dict[str, Any]) -> str | None:
    """Prefer a compact, broadly decodable progressive TikTok rendition."""
    candidates = []
    duration = info.get("duration")
    for item in info.get("formats") or []:
        if not isinstance(item, dict):
            continue
        vcodec = str(item.get("vcodec") or "none").lower()
        acodec = str(item.get("acodec") or "none").lower()
        if vcodec == "none" or acodec == "none" or not item.get("format_id"):
            continue
        width = item.get("width")
        height = item.get("height")
        numeric_edges = [
            value for value in (width, height) if isinstance(value, (int, float))
        ]
        short_edge = min(numeric_edges) if numeric_edges else 0
        estimated_bytes = item.get("filesize") or item.get("filesize_approx")
        bitrate = item.get("tbr")
        if (
            not isinstance(estimated_bytes, (int, float))
            and isinstance(duration, (int, float))
            and isinstance(bitrate, (int, float))
        ):
            estimated_bytes = duration * bitrate * 1000 / 8
        candidates.append(
            (
                item,
                short_edge,
                vcodec.startswith(("h264", "avc")),
                estimated_bytes,
            )
        )

    if not candidates:
        return None
    under_limit = [
        candidate for candidate in candidates
        if candidate[1] and candidate[1] <= TIKTOK_MAX_VIDEO_HEIGHT
    ]
    pool = under_limit or candidates
    inline_safe = [
        candidate
        for candidate in pool
        if not isinstance(candidate[3], (int, float))
        or candidate[3] <= MAX_INLINE_VIDEO_BYTES
    ]
    pool = inline_safe or pool
    h264_pool = [candidate for candidate in pool if candidate[2]]
    pool = h264_pool or pool
    selected = max(
        pool,
        key=lambda candidate: (
            candidate[1],
            candidate[0].get("tbr") or 0,
        ),
    )[0]
    return str(selected["format_id"])


def _tiktok_metadata(info: dict[str, Any], source_url: str) -> dict[str, Any]:
    creator_display_name = info.get("uploader") or info.get("channel")
    source_account_handle = info.get("uploader_id") or info.get("channel_id")
    return {
        "source_platform": "tiktok",
        "caption_or_description": info.get("description") or info.get("title"),
        "uploader": creator_display_name or source_account_handle,
        "creator_display_name": creator_display_name,
        "source_account_handle": source_account_handle,
        "upload_date": info.get("upload_date"),
        "duration_seconds": info.get("duration"),
        "hashtags": info.get("tags"),
        "webpage_url": info.get("webpage_url") or source_url,
        "media_count": 1,
        "media_types": ["video"],
    }


def _run_tiktok_yt_dlp(command: list[str], operation: str) -> subprocess.CompletedProcess[str]:
    """Retry the small set of TikTok failures known to be transient."""
    for attempt in range(1, TIKTOK_FETCH_ATTEMPTS + 1):
        result = subprocess.run(command, capture_output=True, text=True, check=False)
        if result.returncode == 0:
            return result
        message = (result.stderr or "").lower()
        is_transient = any(fragment in message for fragment in TRANSIENT_TIKTOK_ERRORS)
        if not is_transient or attempt == TIKTOK_FETCH_ATTEMPTS:
            return result
        log.warning(
            "TikTok %s failed transiently attempt=%d/%d; retrying in %.1fs",
            operation,
            attempt,
            TIKTOK_FETCH_ATTEMPTS,
            TIKTOK_FETCH_RETRY_SECONDS,
        )
        time.sleep(TIKTOK_FETCH_RETRY_SECONDS)
    raise AssertionError("unreachable")


def _tiktok_fetch_error(operation: str, stderr: str) -> RuntimeError:
    detail = stderr.strip()[-1000:]
    log.error("TikTok %s failed: %s", operation, detail)
    normalized = detail.lower()
    if any(
        fragment in normalized
        for fragment in ("private post", "private account", "login required")
    ):
        return RuntimeError("This TikTok is private or requires a login")
    if any(
        fragment in normalized
        for fragment in ("video unavailable", "video not available", "has been removed")
    ):
        return RuntimeError("This TikTok is unavailable or has been removed")
    return RuntimeError("TikTok could not be downloaded right now; please try again")


def _fetch_tiktok(source_url: str, workdir: Path) -> MediaFetch:
    """Download one public TikTok video and its extraction metadata."""
    workdir.mkdir(parents=True, exist_ok=True)
    cleanup_dir = Path(tempfile.mkdtemp(prefix="tiktok-", dir=workdir))
    try:
        probe_command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--skip-download",
            "--dump-single-json",
            source_url,
        ]
        probe = _run_tiktok_yt_dlp(probe_command, "metadata probe")
        if probe.returncode != 0:
            raise _tiktok_fetch_error("metadata fetch", probe.stderr)
        try:
            info = json.loads(probe.stdout)
        except (json.JSONDecodeError, TypeError, ValueError):
            raise RuntimeError("TikTok metadata fetch returned invalid JSON")
        if not isinstance(info, dict) or info.get("_type") == "playlist":
            raise RuntimeError("TikTok URL did not resolve to one public video")

        output_template = f"{cleanup_dir}/%(id)s.%(ext)s"
        download_command = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--write-info-json",
            "-o",
            output_template,
            "--print",
            "after_move:filepath",
        ]
        if preferred_format := _preferred_tiktok_format(info):
            download_command.extend(["-f", preferred_format])
        download_command.append(source_url)
        download = _run_tiktok_yt_dlp(download_command, "media download")
        downloaded_paths = [
            path
            for line in download.stdout.splitlines()
            if (path := Path(line.strip())).is_file()
        ]
        if download.returncode != 0 and not downloaded_paths:
            raise _tiktok_fetch_error("media download", download.stderr)
        if len(downloaded_paths) != 1:
            raise RuntimeError("TikTok did not produce exactly one video file")

        metadata = _tiktok_metadata(info, source_url)
        log.info(
            "Downloaded TikTok video id=%s duration_seconds=%s",
            info.get("id"),
            info.get("duration"),
        )
        return MediaFetch(downloaded_paths, metadata, cleanup_dir)
    except Exception:
        shutil.rmtree(cleanup_dir, ignore_errors=True)
        raise


def fetch(source_url: str, workdir: Path) -> MediaFetch:
    """Download supported source media for extraction."""
    platform = source_platform(source_url)
    if platform == "instagram":
        return _fetch_instagram(source_url, workdir)
    if platform == "tiktok":
        return _fetch_tiktok(source_url, workdir)
    raise ValueError("fetch() supports Instagram and TikTok URLs")


# ---------- Extractor ----------

TYPE_NAME_GUIDANCE = type_name_guidance()


EXTRACTOR_PROMPT = """Analyze this social post and extract the distinct recommendations that are part of the post's main intent and that someone may want to save for later. Recommendations can include physical places, temporary events, books, movies, articles, songs, products, routes, and other useful entries.

Be selective about what becomes a saved entry:
- An entry must be independently recommended, endorsed, or presented as a principal subject of the post. For a list post, each intended list entry is a principal recommendation.
- Do not create separate entries for incidental mentions, scenery, background signs or posters, examples, ingredients, products merely being used, or places that only establish where the main recommendation happens.
- A host venue can be important context. Preserve it in the main recommendation's description and use it in location_query when it anchors the recommendation. Do not also save the host venue as a separate entry unless the post independently recommends the venue itself.
- A supplier, neighboring business, collaborator, or partner that only supports the main recommendation is context, not a separate entry.
- A run, event, activity, or gathering that merely hosts or frames a product or place is context, not a separate entry.
- A creator's closing call-to-action (for example, comment, DM, link-in-bio, or sign up for my program) is context unless the post is primarily promoting that offering.
- A person or creator can be a principal saved entry. When no dedicated type exists for that person, use Unknown and preserve the source-grounded description. Never classify a person as the kind of work they create: a filmmaker or director is not a Movie, an author is not a Book or Article, and a musician is not a Song.
- For a creator profile, retrospective, or body-of-work montage, save the creator as Unknown when they are a principal subject. Extract an individual work as a separate entry only when its title is directly evidenced in speech, visible text, or the caption and the post independently recommends that work. Do not infer work titles from unlabeled clips or images.
- Likewise, a movie poster visible in the background is not a movie recommendation, and a city shown as a story's setting is not a travel recommendation.
- When the evidence is ambiguous, prefer preserving the information in source_content or a recommendation's description instead of creating an extra entry.

When multiple media items are supplied, they are the slides of one carousel in display order. Analyze all of them together. The source metadata's caption_or_description may contain the post caption or Instagram's combined carousel captions; treat that text as evidence even when an exact caption-to-slide mapping is unavailable.

Source metadata may include tagged_accounts_by_media from Instagram. media_index is 1-based and matches the supplied media order, so it is also the slide_index for a carousel. A tagged account's full_name and username are supporting identity evidence for that media item. Use a tag to identify a principal recommendation when it is consistent with the media and post context, but do not automatically extract every tagged account or assume every account is a physical place.

Also preserve a source_content object with:
- summary: a compact but complete summary of the post
- transcript: all meaningful intelligible speech, in order; use an empty string when there is none
- on_screen_text: all meaningful visible text, in order; use an empty string when there is none

Return one object per individual entry. Do not combine a list of restaurants, books, products, or events into one record.

For each entry, return an object with:
- extracted_name: concise, distinct name of the entry as mentioned or shown. Never use a generic class as its name (for example, "Cafe", "Restaurant", "Store", or "Place"); if no distinct name is supported, do not return an entry.
- type_name: """ + TYPE_NAME_GUIDANCE + """
- description: a detailed, source-grounded explanation containing the useful information conveyed about this entry. Do not add facts that are not in the source.
- location_query: only when the entry has a physical place, area, anchor, or venue that Google Places could resolve. Use the venue for an event or exhibit. Include the name plus directly evidenced neighborhood/city/region hints from the media, caption, or unambiguous source metadata. A creator display name or account handle is supporting context, not proof by itself: use a location clue from it only when its meaning is clear and consistent with the rest of the post. Never guess a city from an ambiguous handle. Omit this field for non-location entries and when there is not enough location evidence.
- location_hints: object with any of { neighborhood, city, region_or_country, on_screen_text, visual_landmarks } — ONLY include fields where you have direct evidence from the supplied media, caption, or unambiguous source metadata. Omit a field rather than guess.
- native_location_relevance: ONLY when source metadata includes native_location. Classify how that post-level Instagram tag relates to this individual entry: "exact" when it identifies the entry or its physical host; "area" when it only identifies a relevant broader neighborhood, city, or region; "unrelated" when it describes somewhere else; or "uncertain" when the relationship is unclear. Do not assume a tag is exact merely because Instagram attached it to the post. A city- or region-level native_location used to locate a more specific venue is always "area", never "exact".
- starts_at, ends_at, and recurrence_text: only when the recommended entry itself occurs or exists during a bounded or recurring time and that timing is directly supported by the source. Use ISO 8601 for starts_at and ends_at and preserve a human-readable recurring schedule in recurrence_text. NEVER use these fields for ordinary business hours, service windows, days open, release or publication metadata, or incidental dates; keep that information in the description. A temporary event or limited-run offering at a stable venue can be its own entry, with the venue used as its location.
- extraction_confidence: "high" | "medium" | "low"
- timestamp_seconds: for a Reel, YouTube video, or video carousel slide, the
  non-negative number of seconds from the start of that video to the beginning
  of the entry's main section. Omit this field when the entry cannot be tied to
  a specific moment. Do not invent a timestamp from caption-only evidence.
- slide_index: for an Instagram carousel, the 1-based slide number that most
  clearly identifies or discusses the place. The first supplied media item is
  slide 1, the second is slide 2, and so on. Omit this field for non-carousel
  posts or when caption text cannot be tied to a specific slide. A video inside
  a carousel may have both slide_index and timestamp_seconds; in that case the
  timestamp is relative to the start of that slide's video.

Return ONLY valid JSON in this shape:
{ "source_content": { "summary": "...", "transcript": "...", "on_screen_text": "..." }, "entries": [ ... ] }

If NO distinct save-worthy entry is identifiable, still return source_content and use an empty entries array. The source post will still be preserved.
"""

EXTRACTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "source_content": {
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "transcript": {"type": "string"},
                "on_screen_text": {"type": "string"},
            },
            "required": ["summary", "transcript", "on_screen_text"],
        },
        "entries": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "extracted_name": {"type": "string"},
                    "type_name": {"type": "string", "enum": list(ENTRY_TYPES)},
                    "description": {"type": "string"},
                    "location_query": {"type": "string"},
                    "location_hints": {
                        "type": "object",
                        "properties": {
                            "neighborhood": {"type": "string"},
                            "city": {"type": "string"},
                            "region_or_country": {"type": "string"},
                            "on_screen_text": {"type": "string"},
                            "visual_landmarks": {"type": "string"},
                        },
                    },
                    "native_location_relevance": {
                        "type": "string",
                        "enum": ["exact", "area", "unrelated", "uncertain"],
                    },
                    "starts_at": {"type": "string"},
                    "ends_at": {"type": "string"},
                    "recurrence_text": {"type": "string"},
                    "extraction_confidence": {
                        "type": "string",
                        "enum": ["high", "medium", "low"],
                    },
                    "timestamp_seconds": {
                        "type": "number",
                    },
                    "slide_index": {
                        "type": "integer",
                    },
                },
                "required": [
                    "extracted_name",
                    "type_name",
                    "description",
                    "extraction_confidence",
                ],
            },
        }
    },
    "required": ["source_content", "entries"],
}

# Inline media is base64-encoded in the JSON request, which adds roughly 33%
# overhead. Keep the source files comfortably below Gemini's 100 MB total
# request limit so there is also room for the extraction prompt and metadata.
MAX_INLINE_VIDEO_BYTES = 70 * 1024 * 1024
MAX_INLINE_MEDIA_BYTES = MAX_INLINE_VIDEO_BYTES


def _extraction_prompt(
    metadata: dict[str, Any],
    user_prompt: str | None = None,
    existing_types: list[str] | None = None,
) -> str:
    prompt = (
        EXTRACTOR_PROMPT
        + "\n\nSource metadata (supporting evidence, but trust the video itself "
          "for on_screen_text / visual_landmarks):\n"
        + json.dumps(metadata, indent=2, ensure_ascii=False)
    )
    if user_prompt:
        prompt += (
            "\n\nUser prompt (highest authority — may clarify, correct, or override "
            "what you'd otherwise infer from the content):\n"
            + user_prompt
        )
    return prompt


_GENERIC_ENTRY_NAMES = {
    *(normalized_type_label(entry_type) for entry_type in ENTRY_TYPES),
    "place",
    "venue",
    "business",
    "location",
    "shop",
    "cafe",
    "coffee shop",
    "movie theater",
    "food popup",
    "food pop up",
}


def _remove_generic_entry_names(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never persist an inferred category as though it were a named Entry."""
    kept = []
    for entry in entries:
        name = normalized_type_label(entry.get("extracted_name"))
        if not name or name in _GENERIC_ENTRY_NAMES:
            log.info("Discarding generic unnamed extraction %r", entry.get("extracted_name"))
            continue
        kept.append(entry)
    return kept


def _remove_invalid_timing_fields(
    entries: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove empty timing values without coupling timing to a type allowlist."""
    sanitized = []
    for original in entries:
        entry = dict(original)
        for field in ("starts_at", "ends_at", "recurrence_text"):
            if entry.get(field) in (None, ""):
                entry.pop(field, None)
        sanitized.append(entry)
    return sanitized


def _normalize_extracted_entries(
    entries: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    return _remove_invalid_timing_fields(
        _remove_generic_entry_names(_normalize_media_references(entries, metadata))
    )


def _normalize_media_references(
    entries: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Drop impossible timestamps and slide indexes before persistence."""
    media_types = metadata.get("media_types") or []
    is_instagram = metadata.get("source_platform") == "instagram"
    is_carousel = is_instagram and len(media_types) > 1

    for entry in entries:
        timestamp = entry.get("timestamp_seconds")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or timestamp < 0
        ):
            entry.pop("timestamp_seconds", None)

        slide_index = entry.get("slide_index")
        if (
            not is_carousel
            or isinstance(slide_index, bool)
            or not isinstance(slide_index, int)
            or not 1 <= slide_index <= len(media_types)
        ):
            entry.pop("slide_index", None)
            slide_index = None

        if not is_instagram or "timestamp_seconds" not in entry:
            continue
        if is_carousel:
            if slide_index is None or media_types[slide_index - 1] != "video":
                entry.pop("timestamp_seconds", None)
        elif media_types and media_types[0] != "video":
            entry.pop("timestamp_seconds", None)

    return entries


def extract_bundle(
    media_paths: Path | list[Path],
    metadata: dict[str, Any],
    user_prompt: str | None = None,
    existing_types: list[str] | None = None,
) -> dict[str, Any]:
    """Analyze downloaded Instagram media and return source content plus entries."""
    client = _client()
    paths = [media_paths] if isinstance(media_paths, Path) else media_paths
    media_size = sum(path.stat().st_size for path in paths)
    if media_size > MAX_INLINE_MEDIA_BYTES:
        raise ValueError(
            "This Instagram post is too large to process safely "
            f"({media_size / (1024 * 1024):.1f} MiB; "
            f"limit {MAX_INLINE_MEDIA_BYTES / (1024 * 1024):.0f} MiB)"
        )

    media_parts = [
        types.Part.from_bytes(
            data=path.read_bytes(),
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        for path in paths
    ]

    prompt = _extraction_prompt(metadata, user_prompt, existing_types)

    model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    response = _call_gemini_with_retry(
        lambda: client.models.generate_content(
            model=model,
            contents=[*media_parts, prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=EXTRACTION_RESPONSE_SCHEMA,
            ),
        ),
        "Instagram extraction",
    )

    parsed = json.loads(response.text)
    extracted = parsed.get("entries", parsed.get("places", []))
    return {
        "source_content": parsed.get("source_content") or {},
        "entries": _normalize_extracted_entries(extracted, metadata),
    }


def extract(
    media_paths: Path | list[Path],
    metadata: dict[str, Any],
    user_prompt: str | None = None,
    existing_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only individual saved entries."""
    return extract_bundle(
        media_paths,
        metadata,
        user_prompt,
        existing_types,
    )["entries"]


def extract_youtube_bundle(
    source_url: str,
    user_prompt: str | None = None,
    existing_types: list[str] | None = None,
) -> dict[str, Any]:
    """Analyze a public YouTube URL and return source content plus entries."""
    metadata = {"source_platform": "youtube", "webpage_url": source_url}
    model = os.environ.get(
        "GEMINI_YOUTUBE_MODEL",
        os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
    )
    response = _call_gemini_with_retry(
        lambda: _client().interactions.create(
            model=model,
            input=[
                {
                    "type": "text",
                    "text": _extraction_prompt(metadata, user_prompt, existing_types),
                },
                {"type": "video", "uri": source_url},
            ],
            response_format=EXTRACTION_RESPONSE_SCHEMA,
            store=False,
        ),
        "YouTube extraction",
    )
    parsed = json.loads(response.output_text)
    extracted = parsed.get("entries", parsed.get("places", []))
    return {
        "source_content": parsed.get("source_content") or {},
        "entries": _normalize_extracted_entries(extracted, metadata),
    }


def extract_youtube_url(
    source_url: str,
    user_prompt: str | None = None,
    existing_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only individual saved entries."""
    return extract_youtube_bundle(source_url, user_prompt, existing_types)["entries"]


# ---------- Resolver ----------

_PLACES_API = "https://places.googleapis.com/v1/places:searchText"
_FIELD_MASK = ",".join([
    "places.id",
    "places.displayName",
    "places.formattedAddress",
    "places.shortFormattedAddress",
    "places.location",
    "places.types",
    "places.primaryType",
    "places.primaryTypeDisplayName",
    "places.googleMapsUri",
])

_VENUE_BOUND_TYPE_NAMES = {"pop-up", "concert", "exhibit"}
_ADMINISTRATIVE_AREA_PLACE_TYPES = {
    "administrative_area_level_1",
    "administrative_area_level_2",
    "administrative_area_level_3",
    "administrative_area_level_4",
    "administrative_area_level_5",
    "administrative_area_level_6",
    "administrative_area_level_7",
    "colloquial_area",
    "continent",
    "country",
    "geocode",
    "locality",
    "neighborhood",
    "political",
    "postal_code",
    "postal_town",
    "sublocality",
    "sublocality_level_1",
    "sublocality_level_2",
    "sublocality_level_3",
    "sublocality_level_4",
    "sublocality_level_5",
}
_LOCATION_QUERY_STOP_WORDS = {
    "at",
    "center",
    "centre",
    "city",
    "concert",
    "event",
    "exhibit",
    "exhibition",
    "food",
    "gallery",
    "hall",
    "in",
    "listening",
    "market",
    "museum",
    "new",
    "pop",
    "room",
    "shop",
    "the",
    "up",
    "venue",
    "york",
}

_EXACT_LOCATION_BIAS_RADIUS_METERS = 500.0
_AREA_LOCATION_BIAS_RADIUS_METERS = 20_000.0


def _location_query_matches_candidate(query: str, candidate: dict[str, Any]) -> bool:
    """Require a venue-bearing word from the query to appear in Google's name."""
    query_words = {
        word
        for word in re.findall(r"\w+", normalized_type_label(query))
        if len(word) >= 3 and word not in _LOCATION_QUERY_STOP_WORDS
    }
    candidate_name = (candidate.get("displayName") or {}).get("text") or ""
    candidate_words = set(re.findall(r"\w+", normalized_type_label(candidate_name)))
    return bool(query_words & candidate_words)


def _requires_venue_match(place: dict[str, Any]) -> bool:
    """Temporary recommendations must resolve through their host venue."""
    return bool(
        normalized_type_label(place.get("type_name")) in _VENUE_BOUND_TYPE_NAMES
        or place.get("starts_at")
        or place.get("ends_at")
        or place.get("recurrence_text")
    )


def _is_administrative_area_candidate(candidate: dict[str, Any]) -> bool:
    """Reject broad geography returned for a more specific location query."""
    candidate_types = {
        str(place_type).strip().casefold()
        for place_type in candidate.get("types") or []
        if str(place_type).strip()
    }
    primary_type = str(candidate.get("primaryType") or "").strip().casefold()
    if primary_type:
        candidate_types.add(primary_type)
    return bool(candidate_types) and candidate_types <= _ADMINISTRATIVE_AREA_PLACE_TYPES


def _native_location_coordinates(
    source_metadata: dict[str, Any] | None,
) -> tuple[float, float] | None:
    location = (source_metadata or {}).get("native_location")
    if not isinstance(location, dict):
        return None
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if (
        isinstance(latitude, bool)
        or isinstance(longitude, bool)
        or not isinstance(latitude, (int, float))
        or not isinstance(longitude, (int, float))
        or not -90 <= latitude <= 90
        or not -180 <= longitude <= 180
    ):
        return None
    return float(latitude), float(longitude)


def _distance_meters(
    first: tuple[float, float],
    second: tuple[float, float],
) -> float:
    """Return great-circle distance between two latitude/longitude pairs."""
    first_latitude, first_longitude = map(math.radians, first)
    second_latitude, second_longitude = map(math.radians, second)
    latitude_delta = second_latitude - first_latitude
    longitude_delta = second_longitude - first_longitude
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude)
        * math.cos(second_latitude)
        * math.sin(longitude_delta / 2) ** 2
    )
    return 2 * 6_371_000 * math.asin(min(1.0, math.sqrt(haversine)))


def _candidate_coordinates(candidate: dict[str, Any]) -> tuple[float, float] | None:
    location = candidate.get("location")
    if not isinstance(location, dict):
        return None
    latitude = location.get("latitude")
    longitude = location.get("longitude")
    if (
        isinstance(latitude, bool)
        or isinstance(longitude, bool)
        or not isinstance(latitude, (int, float))
        or not isinstance(longitude, (int, float))
    ):
        return None
    return float(latitude), float(longitude)


def _matches_exact_native_location(
    query: str,
    candidate: dict[str, Any],
    native_coordinates: tuple[float, float],
) -> bool:
    candidate_coordinates = _candidate_coordinates(candidate)
    return bool(
        candidate_coordinates
        and _distance_meters(native_coordinates, candidate_coordinates)
        <= _EXACT_LOCATION_BIAS_RADIUS_METERS
        and _location_query_matches_candidate(query, candidate)
    )


def _llm_tiebreaker(
    place: dict[str, Any],
    candidates: list[dict[str, Any]],
) -> dict[str, Any]:
    """Ask Gemini to pick the best candidate when Places returns multiple.

    Returns { "pick": int | null, "confidence": str, "reasoning": str }.
    """
    summaries = []
    for i, c in enumerate(candidates):
        display = (c.get("displayName") or {}).get("text", "?")
        addr = c.get("formattedAddress", "?")
        place_types = c.get("types") or []
        types_str = ", ".join(place_types[:4])
        summaries.append(f"{i}. {display} — {addr} — types: {types_str}")

    prompt = f"""You are disambiguating a place that was extracted from a video.

The extractor identified this place:
{json.dumps(place, indent=2, ensure_ascii=False)}

Google Places API returned these candidates:
{chr(10).join(summaries)}

Pick the best match by index. Consider:
- Name similarity (including bilingual/multilingual names — e.g. English name alongside Chinese characters still counts as a match)
- Neighborhood / city / region match vs location_hints
- Type alignment between type_name and the candidate's Google place types
- Details in the source-grounded description that distinguish the venue
- Legacy tags or dishes when they are available on an older saved place

Return JSON of this shape:
{{ "pick": <int|null>, "confidence": "high"|"medium"|"low", "reasoning": "<one sentence>" }}

- high: one candidate clearly matches with multiple aligned signals
- medium: one candidate is more plausible but meaningful ambiguity remains
- low (or pick=null): none of the candidates clearly match; flag for manual review
"""

    log.info(
        "[tiebreaker] %d candidates for %r — asking LLM",
        len(candidates),
        place.get("extracted_name"),
    )
    model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    resp = _call_gemini_with_retry(
        lambda: _client().models.generate_content(
            model=model,
            contents=[prompt],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        ),
        "place tiebreaker",
    )
    decision = json.loads(resp.text)
    log.info(
        "[tiebreaker] pick=%s confidence=%s reasoning=%s",
        decision.get("pick"),
        decision.get("confidence"),
        decision.get("reasoning"),
    )
    return decision


def resolve(
    place: dict[str, Any],
    source_metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Call Places Text Search. Returns one of three statuses.

    For multi-candidate results, runs an LLM tiebreaker to pick the best match.
    """
    explicit_location_query = place.get("location_query")
    if "location_query" in place and not str(explicit_location_query or "").strip():
        return {"status": "not_applicable", "reason": "no physical location"}
    if "type_name" in place and "location_query" not in place:
        return {"status": "not_applicable", "reason": "no resolvable location"}

    name = place.get("extracted_name") or ""
    hints = place.get("location_hints") or {}
    parts = [name]
    for key in ("neighborhood", "city", "region_or_country"):
        v = hints.get(key)
        if v:
            parts.append(v)
    query = str(explicit_location_query or " ".join(p for p in parts if p)).strip()

    if not query:
        return {"status": "unresolved", "reason": "no query text"}

    request_body: dict[str, Any] = {"textQuery": query, "pageSize": 5}
    native_coordinates = _native_location_coordinates(source_metadata)
    native_location_relevance = place.get("native_location_relevance")
    bias_radius = {
        "exact": _EXACT_LOCATION_BIAS_RADIUS_METERS,
        "area": _AREA_LOCATION_BIAS_RADIUS_METERS,
    }.get(native_location_relevance)
    if native_coordinates and bias_radius:
        latitude, longitude = native_coordinates
        request_body["locationBias"] = {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": bias_radius,
            }
        }

    r = requests.post(
        _PLACES_API,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": os.environ["GOOGLE_PLACES_API_KEY"],
            "X-Goog-FieldMask": _FIELD_MASK,
        },
        json=request_body,
        timeout=20,
    )
    if not r.ok:
        return {
            "status": "unresolved",
            "reason": f"places api {r.status_code}: {r.text[:200]}",
        }

    candidates = r.json().get("places", [])
    log.info("[resolve] query=%r → %d candidate(s)", query, len(candidates))
    for i, c in enumerate(candidates):
        dn = (c.get("displayName") or {}).get("text")
        log.info("  [%d] %s — %s", i, dn, c.get("formattedAddress"))

    if not candidates:
        return {"status": "unresolved", "reason": "zero candidates"}

    candidates = [
        candidate
        for candidate in candidates
        if not _is_administrative_area_candidate(candidate)
    ]
    if not candidates:
        return {
            "status": "unresolved",
            "reason": "no specific place candidates",
        }

    if native_coordinates and native_location_relevance == "exact":
        candidates = [
            candidate
            for candidate in candidates
            if _matches_exact_native_location(query, candidate, native_coordinates)
        ]
        if not candidates:
            return {
                "status": "unresolved",
                "reason": "no nearby Google candidate matched the exact native location",
            }

    if len(candidates) == 1:
        if _requires_venue_match(place) and not _location_query_matches_candidate(
            query, candidates[0]
        ):
            return {
                "status": "unresolved",
                "reason": "venue query does not match Google candidate name",
            }
        return {"status": "auto", "place": candidates[0]}

    # Multiple candidates — LLM tiebreaker
    try:
        decision = _llm_tiebreaker(place, candidates)
    except Exception as exc:
        log.exception("[tiebreaker] failed")
        return {
            "status": "needs_review",
            "candidates": candidates,
            "tiebreaker_error": f"{type(exc).__name__}: {exc}",
        }

    pick = decision.get("pick")
    confidence = decision.get("confidence", "low")
    reasoning = decision.get("reasoning", "")

    pick_valid = (
        pick is not None
        and isinstance(pick, int)
        and 0 <= pick < len(candidates)
    )
    if pick_valid and confidence in ("high", "medium"):
        return {
            "status": "auto",
            "place": candidates[pick],
            "tiebreaker_confidence": confidence,
            "tiebreaker_reasoning": reasoning,
        }

    return {
        "status": "needs_review",
        "candidates": candidates,
        "tiebreaker_confidence": confidence,
        "tiebreaker_reasoning": reasoning,
    }


# ---------- Orchestrator ----------


def _preserve_extraction_failure(
    metadata: dict[str, Any],
    exc: Exception,
) -> None:
    metadata["source_content"] = {
        "summary": "",
        "transcript": "",
        "on_screen_text": "",
    }
    metadata["extraction_status"] = "failed"
    metadata["extraction_error"] = {
        "type": type(exc).__name__,
        "message": str(exc)[:1000],
    }


def process_ingest(
    source_url: str | None,
    user_prompt: str | None,
    workdir: Path,
    existing_types: list[str] | None = None,
    progress: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """Full pipeline: returns a dict with source, metadata, extracted, resolved."""
    if not source_url:
        # v0 only wires up URL-based ingest (see doc 09 for the generalized plan).
        raise NotImplementedError("v0 requires a source URL")

    platform = source_platform(source_url)
    if platform == "other":
        raise ValueError(
            "Supported URLs are public Instagram posts, TikTok videos, and YouTube videos"
        )

    if platform == "youtube":
        if progress:
            progress("extracting")
        metadata = {"source_platform": "youtube", "webpage_url": source_url}
        try:
            bundle = extract_youtube_bundle(source_url, user_prompt, existing_types)
        except Exception as exc:
            log.exception("YouTube extraction failed after retries; preserving source")
            _preserve_extraction_failure(metadata, exc)
            entries = []
        else:
            entries = bundle["entries"]
            metadata["source_content"] = bundle["source_content"]
            metadata["extraction_status"] = "complete"
    else:
        if progress:
            progress("fetching")
        fetched = fetch(source_url, workdir)
        metadata = fetched.metadata
        # Downloaded source media is processing-only. We retain the URL and
        # extracted text, but never copy media to persistent storage.
        metadata["media_preserved"] = False
        try:
            if progress:
                progress("extracting")
            try:
                bundle = extract_bundle(
                    fetched.media_paths,
                    metadata,
                    user_prompt,
                    existing_types,
                )
            except Exception as exc:
                log.exception(
                    "%s extraction failed after retries; preserving source",
                    "TikTok" if platform == "tiktok" else platform.capitalize(),
                )
                _preserve_extraction_failure(metadata, exc)
                entries = []
            else:
                entries = bundle["entries"]
                metadata["source_content"] = bundle["source_content"]
                metadata["extraction_status"] = "complete"
        finally:
            try:
                shutil.rmtree(fetched.cleanup_dir)
            except Exception:
                log.exception("cleanup failed (non-fatal)")

    if progress:
        progress("resolving")
    resolved = [
        {"extracted": entry, **resolve(entry, metadata)} for entry in entries
    ]

    return {
        "source_url": source_url,
        "user_prompt": user_prompt,
        "metadata": metadata,
        "entries_extracted": entries,
        "resolved_entries": resolved,
        # Compatibility aliases for the Telegram bot and released iOS clients.
        "places_extracted": entries,
        "resolved_places": resolved,
    }
