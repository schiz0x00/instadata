"""Parsers for Instagram's private API (``/api/v1``) payload shape.

Stories and highlights are only served in this shape, which names the same
concepts differently from GraphQL: ``image_versions2.candidates`` instead of
``display_resources``, ``media_type`` as an integer, ``pk`` instead of ``id``.
Producing the same :class:`Media` from both keeps the rest of the pipeline
unaware that two dialects exist.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..errors import ParsingError
from ..models.media import Dimensions, Location, Media, MediaResource, MediaType, MusicInfo

__all__ = ["V1_MEDIA_TYPES", "parse_reels_tray", "parse_v1_item"]

#: Instagram's integer media-type codes.
V1_MEDIA_TYPES = {1: MediaType.IMAGE, 2: MediaType.VIDEO, 8: MediaType.CAROUSEL}


def parse_v1_item(item: dict[str, Any], *, is_story: bool = False) -> Media:
    """Build a :class:`Media` from one ``/api/v1`` media object.

    Args:
        item: A media object from a feed, reel tray or highlight response.
        is_story: Tag the result as :attr:`MediaType.STORY`, which the
            downloader uses to pick a separate output directory.

    Raises:
        ParsingError: A required field is missing.
    """
    media_id = item.get("pk") or item.get("id")
    if not media_id:
        raise ParsingError("v1 item has no pk", path="item.pk")

    user = item.get("user") or {}
    owner_id = str(user.get("pk") or user.get("id") or item.get("user_id") or "")
    if not owner_id:
        raise ParsingError("v1 item has no owner", path="item.user.pk")

    taken_at = item.get("taken_at")
    if taken_at is None:
        raise ParsingError("v1 item has no taken_at", path="item.taken_at")
    timestamp = datetime.fromtimestamp(int(taken_at), tz=UTC)

    base_type = V1_MEDIA_TYPES.get(int(item.get("media_type") or 1), MediaType.IMAGE)
    if is_story:
        media_type = MediaType.STORY
    elif item.get("product_type") == "clips":
        media_type = MediaType.REEL
    else:
        media_type = base_type

    raw_caption = item.get("caption")
    caption = raw_caption.get("text") if isinstance(raw_caption, dict) else None
    username = user.get("username")

    children = [
        parse_v1_item({**child, "user": user, "taken_at": taken_at}, is_story=is_story)
        for child in item.get("carousel_media") or []
        if isinstance(child, dict)
    ]

    return Media(
        id=str(media_id),
        shortcode=item.get("code"),
        media_type=MediaType.CAROUSEL if children and not is_story else media_type,
        owner_id=owner_id,
        username=username,
        caption=caption,
        timestamp=timestamp,
        like_count=item.get("like_count"),
        comment_count=item.get("comment_count"),
        view_count=item.get("play_count") or item.get("view_count"),
        dimensions=_dimensions(item),
        duration=item.get("video_duration"),
        location=_location(item.get("location")),
        music=_music(item),
        thumbnail_url=_first_image_url(item),
        resources=_resources(item),
        children=children,
        is_pinned=bool(item.get("timeline_pinned_user_ids")),
        is_sponsored=bool(item.get("is_paid_partnership")),
        accessibility_caption=item.get("accessibility_caption"),
    )


def parse_reels_tray(payload: dict[str, Any], *, user_id: str | None = None) -> list[Media]:
    """Flatten a ``reels_media`` response into a list of story items.

    Args:
        payload: Response from the stories or highlights endpoint.
        user_id: When given, keep only reels belonging to this user.
    """
    reels = payload.get("reels_media") or payload.get("reels") or []
    if isinstance(reels, dict):
        reels = list(reels.values())

    media: list[Media] = []
    for reel in reels:
        if not isinstance(reel, dict):
            continue
        owner = str((reel.get("user") or {}).get("pk") or reel.get("id") or "")
        # Exact match, not a prefix: user 123 must not collect user 1234's
        # stories. The tray is keyed by the same numeric id in both the
        # ``user.pk`` and bare ``id`` forms, so one comparison covers both.
        if user_id and owner and owner != str(user_id):
            continue
        media.extend(
            parse_v1_item(item, is_story=True)
            for item in reel.get("items") or []
            if isinstance(item, dict)
        )
    return media


# --------------------------------------------------------------------------- #
# Field helpers
# --------------------------------------------------------------------------- #


def _candidates(item: dict[str, Any]) -> list[dict[str, Any]]:
    """Image candidates for an item, highest quality first as served."""
    versions = item.get("image_versions2") or {}
    candidates = versions.get("candidates") if isinstance(versions, dict) else None
    return [c for c in candidates or [] if isinstance(c, dict)]


def _resources(item: dict[str, Any]) -> list[MediaResource]:
    """Every downloadable representation, video streams first."""
    resources: list[MediaResource] = []
    seen: set[str] = set()

    for version in item.get("video_versions") or []:
        url = version.get("url") if isinstance(version, dict) else None
        if url and url not in seen:
            seen.add(url)
            resources.append(
                MediaResource(
                    url=url,
                    width=version.get("width"),
                    height=version.get("height"),
                    is_video=True,
                )
            )

    for candidate in _candidates(item):
        url = candidate.get("url")
        if url and url not in seen:
            seen.add(url)
            resources.append(
                MediaResource(url=url, width=candidate.get("width"), height=candidate.get("height"))
            )
    return resources


def _first_image_url(item: dict[str, Any]) -> str | None:
    """Best available still, used as the thumbnail."""
    candidates = _candidates(item)
    return candidates[0].get("url") if candidates else None


def _dimensions(item: dict[str, Any]) -> Dimensions | None:
    """Original dimensions, falling back to the largest image candidate."""
    width, height = item.get("original_width"), item.get("original_height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return Dimensions(width=width, height=height)
    for candidate in _candidates(item):
        cw, ch = candidate.get("width"), candidate.get("height")
        if isinstance(cw, int) and isinstance(ch, int) and cw > 0 and ch > 0:
            return Dimensions(width=cw, height=ch)
    return None


def _location(raw: Any) -> Location | None:
    """Build :class:`Location` from a v1 location object."""
    if not isinstance(raw, dict) or not (raw.get("pk") or raw.get("id")):
        return None
    return Location(
        id=str(raw.get("pk") or raw.get("id")),
        name=str(raw.get("name") or ""),
        slug=raw.get("short_name"),
        latitude=raw.get("lat"),
        longitude=raw.get("lng"),
    )


def _music(item: dict[str, Any]) -> MusicInfo | None:
    """Build :class:`MusicInfo` from a v1 clips metadata block."""
    metadata = item.get("clips_metadata") or {}
    if not isinstance(metadata, dict):
        return None
    music = metadata.get("music_info") or {}
    asset = (music or {}).get("music_asset_info") or {}
    original = metadata.get("original_sound_info") or {}
    if not asset and not original:
        return None
    return MusicInfo(
        audio_id=str(asset.get("audio_cluster_id") or original.get("audio_asset_id") or "") or None,
        title=asset.get("title") or original.get("original_audio_title"),
        artist=asset.get("display_artist") or (original.get("ig_artist") or {}).get("username"),
        is_original_audio=bool(original),
    )
