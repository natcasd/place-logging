"""Expose Instagram's native location through yt-dlp's plugin interface.

The built-in extractor already receives this data in the post payload but does
not currently include it in the returned info dict. Subclassing it as a plugin
keeps the rest of the upstream extractor behavior, including future updates.
"""

from __future__ import annotations

from typing import Any

from yt_dlp.extractor.instagram import InstagramIE


def _finite_coordinate(value: Any, minimum: float, maximum: float) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        coordinate = float(value)
    except (TypeError, ValueError):
        return None
    if minimum <= coordinate <= maximum:
        return coordinate
    return None


def _instagram_location(product_info: Any) -> dict[str, Any] | None:
    """Return only useful, non-ID context from Instagram's location object."""
    if isinstance(product_info, list):
        product_info = product_info[0] if product_info else None
    if not isinstance(product_info, dict):
        return None

    raw_location = product_info.get("location")
    if not isinstance(raw_location, dict):
        return None

    location: dict[str, Any] = {}
    for field in ("name", "address", "city"):
        value = raw_location.get(field)
        if isinstance(value, str) and value.strip():
            location[field] = value.strip()

    latitude = _finite_coordinate(raw_location.get("lat"), -90, 90)
    longitude = _finite_coordinate(raw_location.get("lng"), -180, 180)
    if latitude is not None and longitude is not None:
        location["latitude"] = latitude
        location["longitude"] = longitude

    return location or None


class _PlaceLoggerInstagramIE(
    InstagramIE,
    plugin_name="place_logger_location",
):
    """Add fields while delegating the complete extraction to upstream."""

    def _extract_product(self, product_info, video_id=None, get_comments=True):
        result = super()._extract_product(product_info, video_id, get_comments)
        location = _instagram_location(product_info)
        if location:
            result["instagram_location"] = location
            # Preserve compatibility with tools that understand yt-dlp's
            # standard string-valued location field.
            if location.get("name"):
                result.setdefault("location", location["name"])
        return result
