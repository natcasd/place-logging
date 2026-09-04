"""
Ingest pipeline: source URL → platform ingest → Gemini extract → optional location resolve.

Stays synchronous for simplicity; bot.py offloads to a thread via asyncio.to_thread.
"""
from __future__ import annotations

import hashlib
import json
import logging
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from google import genai
from google.genai import types

from thing_types import THING_TYPES, normalized_type_label

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
INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com"}
TIKTOK_HOSTS = {
    "tiktok.com",
    "www.tiktok.com",
    "vm.tiktok.com",
    "tiktokv.com",
    "www.tiktokv.com",
}

INSTAGRAM_MAX_VIDEO_HEIGHT = 720
INSTAGRAM_TARGET_VIDEO_KBPS = 2500


@dataclass(frozen=True)
class InstagramFetch:
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
    return {
        "source_platform": "instagram",
        "caption_or_description": info.get("description"),
        "uploader": info.get("uploader") or info.get("channel"),
        "upload_date": info.get("upload_date"),
        "duration_seconds": info.get("duration"),
        "native_location_tag": info.get("location"),
        "hashtags": info.get("tags"),
        "webpage_url": info.get("webpage_url") or source_url,
        "media_count": len(entries),
        "media_types": media_types,
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


def fetch(source_url: str, workdir: Path) -> InstagramFetch:
    """Download all media from an Instagram image, carousel, or Reel."""
    if source_platform(source_url) != "instagram":
        raise ValueError("fetch() only supports Instagram URLs")

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
        return InstagramFetch(media_paths, metadata, cleanup_dir)
    except Exception:
        shutil.rmtree(cleanup_dir, ignore_errors=True)
        raise


# ---------- Source archive ----------


def archive_media(
    media_paths: list[Path],
    workdir: Path,
    source_url: str,
) -> list[dict[str, Any]]:
    """Persist source media outside the temporary extractor directory."""
    archive_root = workdir / "sources"
    archive_root.mkdir(parents=True, exist_ok=True)
    source_key = hashlib.sha256(source_url.encode("utf-8")).hexdigest()[:16]
    destination = archive_root / f"{source_key}-{uuid.uuid4().hex[:8]}"
    destination.mkdir()

    manifest = []
    try:
        for index, path in enumerate(media_paths, start=1):
            archived = destination / f"{index:03d}-{path.name}"
            shutil.copy2(path, archived)
            manifest.append(
                {
                    "path": str(archived),
                    "filename": archived.name,
                    "mime_type": mimetypes.guess_type(archived.name)[0]
                    or "application/octet-stream",
                    "bytes": archived.stat().st_size,
                }
            )
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise
    return manifest


# ---------- Extractor ----------

TYPE_NAME_GUIDANCE = """Choose exactly one primary type from this fixed list: Restaurant, Café, Bar, Bakery, Park, Hiking Trail, Bike Route, Museum, Art Gallery, Store, Spa, Fitness, Concert, Pop-up, Book, Movie, Article, Song, Product, or Unknown. Do not invent another type. Use Pop-up for every temporary exhibit or exhibition, food pop-up, or limited-time offering; Art Gallery is only for a permanent gallery venue. Use Unknown only when none of the listed types fit."""


def specific_type_names(type_names: list[str] | None) -> list[str]:
    """Return stable, reusable categories without generic fallback values."""
    return [
        type_name
        for type_name in dict.fromkeys(type_names or [])
        if type_name.strip().casefold() not in {"place", "unknown"}
    ]


EXTRACTOR_PROMPT = """Analyze this social post and extract the distinct recommendations that are part of the post's main intent and that someone may want to save for later. Recommendations can include physical places, temporary events, books, movies, articles, songs, products, routes, and other useful things.

Be selective about what becomes a saved thing:
- A thing must be independently recommended, endorsed, or presented as a principal subject of the post. For a list post, each intended list entry is a principal recommendation.
- Do not create separate things for incidental mentions, scenery, background signs or posters, examples, ingredients, products merely being used, or places that only establish where the main recommendation happens.
- A host venue can be important context. Preserve it in the main recommendation's description and use it in location_query when it anchors the recommendation. Do not also save the host venue as a separate thing unless the post independently recommends the venue itself.
- A supplier, neighboring business, collaborator, or partner that only supports the main recommendation is context, not a separate thing.
- A run, event, activity, or gathering that merely hosts or frames a product or place is context, not a separate thing.
- A creator's closing call-to-action (for example, comment, DM, link-in-bio, or sign up for my program) is context unless the post is primarily promoting that offering.
- Example: if a restaurant's toast uses bread from a neighboring bakery, save the restaurant, not the bakery. If a limited-time drink, food pop-up, or exhibit is at a named venue, save the drink/pop-up/exhibit with that venue as location_query, not the venue itself.
- Likewise, a movie poster visible in the background is not a movie recommendation, and a city shown as a story's setting is not a travel recommendation.
- When the evidence is ambiguous, prefer preserving the information in source_content or a recommendation's description instead of creating an extra thing.

When multiple media items are supplied, they are the slides of one carousel in display order. Analyze all of them together. The source metadata's caption_or_description may contain the post caption or Instagram's combined carousel captions; treat that text as evidence even when an exact caption-to-slide mapping is unavailable.

Also preserve a source_content object with:
- summary: a compact but complete summary of the post
- transcript: all meaningful intelligible speech, in order; use an empty string when there is none
- on_screen_text: all meaningful visible text, in order; use an empty string when there is none

Return one object per individual thing. Do not combine a list of restaurants, books, products, or events into one record.

For each thing, return an object with:
- extracted_name: concise, distinct name of the thing as mentioned or shown. Never use a generic class as its name (for example, "Cafe", "Restaurant", "Store", or "Place"); if no distinct name is supported, do not return a thing.
- type_name: """ + TYPE_NAME_GUIDANCE + """
- description: a detailed, source-grounded explanation containing the useful information conveyed about this thing. Do not add facts that are not in the source.
- location_query: only when the thing has a physical place, area, anchor, or venue that Google Places could resolve. Use the venue for an event or exhibit. Include the name plus directly evidenced neighborhood/city/region hints. Omit this field for non-location things and when there is not enough location evidence.
- location_hints: object with any of { neighborhood, city, region_or_country, on_screen_text, visual_landmarks } — ONLY include fields where you have direct evidence from the supplied media or caption. Omit a field rather than guess.
- starts_at: ISO 8601 date or datetime when a temporary thing begins, only when directly supported by the source
- ends_at: ISO 8601 date or datetime when a temporary thing ends, only when directly supported by the source
- recurrence_text: the source's human-readable recurring schedule when relevant, such as "Sundays through October"
- extraction_confidence: "high" | "medium" | "low"
- timestamp_seconds: for a Reel, YouTube video, or video carousel slide, the
  non-negative number of seconds from the start of that video to the beginning
  of the thing's main section. Omit this field when the thing cannot be tied to
  a specific moment. Do not invent a timestamp from caption-only evidence.
- slide_index: for an Instagram carousel, the 1-based slide number that most
  clearly identifies or discusses the place. The first supplied media item is
  slide 1, the second is slide 2, and so on. Omit this field for non-carousel
  posts or when caption text cannot be tied to a specific slide. A video inside
  a carousel may have both slide_index and timestamp_seconds; in that case the
  timestamp is relative to the start of that slide's video.

Return ONLY valid JSON in this shape:
{ "source_content": { "summary": "...", "transcript": "...", "on_screen_text": "..." }, "things": [ ... ] }

If NO distinct save-worthy thing is identifiable, still return source_content and use an empty things array. The source post will still be preserved.
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
        "things": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "extracted_name": {"type": "string"},
                    "type_name": {"type": "string", "enum": list(THING_TYPES)},
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
    "required": ["source_content", "things"],
}

SCOPE_REVIEW_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "keep_indices": {
            "type": "array",
            "items": {"type": "integer"},
        },
    },
    "required": ["keep_indices"],
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


_GENERIC_THING_NAMES = {
    *(normalized_type_label(thing_type) for thing_type in THING_TYPES),
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


def _remove_generic_thing_names(things: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Never persist an inferred category as though it were a named Thing."""
    kept = []
    for thing in things:
        name = normalized_type_label(thing.get("extracted_name"))
        if not name or name in _GENERIC_THING_NAMES:
            log.info("Discarding generic unnamed extraction %r", thing.get("extracted_name"))
            continue
        kept.append(thing)
    return kept


_CTA_RE = re.compile(
    r"\b(comment|dm|direct message|link(?:[- ]in[- ]bio)?|sign[ -]?up|subscribe|book a call)\b",
    re.IGNORECASE,
)
_VENUE_TYPES = {
    "restaurant",
    "cafe",
    "bar",
    "bakery",
    "park",
    "hiking trail",
    "bike route",
    "museum",
    "art gallery",
    "store",
    "spa",
}


def _needs_scope_review(
    things: list[dict[str, Any]], source_content: dict[str, Any]
) -> bool:
    if len(things) > 1:
        return True
    source_text = " ".join(str(value or "") for value in source_content.values())
    return bool(_CTA_RE.search(source_text))


def _scope_review_prompt(
    source_content: dict[str, Any], things: list[dict[str, Any]]
) -> str:
    candidates = [
        {
            "index": index,
            "name": thing.get("extracted_name"),
            "type": thing.get("type_name"),
            "description": thing.get("description"),
            "location_query": thing.get("location_query"),
        }
        for index, thing in enumerate(things)
    ]
    return f"""Review candidate saved Things from one social post. Keep only candidates that are independently recommended or a principal subject of the post.

Reject context-only candidates: host venues, suppliers, neighboring businesses, collaborators, background posters, settings, and creator calls-to-action. If a product, temporary pop-up, or exhibit is at a venue, keep the product/pop-up/exhibit, not the venue.

Source content:
{json.dumps(source_content, ensure_ascii=False)}

Candidates:
{json.dumps(candidates, ensure_ascii=False)}

Return ONLY JSON: {{"keep_indices": [the indexes to save]}}.
"""


def _apply_scope_review(
    things: list[dict[str, Any]], keep_indices: list[int]
) -> list[dict[str, Any]]:
    keep_set = set(keep_indices)
    kept = [thing for index, thing in enumerate(things) if index in keep_set]
    rejected = [thing for index, thing in enumerate(things) if index not in keep_set]
    venue_queries = [
        thing.get("location_query")
        for thing in rejected
        if normalized_type_label(thing.get("type_name")) in _VENUE_TYPES
        and str(thing.get("location_query") or "").strip()
    ]
    if len(venue_queries) == 1:
        for thing in kept:
            if (
                normalized_type_label(thing.get("type_name")) in {"product", "pop-up"}
                and not str(thing.get("location_query") or "").strip()
            ):
                thing["location_query"] = venue_queries[0]
    return kept


def _review_scope_if_needed(
    things: list[dict[str, Any]], source_content: dict[str, Any]
) -> list[dict[str, Any]]:
    if not _needs_scope_review(things, source_content):
        return things
    response = _call_gemini_with_retry(
        lambda: _client().models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            contents=[_scope_review_prompt(source_content, things)],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=SCOPE_REVIEW_RESPONSE_SCHEMA,
            ),
        ),
        "recommendation scope review",
    )
    parsed = json.loads(response.text)
    raw_indices = parsed.get("keep_indices")
    if not isinstance(raw_indices, list):
        raise ValueError("scope review omitted keep_indices")
    indices = sorted(set(raw_indices))
    if any(not isinstance(index, int) or not 0 <= index < len(things) for index in indices):
        raise ValueError("scope review returned invalid candidate index")
    return _apply_scope_review(things, indices)


