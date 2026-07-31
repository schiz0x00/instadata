"""The escalation ladder.

Callers ask for JSON; this decides how to get it. Tiers are tried cheapest
first and the winning tier is remembered, so a run that needed a browser once
does not re-walk the ladder on every subsequent request — and a run that never
needs one never imports Playwright.

Escalation rules:

* :class:`AuthenticationError` — this tier lacks the credentials. Escalate.
* :class:`RateLimitError` — the *account or IP* is throttled, not the tier.
  Back off in place; escalating would only burn the next credential too.
* :class:`NetworkError` / 5xx — retry in place, then escalate if still failing.
* Domain errors (private, not found) — propagate; no tier can fix them.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any, Self

from ..errors import (
    AllTiersFailedError,
    AuthenticationError,
    NotFoundError,
    PrivateAccountError,
    RateLimitError,
    ScraperError,
)
from ..interfaces import CookieProvider, RateLimiter, Transport
from ..models.config import ScraperConfig, TransportTier
from ..retry.policy import RetryPolicy
from ..retry.rate_limiter import AdaptiveRateLimiter
from ..utils.logging import logger
from .base import check_graphql_errors

__all__ = ["EscalatingTransportProvider", "TransportFactory", "default_transport_factory"]

TransportFactory = Callable[[TransportTier], Transport | None]


def default_transport_factory(
    config: ScraperConfig,
    cookies: CookieProvider | None = None,
) -> TransportFactory:
    """Build the standard factory for the four tiers.

    Each transport is constructed lazily, on the first request that reaches
    its tier. Returning ``None`` disables a tier — the authenticated and
    browser tiers disable themselves when no cookie provider was supplied and
    no session can be obtained.
    """

    def factory(tier: TransportTier) -> Transport | None:
        match tier:
            case TransportTier.ANONYMOUS:
                from .http_transport import AnonymousTransport

                return AnonymousTransport(config.transport)
            case TransportTier.IMPERSONATED:
                from .impersonate_transport import ImpersonatedTransport

                return ImpersonatedTransport(config.transport)
            case TransportTier.AUTHENTICATED:
                # A cookie provider is always supplied, but a null one carries
                # no session — this tier would then be a byte-for-byte repeat
                # of tier 2 and buy an extra round trip on every failure path.
                # Only the presence of a cookie file makes it a distinct rung.
                if cookies is None or config.cookies_path is None:
                    return None
                from .impersonate_transport import ImpersonatedTransport

                # Impersonated TLS *and* a session cookie: strictly the
                # strongest non-browser combination available.
                return ImpersonatedTransport(config.transport, cookies=cookies)
            case TransportTier.BROWSER:
                from ..browser.transport import BrowserTransport

                return BrowserTransport(config.transport, cookies=cookies)
        return None

    return factory


class EscalatingTransportProvider:
    """Serves requests through the cheapest transport that still works.

    Args:
        config: Enabled tiers, retry policy and pacing.
        factory: Builds a transport for a tier, or ``None`` to skip it.
        rate_limiter: Shared pacing across every tier and worker.
        retry_policy: Retry behaviour within a single tier.
    """

    def __init__(
        self,
        config: ScraperConfig | None = None,
        *,
        factory: TransportFactory | None = None,
        rate_limiter: RateLimiter | None = None,
        retry_policy: RetryPolicy | None = None,
    ) -> None:
        self._config = config or ScraperConfig()
        self._factory = factory or default_transport_factory(self._config)
        self._rate_limiter = rate_limiter or AdaptiveRateLimiter(self._config.rate_limit)
        self._retry = retry_policy or RetryPolicy(self._config.retry)
        self._transports: dict[TransportTier, Transport | None] = {}
        self._current_index = 0

    @property
    def current_tier(self) -> TransportTier:
        """Tier currently being used for new requests."""
        return self._tiers[min(self._current_index, len(self._tiers) - 1)]

    @property
    def rate_limiter(self) -> RateLimiter:
        """The limiter pacing API calls across every tier.

        Not the downloader's: the CDN is a different host with a different
        quota, so :class:`~instadata.downloader.media.MediaDownloader`
        is built with its own. Share this one only if you deliberately want
        media pulls to spend the API budget.
        """
        return self._rate_limiter

    @property
    def _tiers(self) -> tuple[TransportTier, ...]:
        """Enabled tiers, in escalation order."""
        return self._config.transport.tiers

    def _transport(self, tier: TransportTier) -> Transport | None:
        """Return the transport for ``tier``, building it on first use."""
        if tier not in self._transports:
            self._transports[tier] = self._factory(tier)
            if self._transports[tier] is None:
                logger.debug("tier {} unavailable, skipping", tier.value)
        return self._transports[tier]

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> Any:
        """Fetch and parse JSON, escalating tiers until one succeeds.

        Raises:
            AllTiersFailedError: Every enabled tier failed.
            NotFoundError | PrivateAccountError: Propagated unchanged, since
                no amount of escalation would help.
        """
        failures: dict[str, BaseException] = {}

        for index in range(self._current_index, len(self._tiers)):
            tier = self._tiers[index]
            transport = self._transport(tier)
            if transport is None:
                continue
            try:
                payload = await self._retry.run(
                    lambda t=transport: self._attempt(t, method, url, params, headers, data),
                    description=f"{method} {url} [{tier.value}]",
                )
            except (NotFoundError, PrivateAccountError):
                raise
            except ScraperError as exc:
                failures[tier.value] = exc
                logger.warning("tier {} exhausted: {}", tier.value, exc)
                continue

            if index > self._current_index:
                logger.info("locking onto tier {}", tier.value)
                self._current_index = index
            return payload

        raise AllTiersFailedError(failures)

    async def _attempt(
        self,
        transport: Transport,
        method: str,
        url: str,
        params: Mapping[str, str] | None,
        headers: Mapping[str, str] | None,
        data: Mapping[str, str] | None,
    ) -> Any:
        """One paced request through one transport, with in-band error checks."""
        await self._rate_limiter.acquire()
        try:
            response = await transport.request(
                method, url, params=params, headers=headers, data=data
            )
            payload = response.json()
            # A 200 can still be a logged-out rejection; treat it as one.
            check_graphql_errors(payload, url)
        except RateLimitError as exc:
            self._rate_limiter.record_throttled(exc.retry_after)
            raise
        except AuthenticationError:
            raise
        else:
            self._rate_limiter.record_success()
            return payload

    async def stream(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        chunk_size: int = 65536,
    ) -> Any:
        """Return a byte stream from the cheapest HTTP tier.

        HTML pages need no session, so the browser tier is never used here.

        Paced like any other request, but not retried or escalated: the status
        is only known once the caller starts iterating, by which point this
        method has already returned. Callers treat a failure as "this route is
        exhausted" and fall through to their next one.
        """
        # ponytail: no escalation on the stream path — errors surface at
        # iteration, not at call time. Buffer the body here and reuse
        # request_json's tier loop if an HTML route ever needs the ladder.
        for tier in self._tiers:
            if tier is TransportTier.BROWSER:
                continue
            transport = self._transport(tier)
            if transport is not None:
                await self._rate_limiter.acquire()
                return transport.stream(url, headers=headers, chunk_size=chunk_size)
        raise AllTiersFailedError({"stream": RuntimeError("no HTTP tier available")})

    async def aclose(self) -> None:
        """Close every transport this provider built."""
        for transport in self._transports.values():
            if transport is not None:
                await transport.aclose()
        self._transports.clear()

    async def __aenter__(self) -> Self:
        """Enter an async context, returning this provider."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close every transport on context exit."""
        await self.aclose()
