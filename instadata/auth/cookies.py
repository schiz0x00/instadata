"""Cookie handling.

Credentials are never accepted, stored or transmitted by this package: the
only way to authenticate is to hand it cookies you exported from a browser
you already logged into. That keeps passwords and 2FA entirely out of scope.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path

import anyio
import orjson

from ..errors import AuthenticationError, ConfigurationError
from ..utils.files import atomic_write_bytes
from ..utils.logging import logger

__all__ = [
    "CSRF_COOKIE",
    "SESSION_COOKIE",
    "FileCookieProvider",
    "NullCookieProvider",
    "format_netscape_cookies",
    "parse_netscape_cookies",
]

SESSION_COOKIE = "sessionid"
CSRF_COOKIE = "csrftoken"
_INSTAGRAM_DOMAIN = ".instagram.com"


def parse_netscape_cookies(text: str) -> dict[str, str]:
    """Parse a Netscape ``cookies.txt`` body into a name → value mapping.

    Malformed lines are skipped rather than fatal: browser extensions emit
    slightly different dialects and one bad row should not lose the session.
    """
    cookies: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 7:
            continue
        *_, name, value = fields[:7]
        cookies[name] = value
    return cookies


def format_netscape_cookies(cookies: Mapping[str, str], domain: str = _INSTAGRAM_DOMAIN) -> str:
    """Render cookies as a Netscape ``cookies.txt`` document."""
    expiry = int(time.time()) + 365 * 24 * 3600
    lines = ["# Netscape HTTP Cookie File", "# Written by instadata"]
    lines += [
        "\t".join([domain, "TRUE", "/", "TRUE", str(expiry), name, value])
        for name, value in cookies.items()
    ]
    return "\n".join(lines) + "\n"


class FileCookieProvider:
    """Loads and persists cookies from a file on disk.

    Accepts both formats browsers and extensions produce:

    * JSON — either ``{"sessionid": "..."} `` or a list of cookie objects with
      ``name``/``value`` keys, as exported by most browser extensions.
    * Netscape ``cookies.txt``.

    Writes are always JSON, and always atomic.

    Args:
        path: Cookie file. May not exist yet.
        required: Fail loudly when the file is missing, instead of returning
            an empty jar. Used when the caller explicitly asked for auth.
    """

    def __init__(self, path: Path, *, required: bool = False) -> None:
        self._path = Path(path)
        self._required = required
        self._cache: dict[str, str] | None = None

    @property
    def path(self) -> Path:
        """Backing file path."""
        return self._path

    async def load(self) -> dict[str, str]:
        """Return the cookie jar, reading the file at most once per instance.

        Raises:
            ConfigurationError: ``required`` and the file is absent.
            AuthenticationError: The file exists but carries no session id.
        """
        if self._cache is not None:
            return dict(self._cache)
        if not self._path.exists():
            if self._required:
                raise ConfigurationError(f"cookie file not found: {self._path}")
            logger.debug("no cookie file at {}, staying anonymous", self._path)
            self._cache = {}
            return {}

        async with await anyio.open_file(self._path, "rb") as handle:
            raw = await handle.read()

        cookies = _decode(raw)
        if self._required and SESSION_COOKIE not in cookies:
            raise AuthenticationError(f"{self._path} contains no {SESSION_COOKIE!r} cookie")
        logger.debug("loaded {} cookies from {}", len(cookies), self._path)
        self._cache = cookies
        return dict(cookies)

    async def save(self, cookies: Mapping[str, str]) -> None:
        """Persist cookies as JSON, merging over what is already stored.

        Merging matters because Instagram rotates ``csrftoken`` mid-session;
        a blind overwrite from a partial jar would drop the session id.

        Written 0600: a session id is full account access, and the browser
        tier harvests and saves one without the user explicitly asking.
        """
        merged = {**(self._cache or {}), **dict(cookies)}
        await atomic_write_bytes(
            self._path,
            orjson.dumps(merged, option=orjson.OPT_INDENT_2),
            mode=0o600,
        )
        self._cache = merged
        logger.debug("saved {} cookies to {}", len(merged), self._path)

    async def is_authenticated(self) -> bool:
        """Whether a session cookie is present."""
        return SESSION_COOKIE in await self.load()


class NullCookieProvider:
    """Always-empty jar, used by anonymous tiers and tests."""

    async def load(self) -> dict[str, str]:
        """Return an empty jar."""
        return {}

    async def save(self, cookies: Mapping[str, str]) -> None:
        """Discard the cookies."""


def _decode(raw: bytes) -> dict[str, str]:
    """Decode a cookie file, sniffing JSON before falling back to Netscape."""
    text = raw.decode("utf-8", errors="replace").lstrip()
    if not text.startswith(("{", "[")):
        return parse_netscape_cookies(text)
    try:
        payload = orjson.loads(text)
    except orjson.JSONDecodeError as exc:
        raise ConfigurationError(f"cookie file is neither valid JSON nor Netscape: {exc}") from exc
    if isinstance(payload, dict):
        return {str(k): str(v) for k, v in payload.items() if isinstance(v, (str, int))}
    if isinstance(payload, list):
        return {
            str(item["name"]): str(item["value"])
            for item in payload
            if isinstance(item, dict) and "name" in item and "value" in item
        }
    raise ConfigurationError("unsupported cookie file structure")
