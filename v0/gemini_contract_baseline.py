#!/usr/bin/env python3
"""Read-only Gemini contract benchmark; never ingests or updates saved data."""
from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import logging
import os
from pathlib import Path
import tempfile
from typing import Any

from dotenv import load_dotenv
from google.genai import types

import pipeline
from entry_types import normalized_type_label


DEFAULT_CASES = Path(__file__).parent / "tests" / "fixtures" / "gemini_contract_cases.json"


def _identity(value: Any) -> str:
    return normalized_type_label(value).strip(" .,!?:;'-\"“”‘’")


def score_entries(
    entries: list[dict[str, Any]],
    expected: list[dict[str, Any]],
) -> dict[str, Any]:
    """Score observable extraction behavior, including unwanted extra entries."""
    remaining = list(entries)
    missing: list[str] = []
    wrong_type: list[dict[str, str]] = []
    wrong_fields: list[dict[str, Any]] = []
    for wanted in expected:
        aliases = [_identity(value) for value in [wanted["name"], *wanted.get("aliases", [])]]
        found = next(
            (entry for entry in remaining if _identity(entry.get("extracted_name")) in aliases),
            None,
        )
        if found is None:
            missing.append(wanted["name"])
            continue
        remaining.remove(found)
        if _identity(found.get("type_name")) != _identity(wanted["type"]):
            wrong_type.append(
                {"name": wanted["name"], "expected": wanted["type"], "actual": str(found.get("type_name"))}
            )
        if "has_location_query" in wanted:
            actual = bool(str(found.get("location_query") or "").strip())
            if actual != wanted["has_location_query"]:
                wrong_fields.append(
                    {"name": wanted["name"], "field": "has_location_query", "expected": wanted["has_location_query"], "actual": actual}
                )
        for field, expected_value in wanted.get("fields", {}).items():
            if found.get(field) != expected_value:
                wrong_fields.append(
                    {"name": wanted["name"], "field": field, "expected": expected_value, "actual": found.get(field)}
                )
        for field in wanted.get("absent_fields", []):
            if found.get(field) not in (None, ""):
                wrong_fields.append(
                    {"name": wanted["name"], "field": field, "expected": "absent", "actual": found.get(field)}
                )
    unexpected = [
        {"name": str(entry.get("extracted_name")), "type": str(entry.get("type_name"))}
        for entry in remaining
    ]
    return {
        "passed": not (missing or unexpected or wrong_type or wrong_fields),
        "missing": missing,
        "unexpected": unexpected,
        "wrong_type": wrong_type,
        "wrong_fields": wrong_fields,
    }


def _text_case(case: dict[str, Any], usage: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    metadata = {"source_platform": "instagram", **case.get("metadata", {})}
    prompt = pipeline._extraction_prompt(metadata)
    model = os.environ.get("GEMINI_MODEL", "gemini-3.1-flash-lite")
    response = pipeline._call_gemini_with_retry(
        lambda: pipeline._client().models.generate_content(
            model=model,
            contents=[case["post_text"], prompt],
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=pipeline.EXTRACTION_RESPONSE_SCHEMA,
            ),
        ),
        "text contract baseline",
        model_name=model,
        usage_sink=usage.append,
    )
    parsed = json.loads(response.text)
    return {
        "source_content": parsed.get("source_content") or {},
        "entries": pipeline._normalize_extracted_entries(parsed.get("entries", []), metadata),
    }, len(prompt)


