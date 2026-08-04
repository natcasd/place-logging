"""
Isolated test harness for the LLM tiebreaker.

Reproduces the multi-candidate Bao's Pastry case from earlier runs without
going through the full Telegram → yt-dlp → Gemini-video pipeline. If the
tiebreaker is silently throwing in production, it'll throw here too — with
a full traceback we can actually read.

Usage:
    source .venv/bin/activate
    python test_tiebreaker.py
"""
from __future__ import annotations

import json
import logging
import traceback
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

from pipeline import _llm_tiebreaker  # noqa: E402

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    level=logging.INFO,
)


# Reconstructed from the bot log at 21:58:31 (the run where the tiebreaker
# errored silently for Bao's Pastry). Place extraction was the problem row
# from item 4 in the DB.
PLACE = {
    "extracted_name": "Bao's Pastry",
    "location_hints": {
        "neighborhood": "Flushing",
        "city": "NYC",
        "region_or_country": "China (origin of bakery)",
        "on_screen_text": "Bao's Pastry",
        "visual_landmarks": "storefront with a line outside, people selecting pastries",
    },
    "dishes": ["Ube Egg Tart", "Pork Floss Buns"],
    "why_its_cool": "new bakery from China offering sweet and savory bites",
    "tags": ["Bakery", "Chinese", "Pastry"],
    "extraction_confidence": "high",
}

# What Places API returned that run (from the log at 21:58:31).
CANDIDATES = [
    {
        "id": "ChIJqSD-BzlhwokRmyKAqIyo-9A",
        "displayName": {"text": "Bao's Pastry 鲍师傅糕点", "languageCode": "zh"},
        "formattedAddress": "135-25 Roosevelt Ave, Flushing, NY 11354, USA",
        "types": ["bakery", "point_of_interest", "food_store", "store", "food", "establishment"],
    },
    {
        "id": "ChIJmU270A9gwokRsi7TRWyRUaM",
        "displayName": {"text": "Tai Pan Bakery", "languageCode": "en"},
        "formattedAddress": "37-25 Main St Second Floor, Flushing, NY 11354, USA",
        "types": ["bakery", "point_of_interest", "food_store", "store", "food", "establishment"],
    },
    {
        "id": "ChIJaZoPAKJhwokR4yhNa7qD2iY",
        "displayName": {"text": "Bao & Pancakes Inc. 新天津包子", "languageCode": "en"},
        "formattedAddress": "41-41 Kissena Blvd #101, Flushing, NY 11354, USA",
        "types": ["restaurant", "chinese_restaurant", "food", "point_of_interest", "establishment"],
    },
]


def main() -> None:
    print("=== Input: extracted place ===")
    print(json.dumps(PLACE, indent=2, ensure_ascii=False))
    print()
    print(f"=== Input: {len(CANDIDATES)} candidates ===")
    for i, c in enumerate(CANDIDATES):
        print(f"[{i}] {c['displayName']['text']} — {c['formattedAddress']}")
    print()

    print("=== Calling _llm_tiebreaker() ===")
    try:
        decision = _llm_tiebreaker(PLACE, CANDIDATES)
    except Exception:
        print("!!! EXCEPTION !!!")
        traceback.print_exc()
        return

    print()
    print("=== Decision ===")
    print(json.dumps(decision, indent=2, ensure_ascii=False))

    pick = decision.get("pick")
    if isinstance(pick, int) and 0 <= pick < len(CANDIDATES):
        winner = CANDIDATES[pick]
        print()
        print(f"=== Picked candidate [{pick}] ===")
        print(f"  {winner['displayName']['text']}")
        print(f"  {winner['formattedAddress']}")


if __name__ == "__main__":
    main()
