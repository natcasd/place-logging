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
  timestamp_seconds          REAL,
  slide_index                INTEGER,
  resolution_status          TEXT NOT NULL,
  resolution_candidates_json TEXT
);

CREATE INDEX IF NOT EXISTS idx_places_item      ON places(item_id);
CREATE INDEX IF NOT EXISTS idx_places_google_id ON places(google_place_id);
"""

PLACE_COLUMN_MIGRATIONS = {
    "timestamp_seconds": "REAL",
    "slide_index": "INTEGER",
}


def _migrate_places(con: sqlite3.Connection) -> None:
    existing = {
        row[1]
        for row in con.execute("PRAGMA table_info(places)").fetchall()
    }
    for column, declaration in PLACE_COLUMN_MIGRATIONS.items():
        if column not in existing:
            con.execute(f"ALTER TABLE places ADD COLUMN {column} {declaration}")


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
        _migrate_places(con)
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
                    timestamp_seconds, slide_index,
                    resolution_status, resolution_candidates_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
                    extracted.get("timestamp_seconds"),
                    extracted.get("slide_index"),
                    status,
                    json.dumps(candidates, ensure_ascii=False) if candidates else None,
                ),
            )

        con.commit()
        return item_id
    finally:
        con.close()


def list_places(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Return saved places newest-first for read-only clients."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT
                 p.id,
                 p.item_id,
                 p.ordinal,
                 p.extracted_name,
                 p.google_place_id,
                 p.lat,
                 p.lng,
                 p.formatted_address,
                 p.google_maps_url,
                 p.dishes_json,
                 p.why_its_cool,
                 p.tags_json,
                 p.timestamp_seconds,
                 p.slide_index,
                 p.resolution_status,
                 i.source_url,
                 i.created_at
               FROM places AS p
               JOIN items AS i ON i.id = p.item_id
               ORDER BY i.created_at DESC, p.item_id DESC, p.ordinal ASC
               LIMIT ?""",
            (limit,),
        ).fetchall()

        return [
            {
                "id": row["id"],
                "item_id": row["item_id"],
                "ordinal": row["ordinal"],
                "name": row["extracted_name"],
                "google_place_id": row["google_place_id"],
                "latitude": row["lat"],
                "longitude": row["lng"],
                "formatted_address": row["formatted_address"],
                "google_maps_url": row["google_maps_url"],
                "dishes": json.loads(row["dishes_json"] or "[]"),
                "why_its_cool": row["why_its_cool"] or "",
                "tags": json.loads(row["tags_json"] or "[]"),
                "timestamp_seconds": row["timestamp_seconds"],
                "slide_index": row["slide_index"],
                "resolution_status": row["resolution_status"],
                "source_url": row["source_url"],
                "saved_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        con.close()
