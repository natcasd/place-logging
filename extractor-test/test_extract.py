"""
Test: given a URL or local mp4, extract places using Gemini 2.5 Pro.

Usage:
    python test_extract.py <instagram_or_tiktok_or_youtube_url>
    python test_extract.py <path/to/local.mp4>
"""
import os
import sys
import time
import json
import subprocess
from pathlib import Path

from dotenv import load_dotenv
from google import genai
from google.genai import types

# Load .env from script's directory so it works no matter where we run from
load_dotenv(Path(__file__).parent / ".env")

PROMPT = """Analyze this video and extract every physical place (restaurant, bar, cafe, shop, hotel, landmark, attraction, market, etc.) that is discussed, shown, or recommended in the video.

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


def download_if_url(arg: str) -> Path:
    """Download via yt-dlp if arg is a URL; return the mp4 path either way."""
    if not arg.startswith("http"):
        return Path(arg).resolve()

    out_dir = Path("./downloads").resolve()
    out_dir.mkdir(exist_ok=True)

    print(f"[fetch] yt-dlp → {out_dir}", file=sys.stderr)
    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--write-info-json",
        "-o", f"{out_dir}/%(id)s.%(ext)s",
        "--print", "after_move:filepath",
        arg,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        sys.exit(f"yt-dlp failed (exit {result.returncode})")

    filepath = result.stdout.strip().splitlines()[-1]
    return Path(filepath)


def load_metadata(mp4_path: Path) -> dict:
    """Read the .info.json that yt-dlp wrote alongside the mp4."""
    info_path = mp4_path.with_suffix(".info.json")
    if not info_path.exists():
        return {}
    with open(info_path) as f:
        raw = json.load(f)
    # Keep just the fields an LLM would actually use
    return {
        "caption_or_description": raw.get("description"),
        "uploader": raw.get("uploader") or raw.get("channel"),
        "uploader_url": raw.get("uploader_url"),
        "upload_date": raw.get("upload_date"),
        "duration_seconds": raw.get("duration"),
        "like_count": raw.get("like_count"),
        "comment_count": raw.get("comment_count"),
        "native_location_tag": raw.get("location"),
        "hashtags": raw.get("tags"),
        "webpage_url": raw.get("webpage_url"),
    }


def main() -> None:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        sys.exit("GEMINI_API_KEY not set in env")

    if len(sys.argv) < 2:
        sys.exit("usage: test_extract.py <url_or_mp4>")

    mp4_path = download_if_url(sys.argv[1])
    print(f"[fetch] mp4 at {mp4_path} ({mp4_path.stat().st_size / 1e6:.1f} MB)", file=sys.stderr)

    metadata = load_metadata(mp4_path)
    metadata_blob = json.dumps(metadata, indent=2, ensure_ascii=False)
    print(f"[meta] caption={metadata.get('caption_or_description')[:100] if metadata.get('caption_or_description') else 'None'}...", file=sys.stderr)

    client = genai.Client(api_key=api_key)

    print("[upload] → Gemini Files API", file=sys.stderr)
    uploaded = client.files.upload(file=mp4_path)

    # Wait until the file leaves PROCESSING
    while uploaded.state.name == "PROCESSING":
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
        print(f"[upload] state={uploaded.state.name}", file=sys.stderr)

    if uploaded.state.name != "ACTIVE":
        sys.exit(f"file upload ended in state {uploaded.state.name}")

    model_name = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")
    print(f"[extract] {model_name} (video + metadata + prompt)", file=sys.stderr)
    t0 = time.time()

    full_prompt = (
        PROMPT
        + "\n\nAdditional metadata from the source platform (use as supporting evidence alongside the video, but only trust what's in the video itself for on_screen_text / visual_landmarks fields):\n"
        + metadata_blob
    )

    response = client.models.generate_content(
        model=model_name,
        contents=[uploaded, full_prompt],
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
        ),
    )
    elapsed = time.time() - t0
    print(f"[extract] done in {elapsed:.1f}s", file=sys.stderr)

    # Pretty-print the JSON and print token usage
    try:
        parsed = json.loads(response.text)
        print(json.dumps(parsed, indent=2))
    except json.JSONDecodeError:
        print("=== raw (not JSON) ===", file=sys.stderr)
        print(response.text)

    if hasattr(response, "usage_metadata") and response.usage_metadata:
        um = response.usage_metadata
        print(
            f"\n[usage] prompt_tokens={um.prompt_token_count}  "
            f"output_tokens={um.candidates_token_count}  "
            f"total={um.total_token_count}",
            file=sys.stderr,
        )

    # Cleanup — delete the uploaded file so we don't accumulate
    try:
        client.files.delete(name=uploaded.name)
    except Exception:
        pass


if __name__ == "__main__":
    main()
