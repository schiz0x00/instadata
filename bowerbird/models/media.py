"""Media domain models.

These are the scraper's own vocabulary, deliberately decoupled from
Instagram's GraphQL field names. Parsers in ``extractors`` translate raw
payloads into these types, so an Instagram schema change is confined to the
parsers and never reaches storage, the downloader or the CLI.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, computed_field, field_validator

__all__ = [
    "HASHTAG_RE",
    "MENTION_RE",
    "Dimensions",
    "Location",
    "Media",
    "MediaResource",
    "MediaType",
    "MusicInfo",
]

# Unicode-aware: Instagram allows non-ASCII hashtags. Mentions are ASCII-only
# by Instagram's own username rules (letters, digits, period, underscore).
HASHTAG_RE = re.compile(r"#(\w+)", re.UNICODE)
MENTION_RE = re.compile(r"@([A-Za-z0-9._]+)")

_ILLEGAL_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


class MediaType(StrEnum):
    """Kind of a single media item.

    ``CAROUSEL`` is a container: its own bytes are the cover image, and the
    downloadable items live in :attr:`Media.children`.
    """

    IMAGE = "image"
    VIDEO = "video"
    REEL = "reel"
    CAROUSEL = "carousel"
    PROFILE_PICTURE = "profile_picture"
    STORY = "story"

    @property
    def is_video(self) -> bool:
        """Whether items of this type carry a video stream."""
        return self in (MediaType.VIDEO, MediaType.REEL)

    @property
    def file_extension(self) -> str:
        """Default extension, used when the URL carries no usable suffix."""
        return ".mp4" if self.is_video else ".jpg"


class BaseValueModel(BaseModel):
    """Immutable value object with strict validation."""

    model_config = ConfigDict(frozen=True, extra="ignore", str_strip_whitespace=True)


class Dimensions(BaseValueModel):
    """Pixel dimensions of a media item."""

    width: Annotated[int, Field(gt=0)]
    height: Annotated[int, Field(gt=0)]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def aspect_ratio(self) -> float:
        """Width divided by height."""
        return self.width / self.height


class Location(BaseValueModel):
    """Geotag attached to a post."""

    id: str
    name: str
    slug: str | None = None
    latitude: float | None = None
    longitude: float | None = None


class MusicInfo(BaseValueModel):
    """Audio track attached to a reel or clip."""

    audio_id: str | None = None
    title: str | None = None
    artist: str | None = None
    is_original_audio: bool = False


class MediaResource(BaseValueModel):
    """One downloadable representation of a media item.

    Instagram serves several resolutions per item; keeping them all lets the
    downloader pick a quality without a second API round-trip.
    """

    url: HttpUrl
    width: int | None = None
    height: int | None = None
    is_video: bool = Field(
        default=False,
        description="Whether this URL serves the video stream rather than a still.",
    )

    @property
    def pixels(self) -> int:
        """Pixel count, or ``0`` when the resolution is unknown."""
        return (self.width or 0) * (self.height or 0)


class Media(BaseModel):
    """A single Instagram media item.

    A carousel is represented as one ``Media`` with ``media_type ==
    MediaType.CAROUSEL`` and one child per slide. Children inherit the
    parent's owner, caption and timestamp so each is independently
    downloadable and independently serialisable.
    """

    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)

    id: str
    shortcode: str | None = None
    media_type: MediaType

    owner_id: str
    username: str | None = None

    caption: str | None = None
    timestamp: datetime

    like_count: int | None = None
    comment_count: int | None = None
    view_count: int | None = None

    dimensions: Dimensions | None = None
    duration: float | None = Field(default=None, description="Video duration in seconds.")

    location: Location | None = None
    music: MusicInfo | None = None

    thumbnail_url: HttpUrl | None = None
    resources: list[MediaResource] = Field(
        default_factory=list,
        description="Available representations, unordered.",
    )
    children: list[Media] = Field(
        default_factory=list,
        description="Carousel slides; empty for single-item media.",
    )

    is_pinned: bool = False
    is_sponsored: bool = False
    accessibility_caption: str | None = None

    @field_validator("timestamp")
    @classmethod
    def _ensure_utc(cls, value: datetime) -> datetime:
        """Normalise every timestamp to timezone-aware UTC."""
        if value.tzinfo is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)

    # ---------------------------------------------------------------- derived

    @computed_field  # type: ignore[prop-decorator]
    @property
    def media_urls(self) -> list[str]:
        """Every downloadable URL for this item, children included."""
        own = [str(r.url) for r in self.resources]
        return own + [url for child in self.children for url in child.media_urls]

    @computed_field  # type: ignore[prop-decorator]
    @property
    def hashtags(self) -> list[str]:
        """Hashtags in the caption, lowercased, without ``#``, deduplicated."""
        return _unique(tag.lower() for tag in HASHTAG_RE.findall(self.caption or ""))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def mentions(self) -> list[str]:
        """Usernames mentioned in the caption, without ``@``, deduplicated."""
        return _unique(name.lower().rstrip(".") for name in MENTION_RE.findall(self.caption or ""))

    @computed_field  # type: ignore[prop-decorator]
    @property
    def permalink(self) -> str | None:
        """Canonical instagram.com URL, when a shortcode is known."""
        if not self.shortcode:
            return None
        return f"https://www.instagram.com/p/{self.shortcode}/"

    @property
    def best_resource(self) -> MediaResource | None:
        """The representation worth downloading, or ``None`` when there is none.

        For video items the stream always wins, even though Instagram reports
        no dimensions for it — picking the highest-resolution *still* for a
        reel would silently download a poster frame instead of the video.
        """
        if not self.resources:
            return None
        if (self.media_type.is_video or self.is_video_item) and (
            streams := [r for r in self.resources if r.is_video]
        ):
            return max(streams, key=lambda r: r.pixels)
        return max(self.resources, key=lambda r: r.pixels)

    @property
    def is_video_item(self) -> bool:
        """Whether any representation is a video stream."""
        return any(resource.is_video for resource in self.resources)

    # ---------------------------------------------------------------- naming

    def filename(self, index: int | None = None) -> str:
        """Deterministic, filesystem-safe filename for this item.

        Deterministic naming is what makes resume idempotent: a re-run maps a
        post to the same path and can skip it without consulting the store.

        Args:
            index: Carousel slide number, 1-based. Omitted for single items.
        """
        stamp = self.timestamp.strftime("%Y%m%d_%H%M%S")
        stem = self.shortcode or self.id
        suffix = f"_{index:02d}" if index is not None else ""
        name = f"{stamp}_{stem}{suffix}{self.media_type.file_extension}"
        return _ILLEGAL_FILENAME_CHARS.sub("_", name)

    def flatten(self) -> list[Media]:
        """This item and every child, depth-first."""
        return [self, *(descendant for child in self.children for descendant in child.flatten())]

    def downloadable(self) -> list[Media]:
        """Every node that becomes its own file on disk.

        Leaves only. A carousel is a container whose ``display_url`` is its
        cover — which Instagram serves as the first slide's image — so writing
        the container too would duplicate a slide on every carousel.

        The single definition of "how many files does this post have", shared
        by the downloader and by the completeness check that decides whether a
        post may be recorded as done.
        """
        return [item for item in self.flatten() if item.resources and not item.children]

    def with_username(self, username: str) -> Self:
        """Return a copy with ``username`` set on this item and its children.

        GraphQL timeline nodes omit the owner username on children; the
        caller knows it from the profile it is scraping.
        """
        return self.model_copy(
            update={
                "username": username,
                "children": [child.with_username(username) for child in self.children],
            }
        )


def _unique(values: Iterable[str]) -> list[str]:
    """Deduplicate an iterable of strings, preserving first-seen order."""
    return list(dict.fromkeys(values))


Media.model_rebuild()
