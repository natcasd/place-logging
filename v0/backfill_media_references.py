"""Backfill timestamps and carousel slide indexes without re-resolving places.

The default mode creates a reviewable JSON plan and does not modify SQLite.
Passing --apply loads that existing plan, backs up the database, and updates only
timestamp_seconds and slide_index for rows that are still unchanged.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from pipeline import extract, extract_youtube_url, fetch, source_platform


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def _canonical_source_url(value: str) -> str:
    parts = urlsplit(value)
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", "")
    )


def _json_object(value: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(value or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _is_supported_multi_place_media(source_url: str, metadata: dict[str, Any]) -> bool:
    platform = source_platform(source_url)
    if platform == "youtube":
        return True
    if platform != "instagram":
        return False
    media_types = metadata.get("media_types") or []
    return "/reel/" in urlsplit(source_url).path or len(media_types) > 1


def find_candidates(con: sqlite3.Connection) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    items = con.execute(
        """SELECT i.id, i.source_url, i.raw_payload_json, i.created_at,
                  COUNT(p.id) AS place_count
             FROM items i
             JOIN places p ON p.item_id = i.id
           GROUP BY i.id
           HAVING COUNT(p.id) > 1
              AND SUM(CASE WHEN p.timestamp_seconds IS NULL
                                AND p.slide_index IS NULL
                           THEN 1 ELSE 0 END) > 0
            ORDER BY i.id"""
    ).fetchall()
    candidates = []
    for item in items:
        metadata = _json_object(item["raw_payload_json"])
        if not _is_supported_multi_place_media(item["source_url"], metadata):
            continue
        places = con.execute(
            """SELECT id, ordinal, extracted_name
                 FROM places
                WHERE item_id = ?
                  AND timestamp_seconds IS NULL
                  AND slide_index IS NULL
                ORDER BY ordinal""",
            (item["id"],),
        ).fetchall()
        candidates.append(
            {
                "item_id": item["id"],
                "source_url": item["source_url"],
                "saved_at": item["created_at"],
                "places": [dict(place) for place in places],
            }
        )
    return candidates


def _existing_references(
    con: sqlite3.Connection,
    candidate: dict[str, Any],
) -> tuple[int, dict[str, dict[str, Any]]] | None:
    canonical_url = _canonical_source_url(candidate["source_url"])
    con.row_factory = sqlite3.Row
    rows = con.execute(
        """SELECT i.id AS item_id, i.source_url, p.extracted_name,
                  p.timestamp_seconds, p.slide_index
             FROM items i
             JOIN places p ON p.item_id = i.id
            WHERE i.id != ?
              AND (p.timestamp_seconds IS NOT NULL OR p.slide_index IS NOT NULL)
            ORDER BY i.id DESC, p.ordinal""",
        (candidate["item_id"],),
    ).fetchall()
    by_item: dict[int, dict[str, dict[str, Any]]] = {}
    for row in rows:
        if _canonical_source_url(row["source_url"]) != canonical_url:
            continue
        by_item.setdefault(row["item_id"], {})[_normalized_name(row["extracted_name"])] = {
            "timestamp_seconds": row["timestamp_seconds"],
            "slide_index": row["slide_index"],
        }
    expected = {_normalized_name(place["extracted_name"]) for place in candidate["places"]}
    for item_id, references in by_item.items():
        if expected.issubset(references):
            return item_id, references
    return None


def _extract_for_candidate(
    candidate: dict[str, Any],
    workdir: Path,
) -> list[dict[str, Any]]:
    names = [place["extracted_name"] for place in candidate["places"]]
    prompt = (
        "This is a media-reference backfill for places that were already saved. "
        "Return only these places, using each name exactly as written and in this order: "
        + json.dumps(names, ensure_ascii=False)
        + ". Determine timestamp_seconds or slide_index directly from the supplied media. "
        "Omit the media reference when a place is supported only by caption text or cannot "
        "be tied to a specific moment or slide. Do not add other places."
    )
    platform = source_platform(candidate["source_url"])
    if platform == "youtube":
        return extract_youtube_url(candidate["source_url"], prompt)
    fetched = fetch(candidate["source_url"], workdir)
    try:
        return extract(fetched.media_paths, fetched.metadata, prompt)
    finally:
        shutil.rmtree(fetched.cleanup_dir, ignore_errors=True)


def _updates_from_references(
    candidate: dict[str, Any],
    references: dict[str, dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    updates = []
    unresolved = []
    for place in candidate["places"]:
        reference = references.get(_normalized_name(place["extracted_name"]))
        if not reference or (
            reference.get("timestamp_seconds") is None
            and reference.get("slide_index") is None
        ):
            unresolved.append(place["extracted_name"])
            continue
        updates.append(
            {
                "place_id": place["id"],
                "item_id": candidate["item_id"],
                "extracted_name": place["extracted_name"],
                "timestamp_seconds": reference.get("timestamp_seconds"),
                "slide_index": reference.get("slide_index"),
            }
        )
    return updates, unresolved


def _write_plan(plan_path: Path, plan: dict[str, Any]) -> None:
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = plan_path.with_suffix(plan_path.suffix + ".tmp")
    temporary_path.write_text(json.dumps(plan, indent=2, ensure_ascii=False) + "\n")
    temporary_path.replace(plan_path)


def create_plan(
    db_path: Path,
    workdir: Path,
    plan_path: Path,
    *,
    resume: bool = False,
) -> dict[str, Any]:
    con = sqlite3.connect(db_path)
    try:
        candidates = find_candidates(con)
        if plan_path.exists():
            if not resume:
                raise FileExistsError(f"Refusing to overwrite existing plan: {plan_path}")
            plan = json.loads(plan_path.read_text())
            if plan.get("version") != 1 or plan.get("database") != str(db_path):
                raise ValueError("Existing plan does not match this database or plan version")
            plan["complete"] = False
        else:
            plan = {
                "version": 1,
                "created_at": datetime.now(UTC).isoformat(),
                "database": str(db_path),
                "candidate_count": len(candidates),
                "complete": False,
                "results": [],
            }
            _write_plan(plan_path, plan)

        completed_item_ids = {
            result["item_id"] for result in plan.get("results", [])
        }
        pending = [
            candidate
            for candidate in candidates
            if candidate["item_id"] not in completed_item_ids
        ]
        for index, candidate in enumerate(pending, start=1):
            print(
                f"[{index}/{len(pending)} remaining] item {candidate['item_id']} "
                f"({len(candidate['places'])} places)",
                flush=True,
            )
            reusable = _existing_references(con, candidate)
            try:
                if reusable:
                    reused_item_id, references = reusable
                    method = f"copied_from_item:{reused_item_id}"
                else:
                    extracted = _extract_for_candidate(candidate, workdir)
                    references = {
                        _normalized_name(place.get("extracted_name", "")): place
                        for place in extracted
                    }
                    method = "gemini_extraction"
                updates, unresolved = _updates_from_references(candidate, references)
                result = (
                    {
                        "item_id": candidate["item_id"],
                        "source_url": candidate["source_url"],
                        "method": method,
                        "updates": updates,
                        "unresolved": unresolved,
                    }
                )
            except Exception as exc:  # Continue so one unavailable post is reviewable.
                result = (
                    {
                        "item_id": candidate["item_id"],
                        "source_url": candidate["source_url"],
                        "method": "error",
                        "updates": [],
                        "unresolved": [place["extracted_name"] for place in candidate["places"]],
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
            plan["results"].append(result)
            _write_plan(plan_path, plan)
        plan["complete"] = True
        plan["completed_at"] = datetime.now(UTC).isoformat()
        _write_plan(plan_path, plan)
        return plan
    finally:
        con.close()


def apply_plan(db_path: Path, plan_path: Path, backup_dir: Path) -> tuple[int, Path]:
    plan = json.loads(plan_path.read_text())
    if plan.get("version") != 1:
        raise ValueError("Unsupported backfill plan version")
    updates = [
        update
        for result in plan.get("results", [])
        for update in result.get("updates", [])
    ]
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup_path = backup_dir / f"places-before-media-backfill-{timestamp}.db"

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
                """SELECT item_id, extracted_name, timestamp_seconds, slide_index
                     FROM places WHERE id = ?""",
                (update["place_id"],),
            ).fetchone()
            expected = (
                update["item_id"],
                update["extracted_name"],
                None,
                None,
            )
            if current != expected:
                raise RuntimeError(
                    f"Place row {update['place_id']} changed after plan creation; aborting"
                )
            source.execute(
                """UPDATE places
                      SET timestamp_seconds = ?, slide_index = ?
                    WHERE id = ?""",
                (
                    update.get("timestamp_seconds"),
                    update.get("slide_index"),
                    update["place_id"],
                ),
            )
            applied += 1
        source.commit()
    except Exception:
        source.rollback()
        raise
    finally:
        source.close()
    return applied, backup_path


def _summary(plan: dict[str, Any]) -> dict[str, Any]:
    results = plan.get("results", [])
    return {
        "candidates": plan.get("candidate_count", 0),
        "planned_updates": sum(len(result.get("updates", [])) for result in results),
        "unresolved": sum(len(result.get("unresolved", [])) for result in results),
        "errors": sum(result.get("method") == "error" for result in results),
        "copied_items": sum(str(result.get("method", "")).startswith("copied_from") for result in results),
        "gemini_items": sum(result.get("method") == "gemini_extraction" for result in results),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--backup-dir", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume plan generation, skipping every checkpointed item",
    )
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

    if args.plan.exists() and not args.resume:
        print(f"Refusing to overwrite existing plan: {args.plan}", file=sys.stderr)
        return 2
    plan = create_plan(
        args.db_path,
        args.workdir,
        args.plan,
        resume=args.resume,
    )
    print(json.dumps(_summary(plan), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
