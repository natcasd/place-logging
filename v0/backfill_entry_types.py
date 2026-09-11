"""Reclassify generic ``Place`` rows into specific recommendation types.

The default mode sends saved source context and existing row descriptions to
Gemini, then writes a reviewable JSON plan without modifying SQLite. ``--apply``
backs up the database and updates only ``entry_type`` for unchanged rows.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.genai import types

from entry_types import ENTRY_TYPES, canonical_entry_type
from pipeline import (
    TYPE_NAME_GUIDANCE,
    _call_gemini_with_retry,
    _client,
)


PLAN_VERSION = 1
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "integer"},
                    "type_name": {"type": "string", "enum": list(ENTRY_TYPES)},
                    "reason": {"type": "string"},
                },
                "required": ["entry_id", "type_name", "reason"],
            },
        }
    },
    "required": ["classifications"],
}


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _trimmed(value: Any, limit: int) -> str | None:
    text = " ".join(str(value or "").split()).strip()
    return text[:limit] or None


def find_candidates(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return generic rows grouped by their preserved source post."""
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT p.id, p.item_id, p.ordinal, p.extracted_name,
                  p.description, p.formatted_address, p.location_query,
                  i.source_url, i.raw_payload_json
             FROM places AS p
             JOIN items AS i ON i.id = p.item_id
            WHERE lower(trim(p.entry_type)) = 'place'
            ORDER BY p.item_id, p.ordinal"""
    ).fetchall()
    groups: dict[int, dict[str, Any]] = {}
    for row in rows:
        group = groups.get(row["item_id"])
        if group is None:
            metadata = _json_object(row["raw_payload_json"])
            source_content = metadata.get("source_content")
            if not isinstance(source_content, dict):
                source_content = {}
            group = {
                "item_id": row["item_id"],
                "source_url": row["source_url"],
                "source_context": {
                    "creator": _trimmed(metadata.get("uploader"), 200),
                    "caption": _trimmed(metadata.get("caption_or_description"), 4000),
                    "summary": _trimmed(source_content.get("summary"), 2000),
                },
                "entries": [],
            }
            groups[row["item_id"]] = group
        group["entries"].append(
            {
                "entry_id": row["id"],
                "ordinal": row["ordinal"],
                "name": row["extracted_name"],
                "description": _trimmed(row["description"], 2000),
                "formatted_address": row["formatted_address"],
                "location_query": row["location_query"],
            }
        )
    return list(groups.values())


def _batches(
    groups: list[dict[str, Any]],
    maximum_entries: int = 15,
) -> list[list[dict[str, Any]]]:
    batches: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    current_count = 0
    for group in groups:
        group_count = len(group["entries"])
        if current and current_count + group_count > maximum_entries:
            batches.append(current)
            current = []
            current_count = 0
        current.append(group)
        current_count += group_count
    if current:
        batches.append(current)
    return batches


def _classify_batch(groups: list[dict[str, Any]]) -> list[dict[str, Any]]:
    prompt = f"""Reclassify existing saved recommendations whose old generic type is Place.

This is a type-only migration. Return exactly one classification for every
entry_id supplied. Do not add, remove, merge, or rename records. Use the saved
source context, name, description, address, and location query as evidence.

Category rule:
{TYPE_NAME_GUIDANCE}

