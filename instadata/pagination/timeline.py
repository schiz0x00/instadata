"""Timeline pagination.

Yields one page at a time and never accumulates: a 100k-post account holds at
most 12 parsed items in memory. The cursor is surfaced after every page so a
caller can persist it and resume mid-account.

Instagram caps the timeline page size at 12 regardless of what ``first`` asks
for — verified live on 2026-07-31 by requesting 50 and receiving 12.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import orjson

from ..api.endpoints import DOC_ID_TIMELINE, DOC_ID_TIMELINE_FALLBACKS, GRAPHQL_QUERY_URL
from ..errors import ParsingError
from ..extractors.graphql import parse_timeline_page
from ..interfaces import TransportProvider
from ..models.media import Media
from ..models.profile import Page
from ..utils.logging import logger

__all__ = ["TimelinePaginator"]


class TimelinePaginator:
    """Walks a user's post timeline, newest first.

    Args:
        provider: Transport ladder used for every page request.
        page_size: Items requested per page. Instagram clamps this to 12.
        doc_id: Primary persisted-query id.
        fallback_doc_ids: Tried in order when the primary stops parsing, so a
            retired ``doc_id`` degrades instead of breaking the scraper.
    """

    def __init__(
        self,
        provider: TransportProvider,
        *,
        page_size: int = 12,
        doc_id: str = DOC_ID_TIMELINE,
        fallback_doc_ids: tuple[str, ...] = DOC_ID_TIMELINE_FALLBACKS,
    ) -> None:
        self._provider = provider
        self._page_size = page_size
        self._doc_ids = (doc_id, *fallback_doc_ids)
        self._active_doc_id = doc_id

    @property
    def active_doc_id(self) -> str:
        """Document id currently in use, after any fallback."""
        return self._active_doc_id

    async def fetch_page(
        self,
        user_id: str,
        *,
        cursor: str | None = None,
        username: str | None = None,
    ) -> Page[Media]:
        """Fetch and parse one page.

        Raises:
            ParsingError: Every document id failed to produce a timeline.
        """
        failures: list[str] = []
        for doc_id in self._candidate_doc_ids():
            params = {
                "doc_id": doc_id,
                "variables": orjson.dumps(self._variables(user_id, cursor)).decode(),
            }
            try:
                payload = await self._provider.request_json("GET", GRAPHQL_QUERY_URL, params=params)
                page = parse_timeline_page(payload, username=username, owner_id=str(user_id))
            except ParsingError as exc:
                failures.append(f"{doc_id}: {exc}")
                logger.warning("doc_id {} no longer parses, trying fallback", doc_id)
                continue

            if doc_id != self._active_doc_id:
                logger.info("switched timeline doc_id to {}", doc_id)
                self._active_doc_id = doc_id
            return page

        raise ParsingError(f"no working timeline doc_id; tried {'; '.join(failures)}")

    async def iter_pages(
        self,
        user_id: str,
        *,
        cursor: str | None = None,
        username: str | None = None,
        max_pages: int | None = None,
    ) -> AsyncIterator[Page[Media]]:
        """Yield pages until the timeline is exhausted.

        Args:
            user_id: Numeric account id.
            cursor: Resume point; ``None`` starts from the newest post.
            username: Stamped onto every item, since nodes omit it.
            max_pages: Stop early. ``None`` means walk the whole account.

        Yields:
            One :class:`Page` per request, in feed order.
        """
        pages = 0
        while max_pages is None or pages < max_pages:
            page = await self.fetch_page(user_id, cursor=cursor, username=username)
            pages += 1
            logger.debug(
                "page {} for {}: {} items, next={}",
                pages,
                user_id,
                len(page),
                bool(page.page_info.has_next_page),
            )
            yield page

            if not page.page_info.has_next_page or not page.page_info.end_cursor:
                return
            if page.page_info.end_cursor == cursor:
                # Defensive: a repeated cursor would loop forever.
                logger.warning("cursor did not advance, stopping pagination")
                return
            cursor = page.page_info.end_cursor

    async def iter_media(
        self,
        user_id: str,
        *,
        cursor: str | None = None,
        username: str | None = None,
        max_items: int | None = None,
    ) -> AsyncIterator[Media]:
        """Yield individual media items, flattening pages.

        Args:
            max_items: Stop after this many top-level posts. Carousels count
                as one item, matching what the profile's post count reports.
        """
        emitted = 0
        async for page in self.iter_pages(user_id, cursor=cursor, username=username):
            for media in page.items:
                yield media
                emitted += 1
                if max_items is not None and emitted >= max_items:
                    return

    def _candidate_doc_ids(self) -> tuple[str, ...]:
        """Document ids to try, the last known-good one first."""
        return (self._active_doc_id, *(d for d in self._doc_ids if d != self._active_doc_id))

    def _variables(self, user_id: str, cursor: str | None) -> dict[str, object]:
        """GraphQL variables for one timeline page."""
        variables: dict[str, object] = {
            "id": str(user_id),
            "include_clips_attribution_info": True,
            "first": self._page_size,
        }
        if cursor:
            variables["after"] = cursor
        return variables


async def collect(iterator: AsyncIterator[Media], limit: int | None = None) -> list[Media]:
    """Drain an async media iterator into a list.

    For tests and small jobs only. Production paths consume the iterator
    directly so memory stays flat.
    """
    items: list[Media] = []
    async for media in iterator:
        items.append(media)
        if limit is not None and len(items) >= limit:
            break
    return items
