"""Shared fixtures and payload builders.

Payload shapes here mirror what a live logged-out session returned on
2026-07-31; keeping them in one place means a real Instagram change is
reproduced in one file rather than across every test.
"""

from __future__ import annotations

from typing import Any

import pytest


def image_node(node_id: str = "1", shortcode: str = "AAA") -> dict[str, Any]:
    """A single-image timeline node."""
    return {
        "__typename": "GraphImage",
        "id": node_id,
        "shortcode": shortcode,
        "display_url": f"https://cdn.example/{shortcode}_full.jpg",
        "display_resources": [
            {
                "src": f"https://cdn.example/{shortcode}_640.jpg",
                "config_width": 640,
                "config_height": 640,
            },
            {
                "src": f"https://cdn.example/{shortcode}_1080.jpg",
                "config_width": 1080,
                "config_height": 1080,
            },
        ],
        "thumbnail_src": f"https://cdn.example/{shortcode}_thumb.jpg",
        "is_video": False,
        "dimensions": {"width": 1080, "height": 1080},
        "taken_at_timestamp": 1785528674,
        "owner": {"id": "25025320", "username": "instagram"},
        "edge_media_to_caption": {"edges": [{"node": {"text": "hello #space @nasa"}}]},
        "edge_media_preview_like": {"count": 42},
        "edge_media_to_comment": {"count": 7},
        "accessibility_caption": "Photo of a rocket",
    }


def video_node(node_id: str = "2", shortcode: str = "BBB") -> dict[str, Any]:
    """A reel node, which Instagram marks with ``product_type: clips``."""
    return {
        "__typename": "GraphVideo",
        "id": node_id,
        "shortcode": shortcode,
        "product_type": "clips",
        "display_url": f"https://cdn.example/{shortcode}_poster.jpg",
        "video_url": f"https://cdn.example/{shortcode}.mp4",
        "video_view_count": 1234,
        "is_video": True,
        "dimensions": {"width": 720, "height": 1280},
        "taken_at_timestamp": 1785528000,
        "owner": {"id": "25025320", "username": "instagram"},
        "edge_media_to_caption": {"edges": [{"node": {"text": "reel time"}}]},
        "clips_music_attribution_info": {
            "audio_id": "999",
            "song_name": "Track",
            "artist_name": "Artist",
        },
    }


def carousel_node(node_id: str = "3", shortcode: str = "CCC", children: int = 3) -> dict[str, Any]:
    """A carousel node with ``children`` slides, the last one a video."""
    slides = []
    for index in range(children):
        is_video = index == children - 1
        slide: dict[str, Any] = {
            "__typename": "GraphVideo" if is_video else "GraphImage",
            "id": f"{node_id}{index}",
            "is_video": is_video,
            "display_url": f"https://cdn.example/{shortcode}_{index}.jpg",
            "dimensions": {"width": 1080, "height": 1080},
            "owner": {"id": "25025320"},
            "taken_at_timestamp": 1785527000,
        }
        if is_video:
            slide["video_url"] = f"https://cdn.example/{shortcode}_{index}.mp4"
        slides.append({"node": slide})

    return {
        "__typename": "GraphSidecar",
        "id": node_id,
        "shortcode": shortcode,
        "display_url": f"https://cdn.example/{shortcode}_cover.jpg",
        "dimensions": {"width": 1080, "height": 1080},
        "taken_at_timestamp": 1785527000,
        "owner": {"id": "25025320", "username": "instagram"},
        "edge_media_to_caption": {"edges": [{"node": {"text": "swipe"}}]},
        "edge_sidecar_to_children": {"edges": slides},
    }


def timeline_payload(
    nodes: list[dict[str, Any]],
    *,
    has_next: bool = False,
    cursor: str | None = None,
    count: int = 8543,
) -> dict[str, Any]:
    """A ``/graphql/query/`` timeline response envelope."""
    return {
        "data": {
            "user": {
                "edge_owner_to_timeline_media": {
                    "count": count,
                    "page_info": {"has_next_page": has_next, "end_cursor": cursor},
                    "edges": [{"node": node} for node in nodes],
                }
            }
        }
    }


def profile_payload(*, user_id: str = "25025320", private: bool = False) -> dict[str, Any]:
    """A ``web_profile_info`` response envelope."""
    return {
        "data": {
            "user": {
                "id": user_id,
                "username": "instagram",
                "full_name": "Instagram",
                "biography": "Discover what's next",
                "is_private": private,
                "is_verified": True,
                "edge_followed_by": {"count": 690_000_000},
                "edge_follow": {"count": 100},
                "edge_owner_to_timeline_media": {"count": 8543},
                "profile_pic_url": "https://cdn.example/pp.jpg",
                "profile_pic_url_hd": "https://cdn.example/pp_hd.jpg",
            }
        }
    }


class FakeProvider:
    """Scripted :class:`TransportProvider` double.

    Args:
        responses: Returned in order; a :class:`BaseException` is raised
            instead of returned, which is how transport failures are scripted.
    """

    def __init__(self, responses: list[Any]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def request_json(
        self,
        method: str,
        url: str,
        *,
        params: Any = None,
        headers: Any = None,
        data: Any = None,
    ) -> Any:
        """Return (or raise) the next scripted response."""
        self.calls.append((method, url, dict(params or {})))
        if not self.responses:
            raise AssertionError(f"unexpected extra request: {method} {url}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response

    async def aclose(self) -> None:
        """No-op."""


@pytest.fixture(autouse=True)
def _deterministic_rich_output(monkeypatch: pytest.MonkeyPatch) -> None:
    """Render help text plainly, whatever the surrounding environment.

    rich enables colour when it detects CI, and typer styles option names, so
    ``--proxy`` stops being a contiguous substring of the rendered help — the
    assertions pass on a developer's machine and fail in GitHub Actions. Pin
    colour off and the width wide so what the tests read is what the code
    produced, not what the terminal happened to do to it.
    """
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("TERM", "dumb")
    monkeypatch.setenv("COLUMNS", "200")
    monkeypatch.delenv("FORCE_COLOR", raising=False)


@pytest.fixture(autouse=True)
def _no_leaked_log_sinks() -> Any:
    """Drop any sink a test installed via ``configure_logging``.

    loguru sinks outlive the test that added them, so a CLI test pointed at
    pytest's captured stderr keeps writing to that stream after pytest closes
    it, spraying ``I/O operation on closed file`` over later tests' output.
    """
    yield
    from instadata.utils.logging import logger

    logger.remove()


@pytest.fixture
def instant_sleep() -> Any:
    """Sleep replacement that records delays instead of waiting."""
    delays: list[float] = []

    async def sleep(seconds: float) -> None:
        delays.append(seconds)

    sleep.delays = delays  # type: ignore[attr-defined]
    return sleep
