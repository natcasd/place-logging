"""Application service shared by every ingest transport."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pipeline import process_ingest
from store import (
    delete_place,
    delete_thing,
    delete_things,
    init_db,
    list_places,
    list_sources,
    list_thing_types,
    list_things,
    save_ingest,
)


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
        existing_types = list_thing_types(self.db_path)
        result = process_ingest(
            source_url,
            user_prompt,
            self.workdir,
            existing_types,
        )
        item_id = save_ingest(self.db_path, result)
        return {"item_id": item_id, **result}

    def places(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return saved things through the legacy places interface."""
        return list_places(self.db_path, limit)

    def things(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return canonical things with their source-specific recommendations."""
        return list_things(self.db_path, limit)

    def sources(self, limit: int = 200) -> list[dict[str, Any]]:
        """Return every saved source, including sources needing review."""
        return list_sources(self.db_path, limit)

    def delete_place(self, place_id: int) -> dict[str, int] | None:
        """Delete a logical place while preserving unrelated source places."""
        return delete_place(self.db_path, place_id)

    def delete_thing(self, thing_id: int) -> dict[str, int] | None:
        """Delete one canonical thing without deleting its source posts."""
        return delete_thing(self.db_path, thing_id)

    def delete_things(self, thing_ids: list[int]) -> dict[str, int] | None:
        """Delete canonical things without deleting their source posts."""
        return delete_things(self.db_path, thing_ids)
