"""Core interfaces.

Structural ``Protocol`` types, not ABCs: implementations stay decoupled from
this module, and tests can pass plain fakes without inheriting anything. Every
concrete component is constructed with its collaborators typed as these
protocols, which is what keeps the escalation ladder, the cache backend and
the storage backend swappable.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from .models.config import TransportTier
from .models.media import Media
from .models.profile import Page, Profile

__all__ = [
    "Cache",
    "CookieProvider",
    "Downloader",
    "MediaPaginator",
    "MetadataStore",
    "RateLimiter",
    "Response",
    "StateStore",
    "Transport",
    "TransportProvider",
    "UserIdResolver",
]


@runtime_checkable
class Response(Protocol):
    """Minimal HTTP response surface every transport tier exposes."""

    @property
    def status_code(self) -> int:
        """HTTP status code."""
        ...

    @property
    def headers(self) -> Mapping[str, str]:
        """Response headers, case-insensitive."""
        ...

    @property
    def content(self) -> bytes:
        """Raw response body."""
        ...

    def json(self) -> Any:
        """Body parsed as JSON."""
        ...


@runtime_checkable
class Transport(Protocol):
    """One rung of the escalation ladder.

    Implementations translate their library's exceptions into this package's
    :mod:`~instadata.errors` types, so callers never catch an
    ``httpx``/``curl_cffi``/``playwright`` exception.
    """

    @property
    def tier(self) -> TransportTier:
        """Which rung this transport occupies."""
        ...

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> Response:
        """Perform a single request.

        Raises:
            NetworkError: Transport-level failure.
            HTTPStatusError: Non-success status the tier could not classify.
            AuthenticationError: Session rejected; escalate.
            RateLimitError: Throttled; back off.
        """
        ...

    async def stream(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        """Stream a response body without buffering it whole.

        Used for media downloads, which must never be read into memory.
        """
        ...

    async def aclose(self) -> None:
        """Release connections held by this transport."""
        ...


@runtime_checkable
class TransportProvider(Protocol):
    """Supplies transports and drives escalation between them.

    The caller asks for a request; the provider decides which tier serves it,
    escalating on :class:`AuthenticationError` and retrying per policy. Callers
    never learn which tier succeeded.
    """

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> Any:
        """Perform a request through the ladder and return parsed JSON.

        Raises:
            AllTiersFailedError: Every enabled tier failed.
        """
        ...

    async def aclose(self) -> None:
        """Close every transport this provider created."""
        ...


@runtime_checkable
class UserIdResolver(Protocol):
    """Resolves a username to a numeric user id.

    Several strategies exist because Instagram retires endpoints without
    notice; the composite resolver tries them in order and caches the winner.
    """

    async def resolve(self, username: str) -> Profile:
        """Return the profile for ``username``.

        Raises:
            NotFoundError: No such account.
            PrivateAccountError: Account is not visible to this session.
        """
        ...


@runtime_checkable
class MediaPaginator(Protocol):
    """Walks a cursored GraphQL collection.

    Yields page by page so a 100k-post account never materialises in memory.
    """

    async def iter_pages(
        self,
        user_id: str,
        *,
        cursor: str | None = None,
    ) -> AsyncIterator[Page[Media]]:
        """Yield pages starting after ``cursor``, oldest cursor first."""
        ...


@runtime_checkable
class Downloader(Protocol):
    """Writes media bytes to disk."""

    async def download(self, media: Media, destination: Path) -> Path:
        """Download one item atomically and return the final path.

        Raises:
            DownloadError: The file could not be written.
            MediaUnavailableError: The CDN refused or expired the URL.
        """
        ...


@runtime_checkable
class MetadataStore(Protocol):
    """Persists media metadata."""

    async def save(self, media: Media) -> None:
        """Append or upsert one record."""
        ...

    async def has(self, media_id: str) -> bool:
        """Whether this id was already stored, used to skip duplicate work."""
        ...

    async def aclose(self) -> None:
        """Flush buffered writes."""
        ...


@runtime_checkable
class StateStore(Protocol):
    """Persists resume state for one scrape job."""

    async def load(self, job_key: str) -> Mapping[str, Any] | None:
        """Return saved state, or ``None`` for a fresh job."""
        ...

    async def save(self, job_key: str, state: Mapping[str, Any]) -> None:
        """Persist state atomically after each completed page."""
        ...


@runtime_checkable
class Cache(Protocol):
    """Key-value cache with optional expiry.

    Backs username lookups, profile records and ETags.
    """

    async def get(self, key: str) -> bytes | None:
        """Return the cached value, or ``None`` when absent or expired."""
        ...

    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        """Store a value, expiring after ``ttl`` seconds when given."""
        ...

    async def delete(self, key: str) -> None:
        """Remove a key, ignoring a miss."""
        ...


@runtime_checkable
class RateLimiter(Protocol):
    """Paces outbound requests and adapts to server feedback."""

    async def acquire(self) -> None:
        """Block until the next request is allowed to go out."""
        ...

    def record_success(self) -> None:
        """Report a successful response so pacing can relax."""
        ...

    def record_throttled(self, retry_after: float | None = None) -> None:
        """Report a 429/403 so pacing can tighten."""
        ...


@runtime_checkable
class CookieProvider(Protocol):
    """Supplies and persists session cookies."""

    async def load(self) -> dict[str, str]:
        """Return the current cookie jar, empty when logged out."""
        ...

    async def save(self, cookies: Mapping[str, str]) -> None:
        """Persist cookies for reuse by later runs."""
        ...
