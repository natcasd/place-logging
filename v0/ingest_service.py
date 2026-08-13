"""Application service shared by every ingest transport."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline import process_ingest
from store import init_db, list_places, save_ingest


@dataclass(frozen=True)
class IngestService:
    db_path: Path
    workdir: Path

    def initialize(self) -> None:
        init_db(self.db_path)

    def ingest(
        self,
        source_url: str,
        user_prompt: str | None = None,
    ) -> dict[str, Any]:
        """Process and persist one source, returning the canonical result."""
        result = process_ingest(source_url, user_prompt, self.workdir)
        item_id = save_ingest(self.db_path, result)
        return {"item_id": item_id, **result}

    def places(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return saved places for app clients."""
        return list_places(self.db_path, limit)
