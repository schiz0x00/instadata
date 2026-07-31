"""Username → numeric user id resolution.

Instagram retires resolution endpoints regularly, so this is a chain of
independent strategies rather than one call. The chain runs in cost order and
stops at the first success; the winning result is cached permanently, because
a username's numeric id never changes.

Strategies:

1. ``web_profile_info`` — one small JSON response with the full profile.
2. Timeline probe — the profile page's embedded JSON blobs.
3. Profile HTML — regex over the server-rendered page. Always works while the
   page renders at all, but costs a multi-megabyte download.
"""

from __future__ import annotations

from typing import Protocol

from ..cache.file_cache import JsonCache
from ..errors import NotFoundError, ParsingError, ScraperError
from ..extractors.graphql import parse_profile
from ..extractors.html import extract_json_blobs, extract_user_id
from ..interfaces import TransportProvider
from ..models.profile import Profile
from ..utils.logging import logger
from ..utils.urls import normalize_username
from .endpoints import PROFILE_URL_TEMPLATE, WEB_PROFILE_INFO_URL

__all__ = [
    "CachingUserResolver",
    "HtmlPageStrategy",
    "ResolutionStrategy",
    "WebProfileInfoStrategy",
]


class ResolutionStrategy(Protocol):
    """One way of turning a username into a :class:`Profile`."""

    @property
    def name(self) -> str:
        """Short identifier, used in logs."""
        ...

    async def resolve(self, username: str) -> Profile:
        """Return the profile, or raise a :class:`ScraperError`."""
        ...


class WebProfileInfoStrategy:
    """Primary strategy: the ``web_profile_info`` JSON endpoint.

    Cheapest available — a few kilobytes, and it carries follower counts and
    the private flag alongside the id.
    """

    name = "web_profile_info"

    def __init__(self, provider: TransportProvider) -> None:
        self._provider = provider

    async def resolve(self, username: str) -> Profile:
        """Fetch and parse the profile record.

        Raises:
            NotFoundError: Instagram reports no such user.
            ParsingError: The response shape changed.
        """
        payload = await self._provider.request_json(
            "GET",
            WEB_PROFILE_INFO_URL,
            params={"username": username},
            headers={"Referer": PROFILE_URL_TEMPLATE.format(username=username)},
        )
        user = (payload or {}).get("data", {}).get("user")
        if user is None:
            raise NotFoundError(f"no such user: {username}")
        return parse_profile(payload)


class HtmlPageStrategy:
    """Fallback strategy: scrape the id out of the rendered profile page.

    Expensive (megabytes) but structurally hard for Instagram to remove, since
    the page must contain the id to render itself.
    """

    name = "profile_html"

    def __init__(self, provider: TransportProvider) -> None:
        self._provider = provider

    async def resolve(self, username: str) -> Profile:
        """Fetch the profile page and mine it for the user id.

        Raises:
            ParsingError: The page carried no recognisable id.
        """
        html = await self._fetch_html(username)
        for blob in extract_json_blobs(html, needle='"user"'):
            try:
                return parse_profile(blob)
            except (ParsingError, ValueError):
                continue
        return Profile(user_id=extract_user_id(html), username=username)

    async def _fetch_html(self, username: str) -> str:
        """Download the profile page as text.

        Goes through the transport ladder, but the response is HTML, so the
        JSON path is bypassed with an explicit raw request.
        """
        from .provider import EscalatingTransportProvider

        url = PROFILE_URL_TEMPLATE.format(username=username)
        if isinstance(self._provider, EscalatingTransportProvider):
            stream = await self._provider.stream(url, headers={"Accept": "text/html"})
            chunks = [chunk async for chunk in stream]
            return b"".join(chunks).decode("utf-8", errors="replace")
        raise ParsingError("provider cannot fetch HTML", path=url)


class CachingUserResolver:
    """Resolves usernames through a strategy chain, caching every success.

    Satisfies :class:`~instadata.interfaces.UserIdResolver`.

    Args:
        strategies: Tried in order. First success wins.
        cache: Where resolved profiles are stored. Pass a ``NullCache`` to
            force a fresh lookup.
        ttl: Cache lifetime. ``None`` means forever, which is correct for the
            id; counts go stale, but no consumer treats them as live data.
    """

    def __init__(
        self,
        strategies: list[ResolutionStrategy],
        *,
        cache: JsonCache | None = None,
        ttl: float | None = None,
    ) -> None:
        if not strategies:
            raise ValueError("at least one resolution strategy is required")
        self._strategies = strategies
        self._cache = cache
        self._ttl = ttl

    async def resolve(self, username: str) -> Profile:
        """Return the profile for ``username``, from cache when possible.

        Raises:
            NotFoundError: Every strategy failed.
            PrivateAccountError: Propagated from a strategy.
        """
        username = normalize_username(username)

        if self._cache is not None and (cached := await self._cache.get_json(username)):
            logger.debug("user id cache hit for {}", username)
            return Profile.model_validate(cached)

        failures: list[str] = []
        for strategy in self._strategies:
            try:
                profile = await strategy.resolve(username)
            except NotFoundError:
                raise
            except ScraperError as exc:
                failures.append(f"{strategy.name}: {exc}")
                logger.warning("resolver {} failed for {}: {}", strategy.name, username, exc)
                continue

            logger.info("resolved {} -> {} via {}", username, profile.user_id, strategy.name)
            if self._cache is not None:
                await self._cache.set_json(username, profile.model_dump(mode="json"), ttl=self._ttl)
            return profile

        raise NotFoundError(f"could not resolve {username}; tried {'; '.join(failures)}")

    async def invalidate(self, username: str) -> None:
        """Drop a cached profile, forcing the next resolve to hit the network."""
        if self._cache is not None:
            await self._cache.delete(normalize_username(username))
