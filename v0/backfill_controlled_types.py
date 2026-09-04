"""Reconcile saved legacy occurrences with the controlled Thing type list.

Default mode writes a reviewable plan and makes no database changes.  ``--apply``
backs up the database, updates the preserved legacy occurrences, then rebuilds
the normalized Things, Locations, and source connections from those records.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from store import _backfill_normalized_model, _connect
from thing_types import canonical_thing_type, normalized_type_label


PLAN_VERSION = 1

# These are specific existing recommendations whose previous generic Dessert
# type lost the fact that they are temporary offerings at host venues.
_TYPE_OVERRIDES = {
    "double deuce": "Pop-up",
    "run on clouds slushie": "Pop-up",
}

# These were previously audited as incidental/context-only extractions.  The
# source posts remain; only the erroneous saved occurrences are removed.
_DELETE_NAMES = {
    "oslo",
    "grand central",
    "notre dame cathedral",
    "notre-dame cathedral",
}


def _target_type(name: str, old_type: str) -> str:
    override = _TYPE_OVERRIDES.get(normalized_type_label(name))
    return override or canonical_thing_type(old_type)


def create_plan(db_path: Path) -> dict[str, Any]:
    con = _connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = con.execute(
            """SELECT id, item_id, ordinal, extracted_name, thing_type
                 FROM places
                ORDER BY id"""
        ).fetchall()
    finally:
        con.close()

    deletes: list[dict[str, Any]] = []
    updates: list[dict[str, Any]] = []
    unchanged = 0
    for row in rows:
        name = str(row["extracted_name"] or "")
        old_type = str(row["thing_type"] or "Unknown")
        snapshot = {
            "place_id": row["id"],
            "item_id": row["item_id"],
            "ordinal": row["ordinal"],
            "name": name,
            "old_type": old_type,
        }
        if normalized_type_label(name) in _DELETE_NAMES:
            deletes.append(snapshot)
            continue
        new_type = _target_type(name, old_type)
        if new_type == old_type:
            unchanged += 1
            continue
        updates.append({**snapshot, "new_type": new_type})

    return {
        "version": PLAN_VERSION,
        "database": str(db_path),
        "delete_occurrences": deletes,
        "type_updates": updates,
        "unchanged_occurrences": unchanged,
        "summary": {
            "delete_count": len(deletes),
            "update_count": len(updates),
            "updates_by_target_type": dict(
                sorted(Counter(entry["new_type"] for entry in updates).items())
            ),
        },
    }


def _backup(con: sqlite3.Connection, db_path: Path) -> Path:
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_path = db_path.with_name(f"{db_path.name}.pre-controlled-types-{timestamp}.bak")
    backup = sqlite3.connect(backup_path)
    try:
        con.backup(backup)
    finally:
        backup.close()
    return backup_path


def apply_plan(db_path: Path, plan: dict[str, Any]) -> dict[str, Any]:
    if plan.get("version") != PLAN_VERSION:
        raise ValueError("Unsupported plan version")
    current = create_plan(db_path)
    for key in ("delete_occurrences", "type_updates"):
        if current[key] != plan.get(key):
            raise RuntimeError("Saved Things changed after planning; create a new plan")

    con = _connect(db_path)
    try:
        backup_path = _backup(con, db_path)
        with con:
            # Rebuild from the preserved occurrence rows so canonical identity
            # keys and any newly-equivalent Things are recomputed safely.
            con.execute("DELETE FROM thing_sources")
            con.execute("DELETE FROM things")
            con.execute("DELETE FROM locations")

            delete_ids = [entry["place_id"] for entry in plan["delete_occurrences"]]
            if delete_ids:
                placeholders = ",".join("?" for _ in delete_ids)
                con.execute(f"DELETE FROM places WHERE id IN ({placeholders})", delete_ids)

            for entry in plan["type_updates"]:
                con.execute(
                    "UPDATE places SET thing_type = ? WHERE id = ?",
                    (entry["new_type"], entry["place_id"]),
                )

            _backfill_normalized_model(con)
    finally:
        con.close()

    return {
        "backup_path": str(backup_path),
        "deleted_occurrences": len(plan["delete_occurrences"]),
        "updated_occurrences": len(plan["type_updates"]),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("db_path", type=Path)
    parser.add_argument("--plan", type=Path, help="Write the no-change plan to this JSON file")
    parser.add_argument("--apply", action="store_true", help="Apply a previously generated plan")
    args = parser.parse_args()

    if args.apply and not args.plan:
        parser.error("--apply requires --plan")
    if args.apply:
        plan = json.loads(args.plan.read_text())
        print(json.dumps(apply_plan(args.db_path, plan), indent=2))
        return

    plan = create_plan(args.db_path)
    payload = json.dumps(plan, indent=2, ensure_ascii=False)
    if args.plan:
        args.plan.write_text(payload + "\n")
        print(f"Wrote plan to {args.plan}")
    else:
        print(payload)


if __name__ == "__main__":
    main()
