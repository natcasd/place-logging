"""SQLite persistence. One source ingest → one items row + N saved things."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
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
  location_name              TEXT,
  dishes_json                TEXT,
  why_its_cool               TEXT,
  tags_json                  TEXT,
  timestamp_seconds          REAL,
  slide_index                INTEGER,
  resolution_status          TEXT NOT NULL,
  resolution_candidates_json TEXT,
  thing_type                 TEXT NOT NULL DEFAULT 'Unknown',
  description                TEXT NOT NULL DEFAULT '',
  starts_at                  TEXT,
  ends_at                    TEXT,
  recurrence_text            TEXT,
  location_query             TEXT
);

CREATE INDEX IF NOT EXISTS idx_places_item      ON places(item_id);
CREATE INDEX IF NOT EXISTS idx_places_google_id ON places(google_place_id);
"""

PLACE_COLUMN_MIGRATIONS = {
    "timestamp_seconds": "REAL",
    "slide_index": "INTEGER",
    "thing_type": "TEXT NOT NULL DEFAULT 'Unknown'",
    "description": "TEXT NOT NULL DEFAULT ''",
    "starts_at": "TEXT",
    "ends_at": "TEXT",
    "recurrence_text": "TEXT",
    "location_query": "TEXT",
    "location_name": "TEXT",
}


def _migrate_places(con: sqlite3.Connection) -> None:
    existing = {
        row[1]
        for row in con.execute("PRAGMA table_info(places)").fetchall()
    }
    for column, declaration in PLACE_COLUMN_MIGRATIONS.items():
        if column not in existing:
            con.execute(f"ALTER TABLE places ADD COLUMN {column} {declaration}")


def _backup_before_thing_migration(
    con: sqlite3.Connection,
    db_path: Path,
) -> Path | None:
    existing = {
        row[1]
        for row in con.execute("PRAGMA table_info(places)").fetchall()
    }
    if not existing or set(PLACE_COLUMN_MIGRATIONS).issubset(existing):
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.name}.pre-things-{timestamp}.bak")
    backup = sqlite3.connect(backup_path)
    try:
        con.backup(backup)
    finally:
        backup.close()
    return backup_path


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(db_path)
    try:
        con.executescript(SCHEMA)
        _backup_before_thing_migration(con, db_path)
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
                "thing",
                result["source_url"],
                result.get("user_prompt"),
                json.dumps(result.get("metadata", {}), ensure_ascii=False),
                json.dumps(
                    result.get("things_extracted", result.get("places_extracted", [])),
                    ensure_ascii=False,
                ),
            ),
        )
        item_id = cur.lastrowid

        resolved_things = result.get(
            "resolved_things",
            result.get("resolved_places", []),
        )
        for ordinal, r in enumerate(resolved_things):
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
                    location_name,
                    dishes_json, why_its_cool, tags_json,
                    timestamp_seconds, slide_index,
                    resolution_status, resolution_candidates_json,
                    thing_type, description,
                    starts_at, ends_at, recurrence_text, location_query
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    item_id,
                    ordinal,
                    extracted.get("extracted_name", display.get("text", "?")),
                    place.get("id"),
                    loc.get("latitude"),
                    loc.get("longitude"),
                    place.get("formattedAddress"),
                    place.get("googleMapsUri"),
                    display.get("text"),
                    json.dumps(extracted.get("dishes", []), ensure_ascii=False),
                    extracted.get("why_its_cool", ""),
                    json.dumps(extracted.get("tags", []), ensure_ascii=False),
                    extracted.get("timestamp_seconds"),
                    extracted.get("slide_index"),
                    status,
                    json.dumps(candidates, ensure_ascii=False) if candidates else None,
                    _normalize_type_name(extracted.get("type_name")),
                    extracted.get("description")
                    or extracted.get("why_its_cool", ""),
                    extracted.get("starts_at"),
                    extracted.get("ends_at"),
                    extracted.get("recurrence_text"),
                    extracted.get("location_query"),
                ),
            )

        con.commit()
        return item_id
    finally:
        con.close()


def _normalize_type_name(value: Any) -> str:
    name = " ".join(str(value or "Unknown").split()).strip()
    if not name:
        return "Unknown"
    if name.casefold() == "place":
        return "Unknown"
    return name[:80].title()


