"""SQLite persistence for sources, canonical Things, Locations, and connections."""
from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from source_identity import canonical_source_url
from thing_types import canonical_thing_type

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

NORMALIZED_SCHEMA = """
CREATE TABLE IF NOT EXISTS locations (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  google_place_id   TEXT NOT NULL UNIQUE,
  display_name      TEXT,
  lat               REAL,
  lng               REAL,
  formatted_address TEXT,
  google_maps_url   TEXT,
  created_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS things (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  name            TEXT NOT NULL,
  normalized_name TEXT NOT NULL,
  thing_type      TEXT NOT NULL,
  type_key        TEXT NOT NULL,
  identity_key    TEXT NOT NULL UNIQUE,
  location_id     INTEGER REFERENCES locations(id),
  starts_at       TEXT,
  ends_at         TEXT,
  recurrence_text TEXT,
  created_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS thing_sources (
  id                         INTEGER PRIMARY KEY AUTOINCREMENT,
  thing_id                   INTEGER NOT NULL REFERENCES things(id) ON DELETE CASCADE,
  item_id                    INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
  legacy_place_id            INTEGER UNIQUE REFERENCES places(id),
  ordinal                    INTEGER NOT NULL,
  source_name                TEXT NOT NULL,
  source_type                TEXT NOT NULL,
  description                TEXT NOT NULL DEFAULT '',
  dishes_json                TEXT,
  why_its_cool               TEXT,
  tags_json                  TEXT,
  timestamp_seconds          REAL,
  slide_index                INTEGER,
  resolution_status          TEXT NOT NULL,
  resolution_candidates_json TEXT,
  location_query             TEXT,
  created_at                 TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(item_id, ordinal),
  UNIQUE(thing_id, item_id)
);

CREATE INDEX IF NOT EXISTS idx_things_location ON things(location_id);
CREATE INDEX IF NOT EXISTS idx_thing_sources_thing ON thing_sources(thing_id);
CREATE INDEX IF NOT EXISTS idx_thing_sources_item ON thing_sources(item_id);

CREATE TABLE IF NOT EXISTS ingest_runs (
  id              INTEGER PRIMARY KEY AUTOINCREMENT,
  source_url      TEXT NOT NULL,
  user_prompt     TEXT,
  source_platform TEXT NOT NULL DEFAULT 'other',
  status          TEXT NOT NULL,
  stage           TEXT NOT NULL,
  item_id         INTEGER UNIQUE REFERENCES items(id),
  result_json     TEXT,
  error_type      TEXT,
  error_message   TEXT,
  started_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at    TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ingest_events (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  ingest_run_id INTEGER NOT NULL REFERENCES ingest_runs(id) ON DELETE CASCADE,
  stage         TEXT NOT NULL,
  status        TEXT NOT NULL,
  message       TEXT NOT NULL,
  created_at    TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_ingest_runs_updated ON ingest_runs(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_ingest_events_run ON ingest_events(ingest_run_id, id);
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


def _connect(db_path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    return con


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


def _backup_before_normalized_migration(
    con: sqlite3.Connection,
    db_path: Path,
) -> Path | None:
    normalized_exists = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'thing_sources'"
    ).fetchone()
    legacy_count = con.execute("SELECT COUNT(*) FROM places").fetchone()[0]
    if normalized_exists or not legacy_count:
        return None

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.name}.pre-normalized-{timestamp}.bak")
    backup = sqlite3.connect(backup_path)
    try:
        con.backup(backup)
    finally:
        backup.close()
    return backup_path


def init_db(db_path: Path) -> None:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = _connect(db_path)
    try:
        con.executescript(SCHEMA)
        _backup_before_thing_migration(con, db_path)
        _migrate_places(con)
        _backup_before_normalized_migration(con, db_path)
        con.executescript(NORMALIZED_SCHEMA)
        _backfill_normalized_model(con)
        _backfill_ingest_runs(con)
        con.commit()
    finally:
        con.close()


def save_ingest(db_path: Path, result: dict[str, Any]) -> int:
    """Persist a full ingest result. Returns the item id."""
    con = _connect(db_path)
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
            _save_normalized_occurrence(
                con,
                legacy_place_id=cur.lastrowid,
                item_id=item_id,
                ordinal=ordinal,
                extracted=extracted,
                status=status,
                place=place,
                candidates=candidates,
            )

        con.commit()
        return item_id
    finally:
        con.close()


def find_processed_source(
    db_path: Path,
    source_url: str,
) -> dict[str, Any] | None:
    """Return the newest successful Source with the same post/video identity."""
    identity = canonical_source_url(source_url)
    con = _connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT i.*, r.id AS ingest_id, r.status AS ingest_status
                 FROM items AS i
                 LEFT JOIN ingest_runs AS r ON r.item_id = i.id
                ORDER BY i.created_at DESC, i.id DESC"""
        ).fetchall()
        for row in rows:
            if canonical_source_url(row["source_url"]) != identity:
                continue
            metadata = _decode_json_object(row["raw_payload_json"])
            if metadata.get("extraction_status") == "failed":
                continue
            if row["ingest_status"] == "failed":
                continue
            return {
                "ingest_id": row["ingest_id"] or row["id"],
                "item_id": row["id"],
                "source_url": row["source_url"],
                "user_prompt": row["user_prompt"],
                "metadata": metadata,
                "places_extracted": [],
                "resolved_places": [],
                "things_extracted": [],
                "resolved_things": [],
                "saved_things": _saved_thing_outcomes(con, row["id"]),
                "already_logged": True,
            }
        return None
    finally:
        con.close()


