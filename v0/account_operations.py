"""Small persistent-queue safeguards; no URLs or account IDs in metrics."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from threading import Event


class SavesPaused(Exception):
    pass


class SaveLimitExceeded(Exception):
    pass


@dataclass(frozen=True)
class CaptureLimits:
    user_pending: int = 20
    total_pending: int = 200
    user_daily: int = 100
    total_daily: int = 1000
    manual_retries: int = 5
    pause_file: Path | None = None

    def __post_init__(self):
        if any(type(n) is not int or n < 1 for n in
               (self.user_pending, self.total_pending, self.user_daily, self.total_daily, self.manual_retries)):
            raise ValueError('Save limits must be positive integers')

    def require_running(self):
        if self.pause_file is not None and self.pause_file.exists():
            raise SavesPaused()

    def check(self, con, user_id: str, *, retry_id: int | None = None):
        # Called within the same SQLite write transaction as acceptance. Replays
        # return before this check, so capacity cannot break idempotent recovery.
        self.require_running()
        pending = con.execute('''SELECT COUNT(*), COALESCE(SUM(user_id = ?), 0) FROM ingest_runs
            WHERE status IN ('queued', 'processing', 'retry_scheduled') AND (? IS NULL OR id != ?)''',
            (user_id, retry_id, retry_id)).fetchone()
        if pending[0] >= self.total_pending or pending[1] >= self.user_pending:
            raise SaveLimitExceeded()
        if retry_id is None:
            daily = con.execute('''SELECT COUNT(*), COALESCE(SUM(user_id = ?), 0) FROM ingest_runs
                WHERE intent != 'legacy' AND started_at >= datetime('now', '-1 day')''', (user_id,)).fetchone()
            # Cancelled Activity retains its run, so hiding failures cannot reset usage.
            if daily[0] >= self.total_daily or daily[1] >= self.user_daily:
                raise SaveLimitExceeded()
        elif con.execute('''SELECT COUNT(*) FROM ingest_events WHERE user_id = ?
                AND ingest_run_id = ? AND stage = 'accepted' AND message = 'Retry accepted' ''',
                (user_id, retry_id)).fetchone()[0] >= self.manual_retries:
            raise SaveLimitExceeded()


def queue_metrics(store) -> dict[str, int]:
    with store._transaction() as con:
        pending, age = con.execute('''SELECT COUNT(*),
            MAX(0, COALESCE(CAST((julianday('now') - julianday(MIN(started_at))) * 86400 AS INTEGER), 0))
            FROM ingest_runs WHERE status IN ('queued', 'processing', 'retry_scheduled')''').fetchone()
        failed = con.execute("SELECT COUNT(*) FROM ingest_runs WHERE status = 'failed' AND updated_at >= datetime('now', '-1 day')").fetchone()[0]
        active = con.execute("SELECT COUNT(*) FROM post_processing_cache WHERE status = 'processing' AND lease_expires_at > datetime('now')").fetchone()[0]
        deleting = con.execute("SELECT COUNT(*) FROM users WHERE status = 'deleting'").fetchone()[0]
    return {'pending_saves': pending, 'oldest_pending_seconds': age, 'failed_saves_24h': failed,
            'active_processing_jobs': active, 'pending_account_deletions': deleting}


def monitor_queue(store, stop: Event):
    log = logging.getLogger('jot.operations')
    while not stop.is_set():
        try:
            log.warning('Queue metrics %s', json.dumps(queue_metrics(store), sort_keys=True))
        except Exception:
            log.error('Queue metrics unavailable')
        stop.wait(60)


class PipelineUsageOnly(logging.Filter):
    """Suppress legacy pipeline URLs/payloads; retain numeric provider usage."""
    def filter(self, record):
        if record.msg != 'Gemini usage %s' or not isinstance(record.args, tuple) or len(record.args) != 1:
            return False
        try:
            usage = json.loads(record.args[0])
            counts = {k: v for k, v in usage.items() if k.endswith('_tokens') and type(v) is int and v >= 0}
        except (TypeError, ValueError, AttributeError):
            return False
        if not counts:
            return False
        record.msg = 'Gemini token usage %s'
        record.args = (json.dumps(counts, sort_keys=True),)
        record.exc_info = None
        record.exc_text = None
        record.stack_info = None
        return True


def configure_private_logging():
    pipeline = logging.getLogger('pipeline')
    pipeline.setLevel(logging.INFO)
    if not any(isinstance(f, PipelineUsageOnly) for f in pipeline.filters):
        pipeline.addFilter(PipelineUsageOnly())
    # HTTP debug logs can include credentials and responses; request paths also
    # include private record IDs. Aggregate queue/worker logs remain available.
    for name in ('httpx', 'httpcore', 'urllib3', 'google.auth', 'uvicorn.access'):
        logging.getLogger(name).disabled = True
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
