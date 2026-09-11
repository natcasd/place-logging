"""A/B test old and new extraction prompts on identical downloaded media.

The database is opened read-only. Media is downloaded once per Source, supplied
to both prompt versions, and then deleted. Results are written only to a JSON
report and are never persisted as Entries.
"""
from __future__ import annotations

import argparse
import copy
import json
import mimetypes
import os
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.genai import types

import pipeline


BASELINE_COMMIT = "e89f01efbe0e6ffd7a3a1cc97def208a411353f8"
BASELINE_ENTRY_TYPES = (
    "Restaurant", "Café", "Bar", "Bakery", "Park", "Hiking Trail",
    "Bike Route", "Museum", "Art Gallery", "Store", "Spa", "Fitness",
    "Concert", "Pop-up", "Exhibit", "Book", "Movie", "Article", "Song",
    "Product", "Unknown",
)
BASELINE_TYPE_GUIDANCE = (
    "Choose exactly one primary type from this fixed list: Restaurant, Café, Bar, "
    "Bakery, Park, Hiking Trail, Bike Route, Museum, Art Gallery, Store, Spa, "
    "Fitness, Concert, Pop-up, Exhibit, Book, Movie, Article, Song, Product, or "
    "Unknown. Do not invent another type. Use Exhibit for a museum or gallery "
    "exhibition, installation, or curated show; use Pop-up for a temporary food, "
    "retail, or event offering. Use Unknown when the recommended subject is valid "
    "to save but none of the listed types fit, including a person or creator when "
    "no dedicated person type exists."
)
NEW_TIMING_GUIDANCE = (
    "- starts_at, ends_at, and recurrence_text: only when the recommended entry "
    "itself occurs or exists during a bounded or recurring time and that timing "
    "is directly supported by the source. Use ISO 8601 for starts_at and ends_at "
    "and preserve a human-readable recurring schedule in recurrence_text. NEVER "
    "use these fields for ordinary business hours, service windows, days open, "
    "release or publication metadata, or incidental dates; keep that information "
    "in the description. A temporary event or limited-run offering at a stable "
    "venue can be its own entry, with the venue used as its location."
)
BASELINE_TIMING_GUIDANCE = (
    "- starts_at, ends_at, and recurrence_text: ONLY for a Concert, Pop-up, or "
    "Exhibit, and only when directly supported by the source. Use ISO 8601 for "
    "starts_at and ends_at; preserve a human-readable recurring schedule in "
    "recurrence_text. Never put business hours, opening days, release dates, "
    "publication dates, or other timing on stable types such as Restaurant, Café, "
    "Museum, Book, Movie, or Product. If a temporary event or limited-run offering "
    "at a stable venue is itself the main recommendation, extract that event as a "
    "Concert, Pop-up, or Exhibit and use the venue only as its location."
)


def baseline_prompt_template() -> str:
    prompt = pipeline.EXTRACTOR_PROMPT.replace(
        pipeline.TYPE_NAME_GUIDANCE,
        BASELINE_TYPE_GUIDANCE,
    ).replace(NEW_TIMING_GUIDANCE, BASELINE_TIMING_GUIDANCE)
    if prompt == pipeline.EXTRACTOR_PROMPT:
        raise RuntimeError("Could not construct the pinned baseline prompt")
    return prompt


def baseline_schema() -> dict[str, Any]:
    schema = copy.deepcopy(pipeline.EXTRACTION_RESPONSE_SCHEMA)
    schema["properties"]["entries"]["items"]["properties"]["type_name"]["enum"] = list(
        BASELINE_ENTRY_TYPES
    )
    return schema


def _prompt(
    template: str,
    metadata: dict[str, Any],
    user_prompt: str | None,
) -> str:
    prompt = (
        template
        + "\n\nSource metadata (supporting evidence, but trust the video itself "
        "for on_screen_text / visual_landmarks):\n"
        + json.dumps(metadata, indent=2, ensure_ascii=False)
    )
    if user_prompt:
        prompt += (
            "\n\nUser prompt (highest authority — may clarify, correct, or override "
            "what you'd otherwise infer from the content):\n"
            + user_prompt
        )
    return prompt


def _extract_with_spec(
    media_paths: list[Path],
    metadata: dict[str, Any],
    user_prompt: str | None,
    *,
    prompt_template: str,
    response_schema: dict[str, Any],
    baseline: bool,
) -> dict[str, Any]:
    media_parts = [
        types.Part.from_bytes(
            data=path.read_bytes(),
            mime_type=mimetypes.guess_type(path.name)[0] or "application/octet-stream",
        )
        for path in media_paths
    ]
    response = pipeline._call_gemini_with_retry(
        lambda: pipeline._client().models.generate_content(
            model=os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
            contents=[*media_parts, _prompt(prompt_template, metadata, user_prompt)],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=response_schema,
            ),
        ),
        "baseline extraction" if baseline else "catalog extraction",
    )
    parsed = json.loads(response.text)
    entries = parsed.get("entries", parsed.get("places", []))
    normalized = pipeline._remove_generic_entry_names(
        pipeline._normalize_media_references(entries, metadata)
    )
    if baseline:
        for entry in normalized:
            if entry.get("type_name") not in {"Concert", "Pop-up", "Exhibit"}:
                for field in ("starts_at", "ends_at", "recurrence_text"):
                    entry.pop(field, None)
    else:
        normalized = pipeline._remove_invalid_timing_fields(normalized)
    return {
        "source_content": parsed.get("source_content") or {},
        "entries": normalized,
    }


