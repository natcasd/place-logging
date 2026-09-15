"""Rehearse the multi-user migration on a new, private SQLite copy.

Never rewrites the source and never runs from application startup. The generated
database is a foundation for account-scoped storage, not a deployable API backend.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from source_identity import canonical_source_url


SCHEMA_VERSION = 1
APPLICATION_ID = 0x4A4F544D  # JOTM; distinguishes this from unrelated user_version values.
SCHEMA_PATH = Path(__file__).with_name("multi_user_schema.sql")
TABLES = (
    "captures", "locations", "recommendations", "recommendation_mentions",
    "movie_enrichments", "ingest_runs", "ingest_events",
)
OWNED_TABLES = set(TABLES) - {"locations"}


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [row[1] for row in con.execute(f"PRAGMA table_info({_quote(table)})")]


def _legacy_columns() -> dict[str, list[str]]:
    # Freeze the pre-multi-user layout: later store.py changes must not redefine
    # the old schema accepted by this migration.
    fields = {
        "captures": (
            'id vertical source_url raw_payload_json llm_output_json created_at'
        ),
        "locations": (
            'id google_place_id display_name lat lng formatted_address google_maps_url created_at '
            'updated_at'
        ),
        "recommendations": (
            'id name normalized_name entry_type type_key identity_key location_id starts_at '
            'ends_at recurrence_text created_at updated_at'
        ),
        "recommendation_mentions": (
            'id entry_id item_id ordinal source_name source_type description dishes_json '
            'why_its_cool tags_json timestamp_seconds slide_index resolution_status '
            'resolution_candidates_json location_query created_at'
        ),
        "movie_enrichments": (
            'entry_id provider provider_id resolved_title release_year letterboxd_url match_status '
            'match_confidence checked_at'
        ),
        "ingest_runs": (
            'id source_url source_platform status stage item_id result_json error_type '
            'error_message failure_kind retryable attempt_count max_attempts next_retry_at '
            'last_retry_at started_at updated_at completed_at'
        ),
        "ingest_events": (
            'id ingest_run_id stage status message created_at'
        ),
    }
    return {table: names.split() for table, names in fields.items()}


def _fingerprint(con: sqlite3.Connection, table: str, columns: list[str]) -> dict[str, Any]:
    projection = ", ".join(map(_quote, columns))
    key = "entry_id" if table == "movie_enrichments" else "id"
    digest = hashlib.sha256()
    count = 0
    for row in con.execute(f"SELECT {projection} FROM {_quote(table)} ORDER BY {_quote(key)}"):
        digest.update(json.dumps(
            list(row), ensure_ascii=False, separators=(",", ":"),
            default=lambda value: {"bytes": value.hex()},
        ).encode())
        digest.update(b"\n")
        count += 1
    return {"rows": count, "sha256": digest.hexdigest()}


def _snapshot(con: sqlite3.Connection, layout: dict[str, list[str]]) -> dict[str, Any]:
    return {table: _fingerprint(con, table, columns) for table, columns in layout.items()}


def _check_integrity(con: sqlite3.Connection) -> None:
    if [row[0] for row in con.execute("PRAGMA integrity_check")] != ["ok"]:
        raise ValueError("SQLite integrity check failed")
    violations = con.execute("PRAGMA foreign_key_check").fetchall()
    if violations:
        raise ValueError(f"Foreign key check failed: {violations[:5]}")


def public_identity(url: str | None) -> tuple[str, str] | None:
    """Stable cache identity, including equivalent Instagram /p and /reel URLs."""
    if not url:
        return None
    parsed = urlsplit(canonical_source_url(url))
    parts = [part for part in parsed.path.split("/") if part]
    host = parsed.hostname
    if host == "www.instagram.com" and len(parts) == 2 and parts[0] in {"p", "reel", "tv"}:
        return "instagram", parts[1]
    if host == "www.youtube.com" and parsed.path == "/watch":
        post_id = parse_qs(parsed.query).get("v", [""])[0]
        if post_id:
            return "youtube", post_id
    if host == "www.tiktok.com" and len(parts) == 3 and parts[1] == "video" and parts[2].isdigit():
        return "tiktok", parts[2]
    return None


def _execute_schema(con: sqlite3.Connection) -> None:
    # executescript() would commit an open transaction before executing its SQL.
    statement = ""
    for line in SCHEMA_PATH.read_text().splitlines(keepends=True):
        statement += line
        if sqlite3.complete_statement(statement):
            con.execute(statement)
            statement = ""
    if statement.strip():
        raise ValueError("Incomplete multi-user schema statement")


def _validate_target(con: sqlite3.Connection, owner_id: str) -> None:
    if con.execute("PRAGMA user_version").fetchone()[0] != SCHEMA_VERSION:
        raise ValueError("Unexpected multi-user schema version")
    if con.execute("PRAGMA application_id").fetchone()[0] != APPLICATION_ID:
        raise ValueError("Unexpected database application ID")
    if con.execute("SELECT 1 FROM users WHERE id = ?", (owner_id,)).fetchone() is None:
        raise ValueError("Migration owner is not present in this database")
    _check_integrity(con)


def migrate_copy(con: sqlite3.Connection, *, owner_id: str, owner_name: str) -> dict[str, Any]:
    """Migrate an offline connection transactionally; call prepare_copy for safe file handling.

    Reapplying to this schema is a validated no-op. Unknown schemas, additional
    columns/tables/triggers require explicit review. Historical duplicate captures
    are preserved as unverified and reported, never silently merged or promoted.
    """
    if not owner_id.strip() or not owner_name.strip():
        raise ValueError("Owner ID and display name must be nonempty")
    if con.in_transaction:
        raise ValueError("Migration requires a connection without an open transaction")
    version = con.execute("PRAGMA user_version").fetchone()[0]
    app_id = con.execute("PRAGMA application_id").fetchone()[0]
    if version == SCHEMA_VERSION and app_id == APPLICATION_ID:
        _validate_target(con, owner_id)
        return {"schema_version": version, "already_migrated": True}
    if version != 0 or app_id != 0:
        raise ValueError("Unrecognized database version; refusing to migrate")

    layout = _legacy_columns()
    tables = {row[0] for row in con.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
    )}
    if tables != set(TABLES):
        raise ValueError(f"Expected current single-user tables; missing={sorted(set(TABLES)-tables)}, extra={sorted(tables-set(TABLES))}")
    for table, columns in layout.items():
        actual_columns = _columns(con, table)
        if set(actual_columns) != set(columns):
            raise ValueError(f"Unexpected columns in {table}; update/review the source schema first")
        # ALTER TABLE additions put columns at the end on upgraded installations.
        # Preserve their actual order when reading SELECT * and hashing old values.
        layout[table] = actual_columns
    if con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('trigger', 'view')").fetchone():
        raise ValueError("Source has custom triggers/views; review their migration first")
    _check_integrity(con)

    foreign_keys = con.execute("PRAGMA foreign_keys").fetchone()[0]
    con.execute("PRAGMA foreign_keys = OFF")
    try:
        con.execute("BEGIN IMMEDIATE")
        before = _snapshot(con, layout)
        sequences = dict(con.execute("SELECT name, seq FROM sqlite_sequence"))
        data = {
            table: [dict(zip(columns, row)) for row in con.execute(f"SELECT * FROM {_quote(table)}")]
            for table, columns in layout.items()
        }
        identities: dict[tuple[str, str], list[int]] = {}
        capture_identities = {}
        for capture in data["captures"]:
            identity = public_identity(capture["source_url"])
            if identity is not None:
                identities.setdefault(identity, []).append(capture["id"])
            capture_identities[capture["id"]] = identity

        # All original values are copied back without normalization or promotion.
        # Foreign keys are checked again before the transaction can commit.
        for table in reversed(TABLES):
            con.execute(f"DROP TABLE {_quote(table)}")
        _execute_schema(con)
        con.execute("INSERT INTO users (id, display_name) VALUES (?, ?)", (owner_id, owner_name))
        for table in TABLES:
            for record in data[table]:
                values = dict(record)
                if table in OWNED_TABLES:
                    values["user_id"] = owner_id
                if table == "captures":
                    identity = capture_identities[record["id"]]
                    values.update(
                        input_kind="public_post" if identity else "legacy",
                        capture_channel="legacy",
                        source_platform=identity[0] if identity else None,
                        source_post_id=identity[1] if identity else None,
                        materialization_state="legacy_unverified",
                        last_submitted_at=record["created_at"],
                    )
                elif table == "recommendation_mentions":
                    # Historical ordinal/output correspondence is not assumed.
                    values["output_key"] = f"legacy-mention:{record['id']}"
                elif table == "ingest_runs":
                    values.update(idempotency_key=f"legacy-ingest:{record['id']}", intent="legacy")
                names = ", ".join(map(_quote, values))
                placeholders = ", ".join("?" for _ in values)
                con.execute(f"INSERT INTO {_quote(table)} ({names}) VALUES ({placeholders})", list(values.values()))

        # Preserve AUTOINCREMENT high-water marks, including previously deleted IDs.
        for table, sequence in sequences.items():
            if table in TABLES:
                con.execute("DELETE FROM sqlite_sequence WHERE name = ?", (table,))
                con.execute("INSERT INTO sqlite_sequence (name, seq) VALUES (?, ?)", (table, sequence))
        after = _snapshot(con, layout)
        if after != before:
            raise ValueError("Migration changed original library data; rolling back")
        for table in OWNED_TABLES:
            if con.execute(f"SELECT COUNT(*) FROM {_quote(table)} WHERE user_id != ?", (owner_id,)).fetchone()[0]:
                raise ValueError(f"Owner reconciliation failed in {table}")
        con.execute(f"PRAGMA application_id = {APPLICATION_ID}")
        con.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        _validate_target(con, owner_id)
        con.commit()
        return {
            "schema_version": SCHEMA_VERSION,
            "already_migrated": False,
            "owner_id": owner_id,
            "original_data_preserved": True,
            "tables": after,
            "shared_cache_rows": 0,
            "unverified_legacy_captures": len(data["captures"]),
            "legacy_duplicate_capture_groups": [ids for ids in identities.values() if len(ids) > 1],
            "foreign_key_check": "ok",
            "legacy_api_compatible": False,
        }
    except BaseException:
        con.rollback()
        raise
    finally:
        con.execute(f"PRAGMA foreign_keys = {foreign_keys}")


def prepare_copy(source: Path, output: Path, *, owner_id: str, owner_name: str) -> dict[str, Any]:
    """Take a consistent read-only backup, migrate it, publish without overwriting.

    Output is mode 0600. A failed migration never publishes a partial database.
    The untouched source is the pre-migration recovery copy, not a live rollback.
    """
    source = source.resolve(strict=True)
    output = output.absolute()
    if source == output.resolve() or output.exists() or output.is_symlink():
        raise ValueError("Output must be a new file, different from the source")
    if not output.parent.is_dir():
        raise ValueError("Output directory must already exist")
    descriptor, temporary_name = tempfile.mkstemp(prefix=".multi-user-", suffix=".db", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        original = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
        target = sqlite3.connect(temporary)
        try:
            original.backup(target)
            # The copy must be self-contained when published, even from a WAL source.
            target.execute("PRAGMA journal_mode = DELETE")
            target.execute("PRAGMA foreign_keys = ON")
            report = migrate_copy(target, owner_id=owner_id, owner_name=owner_name)
        finally:
            target.close()
            original.close()
        with temporary.open("rb") as handle:
            os.fsync(handle.fileno())
        # Same directory/filesystem. link() fails if a competing process created output.
        os.link(temporary, output)
        return {**report, "output": str(output)}
    finally:
        temporary.unlink(missing_ok=True)
        for suffix in ("-journal", "-wal", "-shm"):
            Path(str(temporary) + suffix).unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--owner-id", required=True, help="Stable internal account ID; not a login credential")
    parser.add_argument("--owner-name", required=True)
    args = parser.parse_args()
    report = prepare_copy(args.source, args.output, owner_id=args.owner_id, owner_name=args.owner_name)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
