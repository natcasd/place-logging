"""
Test: given an extracted place, call Google Places API Text Search and show candidates.

Usage:
    python test_resolver.py "<name>" ["<extra context>"]

Examples:
    python test_resolver.py "Bo-Ky" "Chinatown New York"
    python test_resolver.py "Jin Mei Dumplings" "25B Henry St New York NY"
"""
import os
import sys
import json
from pathlib import Path

import requests
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env")

API = "https://places.googleapis.com/v1/places:searchText"

# Basic-tier field mask: keeps cost in the free Pro-tier quota.
FIELD_MASK = ",".join([
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


def main() -> None:
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        sys.exit("GOOGLE_PLACES_API_KEY not set in env/.env")

    if len(sys.argv) < 2:
        sys.exit('usage: test_resolver.py "<name>" ["<extra context>"]')

    name = sys.argv[1]
    extra = sys.argv[2] if len(sys.argv) > 2 else ""
    query = f"{name} {extra}".strip()

    print(f"[query] {query!r}", file=sys.stderr)

    body = {
        "textQuery": query,
        "maxResultCount": 5,
    }

    r = requests.post(
        API,
        headers={
            "Content-Type": "application/json",
            "X-Goog-Api-Key": api_key,
            "X-Goog-FieldMask": FIELD_MASK,
        },
        data=json.dumps(body),
        timeout=20,
    )

    if r.status_code != 200:
        print(f"[error] HTTP {r.status_code}", file=sys.stderr)
        print(r.text)
        sys.exit(1)

    data = r.json()
    places = data.get("places", [])
    print(f"[result] {len(places)} candidate(s)", file=sys.stderr)
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
