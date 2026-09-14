"""Shared retry classification and provider-aware backoff rules."""
from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import requests


TRANSIENT_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504}
PERMANENT_STATUS_CODES = {400, 401, 404, 410, 422}
PERMANENT_MEDIA_MARKERS = (
    "private post",
    "private account",
    "login required",
    "video unavailable",
    "video not available",
    "has been removed",
    "unsupported url",
    "not a valid url",
)
TRANSIENT_MARKERS = (
    "http error 403",
    "http error 408",
    "http error 409",
    "http error 425",
    "http error 429",
    "http error 500",
    "http error 502",
    "http error 503",
    "http error 504",
    "rate limit",
    "too many requests",
    "temporarily unavailable",
    "unexpected response from webpage request",
    "unable to extract universal data",
    "unable to download video data",
    "remote end closed connection",
    "connection reset",
    "connection aborted",
    "timed out",
    "timeout",
    "try again",
    "could not be downloaded right now",
    "database is locked",
)


class AnalysisFailure(RuntimeError):
    """An extraction provider failed before returning a usable analysis."""

    def __init__(self, platform: str, cause: Exception) -> None:
        super().__init__(str(cause))
        self.platform = platform
        self.cause = cause


@dataclass(frozen=True)
class RetryDecision:
    retryable: bool
    failure_kind: str
    user_message: str
    retry_after_seconds: float | None = None


def _status_code(exc: Exception) -> int | None:
    for value in (
        getattr(exc, "code", None),
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        try:
            return int(value)
        except (TypeError, ValueError):
            continue
    return None


def _retry_after_header(exc: Exception) -> Any:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers:
        return headers.get("Retry-After") or headers.get("retry-after")
    return getattr(exc, "retry_after", None)


def retry_after_seconds(exc: Exception) -> float | None:
    """Read Retry-After as seconds or an HTTP date, including wrapped failures."""
    original = exc.cause if isinstance(exc, AnalysisFailure) else exc
    value = _retry_after_header(original)
    if value is None:
        match = re.search(
            r"retry[- ]after\s*[:=]?\s*(\d+(?:\.\d+)?)",
            str(original),
            flags=re.IGNORECASE,
        )
        value = match.group(1) if match else None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        pass
    try:
        parsed = parsedate_to_datetime(str(value))
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def classify_failure(
    exc: Exception,
    *,
    stage: str,
    platform: str,
) -> RetryDecision:
    """Map implementation exceptions to stable user-facing failure semantics."""
    original = exc.cause if isinstance(exc, AnalysisFailure) else exc
    code = _status_code(original)
    message = str(original).strip()
    normalized = message.casefold()
    delay = retry_after_seconds(original)

    if isinstance(exc, AnalysisFailure) or stage == "extracting":
        failure_kind = "analysis_failed"
        user_message = "Analysis failed before Jot could extract recommendations."
    elif stage == "fetching":
        failure_kind = "media_fetch_failed"
        user_message = f"{platform.capitalize()} media could not be downloaded."
    elif stage == "saving":
        failure_kind = "save_failed"
        user_message = "Jot could not save the extracted recommendations."
    else:
        failure_kind = "processing_failed"
        user_message = "Jot could not finish processing this source."

    permanent_marker = any(marker in normalized for marker in PERMANENT_MEDIA_MARKERS)
    if permanent_marker or code in PERMANENT_STATUS_CODES:
        retryable = False
    elif code in TRANSIENT_STATUS_CODES:
        retryable = True
    elif isinstance(
        original,
        (requests.Timeout, requests.ConnectionError, sqlite3.OperationalError),
    ):
        retryable = True
    elif any(marker in normalized for marker in TRANSIENT_MARKERS):
        retryable = True
    else:
        # Media and provider failures are often opaque. Bound their retries rather
        # than silently dropping a save; unknown application failures stay final.
        retryable = isinstance(exc, AnalysisFailure) or stage in {
            "fetching",
            "extracting",
        }

    if permanent_marker and message:
        user_message = message
    return RetryDecision(
        retryable=retryable,
        failure_kind=failure_kind,
        user_message=user_message,
        retry_after_seconds=delay,
    )


def retry_delay_seconds(
    exc: Exception,
    *,
    attempt: int,
    base_seconds: float,
    maximum_seconds: float,
) -> float:
    """Honor Retry-After, otherwise use bounded exponential backoff."""
    requested = retry_after_seconds(exc)
    if requested is not None:
        return min(maximum_seconds, requested)
    return min(maximum_seconds, base_seconds * (2 ** max(0, attempt - 1)))
