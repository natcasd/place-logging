"""Expose TikTok POI and photo-mode context through yt-dlp.

The built-in extractor receives this data from TikTok's public webpage but
does not include it in the normalized info dict. Keep the extension small so
ordinary video extraction continues to follow upstream yt-dlp behavior.
"""

from __future__ import annotations

from typing import Any

from yt_dlp.extractor.tiktok import TikTokIE


def _clean_text(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _first_text(mapping: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        if value := _clean_text(mapping.get(key)):
            return value
    return None


def _tiktok_poi(aweme_detail: Any) -> dict[str, str] | None:
    """Return useful non-ID place evidence from web or app post data."""
    if not isinstance(aweme_detail, dict):
        return None
    raw_poi = aweme_detail.get("poi") or aweme_detail.get("poi_info")
    if not isinstance(raw_poi, dict):
        raw_poi = {}

    content_location = (
        aweme_detail.get("contentLocation")
        or aweme_detail.get("content_location")
        or {}
    )
    raw_address = (
        content_location.get("address")
        if isinstance(content_location, dict)
        else {}
    )
    if not isinstance(raw_address, dict):
        raw_address = {}

    values = {
        "name": _first_text(raw_poi, "name", "poi_name"),
        "address": _first_text(raw_poi, "address")
        or _first_text(raw_address, "streetAddress", "street_address"),
        "city": _first_text(raw_poi, "city")
        or _first_text(raw_address, "addressLocality", "address_locality"),
        "region": _first_text(raw_poi, "province")
        or _first_text(raw_address, "addressRegion", "address_region"),
        "country": _first_text(raw_poi, "country")
        or _first_text(raw_address, "addressCountry", "address_country"),
        "category": _first_text(raw_poi, "category"),
        "type_name": _first_text(
            raw_poi,
            "ttTypeNameTiny",
            "tt_type_name_tiny",
            "ttTypeNameMedium",
            "tt_type_name_medium",
            "ttTypeNameSuper",
            "tt_type_name_super",
        ),
    }
    poi = {key: value for key, value in values.items() if value}
    return poi or None


def _image_urls(raw_image: Any) -> list[str]:
    if not isinstance(raw_image, dict):
        return []
    raw_url = (
        raw_image.get("imageURL")
        or raw_image.get("image_url")
        or raw_image.get("display_image")
        or {}
    )
    if not isinstance(raw_url, dict):
        return []
    candidates = raw_url.get("urlList") or raw_url.get("url_list") or []
    return list(dict.fromkeys(
        value.strip()
        for value in candidates
        if isinstance(value, str) and value.strip()
    ))


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _tiktok_image_post(aweme_detail: Any) -> dict[str, Any] | None:
    """Return ordered photo-mode slides with short-lived URL alternatives."""
    if not isinstance(aweme_detail, dict):
        return None
    raw_post = aweme_detail.get("imagePost") or aweme_detail.get("image_post_info")
    if not isinstance(raw_post, dict):
        return None

    slides: list[dict[str, Any]] = []
    for raw_image in raw_post.get("images") or []:
        urls = _image_urls(raw_image)
        if not urls:
            continue
        slide: dict[str, Any] = {"urls": urls}
        if width := _positive_int(
            raw_image.get("imageWidth", raw_image.get("width"))
            if isinstance(raw_image, dict)
            else None
        ):
            slide["width"] = width
        if height := _positive_int(
            raw_image.get("imageHeight", raw_image.get("height"))
            if isinstance(raw_image, dict)
            else None
        ):
            slide["height"] = height
        slides.append(slide)

    if not slides:
        return None
    result: dict[str, Any] = {"slides": slides}
    if title := _first_text(raw_post, "title"):
        result["title"] = title
    return result


def _attach_tiktok_context(
    result: dict[str, Any],
    aweme_detail: Any,
) -> dict[str, Any]:
    if poi := _tiktok_poi(aweme_detail):
        result["tiktok_poi"] = poi
    if image_post := _tiktok_image_post(aweme_detail):
        result["tiktok_image_post"] = image_post
    return result


class _PlaceLoggerTikTokIE(
    TikTokIE,
    plugin_name="place_logger_context",
):
    """Add place and photo context while delegating extraction upstream."""

    def _parse_aweme_video_web(
        self,
        aweme_detail,
        webpage_url,
        video_id,
        extract_flat=False,
    ):
        result = super()._parse_aweme_video_web(
            aweme_detail,
            webpage_url,
            video_id,
            extract_flat,
        )
        return _attach_tiktok_context(result, aweme_detail)

    def _parse_aweme_video_app(self, aweme_detail):
        result = super()._parse_aweme_video_app(aweme_detail)
        return _attach_tiktok_context(result, aweme_detail)