def _normalized_name(entry: dict[str, Any]) -> str:
    return " ".join(str(entry.get("extracted_name") or "").casefold().split())


def compare_outputs(
    baseline: dict[str, Any],
    catalog: dict[str, Any],
) -> dict[str, Any]:
    old_by_name = {_normalized_name(entry): entry for entry in baseline["entries"]}
    new_by_name = {_normalized_name(entry): entry for entry in catalog["entries"]}
    shared = sorted((old_by_name.keys() & new_by_name.keys()) - {""})
    field_changes = []
    for name in shared:
        old = old_by_name[name]
        new = new_by_name[name]
        changes = {
            field: {"baseline": old.get(field), "catalog": new.get(field)}
            for field in (
                "type_name", "location_query", "starts_at", "ends_at",
                "recurrence_text", "slide_index", "timestamp_seconds",
            )
            if old.get(field) != new.get(field)
        }
        if changes:
            field_changes.append(
                {"name": new.get("extracted_name") or old.get("extracted_name"), "changes": changes}
            )
    return {
        "baseline_entry_count": len(baseline["entries"]),
        "catalog_entry_count": len(catalog["entries"]),
        "baseline_only_names": [old_by_name[name].get("extracted_name") for name in sorted(old_by_name.keys() - new_by_name.keys())],
        "catalog_only_names": [new_by_name[name].get("extracted_name") for name in sorted(new_by_name.keys() - old_by_name.keys())],
        "shared_name_count": len(shared),
        "field_changes": field_changes,
    }


def source_rows(con: sqlite3.Connection, item_ids: list[int]) -> list[dict[str, Any]]:
    con.row_factory = sqlite3.Row
    placeholders = ",".join("?" for _ in item_ids)
    rows = con.execute(
        f"""SELECT id, source_url, user_prompt
              FROM items
             WHERE id IN ({placeholders})
             ORDER BY id""",
        item_ids,
    ).fetchall()
    by_id = {row["id"]: dict(row) for row in rows}
    missing = [item_id for item_id in item_ids if item_id not in by_id]
    if missing:
        raise ValueError(f"Missing Source IDs: {missing}")
    return [by_id[item_id] for item_id in item_ids]


def run(
    db_path: Path,
    item_ids: list[int],
    output_path: Path,
    workdir: Path,
) -> dict[str, Any]:
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        sources = source_rows(con, item_ids)
    finally:
        con.close()
    report = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "baseline_commit": BASELINE_COMMIT,
        "model": os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite"),
        "read_only": True,
        "sources": [],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    workdir.mkdir(parents=True, exist_ok=True)
    old_prompt = baseline_prompt_template()
    old_schema = baseline_schema()

    for index, source in enumerate(sources, start=1):
        print(f"[{index}/{len(sources)}] Source {source['id']}: downloading", flush=True)
        fetched = None
        try:
            fetched = pipeline.fetch(source["source_url"], workdir)
            media_paths = list(fetched.media_paths)
            print(f"[{index}/{len(sources)}] Source {source['id']}: baseline", flush=True)
            old = _extract_with_spec(
                media_paths,
                fetched.metadata,
                source["user_prompt"],
                prompt_template=old_prompt,
                response_schema=old_schema,
                baseline=True,
            )
            print(f"[{index}/{len(sources)}] Source {source['id']}: catalog", flush=True)
            new = _extract_with_spec(
                media_paths,
                fetched.metadata,
                source["user_prompt"],
                prompt_template=pipeline.EXTRACTOR_PROMPT,
                response_schema=pipeline.EXTRACTION_RESPONSE_SCHEMA,
                baseline=False,
            )
            result = {
                "item_id": source["id"],
                "status": "compared",
                "media_count": len(media_paths),
                "comparison": compare_outputs(old, new),
                "baseline": old,
                "catalog": new,
            }
        except Exception as exc:
            result = {
                "item_id": source["id"],
                "status": "error",
                "error": f"{type(exc).__name__}: {exc}",
            }
        finally:
            if fetched is not None:
                shutil.rmtree(fetched.cleanup_dir, ignore_errors=True)
        report["sources"].append(result)
        output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--item-id", type=int, action="append", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workdir", type=Path, required=True)
    args = parser.parse_args()
    report = run(args.db_path, args.item_id, args.output, args.workdir)
    compared = [source for source in report["sources"] if source["status"] == "compared"]
    print(json.dumps({
        "requested": len(report["sources"]),
        "compared": len(compared),
        "errors": len(report["sources"]) - len(compared),
        "same_entry_count": sum(
            source["comparison"]["baseline_entry_count"]
            == source["comparison"]["catalog_entry_count"]
            for source in compared
        ),
        "output": str(args.output),
    }, indent=2))
    return 0 if len(compared) == len(report["sources"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
