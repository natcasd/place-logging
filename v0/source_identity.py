"""Stable identity for supported social-media source URLs."""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit, urlunsplit


INSTAGRAM_HOSTS = {"instagram.com", "www.instagram.com", "m.instagram.com"}
YOUTUBE_HOSTS = {
    "youtube.com",
    "www.youtube.com",
    "m.youtube.com",
    "music.youtube.com",
}
TIKTOK_WEB_HOSTS = {"tiktok.com", "www.tiktok.com", "m.tiktok.com"}
TIKTOK_SHORT_HOSTS = {"vm.tiktok.com", "vt.tiktok.com"}
TIKTOK_SHARE_HOSTS = {"tiktokv.com", "www.tiktokv.com"}
TIKTOK_HOSTS = TIKTOK_WEB_HOSTS | TIKTOK_SHORT_HOSTS | TIKTOK_SHARE_HOSTS


def _tiktok_video_id(host: str, parts: list[str]) -> str | None:
    if host in TIKTOK_WEB_HOSTS:
        for index, part in enumerate(parts[:-1]):
            if part.lower() == "video" and parts[index + 1].isdigit():
                return parts[index + 1]
    if (
        host in TIKTOK_SHARE_HOSTS
        and len(parts) >= 3
        and parts[0].lower() == "share"
        and parts[1].lower() == "video"
        and parts[2].isdigit()
    ):
        return parts[2]
    return None


def canonical_source_url(source_url: str) -> str:
    """Return the post/video identity while discarding share-tracking details."""
    value = source_url.strip()
    parsed = urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    parts = [part for part in parsed.path.split("/") if part]

    if host in INSTAGRAM_HOSTS and len(parts) >= 2:
        kind = parts[0].lower()
        if kind in {"p", "reel", "tv"}:
            return f"https://www.instagram.com/{kind}/{parts[1]}/"

    if host == "youtu.be" and parts:
        return f"https://www.youtube.com/watch?v={parts[0]}"

    if host in YOUTUBE_HOSTS:
        video_id: str | None = None
        if parsed.path.rstrip("/") == "/watch":
            video_id = (parse_qs(parsed.query).get("v") or [None])[0]
        elif len(parts) >= 2 and parts[0].lower() in {"shorts", "live", "embed"}:
            video_id = parts[1]
        if video_id:
            return f"https://www.youtube.com/watch?v={video_id}"

    if host in TIKTOK_HOSTS:
        if video_id := _tiktok_video_id(host, parts):
            # TikTok's numeric video ID is stable even when the creator changes
            # their handle or the same video arrives through a share URL.
            return f"https://www.tiktok.com/@_/video/{video_id}"
        return urlunsplit(("https", host, parsed.path.rstrip("/") + "/", "", ""))

    # Unsupported URLs are not rewritten beyond a harmless fragment removal.
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, ""))