def _normalize_media_references(
    things: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Drop impossible timestamps and slide indexes before persistence."""
    media_types = metadata.get("media_types") or []
    is_instagram = metadata.get("source_platform") == "instagram"
    is_carousel = is_instagram and len(media_types) > 1

    for thing in things:
        timestamp = thing.get("timestamp_seconds")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or timestamp < 0
        ):
            thing.pop("timestamp_seconds", None)

        slide_index = thing.get("slide_index")
        if (
            not is_carousel
            or isinstance(slide_index, bool)
            or not isinstance(slide_index, int)
            or not 1 <= slide_index <= len(media_types)
        ):
            thing.pop("slide_index", None)
            slide_index = None

        if not is_instagram or "timestamp_seconds" not in thing:
            continue
        if is_carousel:
            if slide_index is None or media_types[slide_index - 1] != "video":
                thing.pop("timestamp_seconds", None)
        elif media_types and media_types[0] != "video":
            thing.pop("timestamp_seconds", None)

    return things


def extract_bundle(
    media_paths: Path | list[Path],
    metadata: dict[str, Any],
    user_prompt: str | None = None,
    existing_types: list[str] | None = None,
) -> dict[str, Any]:
    """Analyze downloaded Instagram media and return source content plus things."""
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
    extracted = parsed.get("things", parsed.get("places", []))
    source_content = parsed.get("source_content") or {}
    try:
        extracted = _review_scope_if_needed(extracted, source_content)
    except Exception:
        log.exception("Scope review failed; retaining extractor candidates")
    return {
        "source_content": source_content,
        "things": _remove_generic_thing_names(
            _normalize_media_references(extracted, metadata)
        ),
    }


def extract(
    media_paths: Path | list[Path],
    metadata: dict[str, Any],
    user_prompt: str | None = None,
    existing_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only individual saved things."""
    return extract_bundle(
        media_paths,
        metadata,
        user_prompt,
        existing_types,
    )["things"]


def extract_youtube_bundle(
    source_url: str,
    user_prompt: str | None = None,
    existing_types: list[str] | None = None,
) -> dict[str, Any]:
    """Analyze a public YouTube URL and return source content plus things."""
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
    extracted = parsed.get("things", parsed.get("places", []))
    source_content = parsed.get("source_content") or {}
    try:
        extracted = _review_scope_if_needed(extracted, source_content)
    except Exception:
        log.exception("Scope review failed; retaining extractor candidates")
    return {
        "source_content": source_content,
        "things": _remove_generic_thing_names(
            _normalize_media_references(extracted, metadata)
        ),
    }


def extract_youtube_url(
    source_url: str,
    user_prompt: str | None = None,
    existing_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Compatibility wrapper returning only individual saved things."""
    return extract_youtube_bundle(source_url, user_prompt, existing_types)["things"]


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

_VENUE_BOUND_TYPE_NAMES = {"pop-up", "concert"}
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


def resolve(place: dict[str, Any]) -> dict[str, Any]:
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

    r = requests.post(
        _PLACES_API,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": os.environ["GOOGLE_PLACES_API_KEY"],
            "X-Goog-FieldMask": _FIELD_MASK,
        },
        json={"textQuery": query, "maxResultCount": 5},
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
    if len(candidates) == 1:
        if _requires_venue_match(place) and not _location_query_matches_candidate(
            query, candidates[0]
        ):
            return {
                "status": "needs_review",
                "candidates": candidates,
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
    if platform == "tiktok":
        raise NotImplementedError(
            "TikTok ingestion is temporarily unavailable while its downloader support is unstable"
        )
    if platform == "other":
        raise ValueError("Supported URLs are public YouTube videos and Instagram posts")

    if platform == "youtube":
        if progress:
            progress("extracting")
        metadata = {"source_platform": "youtube", "webpage_url": source_url}
        try:
            bundle = extract_youtube_bundle(source_url, user_prompt, existing_types)
        except Exception as exc:
            log.exception("YouTube extraction failed after retries; preserving source")
            _preserve_extraction_failure(metadata, exc)
            things = []
        else:
            things = bundle["things"]
            metadata["source_content"] = bundle["source_content"]
            metadata["extraction_status"] = "complete"
    else:
        if progress:
            progress("fetching")
        fetched = fetch(source_url, workdir)
        metadata = fetched.metadata
        try:
            if progress:
                progress("archiving")
            try:
                metadata["archived_media"] = archive_media(
                    fetched.media_paths,
                    workdir,
                    source_url,
                )
                metadata["media_preserved"] = True
            except Exception:
                # The URL, caption, source analysis, and extracted things are
                # still worth preserving if archival storage is unavailable.
                metadata["media_preserved"] = False
                log.exception("source media archive failed (non-fatal)")
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
                log.exception("Instagram extraction failed after retries; preserving source")
                _preserve_extraction_failure(metadata, exc)
                things = []
            else:
                things = bundle["things"]
                metadata["source_content"] = bundle["source_content"]
                metadata["extraction_status"] = "complete"
        finally:
            try:
                shutil.rmtree(fetched.cleanup_dir)
            except Exception:
                log.exception("cleanup failed (non-fatal)")

    if progress:
        progress("resolving")
    resolved = [{"extracted": thing, **resolve(thing)} for thing in things]

    return {
        "source_url": source_url,
        "user_prompt": user_prompt,
        "metadata": metadata,
        "things_extracted": things,
        "resolved_things": resolved,
        # Compatibility aliases for the Telegram bot and released iOS clients.
        "places_extracted": things,
        "resolved_places": resolved,
    }
