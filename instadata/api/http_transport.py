"""httpx-backed transports: tiers 1 (anonymous) and 3 (authenticated).

The same class serves both rungs; only the cookie provider differs. Verified
against live Instagram: the timeline GraphQL endpoint answers 200 to a plain
anonymous request with no cookies, which is why this is the default tier.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from typing import Self

import httpx

from ..errors import NetworkError
from ..interfaces import CookieProvider
from ..models.config import TransportConfig, TransportTier
from ..utils.logging import logger
from .base import SimpleResponse, classify_status
from .endpoints import BASE_URL, default_headers

__all__ = ["AnonymousTransport", "AuthenticatedTransport", "HttpxTransport"]


class HttpxTransport:
    """Async HTTP transport over a pooled ``httpx.AsyncClient``.

    One client per transport instance, reused for the whole run: connection
    pooling and HTTP/2 multiplexing are most of the speed advantage this tier
    has over launching a browser.

    Args:
        config: Timeouts, pool size, proxy, user agent.
        tier: Which rung this instance represents, for logging and reporting.
        cookies: Cookie source; a null provider keeps the tier anonymous.
        client: Pre-built client, injected by tests.
    """

    def __init__(
        self,
        config: TransportConfig | None = None,
        *,
        tier: TransportTier = TransportTier.ANONYMOUS,
        cookies: CookieProvider | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._config = config or TransportConfig()
        self._tier = tier
        self._cookies = cookies
        self._client = client
        self._owns_client = client is None

    @property
    def tier(self) -> TransportTier:
        """Rung of the escalation ladder this transport occupies."""
        return self._tier

    async def _ensure_client(self) -> httpx.AsyncClient:
        """Build the pooled client on first use, applying any cookie jar."""
        if self._client is None:
            self._client = httpx.AsyncClient(
                http2=self._config.http2,
                timeout=httpx.Timeout(
                    self._config.timeout,
                    connect=self._config.connect_timeout,
                ),
                limits=httpx.Limits(
                    max_connections=self._config.max_connections,
                    max_keepalive_connections=self._config.max_connections,
                ),
                proxy=self._config.proxy,
                follow_redirects=True,
            )
        if self._cookies is not None:
            for name, value in (await self._cookies.load()).items():
                self._client.cookies.set(name, value, domain=".instagram.com")
        return self._client

    async def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        """Merge default Instagram headers with per-request overrides."""
        headers = default_headers(
            self._config.app_id,
            self._config.user_agent,
            referer=f"{BASE_URL}/",
        )
        if self._cookies is not None:
            jar = await self._cookies.load()
            if csrf := jar.get("csrftoken"):
                headers["X-CSRFToken"] = csrf
        headers.update(extra or {})
        return headers

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> SimpleResponse:
        """Perform one request and classify its status.

        Raises:
            NetworkError: Connection, TLS or timeout failure.
            AuthenticationError | RateLimitError | NotFoundError |
            HTTPStatusError: Per :func:`~instadata.api.base.classify_status`.
        """
        client = await self._ensure_client()
        try:
            response = await client.request(
                method,
                url,
                params=dict(params) if params else None,
                headers=await self._headers(headers),
                data=dict(data) if data else None,
            )
        except httpx.HTTPError as exc:
            raise NetworkError(f"{type(exc).__name__} for {url}: {exc}") from exc

        logger.debug("[{}] {} {} -> {}", self._tier.value, method, url, response.status_code)
        body = response.content
        classify_status(response.status_code, str(response.url), body, response.headers)
        return SimpleResponse(
            status_code=response.status_code,
            headers=dict(response.headers),
            content=body,
            url=str(response.url),
            tier=self._tier,
        )

    @asynccontextmanager
    async def stream_response(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> AsyncIterator[httpx.Response]:
        """Open a streaming response, exposing status and headers before the body.

        The downloader needs ``Content-Length`` and ``Accept-Ranges`` before
        deciding whether to resume, so the raw response is yielded rather than
        just its byte stream.
        """
        client = await self._ensure_client()
        merged = await self._headers(headers)
        try:
            async with client.stream("GET", url, headers=merged) as response:
                yield response
        except httpx.HTTPError as exc:
            raise NetworkError(f"{type(exc).__name__} while streaming {url}: {exc}") from exc

    async def stream(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        """Yield the response body in chunks, never buffering it whole."""
        async with self.stream_response(url, headers=headers) as response:
            classify_status(response.status_code, url, b"", response.headers)
            async for chunk in response.aiter_bytes(chunk_size):
                yield chunk

    async def aclose(self) -> None:
        """Close the pooled client, if this transport created it."""
        if self._client is not None and self._owns_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> Self:
        """Enter an async context, returning this transport."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the transport on context exit."""
        await self.aclose()


def AnonymousTransport(config: TransportConfig | None = None) -> HttpxTransport:
    """Tier 1: plain httpx with no cookies. The cheapest path that works."""
    return HttpxTransport(config, tier=TransportTier.ANONYMOUS, cookies=None)


def AuthenticatedTransport(
    config: TransportConfig | None = None,
    *,
    cookies: CookieProvider,
) -> HttpxTransport:
    """Tier 3: httpx carrying a session cookie jar."""
    return HttpxTransport(config, tier=TransportTier.AUTHENTICATED, cookies=cookies)
