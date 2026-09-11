"""Dry-run the new type catalog on a small saved sample and regression fixtures.

This command is deliberately read-only: it never updates SQLite. Its report is
the approval artifact for a later, separately reviewed backfill.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from google.genai import types

from entry_types import CATALOG_PATH, ENTRY_TYPES, canonical_entry_type, type_name_guidance
from pipeline import _call_gemini_with_retry, _client


DEFAULT_SAMPLE_TYPES = (
    "Bar",
    "Store",
    *ENTRY_TYPES,
)
FIXTURE_PATH = Path(__file__).parent / "tests" / "fixtures" / "entry_type_regression_cases.json"
CLASSIFICATION_SCHEMA = {
    "type": "object",
    "properties": {
        "classifications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "case_id": {"type": "string"},
                    "type_name": {"type": "string", "enum": list(ENTRY_TYPES)},
                    "reason": {"type": "string"},
                },
                "required": ["case_id", "type_name", "reason"],
            },
        }
    },
    "required": ["classifications"],
}


def saved_sample(
    con: sqlite3.Connection,
    sample_types: tuple[str, ...] = DEFAULT_SAMPLE_TYPES,
    per_type: int = 5,
) -> list[dict[str, Any]]:
    """Select a deterministic, stratified sample without modifying the DB."""
    con.row_factory = sqlite3.Row
    sample: list[dict[str, Any]] = []
    for entry_type in sample_types:
        rows = con.execute(
            """SELECT e.id, e.name, e.entry_type, e.starts_at, e.ends_at,
                      e.recurrence_text, l.display_name AS location_name,
                      l.formatted_address,
                      GROUP_CONCAT(es.description, '\n') AS source_descriptions,
                      GROUP_CONCAT(es.location_query, '\n') AS location_queries
                 FROM entries AS e
                 LEFT JOIN locations AS l ON l.id = e.location_id
                 LEFT JOIN entry_sources AS es ON es.entry_id = e.id
                WHERE lower(trim(e.entry_type)) = lower(trim(?))
                GROUP BY e.id
                ORDER BY e.id DESC
                LIMIT ?""",
            (entry_type, per_type),
        ).fetchall()
        sample.extend(
            {
                "case_id": f"saved:{row['id']}",
                "entry_id": row["id"],
                "current_type": row["entry_type"],
                "name": row["name"],
                "description": row["source_descriptions"] or "",
                "location_query": row["location_queries"] or None,
                "location_name": row["location_name"],
                "formatted_address": row["formatted_address"],
                "starts_at": row["starts_at"],
                "ends_at": row["ends_at"],
                "recurrence_text": row["recurrence_text"],
            }
            for row in rows
        )
    return sample


def fixture_cases(path: Path = FIXTURE_PATH) -> list[dict[str, Any]]:
    cases = json.loads(path.read_text(encoding="utf-8"))
    for case in cases:
        if case.get("expected_type") not in ENTRY_TYPES:
            raise ValueError(f"Invalid expected type in fixture {case.get('case_id')}")
    return cases


def classify(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Classify only the supplied records with the production catalog guidance."""
    if not cases:
        return []
    prompt = f"""Classify these already-selected, save-worthy recommendations.

This is a type-only evaluation. Return exactly one result for every case_id.
Do not add, remove, merge, or rename records. Location and timing are evidence,
not restrictions: any type may have or lack either property.

{type_name_guidance()}

Cases:
{json.dumps(cases, ensure_ascii=False)}
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
        "entry type migration evaluation",
    )
    returned = json.loads(response.text).get("classifications") or []
    expected_ids = {case["case_id"] for case in cases}
    by_id: dict[str, dict[str, Any]] = {}
    for result in returned:
        case_id = result.get("case_id")
        if case_id not in expected_ids or case_id in by_id:
            raise ValueError("Model returned an unexpected or duplicate case_id")
        by_id[case_id] = {
            "case_id": case_id,
            "type_name": canonical_entry_type(result.get("type_name")),
            "reason": str(result.get("reason") or "")[:500],
        }
    if set(by_id) != expected_ids:
        raise ValueError(f"Model omitted case ids: {sorted(expected_ids - set(by_id))}")
    return [by_id[case["case_id"]] for case in cases]


def build_report(
    saved_cases: list[dict[str, Any]],
    fixtures: list[dict[str, Any]],
    classifier=classify,
) -> dict[str, Any]:
    saved_results = classifier(saved_cases) if saved_cases else []
    fixture_results = classifier(fixtures) if fixtures else []
    saved_by_id = {result["case_id"]: result for result in saved_results}
    fixture_by_id = {result["case_id"]: result for result in fixture_results}

    saved_comparisons = []
    for case in saved_cases:
        result = saved_by_id[case["case_id"]]
        current = case["current_type"]
        saved_comparisons.append(
            {
                **case,
                "proposed_type": result["type_name"],
                "changed": current.casefold() != result["type_name"].casefold(),
                "reason": result["reason"],
            }
        )

    fixture_comparisons = []
    for case in fixtures:
        result = fixture_by_id[case["case_id"]]
        fixture_comparisons.append(
            {
                **case,
                "actual_type": result["type_name"],
                "passed": case["expected_type"] == result["type_name"],
                "reason": result["reason"],
            }
        )

    passed = sum(comparison["passed"] for comparison in fixture_comparisons)
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "catalog_sha256": hashlib.sha256(CATALOG_PATH.read_bytes()).hexdigest(),
        "catalog_types": list(ENTRY_TYPES),
        "read_only": True,
        "saved_sample": {
            "count": len(saved_comparisons),
            "current_counts": dict(Counter(case["current_type"] for case in saved_cases)),
            "proposed_counts": dict(Counter(case["proposed_type"] for case in saved_comparisons)),
            "changed_count": sum(case["changed"] for case in saved_comparisons),
            "comparisons": saved_comparisons,
        },
        "fixtures": {
            "count": len(fixture_comparisons),
            "passed": passed,
            "accuracy": passed / len(fixture_comparisons) if fixture_comparisons else None,
            "comparisons": fixture_comparisons,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--per-type", type=int, default=5)
    parser.add_argument("--fixtures", type=Path, default=FIXTURE_PATH)
    args = parser.parse_args()

    sample: list[dict[str, Any]] = []
    if args.db_path:
        con = sqlite3.connect(f"file:{args.db_path}?mode=ro", uri=True)
        try:
            sample = saved_sample(con, per_type=args.per_type)
        finally:
            con.close()
    report = build_report(sample, fixture_cases(args.fixtures))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps({
        "saved_sample": report["saved_sample"]["count"],
        "saved_changes": report["saved_sample"]["changed_count"],
        "fixture_accuracy": report["fixtures"]["accuracy"],
        "output": str(args.output),
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
