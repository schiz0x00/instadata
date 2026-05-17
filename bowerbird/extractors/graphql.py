"""Parsers for Instagram's GraphQL (``edge_*``) payload shape.

The only module that knows Instagram's field names for timeline data.
Everything downstream consumes :class:`~bowerbird.models.media.Media`,
so a payload change is contained here.

Field names verified against a live logged-out response on 2026-07-31.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ..errors import ParsingError
from ..models.media import Dimensions, Location, Media, MediaResource, MediaType, MusicInfo
from ..models.profile import Page, PageInfo, Profile

__all__ = ["TIMELINE_KEYS", "parse_media_node", "parse_profile", "parse_timeline_page"]

#: Containers Instagram has used for a profile's timeline, newest name first.
TIMELINE_KEYS = (
    "edge_owner_to_timeline_media",
    "edge_felix_video_timeline",
    "edge_web_feed_timeline",
)

_TYPENAME_TO_TYPE = {
    "GraphImage": MediaType.IMAGE,
    "GraphVideo": MediaType.VIDEO,
    "GraphSidecar": MediaType.CAROUSEL,
    "XDTGraphImage": MediaType.IMAGE,
    "XDTGraphVideo": MediaType.VIDEO,
    "XDTGraphSidecar": MediaType.CAROUSEL,
}


def parse_media_node(
    node: dict[str, Any],
    *,
    username: str | None = None,
    owner_id: str | None = None,
    timestamp: datetime | None = None,
) -> Media:
    """Build a :class:`Media` from one timeline node.

    Carousel children are parsed recursively and inherit the parent's caption,
    timestamp and owner, so each child stands on its own downstream.

    Args:
        node: One timeline or post node.
        username: Owner handle. Timeline nodes omit it, so the caller supplies
            the profile it is scraping.
        owner_id: Owner id fallback. Verified live on 2026-07-31: timeline
            nodes carry no ``owner`` object at all, only post pages do.
        timestamp: Timestamp fallback, used for carousel children, which
            Instagram serves without one.

    Raises:
        ParsingError: A field the scraper cannot work without is missing.
    """
    media_id = _require(node, "id")
    media_type = _media_type(node)
    owner = node.get("owner") or {}
    resolved_owner_id = str(owner.get("id") or node.get("owner_id") or owner_id or "")
    if not resolved_owner_id:
        raise ParsingError("node has no owner id", path=f"node[{media_id}].owner.id")

    resolved_timestamp = _timestamp(node, media_id, fallback=timestamp)
    caption = _caption(node)
    resolved_username = username or owner.get("username")

    # Children carry no caption, timestamp, owner or shortcode of their own;
    # they inherit the parent's so each slide is independently meaningful once
    # on disk, and every slide of one post shares a filename prefix.
    children = [
        parse_media_node(
            edge["node"],
            username=resolved_username,
            owner_id=resolved_owner_id,
            timestamp=resolved_timestamp,
        ).model_copy(update={"caption": caption, "shortcode": node.get("shortcode")})
        for edge in _edges(node.get("edge_sidecar_to_children"))
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
    ]

    return Media(
        id=str(media_id),
        shortcode=node.get("shortcode") or node.get("code"),
        media_type=media_type,
        owner_id=resolved_owner_id,
        username=resolved_username,
        caption=caption,
        timestamp=resolved_timestamp,
        like_count=_count(node.get("edge_media_preview_like") or node.get("edge_liked_by")),
        comment_count=_count(node.get("edge_media_to_comment")),
        view_count=node.get("video_view_count") or node.get("video_play_count"),
        dimensions=_dimensions(node.get("dimensions")),
        duration=node.get("video_duration"),
        location=_location(node.get("location")),
        music=_music(node),
        thumbnail_url=node.get("thumbnail_src") or node.get("display_url"),
        resources=_resources(node),
        children=children,
        is_pinned=bool(node.get("pinned_for_users")),
        is_sponsored=bool(node.get("is_paid_partnership") or node.get("is_ad")),
        accessibility_caption=node.get("accessibility_caption"),
    )


def parse_timeline_page(
    payload: dict[str, Any],
    *,
    username: str | None = None,
    owner_id: str | None = None,
) -> Page[Media]:
    """Build a :class:`Page` of media from a timeline GraphQL response.

    Accepts both response envelopes Instagram uses: ``data.user`` from
    ``/graphql/query/`` and ``data.xdt_api__v1__feed__user_timeline_graphql_connection``
    from the newer POST gateway.

    Raises:
        ParsingError: No recognisable timeline container in the payload.
    """
    container = _timeline_container(payload)
    edges = _edges(container)
    items = [
        parse_media_node(edge["node"], username=username, owner_id=owner_id)
        for edge in edges
        if isinstance(edge, dict) and isinstance(edge.get("node"), dict)
    ]
    info = container.get("page_info") or {}
    return Page[Media](
        items=items,
        page_info=PageInfo(
            has_next_page=bool(info.get("has_next_page")),
            end_cursor=info.get("end_cursor"),
        ),
        total_count=container.get("count"),
    )


def parse_profile(payload: dict[str, Any]) -> Profile:
    """Build a :class:`Profile` from a ``web_profile_info`` style response.

    Raises:
        ParsingError: The payload carries no user object or no numeric id.
    """
    user = payload
    for key in ("data", "user"):
        if isinstance(user, dict) and key in user and isinstance(user[key], dict):
            user = user[key]
    if not isinstance(user, dict) or "username" not in user:
        raise ParsingError("no user object in payload", path="data.user")

    user_id = user.get("id") or user.get("pk") or user.get("fbid_v2")
    if not user_id:
        raise ParsingError("user object has no id", path="data.user.id")

    return Profile(
        user_id=str(user_id),
        username=str(user["username"]),
        full_name=user.get("full_name"),
        biography=user.get("biography"),
        external_url=user.get("external_url") or None,
        is_private=bool(user.get("is_private")),
        is_verified=bool(user.get("is_verified")),
        is_business=bool(user.get("is_business_account")),
        follower_count=_count(user.get("edge_followed_by")) or user.get("follower_count"),
        following_count=_count(user.get("edge_follow")) or user.get("following_count"),
        media_count=_count(_timeline_container_or_none(user)) or user.get("media_count"),
        profile_pic_url=user.get("profile_pic_url") or None,
        profile_pic_url_hd=user.get("profile_pic_url_hd") or None,
    )


# --------------------------------------------------------------------------- #
# Field helpers
# --------------------------------------------------------------------------- #


def _require(node: dict[str, Any], key: str) -> Any:
    """Return ``node[key]`` or raise :class:`ParsingError`."""
    value = node.get(key)
    if value is None:
        raise ParsingError(f"missing required field {key!r}", path=f"node.{key}")
    return value


def _media_type(node: dict[str, Any]) -> MediaType:
    """Classify a node, promoting clips to :attr:`MediaType.REEL`."""
    typename = str(node.get("__typename", ""))
    if node.get("product_type") == "clips":
        return MediaType.REEL
    if typename in _TYPENAME_TO_TYPE:
        return _TYPENAME_TO_TYPE[typename]
    if node.get("edge_sidecar_to_children") or node.get("carousel_media"):
        return MediaType.CAROUSEL
    return MediaType.VIDEO if node.get("is_video") else MediaType.IMAGE


def _timestamp(node: dict[str, Any], media_id: Any, fallback: datetime | None = None) -> datetime:
    """Read ``taken_at_timestamp`` as timezone-aware UTC.

    Raises:
        ParsingError: No timestamp on the node and no fallback supplied.
    """
    raw = node.get("taken_at_timestamp") or node.get("taken_at") or node.get("device_timestamp")
    if raw is None:
        if fallback is not None:
            return fallback
        raise ParsingError("node has no timestamp", path=f"node[{media_id}].taken_at_timestamp")
    return datetime.fromtimestamp(int(raw), tz=UTC)


def _caption(node: dict[str, Any]) -> str | None:
    """Extract the caption text from either payload shape."""
    for edge in _edges(node.get("edge_media_to_caption")):
        text = (edge.get("node") or {}).get("text")
        if text:
            return str(text)
    caption = node.get("caption")
    if isinstance(caption, dict):
        return caption.get("text")
    return caption if isinstance(caption, str) else None


def _edges(container: Any) -> list[dict[str, Any]]:
    """Return the ``edges`` list of a connection, or an empty list."""
    if isinstance(container, dict):
        edges = container.get("edges")
        if isinstance(edges, list):
            return [e for e in edges if isinstance(e, dict)]
    return []


def _count(container: Any) -> int | None:
    """Read ``count`` from a connection object."""
    if isinstance(container, dict):
        value = container.get("count")
        return int(value) if isinstance(value, int) else None
    return None


def _dimensions(raw: Any) -> Dimensions | None:
    """Build :class:`Dimensions` when both sides are present and positive."""
    if not isinstance(raw, dict):
        return None
    width, height = raw.get("width"), raw.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return Dimensions(width=width, height=height)
    return None


def _location(raw: Any) -> Location | None:
    """Build :class:`Location` from a geotag object."""
    if not isinstance(raw, dict) or not raw.get("id"):
        return None
    return Location(
        id=str(raw["id"]),
        name=str(raw.get("name") or ""),
        slug=raw.get("slug"),
        latitude=raw.get("lat"),
        longitude=raw.get("lng"),
    )


def _music(node: dict[str, Any]) -> MusicInfo | None:
    """Build :class:`MusicInfo` from a clips audio attribution block."""
    raw = node.get("clips_music_attribution_info") or node.get("music_info")
    if not isinstance(raw, dict):
        return None
    info = raw.get("music_asset_info") if isinstance(raw.get("music_asset_info"), dict) else raw
    return MusicInfo(
        audio_id=_str_or_none(info.get("audio_id") or info.get("audio_cluster_id")),
        title=info.get("song_name") or info.get("title"),
        artist=info.get("artist_name") or info.get("display_artist"),
        is_original_audio=bool(info.get("should_mute_audio") is False or info.get("is_original")),
    )


def _resources(node: dict[str, Any]) -> list[MediaResource]:
    """Collect every downloadable representation of a node.

    Videos yield the stream URL first; the still resolutions are kept because
    they double as the poster frame.
    """
    resources: list[MediaResource] = []
    if video_url := node.get("video_url"):
        resources.append(MediaResource(url=video_url, is_video=True))

    seen: set[str] = {str(r.url) for r in resources}
    for candidate in node.get("display_resources") or []:
        if not isinstance(candidate, dict):
            continue
        url = candidate.get("src")
        if url and url not in seen:
            seen.add(url)
            resources.append(
                MediaResource(
                    url=url,
                    width=candidate.get("config_width"),
                    height=candidate.get("config_height"),
                )
            )

    display_url = node.get("display_url")
    if display_url and display_url not in seen:
        dims = _dimensions(node.get("dimensions"))
        resources.append(
            MediaResource(
                url=display_url,
                width=dims.width if dims else None,
                height=dims.height if dims else None,
            )
        )
    return resources


def _timeline_container(payload: dict[str, Any]) -> dict[str, Any]:
    """Locate the timeline connection inside any known response envelope.

    Raises:
        ParsingError: No known container is present.
    """
    container = _timeline_container_or_none(payload)
    if container is None:
        raise ParsingError("no timeline container in payload", path="data.user")
    return container


def _timeline_container_or_none(payload: Any) -> dict[str, Any] | None:
    """Depth-limited search for a connection object with an ``edges`` list."""
    if not isinstance(payload, dict):
        return None
    for key in TIMELINE_KEYS:
        if isinstance(payload.get(key), dict):
            return payload[key]
    if isinstance(payload.get("edges"), list) and "page_info" in payload:
        return payload
    for key in ("data", "user", "xdt_api__v1__feed__user_timeline_graphql_connection"):
        if isinstance(payload.get(key), dict) and (
            found := _timeline_container_or_none(payload[key])
        ):
            return found
    return None


def _str_or_none(value: Any) -> str | None:
    """Stringify a value, preserving ``None``."""
    return None if value is None else str(value)
