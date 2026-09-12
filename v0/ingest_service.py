"""Application service shared by every ingest transport."""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit

import requests

from entry_types import entry_type_enricher
from movie_enrichment import MovieProvider, enrich_movie_entries
from pipeline import process_ingest
from pipeline import source_platform as detect_source_platform
from source_identity import TIKTOK_HOSTS, TIKTOK_SHORT_HOSTS, canonical_source_url
from store import (
    confirm_activity_location,
    delete_place,
    delete_entry,
    delete_entries,
    find_processed_source,
    init_db,
    finish_ingest_run,
    list_ingest_runs,
    list_places,
    list_sources,
    list_entries,
    save_ingest,
    saved_entry_outcomes,
    start_ingest_run,
    update_ingest_run,
)


STAGE_MESSAGES = {
    "accepted": "Starting processing",
    "fetching": "Downloading source media",
    "extracting": "Finding recommendations",
    "resolving": "Resolving locations",
    "saving": "Saving results",
}

log = logging.getLogger(__name__)


def _clean_tiktok_video_url(source_url: str) -> str:
    parsed = urlsplit(source_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    has_video_id = any(
        part.lower() == "video" and parts[index + 1].isdigit()
        for index, part in enumerate(parts[:-1])
    )
    if host in TIKTOK_HOSTS and has_video_id:
        return f"https://{host}/{'/'.join(parts)}"
    return source_url


def _resolve_shared_source_url(source_url: str) -> str:
    """Resolve opaque TikTok share links before deduplication and storage."""
    value = source_url.strip()
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    path_parts = [part for part in parsed.path.split("/") if part]
    is_web_short_link = (
        host in TIKTOK_HOSTS
        and bool(path_parts)
        and path_parts[0].lower() == "t"
    )
    if host not in TIKTOK_SHORT_HOSTS and not is_web_short_link:
        return _clean_tiktok_video_url(value)
    try:
        with requests.get(
            value,
            allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 (compatible; Jot/1.0)"},
            stream=True,
            timeout=10,
        ) as response:
            response.raise_for_status()
            resolved = response.url
    except requests.RequestException as exc:
        # yt-dlp has its own share-URL resolver, so a failed preflight should
        # not prevent ingestion; it only weakens cross-form deduplication.
        log.warning("Could not pre-resolve TikTok share URL: %s", exc)
        return value
    resolved_host = (urlsplit(resolved).hostname or "").lower().rstrip(".")
    if resolved_host not in TIKTOK_HOSTS:
        log.warning("Ignoring TikTok share redirect to unexpected host %s", resolved_host)
        return value
    return _clean_tiktok_video_url(resolved)


@dataclass(frozen=True)
class IngestService:
    db_path: Path
    workdir: Path
    movie_provider: MovieProvider | None = None
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
    ) -> dict[str, Any]:
        """Process and persist one source, returning the canonical result."""
        source_url = _resolve_shared_source_url(source_url)
        identity = canonical_source_url(source_url)
        with self._source_locks_guard:
            source_lock = self._source_locks.setdefault(identity, Lock())
        with source_lock:
            return self._ingest_once(source_url)

    def _ingest_once(
        self,
        source_url: str,
    ) -> dict[str, Any]:
        existing = find_processed_source(self.db_path, source_url)
        if existing is not None:
            return existing

        run_id = start_ingest_run(
            self.db_path,
            source_url,
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
            result = process_ingest(
                source_url,
                self.workdir,
                progress=report,
            )
            report("saving")
            item_id = save_ingest(self.db_path, result)
            outcomes = saved_entry_outcomes(self.db_path, item_id)
            if self.movie_provider is not None:
                movie_entry_ids = [
                    outcome["entry_id"]
                    for outcome in outcomes
                    if entry_type_enricher(outcome.get("type")) == "movie"
                ]
                enrich_movie_entries(
                    self.db_path,
                    self.movie_provider,
                    movie_entry_ids,
                )
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
                    else f"Saved {len(outcomes)} entry{'s' if len(outcomes) != 1 else ''}"
                ),
                item_id=item_id,
                outcomes=outcomes,
            )
            return {
                "ingest_id": run_id,
                "item_id": item_id,
                "saved_entries": outcomes,
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
        """Return saved entries through the legacy places interface."""
        return list_places(self.db_path, limit)

    def entries(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return canonical entries with their source-specific recommendations."""
        return list_entries(self.db_path, limit)

    def sources(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return every saved source, including sources needing review."""
        return list_sources(self.db_path, limit)

    def activity(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return durable processing history and canonical save outcomes."""
        return list_ingest_runs(self.db_path, limit)

    def delete_place(self, place_id: int) -> dict[str, int] | None:
        """Delete a logical place while preserving unrelated source places."""
        return delete_place(self.db_path, place_id)

    def delete_entry(self, entry_id: int) -> dict[str, int] | None:
        """Delete one canonical entry without deleting its source posts."""
        return delete_entry(self.db_path, entry_id)

    def delete_entries(self, entry_ids: list[int]) -> dict[str, int] | None:
        """Delete canonical entries without deleting their source posts."""
        return delete_entries(self.db_path, entry_ids)

    def confirm_activity_location(
        self,
        ingest_id: int,
        entry_id: int,
        candidate_id: str,
    ) -> dict[str, Any] | None:
        """Confirm a stored candidate for one Activity recommendation."""
        return confirm_activity_location(
            self.db_path,
            ingest_id,
            entry_id,
            candidate_id,
        )