def start_ingest_run(
    db_path: Path,
    source_url: str,
    user_prompt: str | None,
    source_platform: str,
) -> int:
    """Create durable processing history before source work begins."""
    con = _connect(db_path)
    try:
        cursor = con.execute(
            """INSERT INTO ingest_runs (
                 source_url, user_prompt, source_platform, status, stage
               ) VALUES (?, ?, ?, 'processing', 'accepted')""",
            (source_url, user_prompt, source_platform),
        )
        run_id = cursor.lastrowid
        con.execute(
            """INSERT INTO ingest_events (ingest_run_id, stage, status, message)
               VALUES (?, 'accepted', 'processing', 'Save accepted')""",
            (run_id,),
        )
        con.commit()
        return run_id
    finally:
        con.close()


def update_ingest_run(
    db_path: Path,
    run_id: int,
    stage: str,
    message: str,
) -> None:
    """Record a processing stage for in-app visibility."""
    con = _connect(db_path)
    try:
        cursor = con.execute(
            """UPDATE ingest_runs
                  SET status = 'processing', stage = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND status = 'processing'""",
            (stage, run_id),
        )
        if cursor.rowcount:
            con.execute(
                """INSERT INTO ingest_events (ingest_run_id, stage, status, message)
                   VALUES (?, ?, 'processing', ?)""",
                (run_id, stage, message),
            )
        con.commit()
    finally:
        con.close()


def finish_ingest_run(
    db_path: Path,
    run_id: int,
    *,
    status: str,
    stage: str,
    message: str,
    item_id: int | None = None,
    outcomes: list[dict[str, Any]] | None = None,
    error: Exception | None = None,
) -> None:
    """Finish an ingest and snapshot the exact canonical save outcomes."""
    con = _connect(db_path)
    try:
        con.execute(
            """UPDATE ingest_runs
                  SET status = ?, stage = ?, item_id = ?, result_json = ?,
                      error_type = ?, error_message = ?,
                      updated_at = CURRENT_TIMESTAMP,
                      completed_at = CURRENT_TIMESTAMP
                WHERE id = ?""",
            (
                status,
                stage,
                item_id,
                json.dumps(outcomes or [], ensure_ascii=False),
                type(error).__name__ if error else None,
                str(error)[:1000] if error else None,
                run_id,
            ),
        )
        con.execute(
            """INSERT INTO ingest_events (ingest_run_id, stage, status, message)
               VALUES (?, ?, ?, ?)""",
            (run_id, stage, status, message),
        )
        con.commit()
    finally:
        con.close()


