"""
Ingest pipeline: source URL → platform ingest → Gemini extract → Places resolve.

Stays synchronous for simplicity; bot.py offloads to a thread via asyncio.to_thread.
"""
from __future__ import annotations

import json
import logging
import mimetypes
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests
from google import genai
from google.genai import types

log = logging.getLogger(__name__)


# ---------- Gemini client (lazy) ----------

_CLIENT: genai.Client | None = None


def _client() -> genai.Client:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    return _CLIENT


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


# ---------- Extractor ----------

EXTRACTOR_PROMPT = """Analyze this social post and extract every physical place (restaurant, bar, cafe, shop, hotel, landmark, attraction, market, etc.) that is discussed, shown, or recommended in any supplied image, video, or caption.

When multiple media items are supplied, they are the slides of one carousel in display order. Analyze all of them together. The source metadata's caption_or_description may contain the post caption or Instagram's combined carousel captions; treat that text as evidence even when an exact caption-to-slide mapping is unavailable.

For each place, return an object with:
- extracted_name: name of the place as mentioned or shown in the post
- location_hints: object with any of { neighborhood, city, region_or_country, on_screen_text, visual_landmarks } — ONLY include fields where you have direct evidence from the supplied media or caption. Omit a field rather than guess.
- dishes: array of specific dishes, drinks, or items mentioned (empty array if none)
- why_its_cool: one-sentence summary of why the creator recommends it (empty string if no explicit recommendation)
- tags: array of relevant tags (cuisine, vibe, price level, meal type, etc.)
- extraction_confidence: "high" | "medium" | "low"
- timestamp_seconds: for a Reel, YouTube video, or video carousel slide, the
  non-negative number of seconds from the start of that video to the beginning
  of the place's main section. Omit this field when the place cannot be tied to
  a specific moment. Do not invent a timestamp from caption-only evidence.
- slide_index: for an Instagram carousel, the 1-based slide number that most
  clearly identifies or discusses the place. The first supplied media item is
  slide 1, the second is slide 2, and so on. Omit this field for non-carousel
  posts or when caption text cannot be tied to a specific slide. A video inside
  a carousel may have both slide_index and timestamp_seconds; in that case the
  timestamp is relative to the start of that slide's video.

Return ONLY valid JSON in this shape:
{ "places": [ ... ] }

If NO physical place is discussed in the post, return { "places": [] }.
"""

EXTRACTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "places": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "extracted_name": {"type": "string"},
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
                    "dishes": {"type": "array", "items": {"type": "string"}},
                    "why_its_cool": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
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
                    "dishes",
                    "why_its_cool",
                    "tags",
                    "extraction_confidence",
                ],
            },
        }
    },
    "required": ["places"],
}

# Inline media is base64-encoded in the JSON request, which adds roughly 33%
# overhead. Keep the source files comfortably below Gemini's 100 MB total
# request limit so there is also room for the extraction prompt and metadata.
MAX_INLINE_VIDEO_BYTES = 70 * 1024 * 1024
MAX_INLINE_MEDIA_BYTES = MAX_INLINE_VIDEO_BYTES


def _extraction_prompt(
    metadata: dict[str, Any],
    user_prompt: str | None = None,
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


def _normalize_media_references(
    places: list[dict[str, Any]],
    metadata: dict[str, Any],
) -> list[dict[str, Any]]:
    """Drop impossible timestamps and slide indexes before persistence."""
    media_types = metadata.get("media_types") or []
    is_instagram = metadata.get("source_platform") == "instagram"
    is_carousel = is_instagram and len(media_types) > 1

    for place in places:
        timestamp = place.get("timestamp_seconds")
        if (
            isinstance(timestamp, bool)
            or not isinstance(timestamp, (int, float))
            or timestamp < 0
        ):
            place.pop("timestamp_seconds", None)

        slide_index = place.get("slide_index")
        if (
            not is_carousel
            or isinstance(slide_index, bool)
            or not isinstance(slide_index, int)
            or not 1 <= slide_index <= len(media_types)
        ):
            place.pop("slide_index", None)
            slide_index = None

        if not is_instagram or "timestamp_seconds" not in place:
            continue
        if is_carousel:
            if slide_index is None or media_types[slide_index - 1] != "video":
                place.pop("timestamp_seconds", None)
        elif media_types and media_types[0] != "video":
            place.pop("timestamp_seconds", None)

    return places


def extract(
    media_paths: Path | list[Path],
    metadata: dict[str, Any],
    user_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Send downloaded Instagram media inline to Gemini and return its places."""
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

    prompt = _extraction_prompt(metadata, user_prompt)

    model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    response = client.models.generate_content(
        model=model,
        contents=[*media_parts, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=EXTRACTION_RESPONSE_SCHEMA,
        ),
    )

    parsed = json.loads(response.text)
    return _normalize_media_references(parsed.get("places", []), metadata)


def extract_youtube_url(
    source_url: str,
    user_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Have Gemini analyze a public YouTube URL without downloading it."""
    metadata = {"source_platform": "youtube", "webpage_url": source_url}
    model = os.environ.get(
        "GEMINI_YOUTUBE_MODEL",
        os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
    )
    response = _client().interactions.create(
        model=model,
        input=[
            {"type": "text", "text": _extraction_prompt(metadata, user_prompt)},
            {"type": "video", "uri": source_url},
        ],
        response_format=EXTRACTION_RESPONSE_SCHEMA,
        store=False,
    )
    parsed = json.loads(response.output_text)
    return _normalize_media_references(parsed.get("places", []), metadata)


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
- Type alignment (e.g., an extracted "bakery" tag matches candidate types including "bakery")
- Dishes context (if dishes suggest a specific cuisine, a matching candidate is more likely)

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
    resp = _client().models.generate_content(
        model=model,
        contents=[prompt],
        config=types.GenerateContentConfig(response_mime_type="application/json"),
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
    name = place.get("extracted_name") or ""
    hints = place.get("location_hints") or {}
    parts = [name]
    for key in ("neighborhood", "city", "region_or_country"):
        v = hints.get(key)
        if v:
            parts.append(v)
    query = " ".join(p for p in parts if p).strip()

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

def process_ingest(
    source_url: str | None,
    user_prompt: str | None,
    workdir: Path,
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
        metadata = {"source_platform": "youtube", "webpage_url": source_url}
        places = extract_youtube_url(source_url, user_prompt)
    else:
        fetched = fetch(source_url, workdir)
        metadata = fetched.metadata
        try:
            places = extract(fetched.media_paths, metadata, user_prompt)
        finally:
            try:
                shutil.rmtree(fetched.cleanup_dir)
            except Exception:
                log.exception("cleanup failed (non-fatal)")

    resolved = [{"extracted": p, **resolve(p)} for p in places]

    return {
        "source_url": source_url,
        "user_prompt": user_prompt,
        "metadata": metadata,
        "places_extracted": places,
        "resolved_places": resolved,
    }
