"""Bounded public processing with lease renewal and processing-only media.

The caller explicitly supplies a public-post processor. It receives only the
canonical URL, a temporary directory, and a progress callback. No user identity,
private text, location context, or editable library row crosses this boundary.
"""
from __future__ import annotations

import logging
import re
import shutil
from pathlib import Path
from threading import Event, Thread
from typing import Callable

from post_processing_store import LeaseLost, PostProcessingStore
from public_processing_adapter import ProcessedPost

log = logging.getLogger(__name__)
Processor = Callable[[str, Path, Callable[[str], None]], ProcessedPost | dict]


class PostProcessingWorker:
    def __init__(self, store: PostProcessingStore, workdir: Path, processor: Processor,
                 *, enrich: Callable[[], bool] | None = None):
        self.store = store
        self.workdir = workdir / 'jot-public-jobs'
        self.processor = processor
        self.enrich = enrich

    def clean_abandoned_media(self) -> int:
        # Only our token-named directories are eligible. Never scan/delete an
        # arbitrary application's temp files or follow a directory symlink.
        if not self.workdir.exists():
            return 0
        paths = list(self.workdir.iterdir())
        # Snapshot paths first: a new job claimed during this scan must never
        # have its directory mistaken for an abandoned one.
        active = self.store.active_tokens()
        removed = 0
        for path in paths:
            match = re.fullmatch(r'job-([0-9a-f]{32})', path.name)
            if match and match[1] not in active and path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
                removed += 1
        return removed

    def run_once(self) -> bool:
        delivered = self.store.deliver_ready()
        self.clean_abandoned_media()
        lease = self.store.claim()
        if lease is None:
            return bool(delivered)
        directory = self.workdir / ('job-' + lease.token)
        stopped = Event()
        lost = Event()
        stage = 'processing'

        def renew():
            while not stopped.wait(self.store.lease_seconds / 3):
                try:
                    self.store.renew(lease)
                except Exception:
                    lost.set()
                    return

        def progress(value):
            nonlocal stage
            if lost.is_set():
                raise LeaseLost()
            self.store.progress(lease, value)
            stage = value

        heartbeat = Thread(target=renew, name='jot-lease-renewal', daemon=True)
        heartbeat.start()
        try:
            directory.mkdir(parents=True, exist_ok=False)
            result = self.processor(lease.canonical_url, directory, progress)
            if lost.is_set():
                raise LeaseLost()
            progress('saving')
            if isinstance(result, ProcessedPost):
                self.store.publish(lease, result.original, locations=result.locations)
            else:
                self.store.publish(lease, result)
        except LeaseLost:
            pass  # Publication and all progress/failure writes are fenced.
        except Exception as error:
            try:
                self.store.fail(lease, error, stage=stage)
            except LeaseLost:
                pass
        finally:
            stopped.set()
            heartbeat.join(timeout=5)
            if directory.exists():
                shutil.rmtree(directory)
        self.store.deliver_ready()
        return True

    def run(self, stop: Event, *, poll_seconds: float = 2) -> None:
        if poll_seconds <= 0:
            raise ValueError('Polling interval must be positive')
        while not stop.is_set():
            try:
                busy = self.run_once()
                if self.enrich is not None:
                    busy = self.enrich() or busy
            except Exception as error:
                # Avoid logging provider payloads, URLs, or private context.
                log.error('Public worker iteration failed: %s', type(error).__name__)
                busy = False
            if not busy:
                stop.wait(poll_seconds)
