"""High-level Instagram API client.

Composes the transport ladder, the resolver and the paginator into the small
surface the rest of the program actually wants: a profile, an async stream of
media, a single post, stories, highlights. Nothing here knows which transport
tier answered, and nothing here touches the filesystem.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any, Self

from ..auth.cookies import FileCookieProvider, NullCookieProvider
from ..cache.file_cache import FileCache, JsonCache
from ..errors import AuthenticationError, NotFoundError, PrivateAccountError, ScraperError
from ..extractors.graphql import parse_media_node
from ..extractors.html import extract_json_blobs
from ..extractors.v1 import parse_reels_tray, parse_v1_item
from ..interfaces import CookieProvider
from ..models.config import ScraperConfig
from ..models.media import Media
from ..models.profile import Page, Profile
from ..pagination.timeline import TimelinePaginator
from ..utils.logging import logger
from ..utils.shortcode import shortcode_to_media_id
from ..utils.urls import extract_shortcode
from .endpoints import (
    HIGHLIGHTS_TRAY_URL,
    MEDIA_INFO_URL,
    POST_URL_TEMPLATE,
    STORIES_REEL_URL,
)
from .provider import EscalatingTransportProvider, default_transport_factory
from .resolver import CachingUserResolver, HtmlPageStrategy, WebProfileInfoStrategy

__all__ = ["InstagramClient"]


class InstagramClient:
    """Read-only Instagram client.

    Prefer :meth:`build` over the constructor unless you are injecting
    doubles; the constructor takes fully-built collaborators so every one of
    them can be replaced in tests.

    Args:
        provider: Transport ladder.
        resolver: Username to profile resolution.
        paginator: Timeline walker.
        config: Root configuration.
        cookies: Session provider, used by the endpoints that require auth.
    """

    def __init__(
        self,
        *,
        provider: EscalatingTransportProvider,
        resolver: CachingUserResolver,
        paginator: TimelinePaginator,
        config: ScraperConfig | None = None,
        cookies: CookieProvider | None = None,
    ) -> None:
        self._provider = provider
        self._resolver = resolver
        self._paginator = paginator
        self._config = config or ScraperConfig()
        self._cookies = cookies or NullCookieProvider()

    @classmethod
    def build(cls, config: ScraperConfig | None = None) -> Self:
        """Wire a client from configuration, using the standard components."""
        config = config or ScraperConfig()
        cookies: CookieProvider
        if config.cookies_path is not None:
            cookies = FileCookieProvider(config.cookies_path)
        else:
            cookies = NullCookieProvider()

        provider = EscalatingTransportProvider(
            config,
            factory=default_transport_factory(config, cookies),
        )
        cache = JsonCache(FileCache(config.cache_dir / "profiles"), namespace="profile")
        resolver = CachingUserResolver(
            [WebProfileInfoStrategy(provider), HtmlPageStrategy(provider)],
            cache=cache,
        )
        paginator = TimelinePaginator(provider, page_size=config.page_size)
        return cls(
            provider=provider,
            resolver=resolver,
            paginator=paginator,
            config=config,
            cookies=cookies,
        )

    @property
    def provider(self) -> EscalatingTransportProvider:
        """The transport ladder, exposed for callers that need to share it.

        Its rate limiter paces API calls only; media downloads run on their
        own budget, since the CDN is a different host with a different quota.
        """
        return self._provider

    @property
    def config(self) -> ScraperConfig:
        """Root configuration."""
        return self._config

    # ------------------------------------------------------------- profiles

    async def get_profile(self, username: str) -> Profile:
        """Resolve a username to a full profile record.

        Raises:
            NotFoundError: No such account.
        """
        return await self._resolver.resolve(username)

    # ---------------------------------------------------------------- posts

    async def iter_posts(
        self,
        username: str,
        *,
        cursor: str | None = None,
        max_items: int | None = None,
    ) -> AsyncIterator[Media]:
        """Yield a profile's posts, newest first.

        Resolves the username on the first call and streams pages lazily; the
        whole timeline is never held in memory.

        Raises:
            PrivateAccountError: The account is private and not followed.
        """
        profile = await self.get_profile(username)
        await self._guard_visibility(profile)
        async for media in self._paginator.iter_media(
            profile.user_id,
            cursor=cursor,
            username=profile.username,
            max_items=max_items,
        ):
            yield media

    async def iter_post_pages(
        self,
        username: str,
        *,
        cursor: str | None = None,
    ) -> AsyncIterator[Page[Media]]:
        """Yield raw pages, so callers can persist the cursor per page."""
        profile = await self.get_profile(username)
        await self._guard_visibility(profile)
        async for page in self._paginator.iter_pages(
            profile.user_id, cursor=cursor, username=profile.username
        ):
            yield page

    async def get_post(self, url_or_shortcode: str) -> Media:
        """Fetch one post, reel or carousel by URL or shortcode.

        Two routes, cheapest first: the post page's embedded JSON, then the
        media-info endpoint keyed by the numeric id derived from the shortcode.
        The second needs a session — verified on 2026-07-31 that a logged-out
        post page ships no media JSON, hydrated or not.

        Raises:
            NotFoundError: The shortcode is malformed or the post is gone.
            AuthenticationError: The page route found nothing and no session
                is configured for the fallback.
        """
        shortcode = extract_shortcode(url_or_shortcode)
        if not shortcode:
            raise NotFoundError(f"not a post URL or shortcode: {url_or_shortcode!r}")

        if media := await self._post_from_page(shortcode):
            return media

        jar = await self._cookies.load()
        if "sessionid" not in jar:
            raise AuthenticationError(
                f"post {shortcode} needs a session: Instagram no longer embeds post JSON "
                "for logged-out clients. Pass --cookies, or scrape the owner's profile."
            )
        return await self._post_from_media_info(shortcode)

    async def _post_from_page(self, shortcode: str) -> Media | None:
        """Try to parse a post out of its own page. Returns ``None`` on a miss.

        A transport failure here is a miss, not a fatal error: Instagram
        answers logged-out post pages with 401/403 as readily as with an empty
        200, and both mean the same thing — this route is exhausted, try the
        next one. Letting the exception escape would make the media-info
        fallback in :meth:`get_post` unreachable.
        """
        url = POST_URL_TEMPLATE.format(shortcode=shortcode)
        try:
            stream = await self._provider.stream(url, headers={"Accept": "text/html"})
            html = b"".join([chunk async for chunk in stream]).decode("utf-8", errors="replace")
        except NotFoundError:
            raise
        except ScraperError as exc:
            logger.debug("post page for {} unavailable ({}), trying media-info", shortcode, exc)
            return None

        for blob in extract_json_blobs(html, needle="shortcode"):
            if node := _find_media_node(blob, shortcode):
                logger.debug("parsed post {} from embedded JSON", shortcode)
                return parse_media_node(node)
        logger.debug("post page for {} carried no media JSON", shortcode)
        return None

    async def _post_from_media_info(self, shortcode: str) -> Media:
        """Fetch a post through the media-info endpoint.

        Raises:
            NotFoundError: The endpoint returned no items.
        """
        media_id = shortcode_to_media_id(shortcode)
        payload = await self._provider.request_json(
            "GET",
            MEDIA_INFO_URL.format(media_id=media_id),
            headers={"Referer": POST_URL_TEMPLATE.format(shortcode=shortcode)},
        )
        items = (payload or {}).get("items") or []
        if not items:
            raise NotFoundError(f"post {shortcode} returned no media")
        return parse_v1_item(items[0])

    # ----------------------------------------------------- stories and reels

    async def get_stories(self, username: str) -> list[Media]:
        """Fetch a user's active stories.

        Requires a session: Instagram serves no stories to logged-out clients.

        Raises:
            AuthenticationError: No session cookie is configured.
        """
        await self._require_session("stories")
        profile = await self.get_profile(username)
        payload = await self._provider.request_json(
            "GET",
            STORIES_REEL_URL,
            params={"reel_ids": profile.user_id},
        )
        return parse_reels_tray(payload, user_id=profile.user_id)

    async def get_highlights(self, username: str) -> list[Media]:
        """Fetch every media item across a user's highlight reels.

        Raises:
            AuthenticationError: No session cookie is configured.
        """
        await self._require_session("highlights")
        profile = await self.get_profile(username)
        tray = await self._provider.request_json(
            "GET", HIGHLIGHTS_TRAY_URL.format(user_id=profile.user_id)
        )
        reel_ids = [
            str(item.get("id"))
            for item in (tray or {}).get("tray", [])
            if isinstance(item, dict) and item.get("id")
        ]
        if not reel_ids:
            return []

        media: list[Media] = []
        # Batched: the endpoint accepts several reel ids per request, and each
        # request is a rate-limit event we would rather not spend per reel.
        for batch in _chunks(reel_ids, 10):
            payload = await self._provider.request_json(
                "GET", STORIES_REEL_URL, params={"reel_ids": ",".join(batch)}
            )
            media.extend(parse_reels_tray(payload))
        return media

    async def get_profile_picture(self, username: str) -> Media:
        """Return the profile picture as a downloadable media item.

        Raises:
            NotFoundError: The account exposes no avatar.
        """
        profile = await self.get_profile(username)
        media = profile.profile_picture_media()
        if media is None:
            raise NotFoundError(f"{username} has no profile picture URL")
        return media

    # -------------------------------------------------------------- helpers

    async def _guard_visibility(self, profile: Profile) -> None:
        """Fail fast on a private account we cannot read.

        A session may well be able to read a private account — that is the
        entire point of passing ``--cookies`` for an account that follows it.
        So this only refuses when there is no session at all; with one, the
        request goes out and Instagram decides, raising
        :class:`PrivateAccountError` from the payload if it says no.

        Raises:
            PrivateAccountError: Private, and no session is configured.
        """
        if not profile.is_private:
            return
        if "sessionid" in await self._cookies.load():
            logger.debug("{} is private; trying with the configured session", profile.username)
            return
        raise PrivateAccountError(
            f"{profile.username} is private; supply --cookies for an account that follows them"
        )

    async def _require_session(self, feature: str) -> None:
        """Ensure a session cookie exists before calling an auth-only endpoint.

        Raises:
            AuthenticationError: No session cookie is available.
        """
        jar = await self._cookies.load()
        if "sessionid" not in jar:
            raise AuthenticationError(f"{feature} require a session; pass --cookies")

    async def aclose(self) -> None:
        """Close every transport the client owns."""
        await self._provider.aclose()

    async def __aenter__(self) -> Self:
        """Enter an async context, returning this client."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the client on context exit."""
        await self.aclose()


def _find_media_node(blob: Any, shortcode: str, depth: int = 0) -> dict[str, Any] | None:
    """Depth-limited search for the media object matching ``shortcode``."""
    if depth > 12 or not isinstance(blob, (dict, list)):
        return None
    if isinstance(blob, dict):
        if blob.get("shortcode") == shortcode and ("__typename" in blob or "display_url" in blob):
            return blob
        for key in ("shortcode_media", "xdt_shortcode_media", "media"):
            if isinstance(blob.get(key), dict) and (
                found := _find_media_node(blob[key], shortcode, depth + 1)
            ):
                return found
        for value in blob.values():
            if found := _find_media_node(value, shortcode, depth + 1):
                return found
        return None
    for item in blob:
        if found := _find_media_node(item, shortcode, depth + 1):
            return found
    return None


def _chunks(values: list[str], size: int) -> list[list[str]]:
    """Split a list into fixed-size chunks."""
    return [values[i : i + size] for i in range(0, len(values), size)]