def saved_thing_outcomes(db_path: Path, item_id: int) -> list[dict[str, Any]]:
    """Return what one Source added, including conservative match outcomes."""
    con = _connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        return _saved_thing_outcomes(con, item_id)
    finally:
        con.close()


def _normalize_type_name(value: Any) -> str:
    return canonical_thing_type(value)


def _normalize_identity(value: Any) -> str:
    """Normalize conservative identity fields without fuzzy matching."""
    return " ".join(re.sub(r"[^\w]+", " ", str(value or "").casefold()).split())


def _contains_marker(value: str, marker: str) -> bool:
    return f" {marker} " in f" {value} "


_TEMPORARY_TYPE_MARKERS = (
    "concert",
    "event",
    "exhibit",
    "exhibition",
    "festival",
    "performance",
    "pop up",
    "popup",
    "screening",
    "show",
)

_LOCATION_TYPE_FAMILIES = {
    "food": (
        "bakery",
        "bar",
        "cafe",
        "café",
        "coffee",
        "deli",
        "restaurant",
    ),
    "museum": ("museum",),
    "gallery": ("art gallery", "gallery"),
    "shop": ("bookstore", "boutique", "market", "shop", "store"),
    "outdoors": ("hike", "park", "trail"),
    "wellness": ("salon", "spa", "wellness"),
}


def _type_key(thing_type: str, *, has_location: bool, is_temporary: bool) -> str:
    normalized = _normalize_identity(thing_type)
    if not has_location or is_temporary:
        return normalized
    for family, markers in _LOCATION_TYPE_FAMILIES.items():
        if any(_contains_marker(normalized, marker) for marker in markers):
            return family
    return normalized


def _is_temporary(extracted: dict[str, Any], thing_type: str) -> bool:
    normalized_type = _normalize_identity(thing_type)
    normalized_name = _normalize_identity(extracted.get("extracted_name"))
    return bool(
        extracted.get("starts_at")
        or extracted.get("ends_at")
        or extracted.get("recurrence_text")
        or any(
            _contains_marker(normalized_type, marker)
            or _contains_marker(normalized_name, marker)
            for marker in _TEMPORARY_TYPE_MARKERS
        )
    )


def _identity_key(
    extracted: dict[str, Any],
    thing_type: str,
    google_place_id: str | None,
) -> tuple[str, str, str]:
    normalized_name = _normalize_identity(extracted.get("extracted_name"))
    temporary = _is_temporary(extracted, thing_type)
    type_key = _type_key(
        thing_type,
        has_location=bool(google_place_id),
        is_temporary=temporary,
    )
    if google_place_id and not temporary:
        key = f"location:{google_place_id}|kind:{type_key}"
    else:
        location_part = (
            f"google:{google_place_id}"
            if google_place_id
            else f"query:{_normalize_identity(extracted.get('location_query'))}"
        )
        key = "|".join(
            (
                "temporary" if temporary else "thing",
                location_part,
                f"name:{normalized_name}",
                f"type:{type_key}",
                f"starts:{extracted.get('starts_at') or ''}",
                f"ends:{extracted.get('ends_at') or ''}",
            )
        )
    return key, normalized_name, type_key


def _upsert_location(
    con: sqlite3.Connection,
    place: dict[str, Any],
) -> int | None:
    google_place_id = place.get("id")
    if not google_place_id:
        return None
    location = place.get("location") or {}
    display = place.get("displayName") or {}
    con.execute(
        """INSERT INTO locations (
             google_place_id, display_name, lat, lng,
             formatted_address, google_maps_url
           ) VALUES (?, ?, ?, ?, ?, ?)
           ON CONFLICT(google_place_id) DO UPDATE SET
             display_name = COALESCE(excluded.display_name, locations.display_name),
             lat = COALESCE(excluded.lat, locations.lat),
             lng = COALESCE(excluded.lng, locations.lng),
             formatted_address = COALESCE(
               excluded.formatted_address, locations.formatted_address
             ),
             google_maps_url = COALESCE(
               excluded.google_maps_url, locations.google_maps_url
             ),
             updated_at = CURRENT_TIMESTAMP""",
        (
            google_place_id,
            display.get("text"),
            location.get("latitude"),
            location.get("longitude"),
            place.get("formattedAddress"),
            place.get("googleMapsUri"),
        ),
    )
    return con.execute(
        "SELECT id FROM locations WHERE google_place_id = ?",
        (google_place_id,),
    ).fetchone()[0]


