"""Public URL acceptance helpers; no private library or extraction access."""
from __future__ import annotations

from urllib.parse import urljoin, urlsplit

import requests

from multi_user_migration import public_identity
from source_identity import TIKTOK_HOSTS, TIKTOK_SHORT_HOSTS, canonical_source_url


class SourceResolutionUnavailable(Exception):
    pass


def _validated_url(value: str) -> str:
    value = value.strip()
    if not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError('A supported public post URL is required')
    parsed = urlsplit(value)
    if (parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.port not in {None, 80, 443} or '\\' in value):
        raise ValueError('A supported public post URL is required')
    return value


def resolve_public_url(source_url: str) -> str:
    value = _validated_url(source_url)
    if public_identity(value) is not None:
        return canonical_source_url(value)
    parsed = urlsplit(value)
    host = parsed.hostname.lower().rstrip('.')
    parts = parsed.path.strip('/').split('/')
    if host not in TIKTOK_SHORT_HOSTS and not (host in TIKTOK_HOSTS and parts[0] == 't'):
        raise ValueError('Use an Instagram post, YouTube video, or TikTok post URL')

    # Check each destination before following it; never attach netrc credentials.
    try:
        with requests.Session() as session:
            session.trust_env = False
            for _ in range(5):
                parsed = urlsplit(_validated_url(value))
                if parsed.hostname.lower().rstrip('.') not in TIKTOK_HOSTS:
                    raise ValueError('Unsupported share-link destination')
                if public_identity(value) is not None:
                    return canonical_source_url(value)
                with session.get(value, allow_redirects=False, stream=True, timeout=(3, 5),
                                 headers={'User-Agent': 'Mozilla/5.0 (compatible; Jot/1.0)'}) as response:
                    if response.status_code not in {301, 302, 303, 307, 308}:
                        raise SourceResolutionUnavailable()
                    location = response.headers.get('Location')
                    if not location:
                        raise SourceResolutionUnavailable()
                    value = urljoin(value, location)
    except requests.RequestException:
        raise SourceResolutionUnavailable() from None
    raise SourceResolutionUnavailable()
