"""Stable, browseable recommendation types shared by extraction and storage."""
from __future__ import annotations

import unicodedata
from typing import Any


THING_TYPES = (
    "Restaurant",
    "Café",
    "Bar",
    "Bakery",
    "Park",
    "Hiking Trail",
    "Bike Route",
    "Museum",
    "Art Gallery",
    "Store",
    "Spa",
    "Fitness",
    "Concert",
    "Pop-up",
    "Exhibit",
    "Book",
    "Movie",
    "Article",
    "Song",
    "Product",
    "Unknown",
)


def normalized_type_label(value: Any) -> str:
    """Normalize labels for matching while ignoring capitalization and accents."""
    text = " ".join(str(value or "").split()).casefold()
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", text)
        if not unicodedata.combining(character)
    )


_CANONICAL_TYPES = {normalized_type_label(name): name for name in THING_TYPES}

_TYPE_ALIASES = {
    "cafe": "Café",
    "coffee shop": "Café",
    "deli": "Restaurant",
    "dessert shop": "Bakery",
    "ice cream shop": "Bakery",
    "food popup": "Pop-up",
    "food pop up": "Pop-up",
    "exhibition": "Exhibit",
    "fitness exercise": "Fitness",
    "fitness coaching program": "Fitness",
    "grocery store": "Store",
    "market": "Store",
    "butcher shop": "Store",
    "cheese shop": "Store",
    "bike shop": "Store",
    "movie theater": "Movie",
}


def canonical_thing_type(value: Any) -> str:
    """Return the allowed display type, with Unknown for unsupported values."""
    normalized = normalized_type_label(value)
    return _CANONICAL_TYPES.get(normalized, _TYPE_ALIASES.get(normalized, "Unknown"))
