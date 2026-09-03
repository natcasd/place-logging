"""Backfill missing Location names from Google Places IDs.

The default mode performs read-only Google Place Details lookups and writes a
reviewable JSON plan. ``--apply`` requires that completed plan, creates a SQLite
backup, and updates only Locations whose display name is still missing. Legacy
place rows are updated too so the compatibility API remains consistent.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

import requests


PLAN_VERSION = 1
PLACE_DETAILS_API = "https://places.googleapis.com/v1/places/{place_id}"
TRANSIENT_STATUS_CODES = {429, 500, 502, 503, 504}


def find_candidates(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return Locations that have a Place ID but no authoritative name."""
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT l.id AS location_id, l.google_place_id,
                  GROUP_CONCAT(DISTINCT t.name) AS thing_names
             FROM locations AS l
             LEFT JOIN things AS t ON t.location_id = l.id
            WHERE l.display_name IS NULL OR trim(l.display_name) = ''
            GROUP BY l.id
            ORDER BY l.id"""
    ).fetchall()
    return [
        {
            "location_id": row["location_id"],
            "google_place_id": row["google_place_id"],
            "thing_names": (row["thing_names"] or "").split(","),
        }
        for row in rows
    ]


def fetch_display_name(
    google_place_id: str,
    api_key: str,
    *,
    attempts: int = 5,
    request_get: Any = requests.get,
    sleep: Any = time.sleep,
) -> str:
    """Fetch one Google-authored display name with transient retry/backoff."""
    url = PLACE_DETAILS_API.format(place_id=quote(google_place_id, safe=""))
    for attempt in range(1, attempts + 1):
        response = request_get(
            url,
            headers={
                "X-Goog-Api-Key": api_key,
                "X-Goog-FieldMask": "id,displayName",
            },
            timeout=20,
        )
        if response.ok:
            payload = response.json()
            name = " ".join(
                str((payload.get("displayName") or {}).get("text") or "").split()
            )
            if not name:
                raise ValueError(f"Google returned no displayName for {google_place_id}")
            return name
        if response.status_code not in TRANSIENT_STATUS_CODES or attempt == attempts:
            raise RuntimeError(
                f"Google Places {response.status_code} for {google_place_id}: "
                f"{response.text[:200]}"
            )
        retry_after = response.headers.get("Retry-After")
        try:
            delay = float(retry_after) if retry_after else 2 ** (attempt - 1)
        except ValueError:
            delay = 2 ** (attempt - 1)
        sleep(min(delay, 30))
    raise AssertionError("retry loop exhausted")


def _write_plan(plan_path: Path, plan: dict[str, Any]) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan_path.with_suffix(plan_path.suffix + ".tmp")
    temporary.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(plan_path)


def create_plan(
    db_path: Path,
    plan_path: Path,
    api_key: str,
    *,
    resume: bool = False,
    request_get: Any = requests.get,
    sleep: Any = time.sleep,
) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    try:
        candidates = find_candidates(con)
    finally:
        con.close()

    if plan_path.exists():
        if not resume:
            raise FileExistsError(f"Refusing to overwrite existing plan: {plan_path}")
        plan = json.loads(plan_path.read_text())
        if plan.get("version") != PLAN_VERSION or plan.get("database") != str(db_path):
            raise ValueError("Existing plan does not match this database or plan version")
        plan["complete"] = False
    else:
        plan = {
            "version": PLAN_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "database": str(db_path),
            "candidate_count": len(candidates),
            "complete": False,
            "results": [],
        }
        _write_plan(plan_path, plan)

    successful_ids = {
        result["location_id"]
        for result in plan.get("results", [])
        if result.get("display_name")
    }
    plan["results"] = [
        result for result in plan.get("results", []) if result.get("display_name")
    ]
    pending = [
        candidate
        for candidate in candidates
        if candidate["location_id"] not in successful_ids
    ]
    for index, candidate in enumerate(pending, start=1):
        print(
            f"[{index}/{len(pending)}] {candidate['google_place_id']}",
            flush=True,
        )
        try:
            name = fetch_display_name(
                candidate["google_place_id"],
                api_key,
                request_get=request_get,
                sleep=sleep,
            )
            result = {**candidate, "display_name": name}
        except Exception as exc:
            result = {
                **candidate,
                "display_name": None,
                "error": f"{type(exc).__name__}: {exc}",
            }
        plan["results"].append(result)
        _write_plan(plan_path, plan)
        sleep(0.1)

    errors = [result for result in plan["results"] if not result.get("display_name")]
    plan["complete"] = not errors and len(plan["results"]) == len(candidates)
    plan["completed_at"] = datetime.now(UTC).isoformat()
    _write_plan(plan_path, plan)
    return plan


def apply_plan(db_path: Path, plan_path: Path, backup_dir: Path) -> tuple[int, Path]:
    plan = json.loads(plan_path.read_text())
    if plan.get("version") != PLAN_VERSION or not plan.get("complete"):
        raise ValueError("A complete location-name backfill plan is required")
    updates = plan.get("results", [])
    if len(updates) != plan.get("candidate_count"):
        raise ValueError("Plan result count does not match its candidate count")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"places-before-location-name-backfill-{timestamp}.db"

    source = sqlite3.connect(db_path)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
    finally:
        backup.close()

    try:
        source.execute("BEGIN IMMEDIATE")
        for update in updates:
            current = source.execute(
                "SELECT google_place_id, display_name FROM locations WHERE id = ?",
                (update["location_id"],),
            ).fetchone()
            if (
                current is None
                or current[0] != update["google_place_id"]
                or str(current[1] or "").strip()
            ):
                raise RuntimeError(
                    f"Location {update['location_id']} changed after planning; aborting"
                )
            source.execute(
                "UPDATE locations SET display_name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (update["display_name"], update["location_id"]),
            )
            source.execute(
                """UPDATE places SET location_name = ?
                    WHERE google_place_id = ?
                      AND (location_name IS NULL OR trim(location_name) = '')""",
                (update["display_name"], update["google_place_id"]),
            )
        source.commit()
    except Exception:
        source.rollback()
        raise
    finally:
        source.close()
    return len(updates), backup_path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    if args.apply:
        if not args.plan.exists():
            parser.error("--apply requires an existing --plan file")
        applied, backup_path = apply_plan(
            args.db_path,
            args.plan,
            args.backup_dir or args.db_path.parent / "backups",
        )
        print(json.dumps({"applied": applied, "backup": str(backup_path)}, indent=2))
        return 0

    api_key = os.environ.get("GOOGLE_PLACES_API_KEY", "")
    if not api_key:
        print("GOOGLE_PLACES_API_KEY is required", file=sys.stderr)
        return 2
    try:
        plan = create_plan(
            args.db_path,
            args.plan,
            api_key,
            resume=args.resume,
        )
    except FileExistsError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "candidates": plan["candidate_count"],
                "resolved": sum(
                    bool(result.get("display_name")) for result in plan["results"]
                ),
                "errors": sum(
                    not result.get("display_name") for result in plan["results"]
                ),
                "complete": plan["complete"],
            },
            indent=2,
        )
    )
    return 0 if plan["complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
