"""The validated, shared catalog of saveable entry types."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import unicodedata
from typing import Any


CATALOG_PATH = Path(__file__).with_name("entry_type_catalog.json")


@dataclass(frozen=True)
class EntryTypeDefinition:
    name: str
    description: str
    icon_system: str | None
    icon_asset: str | None
    enricher: str | None
    art_asset: str | None
    art_tint: str | None


def normalized_type_label(value: Any) -> str:
    """Normalize labels for matching while ignoring capitalization and accents."""
    text = " ".join(str(value or "").split()).casefold()
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    )


def _load_catalog() -> tuple[EntryTypeDefinition, ...]:
    raw_catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if not isinstance(raw_catalog, list) or not raw_catalog:
        raise RuntimeError("entry_type_catalog.json must contain a non-empty array")

    definitions: list[EntryTypeDefinition] = []
    normalized_names: set[str] = set()
    for index, raw in enumerate(raw_catalog):
        if not isinstance(raw, dict):
            raise RuntimeError(f"Entry type at index {index} must be an object")
        name = str(raw.get("name") or "").strip()
        description = str(raw.get("description") or "").strip()
        icon = raw.get("icon") or {}
        art = raw.get("art") or {}
        if not name or not description:
            raise RuntimeError(f"Entry type at index {index} needs name and description")
        normalized_name = normalized_type_label(name)
        if normalized_name in normalized_names:
            raise RuntimeError(f"Duplicate entry type name: {name}")
        normalized_names.add(normalized_name)
        icon_system = icon.get("system")
        icon_asset = icon.get("asset")
        if bool(icon_system) == bool(icon_asset):
            raise RuntimeError(
                f"{name} must define exactly one icon.system or icon.asset"
            )
        definitions.append(
            EntryTypeDefinition(
                name=name,
                description=description,
                icon_system=icon_system,
                icon_asset=icon_asset,
                enricher=raw.get("enricher"),
                art_asset=art.get("asset"),
                art_tint=art.get("tint"),
            )
        )

    if definitions[-1].name != "Unknown":
        raise RuntimeError("Unknown must be the final entry type")
    return tuple(definitions)


ENTRY_TYPE_DEFINITIONS = _load_catalog()
ENTRY_TYPES = tuple(definition.name for definition in ENTRY_TYPE_DEFINITIONS)
_DEFINITIONS_BY_NORMALIZED_NAME = {
    normalized_type_label(definition.name): definition
    for definition in ENTRY_TYPE_DEFINITIONS
}


def canonical_entry_type(value: Any) -> str:
    """Return an exact catalog name, with Unknown for unsupported values."""
    definition = _DEFINITIONS_BY_NORMALIZED_NAME.get(normalized_type_label(value))
    return definition.name if definition else "Unknown"


def entry_type_definition(value: Any) -> EntryTypeDefinition:
    """Return catalog behavior for a type, falling back to Unknown."""
    return _DEFINITIONS_BY_NORMALIZED_NAME.get(
        normalized_type_label(value),
        _DEFINITIONS_BY_NORMALIZED_NAME["unknown"],
    )


def entry_type_enricher(value: Any) -> str | None:
    """Return the optional post-save enricher declared by the catalog."""
    return entry_type_definition(value).enricher


def entry_types_for_enricher(enricher: str) -> tuple[str, ...]:
    """Return the catalog types handled by one post-save enricher."""
    return tuple(
        definition.name
        for definition in ENTRY_TYPE_DEFINITIONS
        if definition.enricher == enricher
    )


def type_name_guidance() -> str:
    """Render the classifier instructions directly from the type catalog."""
    lines = [
        "Choose exactly one primary type from this fixed list. Do not invent another type:"
    ]
    lines.extend(
        f'- {definition.name}: {definition.description}'
        for definition in ENTRY_TYPE_DEFINITIONS
    )
    return "\n".join(lines)