def _save_normalized_occurrence(
    con: sqlite3.Connection,
    *,
    legacy_place_id: int,
    item_id: int,
    ordinal: int,
    extracted: dict[str, Any],
    status: str,
    place: dict[str, Any],
    candidates: list[Any],
    source_created_at: str | None = None,
) -> int:
    location_id = _upsert_location(con, place)
    thing_type = _normalize_type_name(extracted.get("type_name"))
    identity_key, normalized_name, type_key = _identity_key(
        extracted,
        thing_type,
        place.get("id"),
    )
    name = " ".join(str(extracted.get("extracted_name") or "Unknown").split())
    existing = con.execute(
        "SELECT id FROM things WHERE identity_key = ?",
        (identity_key,),
    ).fetchone()
    if existing:
        thing_id = existing[0]
        con.execute(
            """UPDATE things
               SET updated_at = COALESCE(?, CURRENT_TIMESTAMP)
               WHERE id = ?
                 AND COALESCE(?, CURRENT_TIMESTAMP) >= updated_at""",
            (source_created_at, thing_id, source_created_at),
        )
    else:
        cursor = con.execute(
            """INSERT INTO things (
                 name, normalized_name, thing_type, type_key, identity_key,
                 location_id, starts_at, ends_at, recurrence_text,
                 created_at, updated_at
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                         COALESCE(?, CURRENT_TIMESTAMP),
                         COALESCE(?, CURRENT_TIMESTAMP))""",
            (
                name,
                normalized_name,
                thing_type,
                type_key,
                identity_key,
                location_id,
                extracted.get("starts_at"),
                extracted.get("ends_at"),
                extracted.get("recurrence_text"),
                source_created_at,
                source_created_at,
            ),
        )
        thing_id = cursor.lastrowid

    con.execute(
        """INSERT OR IGNORE INTO thing_sources (
             thing_id, item_id, legacy_place_id, ordinal, source_name, source_type,
             description, dishes_json, why_its_cool, tags_json,
             timestamp_seconds, slide_index, resolution_status,
             resolution_candidates_json, location_query, created_at
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                     COALESCE(?, CURRENT_TIMESTAMP))""",
        (
            thing_id,
            item_id,
            legacy_place_id,
            ordinal,
            name,
            thing_type,
            extracted.get("description") or extracted.get("why_its_cool", ""),
            json.dumps(extracted.get("dishes", []), ensure_ascii=False),
            extracted.get("why_its_cool", ""),
            json.dumps(extracted.get("tags", []), ensure_ascii=False),
            extracted.get("timestamp_seconds"),
            extracted.get("slide_index"),
            status,
            json.dumps(candidates, ensure_ascii=False) if candidates else None,
            extracted.get("location_query"),
            source_created_at,
        ),
    )
    return thing_id


