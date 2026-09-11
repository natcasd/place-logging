"""Expose Instagram's native location and user tags through yt-dlp.

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


def _instagram_tagged_accounts(product_media: Any) -> list[dict[str, str]]:
    """Return the useful identity fields from Instagram's media user tags."""
    if not isinstance(product_media, dict):
        return []

    raw_tags = product_media.get("usertags")
    if not isinstance(raw_tags, dict):
        return []

    accounts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for raw_tag in raw_tags.get("in") or []:
        if not isinstance(raw_tag, dict):
            continue
        raw_user = raw_tag.get("user")
        if not isinstance(raw_user, dict):
            continue

        username = raw_user.get("username")
        full_name = raw_user.get("full_name")
        account = {
            key: value.strip()
            for key, value in (("username", username), ("full_name", full_name))
            if isinstance(value, str) and value.strip()
        }
        if not account:
            continue

        identity = (
            account.get("username", "").casefold(),
            account.get("full_name", "").casefold(),
        )
        if identity in seen:
            continue
        seen.add(identity)
        accounts.append(account)

    return accounts


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

        normalized_product = product_info
        if isinstance(normalized_product, list):
            normalized_product = normalized_product[0] if normalized_product else None
        if not isinstance(normalized_product, dict):
            return result

        carousel_media = normalized_product.get("carousel_media")
        if isinstance(carousel_media, list) and result.get("_type") == "playlist":
            for raw_media, entry in zip(carousel_media, result.get("entries") or []):
                accounts = _instagram_tagged_accounts(raw_media)
                if accounts and isinstance(entry, dict):
                    entry["instagram_tagged_accounts"] = accounts
        else:
            accounts = _instagram_tagged_accounts(normalized_product)
            if accounts:
                result["instagram_tagged_accounts"] = accounts
        return result