def _media_case(case: dict[str, Any], usage: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    source_url = case["source_url"]
    if pipeline.source_platform(source_url) == "youtube":
        bundle = pipeline.extract_youtube_bundle(source_url, usage_sink=usage.append)
        prompt_chars = len(pipeline._extraction_prompt({"source_platform": "youtube", "webpage_url": source_url}))
        return bundle, prompt_chars
    with tempfile.TemporaryDirectory(prefix="gemini-contract-baseline-") as temp_dir:
        fetched = pipeline.fetch(source_url, Path(temp_dir))
        metadata = {**fetched.metadata, "media_preserved": False}
        prompt_chars = len(pipeline._extraction_prompt(metadata))
        bundle = pipeline.extract_bundle(fetched.media_paths, metadata, usage_sink=usage.append)
        return bundle, prompt_chars


def _tiebreaker_case(case: dict[str, Any], usage: list[dict[str, Any]]) -> tuple[dict[str, Any], int]:
    place = case["place"]
    candidates = case["candidates"]
    prompt_chars = len(pipeline._tiebreaker_prompt(place, candidates))
    decision = pipeline._llm_tiebreaker(place, candidates, usage_sink=usage.append)
    return decision, prompt_chars


def score_tiebreaker(decision: dict[str, Any], expected: dict[str, Any]) -> dict[str, Any]:
    wrong_fields = [
        {"field": field, "expected": value, "actual": decision.get(field)}
        for field, value in expected.items()
        if decision.get(field) != value
    ]
    return {
        "passed": not wrong_fields,
        "missing": [],
        "unexpected": [],
        "wrong_type": [],
        "wrong_fields": wrong_fields,
    }


def run_cases(
    cases: list[dict[str, Any]],
    suite: str,
    selected_ids: set[str] | None = None,
) -> dict[str, Any]:
    selected = [
        case for case in cases
        if (suite == "all" or case["suite"] == suite)
        and (selected_ids is None or case["id"] in selected_ids)
    ]
    results = []
    for case in selected:
        usage: list[dict[str, Any]] = []
        try:
            if case["suite"] == "text":
                bundle, prompt_chars = _text_case(case, usage)
            elif case["suite"] == "media":
                bundle, prompt_chars = _media_case(case, usage)
            else:
                decision, prompt_chars = _tiebreaker_case(case, usage)
                results.append(
                    {
                        "id": case["id"],
                        "suite": case["suite"],
                        "prompt_chars": prompt_chars,
                        "usage": usage,
                        "score": score_tiebreaker(decision, case["expected_decision"]),
                        "predicted": {"pick": decision.get("pick"), "confidence": decision.get("confidence")},
                    }
                )
                continue
            entries = bundle["entries"]
            score = score_entries(entries, case["expected_entries"])
            results.append(
                {
                    "id": case["id"],
                    "suite": case["suite"],
                    "prompt_chars": prompt_chars,
                    "usage": usage,
                    "score": score,
                    "predicted": [
                        {"name": entry.get("extracted_name"), "type": entry.get("type_name")}
                        for entry in entries
                    ],
                    "source_content_chars": {
                        key: len(str(value or ""))
                        for key, value in bundle.get("source_content", {}).items()
                    },
                }
            )
        except Exception as exc:
            results.append(
                {"id": case["id"], "suite": case["suite"], "usage": usage, "error": f"{type(exc).__name__}: {exc}"}
            )
    contract = "\n".join(
        (
            pipeline.EXTRACTOR_PROMPT,
            json.dumps(pipeline.EXTRACTION_RESPONSE_SCHEMA, sort_keys=True),
            inspect.getsource(pipeline._extraction_prompt),
            inspect.getsource(pipeline._tiebreaker_prompt),
            inspect.getsource(pipeline._llm_tiebreaker),
        )
    )
    return {
        "contract_sha256": hashlib.sha256(contract.encode()).hexdigest(),
        "cases": results,
        "summary": summarize(results),
    }


def summarize(results: list[dict[str, Any]]) -> dict[str, int]:
    return {
        "selected": len(results),
        "completed": sum("score" in result for result in results),
        "passed": sum(result.get("score", {}).get("passed", False) for result in results),
        **{
            field: sum(
                record.get(field) or 0
                for result in results for record in result.get("usage", [])
            )
            for field in ("input_tokens", "output_tokens", "total_tokens")
        },
    }


def merge_reports(reports: list[dict[str, Any]]) -> dict[str, Any]:
    if not reports:
        raise ValueError("At least one report is required")
    hashes = {report["contract_sha256"] for report in reports}
    if len(hashes) != 1:
        raise ValueError("Cannot merge reports from different Gemini contracts")
    by_id: dict[str, dict[str, Any]] = {}
    for report in reports:
        for result in report["cases"]:
            prior = by_id.get(result["id"])
            # Only fill transport failures; do not cherry-pick a better model
            # sample over an earlier completed quality result.
            if prior is None or "score" not in prior:
                by_id[result["id"]] = result
    cases = list(by_id.values())
    return {"contract_sha256": reports[0]["contract_sha256"], "cases": cases, "summary": summarize(cases)}


def compare_reports(prior: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    old = {case["id"]: case for case in prior["cases"] if "score" in case}
    new = {case["id"]: case for case in current["cases"] if "score" in case}
    common = sorted(old.keys() & new.keys())

    def defects(result: dict[str, Any]) -> int:
        score = result["score"]
        return sum(len(score[key]) for key in ("missing", "unexpected", "wrong_type", "wrong_fields"))

    regressions = [case_id for case_id in common if defects(new[case_id]) > defects(old[case_id])]
    improvements = [case_id for case_id in common if defects(new[case_id]) < defects(old[case_id])]

    def tokens(rows: dict[str, dict[str, Any]], field: str) -> int:
        return sum(
            record.get(field) or 0
            for case_id in common for record in rows[case_id]["usage"]
        )

    return {
        "matched_cases": len(common),
        "regressions": regressions,
        "improvements": improvements,
        "missing_prior_cases": sorted(new.keys() - old.keys()),
        "missing_current_cases": sorted(old.keys() - new.keys()),
        "prior_input_tokens": tokens(old, "input_tokens"),
        "current_input_tokens": tokens(new, "input_tokens"),
        "prior_output_tokens": tokens(old, "output_tokens"),
        "current_output_tokens": tokens(new, "output_tokens"),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--suite", choices=("text", "media", "tiebreaker", "all"), default="text")
    parser.add_argument("--ids", help="Comma-separated case IDs to run instead of the full suite")
    parser.add_argument("--merge-reports", type=Path, nargs="+", help="Combine retries from one unchanged contract without new Gemini calls")
    parser.add_argument("--compare-to", type=Path, help="Compare this run with a baseline report")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.merge_reports:
        report = merge_reports(
            [json.loads(path.read_text(encoding="utf-8")) for path in args.merge_reports]
        )
    else:
        load_dotenv(Path(__file__).with_name(".env"))
        if not os.environ.get("GEMINI_API_KEY"):
            parser.error("GEMINI_API_KEY must be configured")
        cases = json.loads(args.cases.read_text(encoding="utf-8"))
        if not isinstance(cases, list):
            parser.error("cases file must be a JSON array")
        selected_ids = set(args.ids.split(",")) if args.ids else None
        if selected_ids is not None:
            unknown_ids = selected_ids - {case["id"] for case in cases}
            if unknown_ids:
                parser.error(f"unknown case IDs: {', '.join(sorted(unknown_ids))}")
        report = run_cases(cases, args.suite, selected_ids)
    if args.compare_to:
        report["comparison"] = compare_reports(
            json.loads(args.compare_to.read_text(encoding="utf-8")), report
        )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("summary", "comparison") if key in report}, sort_keys=True))
    return 0 if report["summary"]["completed"] == report["summary"]["selected"] else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    raise SystemExit(main())