def _backfill_normalized_model(con: sqlite3.Connection) -> None:
    """Idempotently convert every legacy occurrence into the normalized model."""
    con.row_factory = sqlite3.Row
    legacy_columns = {
        row[1] for row in con.execute("PRAGMA table_info(places)").fetchall()
    }
    required_columns = {
        "id",
        "item_id",
        "ordinal",
        "extracted_name",
        "google_place_id",
        "lat",
        "lng",
        "formatted_address",
        "google_maps_url",
        "location_name",
        "dishes_json",
        "why_its_cool",
        "tags_json",
        "timestamp_seconds",
        "slide_index",
        "resolution_status",
        "resolution_candidates_json",
        "thing_type",
        "description",
        "starts_at",
        "ends_at",
        "recurrence_text",
        "location_query",
    }
    if not required_columns.issubset(legacy_columns):
        return
    rows = con.execute(
        """SELECT p.*, i.created_at AS source_created_at
           FROM places AS p
           JOIN items AS i ON i.id = p.item_id
           LEFT JOIN thing_sources AS ts ON ts.legacy_place_id = p.id
           WHERE ts.id IS NULL
           ORDER BY i.created_at ASC, p.item_id ASC, p.ordinal ASC"""
    ).fetchall()
    for row in rows:
        place = {
            "id": row["google_place_id"],
            "displayName": {"text": row["location_name"]}
            if row["location_name"]
            else {},
            "location": {"latitude": row["lat"], "longitude": row["lng"]},
            "formattedAddress": row["formatted_address"],
            "googleMapsUri": row["google_maps_url"],
        }
        extracted = {
            "extracted_name": row["extracted_name"],
            "type_name": row["thing_type"],
            "description": row["description"],
            "dishes": json.loads(row["dishes_json"] or "[]"),
            "why_its_cool": row["why_its_cool"] or "",
            "tags": json.loads(row["tags_json"] or "[]"),
            "timestamp_seconds": row["timestamp_seconds"],
            "slide_index": row["slide_index"],
            "starts_at": row["starts_at"],
            "ends_at": row["ends_at"],
            "recurrence_text": row["recurrence_text"],
            "location_query": row["location_query"],
        }
        _save_normalized_occurrence(
            con,
            legacy_place_id=row["id"],
            item_id=row["item_id"],
            ordinal=row["ordinal"],
            extracted=extracted,
            status=row["resolution_status"],
            place=place,
            candidates=json.loads(row["resolution_candidates_json"] or "[]"),
            source_created_at=row["source_created_at"],
        )


def _saved_thing_outcomes(
    con: sqlite3.Connection,
    item_id: int,
) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT t.id AS thing_id, ts.source_name AS name,
                  ts.source_type AS thing_type, t.location_id,
                  l.display_name AS location_name, l.lat, l.lng,
                  ts.resolution_status,
                  ts.id AS source_connection_id,
                  (SELECT MIN(first_ts.id)
                     FROM thing_sources AS first_ts
                    WHERE first_ts.thing_id = t.id) AS first_source_id,
                  (SELECT COUNT(*)
                     FROM thing_sources AS prior_ts
                    WHERE prior_ts.thing_id = t.id
                      AND prior_ts.id <= ts.id) AS source_count
             FROM thing_sources AS ts
             JOIN things AS t ON t.id = ts.thing_id
             LEFT JOIN locations AS l ON l.id = t.location_id
            WHERE ts.item_id = ?
            ORDER BY ts.ordinal, ts.id""",
        (item_id,),
    ).fetchall()
    return [
        {
            "thing_id": row["thing_id"],
            "name": row["name"],
            "type": row["thing_type"],
            "location_id": row["location_id"],
            "location_name": row["location_name"],
            "latitude": row["lat"],
            "longitude": row["lng"],
            "resolution_status": row["resolution_status"],
            "is_new": row["source_connection_id"] == row["first_source_id"],
            "source_count": row["source_count"],
        }
        for row in rows
    ]


def _backfill_ingest_runs(con: sqlite3.Connection) -> None:
    """Give existing Sources a completed Activity record without changing them."""
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT i.*
             FROM items AS i
             LEFT JOIN ingest_runs AS r ON r.item_id = i.id
            WHERE r.id IS NULL
            ORDER BY i.created_at, i.id"""
    ).fetchall()
    for row in rows:
        metadata = _decode_json_object(row["raw_payload_json"])
        outcomes = _saved_thing_outcomes(con, row["id"])
        needs_review = (
            not outcomes
            or metadata.get("extraction_status") == "failed"
            or any(
                outcome["resolution_status"] in {"needs_review", "unresolved"}
                for outcome in outcomes
            )
        )
        status = "partial" if needs_review else "completed"
        message = (
            "Source saved with results needing review"
            if needs_review
            else f"Saved {len(outcomes)} thing{'s' if len(outcomes) != 1 else ''}"
        )
        cursor = con.execute(
            """INSERT INTO ingest_runs (
                 source_url, user_prompt, source_platform, status, stage,
                 item_id, result_json, started_at, updated_at, completed_at
               ) VALUES (?, ?, ?, ?, 'completed', ?, ?, ?, ?, ?)""",
            (
                row["source_url"],
                row["user_prompt"],
                metadata.get("source_platform") or "other",
                status,
                row["id"],
                json.dumps(outcomes, ensure_ascii=False),
                row["created_at"],
                row["created_at"],
                row["created_at"],
            ),
        )
        con.execute(
            """INSERT INTO ingest_events (
                 ingest_run_id, stage, status, message, created_at
               ) VALUES (?, 'completed', ?, ?, ?)""",
            (cursor.lastrowid, status, message, row["created_at"]),
        )


