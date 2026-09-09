#!/usr/bin/env python3
"""Read-only Place Logger production case discovery and export."""

from __future__ import annotations

import argparse
import base64
import json
import subprocess
import sys
from typing import Any


REMOTE_PROGRAM = r'''
import base64
import json
import re
import sqlite3
import sys
import unicodedata

request = json.loads(base64.b64decode(sys.argv[1]).decode())
con = sqlite3.connect("file:/data/places.db?mode=ro", uri=True)
con.row_factory = sqlite3.Row


def decode(value, fallback):
    try:
        parsed = json.loads(value) if value else fallback
    except (TypeError, ValueError):
        return fallback
    return parsed


def normalized(value):
    return unicodedata.normalize("NFKD", str(value or "")).encode(
        "ascii", "ignore"
    ).decode().casefold()


def contains_query(value, query):
    value = normalized(value)
    if value == query:
        return True
    if re.search(r"(?<![a-z0-9])" + re.escape(query) + r"(?![a-z0-9])", value):
        return True
    return len(query) >= 5 and query in value


def matches():
    sql = """SELECT e.id AS entry_id,
                    es.id AS source_connection_id,
                    es.item_id,
                    e.name,
                    e.entry_type,
                    es.source_name,
                    es.location_query,
                    es.resolution_status,
                    l.display_name AS location_name,
                    l.formatted_address,
                    i.source_url,
                    i.created_at
               FROM entries AS e
               JOIN entry_sources AS es ON es.entry_id = e.id
               JOIN items AS i ON i.id = es.item_id
               LEFT JOIN locations AS l ON l.id = e.location_id
              ORDER BY i.created_at DESC, es.id DESC"""
    rows = [dict(row) for row in con.execute(sql)]
    id_filters = {
        key: request.get(key)
        for key in ("entry_id", "source_connection_id", "item_id")
        if request.get(key) is not None
    }
    if request.get("ingest_id") is not None:
        ingest = con.execute(
            "SELECT item_id FROM ingest_runs WHERE id = ?",
            (request["ingest_id"],),
        ).fetchone()
        if not ingest or ingest["item_id"] is None:
            return []
        id_filters["item_id"] = ingest["item_id"]
    if id_filters:
        rows = [
            row for row in rows
            if all(row.get(key) == value for key, value in id_filters.items())
        ]
    query = normalized(request.get("query"))
    if query:
        fields = ("name", "source_name", "location_name", "formatted_address", "source_url")
        rows = [
            row for row in rows
            if any(contains_query(row.get(field), query) for field in fields)
        ]
    return rows[: request.get("limit", 20)]


def export_case():
    found = matches()
    if len(found) != 1:
        return {"status": "ambiguous", "matches": found}
    selected = found[0]
    source_connection_id = selected["source_connection_id"]
    occurrence = con.execute(
        """SELECT es.*, e.name AS canonical_name, e.entry_type AS canonical_type,
                  e.identity_key, e.location_id,
                  l.google_place_id, l.display_name AS location_name,
                  l.lat, l.lng, l.formatted_address, l.google_maps_url
             FROM entry_sources AS es
             JOIN entries AS e ON e.id = es.entry_id
             LEFT JOIN locations AS l ON l.id = e.location_id
            WHERE es.id = ?""",
        (source_connection_id,),
    ).fetchone()
    item = con.execute(
        "SELECT * FROM items WHERE id = ?", (selected["item_id"],)
    ).fetchone()
    legacy = con.execute(
        "SELECT * FROM places WHERE id = ?", (occurrence["legacy_place_id"],)
    ).fetchone() if occurrence["legacy_place_id"] is not None else None
    runs = [dict(row) for row in con.execute(
        "SELECT * FROM ingest_runs WHERE item_id = ? ORDER BY id",
        (selected["item_id"],),
    )]
    for run in runs:
        run["result_json"] = decode(run.get("result_json"), [])
        run["events"] = [dict(row) for row in con.execute(
            "SELECT * FROM ingest_events WHERE ingest_run_id = ? ORDER BY id",
            (run["id"],),
        )]
    sibling_sources = [dict(row) for row in con.execute(
        """SELECT es.id AS source_connection_id, es.item_id, es.source_name,
                  es.source_type, es.resolution_status, i.source_url, i.created_at
             FROM entry_sources AS es
             JOIN items AS i ON i.id = es.item_id
            WHERE es.entry_id = ? ORDER BY i.created_at DESC""",
        (occurrence["entry_id"],),
    )]
    item_dict = dict(item)
    item_dict["raw_payload_json"] = decode(item_dict.get("raw_payload_json"), {})
    item_dict["llm_output_json"] = decode(item_dict.get("llm_output_json"), [])
    occurrence_dict = dict(occurrence)
    for key in ("dishes_json", "tags_json", "resolution_candidates_json"):
        occurrence_dict[key] = decode(occurrence_dict.get(key), [])
    legacy_dict = dict(legacy) if legacy else None
    if legacy_dict:
        for key in ("dishes_json", "tags_json", "resolution_candidates_json"):
            legacy_dict[key] = decode(legacy_dict.get(key), [])
    return {
        "status": "ok",
        "selected": selected,
        "item": item_dict,
        "source_occurrence": occurrence_dict,
        "legacy_place": legacy_dict,
        "ingest_runs": runs,
        "canonical_entry_sources": sibling_sources,
        "limitations": [
            "llm_output_json contains normalized entries, not a guaranteed raw model response",
            "the historical model name and rendered prompt are not persisted",
            "reacquired source media is not guaranteed to match the original bytes",
        ],
    }


try:
    result = matches() if request["operation"] == "find" else export_case()
    print(json.dumps(result, ensure_ascii=False, indent=2))
finally:
    con.close()
'''


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("find", "export"))
    parser.add_argument("--app", default="place-logging")
    parser.add_argument("--query")
    parser.add_argument("--entry-id", type=int)
    parser.add_argument("--source-connection-id", type=int)
    parser.add_argument("--item-id", type=int)
    parser.add_argument("--ingest-id", type=int)
    parser.add_argument("--limit", type=int, default=20)
    return parser


def remote_command(request: dict[str, Any]) -> str:
    encoded_program = base64.b64encode(REMOTE_PROGRAM.encode()).decode()
    encoded_request = base64.b64encode(
        json.dumps(request, separators=(",", ":")).encode()
    ).decode()
    bootstrap = (
        "import base64,sys;"
        f"exec(base64.b64decode('{encoded_program}').decode())"
    )
    return f'python -c "{bootstrap}" \'{encoded_request}\''


def main() -> int:
    args = build_parser().parse_args()
    selectors = (
        args.query,
        args.entry_id,
        args.source_connection_id,
        args.item_id,
        args.ingest_id,
    )
    if not any(value is not None for value in selectors):
        raise SystemExit("provide --query or an ID selector")
    if not 1 <= args.limit <= 100:
        raise SystemExit("--limit must be between 1 and 100")
    request = {
        "operation": args.operation,
        "query": args.query,
        "entry_id": args.entry_id,
        "source_connection_id": args.source_connection_id,
        "item_id": args.item_id,
        "ingest_id": args.ingest_id,
        "limit": args.limit,
    }
    result = subprocess.run(
        [
            "flyctl",
            "ssh",
            "console",
            "--app",
            args.app,
            "--command",
            remote_command(request),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        if result.stderr:
            print(result.stderr.rstrip(), file=sys.stderr)
        return result.returncode
    print(result.stdout.rstrip())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
