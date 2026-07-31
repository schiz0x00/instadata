"""curl_cffi transport: tier 2, real browser TLS fingerprint.

Instagram fingerprints TLS and HTTP/2 settings. Plain httpx has a Python-shaped
JA3 that is trivially distinguishable from Chrome; curl_cffi reproduces
Chrome's handshake byte for byte. Still no browser process, so this stays
roughly as cheap as tier 1 — it is only tier 2 because it costs a heavier
dependency and slightly more per-request overhead.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Self

from curl_cffi import CurlError
from curl_cffi.requests import AsyncSession

from ..errors import NetworkError
from ..interfaces import CookieProvider
from ..models.config import TransportConfig, TransportTier
from ..utils.logging import logger
from .base import SimpleResponse, classify_status
from .endpoints import BASE_URL, default_headers

__all__ = ["ImpersonatedTransport"]


class ImpersonatedTransport:
    """HTTP transport that impersonates Chrome's TLS and HTTP/2 fingerprint.

    Args:
        config: Timeouts, proxy, and the ``impersonate`` profile name.
        cookies: Optional jar; supplying one turns this into an
            authenticated tier-2 transport, which is strictly better than
            falling all the way through to a browser.
        session: Pre-built session, injected by tests.
    """

    def __init__(
        self,
        config: TransportConfig | None = None,
        *,
        cookies: CookieProvider | None = None,
        session: AsyncSession | None = None,
    ) -> None:
        self._config = config or TransportConfig()
        self._cookies = cookies
        self._session = session
        self._owns_session = session is None

    @property
    def tier(self) -> TransportTier:
        """Rung of the escalation ladder this transport occupies."""
        return TransportTier.IMPERSONATED

    async def _ensure_session(self) -> AsyncSession:
        """Create the pooled session on first use."""
        if self._session is None:
            self._session = AsyncSession(
                impersonate=self._config.impersonate,
                timeout=self._config.timeout,
                max_clients=self._config.max_connections,
                proxy=self._config.proxy,
                verify=True,
            )
        return self._session

    async def _headers(self, extra: Mapping[str, str] | None) -> dict[str, str]:
        """Instagram's default headers plus any per-request overrides.

        ``User-Agent`` is deliberately dropped: curl_cffi sets one matching
        the impersonated build, and a mismatched UA undoes the impersonation.
        """
        headers = default_headers(self._config.app_id, self._config.user_agent, f"{BASE_URL}/")
        headers.pop("User-Agent", None)
        if self._cookies is not None:
            jar = await self._cookies.load()
            if csrf := jar.get("csrftoken"):
                headers["X-CSRFToken"] = csrf
        headers.update(extra or {})
        return headers

    async def _cookie_dict(self) -> dict[str, str] | None:
        """Cookies to attach, or ``None`` when anonymous."""
        if self._cookies is None:
            return None
        return await self._cookies.load() or None

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> SimpleResponse:
        """Perform one impersonated request and classify its status.

        Raises:
            NetworkError: libcurl-level failure.
            AuthenticationError | RateLimitError | NotFoundError |
            HTTPStatusError: Per :func:`~instadata.api.base.classify_status`.
        """
        session = await self._ensure_session()
        try:
            response: Any = await session.request(
                method,
                url,
                params=dict(params) if params else None,
                headers=await self._headers(headers),
                data=dict(data) if data else None,
                cookies=await self._cookie_dict(),
            )
        except CurlError as exc:
            raise NetworkError(f"curl error for {url}: {exc}") from exc

        logger.debug("[impersonated] {} {} -> {}", method, url, response.status_code)
        body = response.content
        response_headers = dict(response.headers)
        classify_status(response.status_code, url, body, response_headers)
        return SimpleResponse(
            status_code=response.status_code,
            headers=response_headers,
            content=body,
            url=url,
            tier=self.tier,
        )

    async def stream(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        """Yield the response body in chunks."""
        session = await self._ensure_session()
        merged = await self._headers(headers)
        try:
            async with session.stream(
                "GET", url, headers=merged, cookies=await self._cookie_dict()
            ) as response:
                classify_status(response.status_code, url, b"", dict(response.headers))
                async for chunk in response.aiter_content(chunk_size):
                    yield chunk
        except CurlError as exc:
            raise NetworkError(f"curl error while streaming {url}: {exc}") from exc

    async def aclose(self) -> None:
        """Close the session, if this transport created it."""
        if self._session is not None and self._owns_session:
            await self._session.close()
            self._session = None

    async def __aenter__(self) -> Self:
        """Enter an async context, returning this transport."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the transport on context exit."""
        await self.aclose()