def list_ingest_runs(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Return durable processing history, results, and readable stage events."""
    con = _connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT r.*, i.raw_payload_json
                 FROM ingest_runs AS r
                 LEFT JOIN items AS i ON i.id = r.item_id
                ORDER BY r.started_at DESC, r.id DESC
                LIMIT ?""",
            (limit,),
        ).fetchall()
        if not rows:
            return []
        run_ids = [row["id"] for row in rows]
        placeholders = ",".join("?" for _ in run_ids)
        event_rows = con.execute(
            f"""SELECT id, ingest_run_id, stage, status, message, created_at
                  FROM ingest_events
                 WHERE ingest_run_id IN ({placeholders})
                 ORDER BY id""",
            run_ids,
        ).fetchall()
        events_by_run: dict[int, list[dict[str, Any]]] = {}
        for event in event_rows:
            events_by_run.setdefault(event["ingest_run_id"], []).append(
                {
                    "id": event["id"],
                    "stage": event["stage"],
                    "status": event["status"],
                    "message": event["message"],
                    "created_at": event["created_at"],
                }
            )

        activity = []
        for row in rows:
            metadata = _decode_json_object(row["raw_payload_json"])
            source_content = metadata.get("source_content") or {}
            activity.append(
                {
                    "id": row["id"],
                    "item_id": row["item_id"],
                    "source_url": row["source_url"],
                    "source_platform": row["source_platform"],
                    "creator": metadata.get("uploader"),
                    "caption": metadata.get("caption_or_description"),
                    "summary": source_content.get("summary"),
                    "status": row["status"],
                    "stage": row["stage"],
                    "error_type": row["error_type"],
                    "error_message": row["error_message"],
                    "started_at": row["started_at"],
                    "updated_at": row["updated_at"],
                    "completed_at": row["completed_at"],
                    "results": _decode_json_list(row["result_json"]),
                    "events": events_by_run.get(row["id"], []),
                }
            )
        return activity
    finally:
        con.close()


