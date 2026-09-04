"""Application service shared by every ingest transport."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any

from pipeline import process_ingest
from pipeline import source_platform as detect_source_platform
from source_identity import canonical_source_url
from store import (
    delete_place,
    delete_thing,
    delete_things,
    find_processed_source,
    init_db,
    finish_ingest_run,
    list_ingest_runs,
    list_places,
    list_sources,
    list_thing_types,
    list_things,
    save_ingest,
    saved_thing_outcomes,
    start_ingest_run,
    update_ingest_run,
)


STAGE_MESSAGES = {
    "accepted": "Starting processing",
    "fetching": "Downloading source media",
    "archiving": "Preserving source media",
    "extracting": "Finding recommendations",
    "resolving": "Resolving locations",
    "saving": "Saving results",
}


@dataclass(frozen=True)
class IngestService:
    db_path: Path
    workdir: Path
    _source_locks: dict[str, Lock] = field(
        default_factory=dict,
        init=False,
        repr=False,
        compare=False,
    )
    _source_locks_guard: Lock = field(
        default_factory=Lock,
        init=False,
        repr=False,
        compare=False,
    )

    def initialize(self) -> None:
        init_db(self.db_path)

    def ingest(
        self,
        source_url: str,
        user_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Process and persist one source, returning the canonical result."""
        identity = canonical_source_url(source_url)
        with self._source_locks_guard:
            source_lock = self._source_locks.setdefault(identity, Lock())
        with source_lock:
            return self._ingest_once(source_url, user_prompt)

    def _ingest_once(
        self,
        source_url: str,
        user_prompt: str | None,
    ) -> dict[str, Any]:
        existing = find_processed_source(self.db_path, source_url)
        if existing is not None:
            return existing

        run_id = start_ingest_run(
            self.db_path,
            source_url,
            user_prompt,
            detect_source_platform(source_url),
        )
        current_stage = "accepted"
        item_id: int | None = None
        outcomes: list[dict[str, Any]] = []

        def report(stage: str) -> None:
            nonlocal current_stage
            current_stage = stage
            update_ingest_run(
                self.db_path,
                run_id,
                stage,
                STAGE_MESSAGES.get(stage, stage.replace("_", " ").title()),
            )

        try:
            existing_types = list_thing_types(self.db_path)
            result = process_ingest(
                source_url,
                user_prompt,
                self.workdir,
                existing_types,
                progress=report,
            )
            report("saving")
            item_id = save_ingest(self.db_path, result)
            outcomes = saved_thing_outcomes(self.db_path, item_id)
            needs_review = (
                not outcomes
                or (result.get("metadata") or {}).get("extraction_status") == "failed"
                or any(
                    outcome["resolution_status"] in {"needs_review", "unresolved"}
                    for outcome in outcomes
                )
            )
            final_status = "partial" if needs_review else "completed"
            finish_ingest_run(
                self.db_path,
                run_id,
                status=final_status,
                stage="completed",
                message=(
                    "Source saved with results needing review"
                    if needs_review
                    else f"Saved {len(outcomes)} thing{'s' if len(outcomes) != 1 else ''}"
                ),
                item_id=item_id,
                outcomes=outcomes,
            )
            return {
                "ingest_id": run_id,
                "item_id": item_id,
                "saved_things": outcomes,
                "already_logged": False,
                **result,
            }
        except Exception as exc:
            finish_ingest_run(
                self.db_path,
                run_id,
                status="failed",
                stage=current_stage,
                message=f"Failed while {STAGE_MESSAGES.get(current_stage, current_stage)}",
                item_id=item_id,
                outcomes=outcomes,
                error=exc,
            )
            raise

    def places(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return saved things through the legacy places interface."""
        return list_places(self.db_path, limit)

    def things(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return canonical things with their source-specific recommendations."""
        return list_things(self.db_path, limit)

    def sources(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return every saved source, including sources needing review."""
        return list_sources(self.db_path, limit)

    def activity(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return durable processing history and canonical save outcomes."""
        return list_ingest_runs(self.db_path, limit)

    def delete_place(self, place_id: int) -> dict[str, int] | None:
        """Delete a logical place while preserving unrelated source places."""
        return delete_place(self.db_path, place_id)

    def delete_thing(self, thing_id: int) -> dict[str, int] | None:
        """Delete one canonical thing without deleting its source posts."""
        return delete_thing(self.db_path, thing_id)

    def delete_things(self, thing_ids: list[int]) -> dict[str, int] | None:
        """Delete canonical things without deleting their source posts."""
        return delete_things(self.db_path, thing_ids)
