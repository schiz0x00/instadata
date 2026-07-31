"""URL helpers for Instagram and its CDN."""

from __future__ import annotations

import re
from urllib.parse import urlparse

__all__ = [
    "PROXY_SCHEMES",
    "SHORTCODE_RE",
    "extract_shortcode",
    "is_expired_cdn_url",
    "normalize_username",
    "split_proxy_credentials",
    "url_extension",
    "validate_proxy_url",
]

#: Proxy schemes usable on *every* tier, which is the intersection, not the
#: union. httpx is the narrowest: it wires only socks5/socks5h into httpcore,
#: so ``socks4`` is excluded even though socksio and libcurl both speak it.
#: Accepting it would pass validation and then die at tier 1 with a bare
#: ValueError the escalation ladder does not catch.
#:
#: The SOCKS schemes need httpx's ``socks`` extra (declared in pyproject);
#: curl_cffi gets them from libcurl with no Python package involved.
PROXY_SCHEMES = frozenset({"http", "https", "socks5", "socks5h"})

SHORTCODE_RE = re.compile(r"/(?:p|reel|reels|tv)/([A-Za-z0-9_-]+)")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")
_KNOWN_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp", ".mp4", ".mov", ".heic"})


def extract_shortcode(value: str) -> str | None:
    """Return the shortcode from a post/reel URL, or ``None``.

    Accepts a bare shortcode unchanged, so the CLI can take either form.
    """
    value = value.strip()
    if match := SHORTCODE_RE.search(value):
        return match.group(1)
    if "/" not in value and re.fullmatch(r"[A-Za-z0-9_-]{5,}", value):
        return value
    return None


def normalize_username(value: str) -> str:
    """Reduce a profile URL, ``@handle`` or bare name to a bare username.

    Raises:
        ValueError: The result is not a syntactically valid username.
    """
    value = value.strip().rstrip("/")
    if "instagram.com" in value:
        path = urlparse(value if "//" in value else f"https://{value}").path
        value = path.strip("/").split("/")[0]
    value = value.lstrip("@").lower()
    if not _USERNAME_RE.fullmatch(value):
        raise ValueError(f"invalid instagram username: {value!r}")
    return value


def url_extension(url: str, default: str = "") -> str:
    """Return the file extension of a CDN URL, ignoring its query string.

    CDN URLs carry long signed query strings; only the path is inspected.
    """
    suffix = urlparse(url).path.rsplit(".", 1)
    if len(suffix) != 2:
        return default
    candidate = f".{suffix[1].lower()}"
    return candidate if candidate in _KNOWN_EXTENSIONS else default


def validate_proxy_url(value: str) -> str:
    """Return ``value`` unchanged if it is a usable proxy URL.

    Checked at construction so a typo fails with one clear line instead of an
    ``ImportError`` or ``ValueError`` from deep inside a transport, several
    tiers into a run.

    Raises:
        ValueError: Unsupported scheme, or no host.
    """
    parsed = urlparse(value)
    if parsed.scheme not in PROXY_SCHEMES:
        supported = ", ".join(sorted(PROXY_SCHEMES))
        raise ValueError(
            f"unsupported proxy scheme {parsed.scheme or value!r}; expected one of: {supported}"
        )
    if not parsed.hostname:
        raise ValueError(f"proxy URL has no host: {value!r}")
    return value


def split_proxy_credentials(proxy: str) -> tuple[str, str | None, str | None]:
    """Split ``scheme://user:pass@host:port`` into server, username, password.

    Chromium's ``--proxy-server`` ignores credentials embedded in the URL, so
    Playwright takes them as separate ``username``/``password`` fields. Passing
    the whole URL as ``server`` yields a 407 on every request.

    Returns:
        ``(server_without_credentials, username, password)``.
    """
    parsed = urlparse(proxy)
    host = parsed.hostname or ""
    if parsed.port:
        host = f"{host}:{parsed.port}"
    server = f"{parsed.scheme}://{host}" if parsed.scheme else host
    return server, parsed.username or None, parsed.password or None


def is_expired_cdn_url(status_code: int) -> bool:
    """Whether a CDN status means the signed URL expired rather than the media died.

    Instagram's CDN answers an expired signature with 403; the post is usually
    still alive, so the caller should re-fetch metadata instead of giving up.
    """
    return status_code in (403, 410)