def list_things(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Return canonical things with every source-specific recommendation."""
    con = _connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        selected = con.execute(
            """SELECT t.id, MAX(i.created_at) AS latest_saved_at
               FROM things AS t
               JOIN thing_sources AS ts ON ts.thing_id = t.id
               JOIN items AS i ON i.id = ts.item_id
               GROUP BY t.id
               ORDER BY latest_saved_at DESC, t.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        if not selected:
            return []
        thing_ids = [row["id"] for row in selected]
        placeholders = ",".join("?" for _ in thing_ids)
        rows = con.execute(
            f"""SELECT
                 t.id, t.name, t.thing_type, t.starts_at, t.ends_at,
                 t.recurrence_text, t.location_id,
                 l.google_place_id, l.display_name AS location_name,
                 l.lat, l.lng, l.formatted_address, l.google_maps_url,
                 ts.id AS source_connection_id, ts.item_id, ts.ordinal,
                 ts.source_name, ts.source_type,
                 ts.description, ts.dishes_json, ts.why_its_cool,
                 ts.tags_json, ts.timestamp_seconds, ts.slide_index,
                 ts.resolution_status, ts.location_query,
                 i.source_url, i.raw_payload_json, i.created_at
               FROM things AS t
               LEFT JOIN locations AS l ON l.id = t.location_id
               JOIN thing_sources AS ts ON ts.thing_id = t.id
               JOIN items AS i ON i.id = ts.item_id
               WHERE t.id IN ({placeholders})
               ORDER BY i.created_at DESC, ts.id DESC""",
            thing_ids,
        ).fetchall()
        by_id: dict[int, dict[str, Any]] = {}
        for row in rows:
            metadata = _decode_json_object(row["raw_payload_json"])
            source = {
                "id": row["source_connection_id"],
                "item_id": row["item_id"],
                "ordinal": row["ordinal"],
                "name": row["source_name"],
                "type": row["source_type"],
                "source_url": row["source_url"],
                "source_platform": metadata.get("source_platform") or "other",
                "creator": metadata.get("uploader"),
                "description": row["description"] or row["why_its_cool"] or "",
                "dishes": _decode_json_list(row["dishes_json"]),
                "why_its_cool": row["why_its_cool"] or "",
                "tags": _decode_json_list(row["tags_json"]),
                "timestamp_seconds": row["timestamp_seconds"],
                "slide_index": row["slide_index"],
                "resolution_status": row["resolution_status"],
                "location_query": row["location_query"],
                "saved_at": row["created_at"],
            }
            thing = by_id.get(row["id"])
            if thing is None:
                thing = {
                    "id": row["id"],
                    "location_id": row["location_id"],
                    "item_id": row["item_id"],
                    "ordinal": row["ordinal"],
                    "name": row["name"],
                    "google_place_id": row["google_place_id"],
                    "latitude": row["lat"],
                    "longitude": row["lng"],
                    "formatted_address": row["formatted_address"],
                    "google_maps_url": row["google_maps_url"],
                    "location_name": row["location_name"],
                    "dishes": source["dishes"],
                    "why_its_cool": source["why_its_cool"],
                    "tags": source["tags"],
                    "timestamp_seconds": source["timestamp_seconds"],
                    "slide_index": source["slide_index"],
                    "resolution_status": source["resolution_status"],
                    "type": row["thing_type"],
                    "description": source["description"],
                    "starts_at": row["starts_at"],
                    "ends_at": row["ends_at"],
                    "recurrence_text": row["recurrence_text"],
                    "location_query": source["location_query"],
                    "source_url": source["source_url"],
                    "saved_at": source["saved_at"],
                    "sources": [],
                }
                by_id[row["id"]] = thing
            thing["sources"].append(source)

        return [by_id[thing_id] for thing_id in thing_ids]
    finally:
        con.close()


def list_places(db_path: Path, limit: int = 200) -> list[dict[str, Any]]:
    """Return legacy occurrence rows for clients still using the places API."""
    con = _connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT p.*, i.source_url, i.created_at
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
                "dishes": _decode_json_list(row["dishes_json"]),
                "why_its_cool": row["why_its_cool"] or "",
                "tags": _decode_json_list(row["tags_json"]),
                "timestamp_seconds": row["timestamp_seconds"],
                "slide_index": row["slide_index"],
                "resolution_status": row["resolution_status"],
                "type": row["thing_type"] or "Unknown",
                "description": row["description"] or row["why_its_cool"] or "",
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "recurrence_text": row["recurrence_text"],
                "location_query": row["location_query"],
                "source_url": row["source_url"],
                "saved_at": row["created_at"],
                "sources": [],
            }
            for row in rows
        ]
    finally:
        con.close()


