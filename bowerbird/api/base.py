"""Transport foundations: response value object and status classification.

Both HTTP tiers share the classification logic here, so ``httpx`` and
``curl_cffi`` raise identical exceptions for identical server behaviour and
the escalation ladder above them needs no per-tier special cases.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import orjson

from ..errors import (
    AuthenticationError,
    HTTPStatusError,
    NotFoundError,
    ParsingError,
    PrivateAccountError,
    RateLimitError,
)
from ..models.config import TransportTier

__all__ = ["SimpleResponse", "check_graphql_errors", "classify_status", "parse_json"]

_LOGGED_OUT_MARKERS = (
    "unauthorized logged out",
    "login_required",
    "checkpoint_required",
)
#: Checked before the logged-out markers: Instagram serves this to sessions
#: that are perfectly valid but going too fast, and escalating a tier for it
#: would burn the next credential instead of waiting.
_RATE_LIMIT_MARKERS = ("wait a few minutes", "rate limit", "too many requests")


@dataclass(frozen=True, slots=True)
class SimpleResponse:
    """Transport-agnostic HTTP response.

    Satisfies the :class:`~bowerbird.interfaces.Response` protocol.
    Both HTTP tiers convert their native response into this so nothing above
    the transport layer touches a library type.
    """

    status_code: int
    headers: Mapping[str, str]
    content: bytes
    url: str = ""
    tier: TransportTier | None = field(default=None, compare=False)

    def json(self) -> Any:
        """Parse the body as JSON.

        Raises:
            ParsingError: The body is not JSON.
        """
        return parse_json(self.content, self.url)

    @property
    def is_success(self) -> bool:
        """Whether the status is 2xx."""
        return 200 <= self.status_code < 300


def parse_json(content: bytes, url: str = "") -> Any:
    """Decode a JSON body, tolerating Instagram's anti-JSON-hijacking prefix.

    Some endpoints prepend ``for (;;);`` to their payload.

    Raises:
        ParsingError: The body is not valid JSON.
    """
    if content.startswith(b"for (;;);"):
        content = content[len(b"for (;;);") :]
    try:
        return orjson.loads(content)
    except orjson.JSONDecodeError as exc:
        preview = content[:200].decode("utf-8", errors="replace")
        raise ParsingError(f"response was not JSON: {preview!r} ({exc})", path=url) from exc


def classify_status(
    status_code: int,
    url: str,
    body: bytes,
    headers: Mapping[str, str] | None = None,
) -> None:
    """Raise the right exception for a non-success status.

    Returns quietly for 2xx. The mapping drives the ladder above:
    :class:`AuthenticationError` escalates a tier, :class:`RateLimitError`
    backs off inside the current tier.

    Raises:
        AuthenticationError: 401, or a 403 that is not a rate limit.
        RateLimitError: 429, or a 403 whose body mentions throttling.
        NotFoundError: 404.
        HTTPStatusError: Anything else non-success.
    """
    if 200 <= status_code < 300:
        return

    text = body[:512].decode("utf-8", errors="replace").lower()
    retry_after = _retry_after(headers)

    if status_code == 429:
        raise RateLimitError(f"rate limited by {url}", retry_after=retry_after)
    if status_code == 401:
        raise AuthenticationError(f"unauthorized for {url}: {text[:200]!r}")
    if status_code == 403:
        # The same markers the in-band GraphQL check uses. A bare `"rate" in
        # text` also fires on "corporate" and "accurate", turning an auth
        # failure into a back-off that never escalates a tier.
        if any(marker in text for marker in _RATE_LIMIT_MARKERS):
            raise RateLimitError(f"soft rate limit on {url}", retry_after=retry_after)
        raise AuthenticationError(f"forbidden for {url}: {text[:200]!r}")
    if status_code == 404:
        raise NotFoundError(f"not found: {url}")
    raise HTTPStatusError(status_code, url, text)


def check_graphql_errors(payload: Any, url: str = "") -> None:
    """Raise for errors Instagram reports inside a 200 response.

    Instagram answers ``Unauthorized logged out query.`` with HTTP 200 and an
    ``errors`` array, so status codes alone are not enough to decide whether
    a tier actually worked.

    Raises:
        AuthenticationError: The payload says we are logged out.
        PrivateAccountError: The payload says the account is private.
        ParsingError: The payload reports an error we cannot classify.
    """
    if not isinstance(payload, dict):
        return
    errors = payload.get("errors")
    if not errors:
        if payload.get("status") == "fail":
            message = str(payload.get("message", "")).lower()
            _raise_for_message(message, url)
        return

    messages = [str(e.get("message", "")) for e in errors if isinstance(e, dict)]
    joined = " ".join(messages).lower()
    _raise_for_message(joined, url, raw=messages)


def _raise_for_message(message: str, url: str, raw: list[str] | None = None) -> None:
    """Map an in-band error message onto an exception."""
    detail = "; ".join(raw) if raw else message
    if any(marker in message for marker in _RATE_LIMIT_MARKERS):
        raise RateLimitError(f"{url}: {detail}")
    if any(marker in message for marker in _LOGGED_OUT_MARKERS):
        raise AuthenticationError(f"{url}: {detail}")
    if "private" in message or "not authorized to view" in message:
        raise PrivateAccountError(f"{url}: {detail}")
    raise ParsingError(f"graphql error: {detail}", path=url)


def _retry_after(headers: Mapping[str, str] | None) -> float | None:
    """Read ``Retry-After`` as seconds, ignoring HTTP-date form."""
    if not headers:
        return None
    value = next((v for k, v in headers.items() if k.lower() == "retry-after"), None)
    try:
        return float(value) if value is not None else None
    except ValueError:
        return None