def list_things(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Return all saved things newest-first, including non-location things."""
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
                 p.location_name,
                 p.dishes_json,
                 p.why_its_cool,
                 p.tags_json,
                 p.timestamp_seconds,
                 p.slide_index,
                 p.resolution_status,
                 p.thing_type,
                 p.description,
                 p.starts_at,
                 p.ends_at,
                 p.recurrence_text,
                 p.location_query,
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
                "location_name": row["location_name"],
                "dishes": json.loads(row["dishes_json"] or "[]"),
                "why_its_cool": row["why_its_cool"] or "",
                "tags": json.loads(row["tags_json"] or "[]"),
                "timestamp_seconds": row["timestamp_seconds"],
                "slide_index": row["slide_index"],
                "resolution_status": row["resolution_status"],
                "type": row["thing_type"] or "Place",
                "description": row["description"] or row["why_its_cool"] or "",
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "recurrence_text": row["recurrence_text"],
                "location_query": row["location_query"],
                "source_url": row["source_url"],
                "saved_at": row["created_at"],
            }
            for row in rows
        ]
    finally:
        con.close()


def list_places(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Compatibility alias for released clients."""
    return list_things(db_path, limit)


def list_thing_types(db_path: Path) -> list[str]:
    """Return the open vocabulary currently used by saved things."""
    con = sqlite3.connect(db_path)
    try:
        try:
            rows = con.execute(
                """SELECT DISTINCT thing_type
                   FROM places
                   WHERE thing_type IS NOT NULL AND trim(thing_type) != ''
                   ORDER BY thing_type COLLATE NOCASE"""
            ).fetchall()
        except sqlite3.OperationalError:
            return []
        return [row[0] for row in rows]
    finally:
        con.close()


def list_sources(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Return every preserved source, including sources with zero extracted things."""
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT i.id, i.source_url, i.user_prompt, i.raw_payload_json,
                      i.created_at, COUNT(p.id) AS thing_count
               FROM items AS i
               LEFT JOIN places AS p ON p.item_id = i.id
               GROUP BY i.id
               ORDER BY i.created_at DESC, i.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        sources = []
        for row in rows:
            try:
                metadata = json.loads(row["raw_payload_json"] or "{}")
            except (json.JSONDecodeError, TypeError):
                metadata = {}
            archived_media = metadata.get("archived_media") or []
            sources.append(
                {
                    "id": row["id"],
                    "source_url": row["source_url"],
                    "user_prompt": row["user_prompt"],
                    "source_platform": metadata.get("source_platform") or "other",
                    "creator": metadata.get("uploader"),
                    "caption": metadata.get("caption_or_description"),
                    "summary": (metadata.get("source_content") or {}).get("summary"),
                    "media_count": metadata.get("media_count") or len(archived_media),
                    "media_preserved": bool(metadata.get("media_preserved")),
                    "thing_count": row["thing_count"],
                    "needs_review": row["thing_count"] == 0,
                    "saved_at": row["created_at"],
                }
            )
        return sources
    finally:
        con.close()


def delete_thing(db_path: Path, thing_id: int) -> dict[str, int] | None:
    """Delete one saved thing reference while retaining its source record."""
    con = sqlite3.connect(db_path)
    try:
        row = con.execute("SELECT id FROM places WHERE id = ?", (thing_id,)).fetchone()
        if row is None:
            return None
        cursor = con.execute("DELETE FROM places WHERE id = ?", (thing_id,))
        con.commit()
        return {"deleted_things": cursor.rowcount, "deleted_sources": 0}
    finally:
        con.close()


def delete_things(db_path: Path, thing_ids: list[int]) -> dict[str, int] | None:
    """Atomically delete exact thing references while preserving every source."""
    unique_ids = list(dict.fromkeys(thing_ids))
    if not unique_ids:
        return None

    placeholders = ",".join("?" for _ in unique_ids)
    con = sqlite3.connect(db_path)
    try:
        found = {
            row[0]
            for row in con.execute(
                f"SELECT id FROM places WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        }
        if found != set(unique_ids):
            return None
        cursor = con.execute(
            f"DELETE FROM places WHERE id IN ({placeholders})",
            unique_ids,
        )
        con.commit()
        return {"deleted_things": cursor.rowcount, "deleted_sources": 0}
    finally:
        con.close()


def delete_place(db_path: Path, place_id: int) -> dict[str, int] | None:
    """Delete one logical place and remove any newly orphaned ingest items.

    Resolved places are matched by Google Place ID so every saved reference to
    that restaurant is removed. An unresolved place has no stable identity, so
    only the selected row is removed. Other places extracted from the same
    source item are always preserved.
    """
    con = sqlite3.connect(db_path)
    try:
        row = con.execute(
            "SELECT id, item_id, google_place_id FROM places WHERE id = ?",
            (place_id,),
        ).fetchone()
        if row is None:
            return None

        google_place_id = row[2]
        if google_place_id:
            affected_rows = con.execute(
                "SELECT DISTINCT item_id FROM places WHERE google_place_id = ?",
                (google_place_id,),
            ).fetchall()
            cursor = con.execute(
                "DELETE FROM places WHERE google_place_id = ?",
                (google_place_id,),
            )
        else:
            affected_rows = [(row[1],)]
            cursor = con.execute("DELETE FROM places WHERE id = ?", (place_id,))

        deleted_places = cursor.rowcount
        deleted_items = 0
        for (item_id,) in affected_rows:
            cursor = con.execute(
                """DELETE FROM items
                   WHERE id = ?
                     AND NOT EXISTS (
                       SELECT 1 FROM places WHERE places.item_id = items.id
                     )""",
                (item_id,),
            )
            deleted_items += cursor.rowcount

        con.commit()
        return {
            "deleted_places": deleted_places,
            "deleted_items": deleted_items,
        }
    finally:
        con.close()