def list_thing_types(db_path: Path) -> list[str]:
    """Return the open vocabulary currently used by saved things."""
    con = _connect(db_path)
    try:
        try:
            rows = con.execute(
                """SELECT DISTINCT thing_type
                   FROM things
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
    con = _connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT i.id, i.source_url, i.user_prompt, i.raw_payload_json,
                      i.created_at, COUNT(DISTINCT ts.thing_id) AS thing_count
               FROM items AS i
               LEFT JOIN thing_sources AS ts ON ts.item_id = i.id
               GROUP BY i.id
               ORDER BY i.created_at DESC, i.id DESC
               LIMIT ?""",
            (limit,),
        ).fetchall()
        sources = []
        for row in rows:
            metadata = _decode_json_object(row["raw_payload_json"])
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


def _decode_json_object(value: Any) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (json.JSONDecodeError, TypeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _decode_json_list(value: Any) -> list[Any]:
    try:
        decoded = json.loads(value or "[]")
    except (json.JSONDecodeError, TypeError):
        return []
    return decoded if isinstance(decoded, list) else []


def delete_thing(db_path: Path, thing_id: int) -> dict[str, int] | None:
    """Delete one canonical thing and its connections, retaining source posts."""
    con = _connect(db_path)
    try:
        row = con.execute("SELECT id FROM things WHERE id = ?", (thing_id,)).fetchone()
        if row is None:
            return None
        _delete_canonical_things(con, [thing_id])
        con.commit()
        return {"deleted_things": 1, "deleted_sources": 0}
    finally:
        con.close()


def delete_things(db_path: Path, thing_ids: list[int]) -> dict[str, int] | None:
    """Atomically delete canonical things while preserving every source post."""
    unique_ids = list(dict.fromkeys(thing_ids))
    if not unique_ids:
        return None

    placeholders = ",".join("?" for _ in unique_ids)
    con = _connect(db_path)
    try:
        found = {
            row[0]
            for row in con.execute(
                f"SELECT id FROM things WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
        }
        if found != set(unique_ids):
            return None
        _delete_canonical_things(con, unique_ids)
        con.commit()
        return {"deleted_things": len(unique_ids), "deleted_sources": 0}
    finally:
        con.close()


def _delete_canonical_things(con: sqlite3.Connection, thing_ids: list[int]) -> int:
    placeholders = ",".join("?" for _ in thing_ids)
    legacy_ids = [
        row[0]
        for row in con.execute(
            f"""SELECT legacy_place_id FROM thing_sources
                WHERE thing_id IN ({placeholders}) AND legacy_place_id IS NOT NULL""",
            thing_ids,
        ).fetchall()
    ]
    con.execute(
        f"DELETE FROM thing_sources WHERE thing_id IN ({placeholders})",
        thing_ids,
    )
    if legacy_ids:
        legacy_placeholders = ",".join("?" for _ in legacy_ids)
        con.execute(
            f"DELETE FROM places WHERE id IN ({legacy_placeholders})",
            legacy_ids,
        )
    cursor = con.execute(
        f"DELETE FROM things WHERE id IN ({placeholders})",
        thing_ids,
    )
    return cursor.rowcount


def delete_place(db_path: Path, place_id: int) -> dict[str, int] | None:
    """Compatibility deletion by legacy occurrence id; source posts survive."""
    con = _connect(db_path)
    try:
        row = con.execute(
            """SELECT p.id, ts.thing_id
               FROM places AS p
               LEFT JOIN thing_sources AS ts ON ts.legacy_place_id = p.id
               WHERE p.id = ?""",
            (place_id,),
        ).fetchone()
        if row is None:
            return None
        if row[1] is not None:
            deleted_places = con.execute(
                "SELECT COUNT(*) FROM thing_sources WHERE thing_id = ?",
                (row[1],),
            ).fetchone()[0]
            _delete_canonical_things(con, [row[1]])
        else:
            cursor = con.execute("DELETE FROM places WHERE id = ?", (place_id,))
            deleted_places = cursor.rowcount

        con.commit()
        return {
            "deleted_places": deleted_places,
            "deleted_items": 0,
        }
    finally:
        con.close()
