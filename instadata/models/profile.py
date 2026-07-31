"""Profile and pagination-page models."""

from __future__ import annotations

from datetime import UTC
from typing import Generic, TypeVar

from pydantic import BaseModel, ConfigDict, Field, HttpUrl

from .media import Media, MediaResource, MediaType

__all__ = ["Page", "PageInfo", "Profile"]

T = TypeVar("T")


class Profile(BaseModel):
    """A resolved Instagram profile.

    The scraper only needs :attr:`user_id` to paginate, but the rest is cheap
    to keep and expensive to re-fetch, so the whole record is cached.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    user_id: str
    username: str
    full_name: str | None = None
    biography: str | None = None
    external_url: HttpUrl | None = None

    is_private: bool = False
    is_verified: bool = False
    is_business: bool = False

    follower_count: int | None = None
    following_count: int | None = None
    media_count: int | None = None

    profile_pic_url: HttpUrl | None = None
    profile_pic_url_hd: HttpUrl | None = None

    def profile_picture_media(self) -> Media | None:
        """Represent the avatar as a downloadable :class:`Media`.

        Lets the profile picture flow through the same downloader path as
        posts instead of needing a special case.
        """
        from datetime import datetime

        url = self.profile_pic_url_hd or self.profile_pic_url
        if url is None:
            return None
        return Media(
            id=f"{self.user_id}_profile_picture",
            shortcode=None,
            media_type=MediaType.PROFILE_PICTURE,
            owner_id=self.user_id,
            username=self.username,
            timestamp=datetime.now(tz=UTC),
            resources=[MediaResource(url=url)],
        )


class PageInfo(BaseModel):
    """Cursor state for one GraphQL page."""

    model_config = ConfigDict(frozen=True, extra="ignore")

    has_next_page: bool = False
    end_cursor: str | None = None


class Page(BaseModel, Generic[T]):
    """One page of results plus the cursor needed to fetch the next.

    Generic so the same paginator machinery serves posts, reels and stories.
    """

    model_config = ConfigDict(extra="ignore")

    items: list[T] = Field(default_factory=list)
    page_info: PageInfo = Field(default_factory=PageInfo)
    total_count: int | None = Field(
        default=None,
        description="Total items the account exposes, when advertised.",
    )

    def __len__(self) -> int:
        """Number of items on this page."""
        return len(self.items)
