"""
Ingest pipeline: source URL → platform ingest → Gemini extract → Places resolve.

Stays synchronous for simplicity; bot.py offloads to a thread via asyncio.to_thread.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
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


def fetch(source_url: str, workdir: Path) -> tuple[Path, dict[str, Any]]:
    """Download an Instagram video with yt-dlp."""
    if source_platform(source_url) != "instagram":
        raise ValueError("fetch() only supports Instagram URLs")

    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--write-info-json",
        "-o", f"{workdir}/%(id)s.%(ext)s",
        "--print", "after_move:filepath",
        source_url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"yt-dlp failed: {result.stderr.strip()[-1000:]}")

    mp4_path = Path(result.stdout.strip().splitlines()[-1])
    info_path = mp4_path.with_suffix(".info.json")

    metadata: dict[str, Any] = {}
    if info_path.exists():
        raw = json.loads(info_path.read_text())
        metadata = {
            "caption_or_description": raw.get("description"),
            "uploader": raw.get("uploader") or raw.get("channel"),
            "upload_date": raw.get("upload_date"),
            "duration_seconds": raw.get("duration"),
            "native_location_tag": raw.get("location"),
            "hashtags": raw.get("tags"),
            "webpage_url": raw.get("webpage_url"),
        }

    return mp4_path, metadata


# ---------- Extractor ----------

EXTRACTOR_PROMPT = """Analyze this video and extract every physical place (restaurant, bar, cafe, shop, hotel, landmark, attraction, market, etc.) that is discussed, shown, or recommended in the video.

For each place, return an object with:
- extracted_name: name of the place as mentioned / shown in the video
- location_hints: object with any of { neighborhood, city, region_or_country, on_screen_text, visual_landmarks } — ONLY include fields where you have direct evidence from the video itself. Omit a field rather than guess.
- dishes: array of specific dishes, drinks, or items mentioned (empty array if none)
- why_its_cool: one-sentence summary of why the creator recommends it (empty string if no explicit recommendation)
- tags: array of relevant tags (cuisine, vibe, price level, meal type, etc.)
- extraction_confidence: "high" | "medium" | "low"

Return ONLY valid JSON in this shape:
{ "places": [ ... ] }

If NO physical place is discussed in the video, return { "places": [] }.
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


def extract(
    mp4_path: Path,
    metadata: dict[str, Any],
    user_prompt: str | None = None,
) -> list[dict[str, Any]]:
    """Upload mp4 to Gemini, run extraction, return places array."""
    client = _client()
    uploaded = client.files.upload(file=mp4_path)

    while uploaded.state.name == "PROCESSING":
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)

    if uploaded.state.name != "ACTIVE":
        raise RuntimeError(f"Gemini file upload ended in state {uploaded.state.name}")

    prompt = _extraction_prompt(metadata, user_prompt)

    model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    response = client.models.generate_content(
        model=model,
        contents=[uploaded, prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=EXTRACTION_RESPONSE_SCHEMA,
        ),
    )

    # Cleanup the uploaded file
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass

    parsed = json.loads(response.text)
    return parsed.get("places", [])


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
    return parsed.get("places", [])


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
        mp4_path, metadata = fetch(source_url, workdir)
        try:
            places = extract(mp4_path, metadata, user_prompt)
        finally:
            try:
                mp4_path.unlink(missing_ok=True)
                mp4_path.with_suffix(".info.json").unlink(missing_ok=True)
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