Saved source groups and records:
{json.dumps(groups, ensure_ascii=False)}
"""
    response = _call_gemini_with_retry(
        lambda: _client().models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            contents=[prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=CLASSIFICATION_SCHEMA,
            ),
        ),
        "type backfill",
    )
    parsed = json.loads(response.text)
    classifications = parsed.get("classifications") or []
    expected_ids = {
        entry["entry_id"] for group in groups for entry in group["entries"]
    }
    by_id: dict[int, dict[str, Any]] = {}
    for classification in classifications:
        entry_id = classification.get("entry_id")
        raw_type_name = " ".join(
            str(classification.get("type_name") or "").split()
        ).strip()
        if entry_id not in expected_ids or entry_id in by_id:
            raise ValueError("Gemini returned an unexpected or duplicate entry_id")
        if not raw_type_name or raw_type_name not in ENTRY_TYPES:
            raise ValueError(f"Gemini returned an invalid type for entry {entry_id}")
        type_name = canonical_entry_type(raw_type_name)
        by_id[entry_id] = {
            "entry_id": entry_id,
            "type_name": type_name,
            "reason": _trimmed(classification.get("reason"), 500) or "",
        }
    if set(by_id) != expected_ids:
        missing = sorted(expected_ids - set(by_id))
        raise ValueError(f"Gemini omitted entry ids: {missing}")
    return [by_id[entry_id] for entry_id in sorted(by_id)]


def _write_plan(plan_path: Path, plan: dict[str, Any]) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = plan_path.with_suffix(plan_path.suffix + ".tmp")
    temporary.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(plan_path)


def create_plan(
    db_path: Path,
    plan_path: Path,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    try:
        groups = find_candidates(con)
        candidate_rows = [entry for group in groups for entry in group["entries"]]
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
                "created_at": datetime.now(timezone.utc).isoformat(),
                "database": str(db_path),
                "candidate_count": len(candidate_rows),
                "complete": False,
                "results": [],
            }
            _write_plan(plan_path, plan)

        successful_ids = {
            update["entry_id"]
            for result in plan.get("results", [])
            if result.get("method") == "gemini_classification"
            for update in result.get("updates", [])
        }
        pending_groups = []
        for group in groups:
            pending_entries = [
                entry for entry in group["entries"] if entry["entry_id"] not in successful_ids
            ]
            if pending_entries:
                pending_groups.append({**group, "entries": pending_entries})
        plan["results"] = [
            result
            for result in plan.get("results", [])
            if result.get("method") != "error"
        ]

        batches = _batches(pending_groups)
        for index, batch in enumerate(batches, start=1):
            ids = [entry["entry_id"] for group in batch for entry in group["entries"]]
            print(f"[{index}/{len(batches)}] classifying {len(ids)} entries", flush=True)
            try:
                updates = _classify_batch(batch)
                result = {"method": "gemini_classification", "updates": updates}
            except Exception as exc:
                result = {
                    "method": "error",
                    "entry_ids": ids,
                    "updates": [],
                    "error": f"{type(exc).__name__}: {exc}",
                }
            plan["results"].append(result)
            _write_plan(plan_path, plan)

        errors = [result for result in plan["results"] if result.get("method") == "error"]
        plan["complete"] = not errors
        plan["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_plan(plan_path, plan)
        return plan
    finally:
        con.close()


def apply_plan(db_path: Path, plan_path: Path, backup_dir: Path) -> tuple[int, Path]:
    plan = json.loads(plan_path.read_text())
    if plan.get("version") != PLAN_VERSION or not plan.get("complete"):
        raise ValueError("A complete type-backfill plan is required")
    updates = [
        update
        for result in plan.get("results", [])
        for update in result.get("updates", [])
    ]
    update_ids = [update["entry_id"] for update in updates]
    if (
        len(updates) != plan.get("candidate_count")
        or len(set(update_ids)) != len(update_ids)
    ):
        raise ValueError("Type-backfill plan does not cover each candidate exactly once")
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"places-before-type-backfill-{timestamp}.db"

    source = sqlite3.connect(db_path)
    backup = sqlite3.connect(backup_path)
    try:
        source.backup(backup)
    finally:
        backup.close()

    applied = 0
    try:
        source.execute("BEGIN IMMEDIATE")
        for update in updates:
            current = source.execute(
                "SELECT entry_type FROM places WHERE id = ?",
                (update["entry_id"],),
            ).fetchone()
            if current is None or current[0].strip().casefold() != "place":
                raise RuntimeError(
                    f"Entry row {update['entry_id']} changed after plan creation; aborting"
                )
            source.execute(
                "UPDATE places SET entry_type = ? WHERE id = ?",
                (update["type_name"], update["entry_id"]),
            )
            applied += 1
        remaining = source.execute(
            "SELECT COUNT(*) FROM places WHERE lower(trim(entry_type)) = 'place'"
        ).fetchone()[0]
        if remaining:
            raise RuntimeError(f"Backfill would leave {remaining} generic Place rows")
        source.commit()
    except Exception:
        source.rollback()
        raise
    finally:
        source.close()
    return applied, backup_path


def summary(plan: dict[str, Any]) -> dict[str, Any]:
    updates = [
        update
        for result in plan.get("results", [])
        for update in result.get("updates", [])
    ]
    return {
        "candidates": plan.get("candidate_count", 0),
        "planned_updates": len(updates),
        "type_counts": dict(sorted(Counter(update["type_name"] for update in updates).items())),
        "unknown": sum(update["type_name"] == "Unknown" for update in updates),
        "errors": sum(result.get("method") == "error" for result in plan.get("results", [])),
        "complete": bool(plan.get("complete")),
    }


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

    plan = create_plan(args.db_path, args.plan, resume=args.resume)
    print(json.dumps(summary(plan), indent=2))
    return 0 if plan.get("complete") else 1


if __name__ == "__main__":
    raise SystemExit(main())
