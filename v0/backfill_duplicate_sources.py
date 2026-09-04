"""Collapse repeat saves of the same social-media post into one Source.

The default mode writes a reviewable JSON plan without changing the database.
``--apply`` verifies that the duplicate groups have not changed, creates a full
SQLite backup, then performs the planned cleanup in one transaction.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from source_identity import canonical_source_url


PLAN_VERSION = 1


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        decoded = json.loads(value or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _is_processed(item: sqlite3.Row) -> bool:
    metadata = _json_object(item["raw_payload_json"])
    return (
        metadata.get("extraction_status") != "failed"
        and item["ingest_status"] != "failed"
    )


def _item_snapshot(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "item_id": row["id"],
        "source_url": row["source_url"],
        "created_at": row["created_at"],
        "ingest_status": row["ingest_status"],
        "processed": _is_processed(row),
    }


def duplicate_groups(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Describe all canonical Source identities represented by multiple items."""
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT i.*, r.status AS ingest_status
             FROM items AS i
             LEFT JOIN ingest_runs AS r ON r.item_id = i.id
            ORDER BY i.created_at, i.id"""
    ).fetchall()
    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[canonical_source_url(row["source_url"])].append(row)

    groups: list[dict[str, Any]] = []
    for canonical_url, items in grouped.items():
        if len(items) < 2:
            continue
        # Prefer a successfully processed Source, then its most recent result.
        keeper = max(
            items,
            key=lambda item: (
                _is_processed(item),
                item["created_at"],
                item["id"],
            ),
        )
        groups.append(
            {
                "canonical_url": canonical_url,
                "keeper": _item_snapshot(keeper),
                "duplicates": [
                    _item_snapshot(item) for item in items if item["id"] != keeper["id"]
                ],
            }
        )
    return sorted(groups, key=lambda group: group["canonical_url"])


def _signature(groups: list[dict[str, Any]]) -> str:
    payload = json.dumps(groups, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def create_plan(db_path: Path, plan_path: Path) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    try:
        groups = duplicate_groups(con)
        plan = {
            "version": PLAN_VERSION,
            "created_at": datetime.now(UTC).isoformat(),
            "database": str(db_path),
            "group_count": len(groups),
            "duplicate_item_count": sum(len(group["duplicates"]) for group in groups),
            "signature": _signature(groups),
            "groups": groups,
        }
    finally:
        con.close()

    plan_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan_path.with_suffix(plan_path.suffix + ".tmp")
    temporary.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(plan_path)
    return plan


def _normalized(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


def _connection_key(row: sqlite3.Row) -> tuple[str, str, str, str]:
    return (
        _normalized(row["source_name"]),
        _normalized(row["source_type"]),
        row["starts_at"] or "",
        row["ends_at"] or "",
    )


def _connections(con: sqlite3.Connection, item_id: int) -> list[sqlite3.Row]:
    con.row_factory = sqlite3.Row
    return con.execute(
        """SELECT ts.*, t.starts_at, t.ends_at
             FROM thing_sources AS ts
             JOIN things AS t ON t.id = ts.thing_id
            WHERE ts.item_id = ?
            ORDER BY ts.ordinal, ts.id""",
        (item_id,),
    ).fetchall()


def _delete_connection(con: sqlite3.Connection, connection: sqlite3.Row) -> None:
    con.execute("DELETE FROM thing_sources WHERE id = ?", (connection["id"],))
    if connection["legacy_place_id"] is not None:
        con.execute("DELETE FROM places WHERE id = ?", (connection["legacy_place_id"],))


def _merge_thing(con: sqlite3.Connection, old_id: int, target_id: int) -> None:
    """Move nonredundant Source links to target, then remove the duplicate Thing."""
    if old_id == target_id:
        return
    con.row_factory = sqlite3.Row
    old_connections = con.execute(
        "SELECT * FROM thing_sources WHERE thing_id = ? ORDER BY id", (old_id,)
    ).fetchall()
    for connection in old_connections:
        conflict = con.execute(
            "SELECT 1 FROM thing_sources WHERE thing_id = ? AND item_id = ?",
            (target_id, connection["item_id"]),
        ).fetchone()
        if conflict:
            _delete_connection(con, connection)
        else:
            con.execute(
                "UPDATE thing_sources SET thing_id = ? WHERE id = ?",
                (target_id, connection["id"]),
            )
    con.execute("DELETE FROM things WHERE id = ?", (old_id,))


def _merge_item(con: sqlite3.Connection, duplicate_id: int, keeper_id: int) -> None:
    keeper_connections = _connections(con, keeper_id)
    keeper_by_key = {_connection_key(row): row for row in keeper_connections}
    keeper_thing_ids = {row["thing_id"] for row in keeper_connections}
    next_ordinal = max((row["ordinal"] for row in keeper_connections), default=-1) + 1

    for connection in _connections(con, duplicate_id):
        target = keeper_by_key.get(_connection_key(connection))
        if target is not None and connection["thing_id"] != target["thing_id"]:
            _merge_thing(con, connection["thing_id"], target["thing_id"])
            keeper_thing_ids.add(target["thing_id"])
            continue
        if target is not None or connection["thing_id"] in keeper_thing_ids:
            continue

        # Preserve a recommendation found by only one processing pass by moving
        # its connection (and compatibility row) onto the surviving Source.
        if connection["legacy_place_id"] is not None:
            con.execute(
                "UPDATE places SET item_id = ?, ordinal = ? WHERE id = ?",
                (keeper_id, next_ordinal, connection["legacy_place_id"]),
            )
        con.execute(
            "UPDATE thing_sources SET item_id = ?, ordinal = ? WHERE id = ?",
            (keeper_id, next_ordinal, connection["id"]),
        )
        keeper_by_key[_connection_key(connection)] = connection
        keeper_thing_ids.add(connection["thing_id"])
        next_ordinal += 1

    # Anything still attached to the redundant Source was already represented
    # by the keeper and can now be removed with its compatibility row.
    for connection in _connections(con, duplicate_id):
        _delete_connection(con, connection)
    con.execute("DELETE FROM places WHERE item_id = ?", (duplicate_id,))
    con.execute("DELETE FROM ingest_runs WHERE item_id = ?", (duplicate_id,))
    con.execute("DELETE FROM items WHERE id = ?", (duplicate_id,))


def apply_plan(
    db_path: Path,
    plan_path: Path,
    backup_dir: Path,
) -> tuple[dict[str, int], Path]:
    plan = json.loads(plan_path.read_text())
    if plan.get("version") != PLAN_VERSION:
        raise ValueError("Unsupported duplicate-source plan version")

    con = sqlite3.connect(db_path)
    con.execute("PRAGMA foreign_keys = ON")
    con.row_factory = sqlite3.Row
    current_groups = duplicate_groups(con)
    if _signature(current_groups) != plan.get("signature"):
        con.close()
        raise RuntimeError("Duplicate Sources changed after planning; create a new plan")

    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"places-before-source-dedupe-{timestamp}.db"
    backup = sqlite3.connect(backup_path)
    try:
        con.backup(backup)
    finally:
        backup.close()

    before = {
        "items": con.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "things": con.execute("SELECT COUNT(*) FROM things").fetchone()[0],
        "locations": con.execute("SELECT COUNT(*) FROM locations").fetchone()[0],
        "connections": con.execute("SELECT COUNT(*) FROM thing_sources").fetchone()[0],
    }
    try:
        con.execute("BEGIN IMMEDIATE")
        locked_groups = duplicate_groups(con)
        if _signature(locked_groups) != plan.get("signature"):
            raise RuntimeError("Duplicate Sources changed after planning; create a new plan")
        for group in locked_groups:
            keeper_id = group["keeper"]["item_id"]
            for duplicate in group["duplicates"]:
                _merge_item(con, duplicate["item_id"], keeper_id)

        con.execute(
            "DELETE FROM things WHERE NOT EXISTS "
            "(SELECT 1 FROM thing_sources WHERE thing_sources.thing_id = things.id)"
        )
        con.execute(
            "DELETE FROM locations WHERE NOT EXISTS "
            "(SELECT 1 FROM things WHERE things.location_id = locations.id)"
        )
        foreign_key_errors = con.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_key_errors:
            raise RuntimeError(f"Foreign key check failed: {foreign_key_errors[:3]}")
        if duplicate_groups(con):
            raise RuntimeError("Duplicate Source groups remain after cleanup")
        con.commit()
    except Exception:
        con.rollback()
        con.close()
        raise

    after = {
        "items": con.execute("SELECT COUNT(*) FROM items").fetchone()[0],
        "things": con.execute("SELECT COUNT(*) FROM things").fetchone()[0],
        "locations": con.execute("SELECT COUNT(*) FROM locations").fetchone()[0],
        "connections": con.execute("SELECT COUNT(*) FROM thing_sources").fetchone()[0],
    }
    con.close()
    return {
        "groups_removed": len(current_groups),
        "items_removed": before["items"] - after["items"],
        "things_removed": before["things"] - after["things"],
        "locations_removed": before["locations"] - after["locations"],
        "connections_removed": before["connections"] - after["connections"],
    }, backup_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--backup-dir", type=Path)
    args = parser.parse_args()

    if args.apply:
        backup_dir = args.backup_dir or args.db_path.parent / "backups"
        summary, backup_path = apply_plan(args.db_path, args.plan, backup_dir)
        print(json.dumps({**summary, "backup": str(backup_path)}, indent=2))
    else:
        plan = create_plan(args.db_path, args.plan)
        print(json.dumps(plan, indent=2))


if __name__ == "__main__":
    main()
