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
from retry_policy import AnalysisFailure, classify_failure, retry_delay_seconds
from source_identity import TIKTOK_HOSTS, TIKTOK_SHORT_HOSTS, canonical_source_url
from store import (
    add_ingest_event,
    confirm_activity_location,
    delete_entry,
    delete_entries,
    delete_failed_ingest_run,
    due_retry_ids,
    find_reusable_ingest_run,
    find_processed_source,
    get_ingest_run,
    init_db,
    finish_ingest_run,
    list_ingest_runs,
    list_sources,
    list_entries,
    prepare_ingest_retry,
    record_ingest_failure,
    recover_interrupted_ingests,
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

DURABLE_RETRY_BASE_SECONDS = 30.0
DURABLE_RETRY_MAX_SECONDS = 30.0 * 60.0


def _clean_tiktok_post_url(source_url: str) -> str:
    parsed = urlsplit(source_url)
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]
    has_post_id = any(
        part.lower() in {"video", "photo"} and parts[index + 1].isdigit()
        for index, part in enumerate(parts[:-1])
    )
    if host in TIKTOK_HOSTS and has_post_id:
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
        return _clean_tiktok_post_url(value)
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
    return _clean_tiktok_post_url(resolved)


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
            return {"status": "completed", **existing}

        platform = detect_source_platform(source_url)
        if platform == "other":
            raise ValueError(
                "Supported URLs are public Instagram posts, TikTok posts, and YouTube videos"
            )

        reusable_run_id = find_reusable_ingest_run(self.db_path, source_url)
        if reusable_run_id is not None:
            claimed = prepare_ingest_retry(
                self.db_path,
                reusable_run_id,
                automatic=False,
            )
            if claimed is not None:
                return self._process_run(reusable_run_id, claimed["source_url"])

        run_id = start_ingest_run(
            self.db_path,
            source_url,
            platform,
        )
        return self._process_run(run_id, source_url)

    def _process_run(
        self,
        run_id: int,
        source_url: str,
    ) -> dict[str, Any]:
        current_stage = "accepted"
        item_id: int | None = None
        outcomes: list[dict[str, Any]] = []
        result: dict[str, Any] | None = None

        def report(stage: str) -> None:
            nonlocal current_stage
            current_stage = stage
            update_ingest_run(
                self.db_path,
                run_id,
                stage,
                STAGE_MESSAGES.get(stage, stage.replace("_", " ").title()),
            )

        def report_retry(
            stage: str,
            message: str,
            delay: float,
            attempt: int,
            max_attempts: int,
        ) -> None:
            add_ingest_event(
                self.db_path,
                run_id,
                stage=stage,
                status="retrying",
                message=f"{message} (attempt {attempt} of {max_attempts})",
            )

        try:
            result = process_ingest(
                source_url,
                self.workdir,
                progress=report,
                retry_progress=report_retry,
            )
            metadata = result.get("metadata") or {}
            if metadata.get("extraction_status") == "failed":
                # Older pipeline implementations represented a provider failure
                # as an empty result. Convert that legacy shape into the durable
                # failure path before anything can be persisted.
                extraction_error = metadata.get("extraction_error") or {}
                message = extraction_error.get("message") or "Analysis failed"
                raise AnalysisFailure(
                    detect_source_platform(source_url),
                    RuntimeError(message),
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
                "status": final_status,
                **result,
            }
        except Exception as exc:
            if item_id is not None and result is not None:
                # The durable Source exists, so do not rerun it and create a
                # duplicate because an optional post-save enrichment failed.
                finish_ingest_run(
                    self.db_path,
                    run_id,
                    status="partial",
                    stage="completed",
                    message="Source saved, but optional enrichment needs attention",
                    item_id=item_id,
                    outcomes=outcomes,
                    error=exc,
                )
                return {
                    "ingest_id": run_id,
                    "item_id": item_id,
                    "saved_entries": outcomes,
                    "already_logged": False,
                    "status": "partial",
                    **result,
                }

            platform = detect_source_platform(source_url)
            decision = classify_failure(
                exc,
                stage=current_stage,
                platform=platform,
            )
            row = get_ingest_run(self.db_path, run_id) or {}
            attempt_count = int(row.get("attempt_count") or 1)
            delay = retry_delay_seconds(
                exc,
                attempt=attempt_count,
                base_seconds=DURABLE_RETRY_BASE_SECONDS,
                maximum_seconds=DURABLE_RETRY_MAX_SECONDS,
            )
            final_status = record_ingest_failure(
                self.db_path,
                run_id,
                stage=current_stage,
                error=exc,
                failure_kind=decision.failure_kind,
                user_message=decision.user_message,
                retryable=decision.retryable,
                retry_delay_seconds=delay,
            )
            failed = get_ingest_run(self.db_path, run_id) or {}
            return {
                "ingest_id": run_id,
                "item_id": None,
                "source_url": source_url,
                "metadata": {"source_platform": platform},
                "places_extracted": [],
                "resolved_places": [],
                "entries_extracted": [],
                "resolved_entries": [],
                "saved_entries": [],
                "already_logged": False,
                "status": final_status,
                "failure_kind": decision.failure_kind,
                "error_message": decision.user_message,
                "next_retry_at": failed.get("next_retry_at"),
            }

    def retry_ingest(
        self,
        ingest_id: int,
        *,
        automatic: bool = False,
    ) -> dict[str, Any] | None:
        """Retry one logical Activity item without creating another Source."""
        existing = get_ingest_run(self.db_path, ingest_id)
        if existing is None:
            return None
        identity = canonical_source_url(existing["source_url"])
        with self._source_locks_guard:
            source_lock = self._source_locks.setdefault(identity, Lock())
        with source_lock:
            claimed = prepare_ingest_retry(
                self.db_path,
                ingest_id,
                automatic=automatic,
            )
            if claimed is None:
                return None
            return self._process_run(ingest_id, claimed["source_url"])

    def run_due_retries(self, limit: int = 10) -> int:
        """Claim and process due retries; safe to call repeatedly from a worker."""
        processed = 0
        for ingest_id in due_retry_ids(self.db_path, limit):
            if self.retry_ingest(ingest_id, automatic=True) is not None:
                processed += 1
        return processed

    def recover_interrupted_ingests(self) -> int:
        return recover_interrupted_ingests(self.db_path)

    def delete_failed_activity(self, ingest_id: int) -> bool | None:
        """Delete one failure and cancel any scheduled retry for it."""
        existing = get_ingest_run(self.db_path, ingest_id)
        if existing is None:
            return None
        identity = canonical_source_url(existing["source_url"])
        with self._source_locks_guard:
            source_lock = self._source_locks.setdefault(identity, Lock())
        with source_lock:
            return delete_failed_ingest_run(self.db_path, ingest_id)

    def entries(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return canonical entries with their source-specific recommendations."""
        return list_entries(self.db_path, limit)

    def sources(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return every saved source, including sources needing review."""
        return list_sources(self.db_path, limit)

    def activity(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return durable processing history and canonical save outcomes."""
        return list_ingest_runs(self.db_path, limit)

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
