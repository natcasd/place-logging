"""SQLite persistence. One ingest → one items row + N places rows."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
  id               INTEGER PRIMARY KEY AUTOINCREMENT,
  vertical         TEXT NOT NULL,
  source_url       TEXT NOT NULL,
  user_prompt      TEXT,
  raw_payload_json TEXT,
  llm_output_json  TEXT,
  created_at       TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS places (
  id                         INTEGER PRIMARY KEY AUTOINCREMENT,
  item_id                    INTEGER NOT NULL REFERENCES items(id),
  ordinal                    INTEGER NOT NULL,
  extracted_name             TEXT NOT NULL,
  google_place_id            TEXT,
  lat                        REAL,
  lng                        REAL,
  formatted_address          TEXT,
  google_maps_url            TEXT,
  dishes_json                TEXT,
  why_its_cool               TEXT,
  tags_json                  TEXT,
  resolution_status          TEXT NOT NULL,
  resolution_candidates_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_places_item      ON places(item_id);
CREATE INDEX IF NOT EXISTS idx_places_google_id ON places(google_place_id);
"""


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
        con.commit()
    finally:
        con.close()


def save_ingest(db_path: Path, result: dict[str, Any]) -> int:
    """Persist a full ingest result. Returns the item id."""
    con = sqlite3.connect(db_path)
    try:
        cur = con.cursor()
        cur.execute(
            """INSERT INTO items
               (vertical, source_url, user_prompt, raw_payload_json, llm_output_json)
               VALUES (?, ?, ?, ?, ?)""",
            (
                "place",
                result["source_url"],
                result.get("user_prompt"),
                json.dumps(result.get("metadata", {}), ensure_ascii=False),
                json.dumps(result.get("places_extracted", []), ensure_ascii=False),
            ),
        )
        item_id = cur.lastrowid

        for ordinal, r in enumerate(result.get("resolved_places", [])):
            extracted = r.get("extracted", {}) or {}
            status = r.get("status", "unresolved")
            place = r.get("place", {}) or {}
            candidates = r.get("candidates", []) or []

            loc = place.get("location") or {}
            display = place.get("displayName") or {}

            cur.execute(
                """INSERT INTO places (
                    item_id, ordinal, extracted_name,
                    google_place_id, lat, lng,
                    formatted_address, google_maps_url,
                    dishes_json, why_its_cool, tags_json,
                    resolution_status, resolution_candidates_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    ordinal,
                    extracted.get("extracted_name", display.get("text", "?")),
                    place.get("id"),
                    loc.get("latitude"),
                    loc.get("longitude"),
                    place.get("formattedAddress"),
                    place.get("googleMapsUri"),
                    json.dumps(extracted.get("dishes", []), ensure_ascii=False),
                    extracted.get("why_its_cool", ""),
                    json.dumps(extracted.get("tags", []), ensure_ascii=False),
                    status,
                    json.dumps(candidates, ensure_ascii=False) if candidates else None,
                ),
            )

        con.commit()
        return item_id
    finally:
        con.close()
