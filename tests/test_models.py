"""Unit tests for the domain models."""

from __future__ import annotations

from datetime import UTC, datetime, timezone
from pathlib import Path

import pytest
from pydantic import ValidationError

from bowerbird.models import (
    Dimensions,
    Media,
    MediaResource,
    MediaType,
    Page,
    PageInfo,
    Profile,
    ScraperConfig,
)
from bowerbird.models.config import TransportConfig

UTC_NOON = datetime(2026, 7, 31, 12, 0, 0, tzinfo=UTC)


def make_media(**overrides: object) -> Media:
    """Build a minimal valid Media, overriding selected fields."""
    defaults: dict[str, object] = {
        "id": "1",
        "shortcode": "ABC",
        "media_type": MediaType.IMAGE,
        "owner_id": "25025320",
        "timestamp": UTC_NOON,
        "resources": [MediaResource(url="https://cdn.example/a.jpg", width=1080, height=1080)],
    }
    return Media(**(defaults | overrides))  # type: ignore[arg-type]


class TestMediaDerivedFields:
    def test_hashtags_and_mentions_are_normalised(self) -> None:
        media = make_media(caption="Hello #Space #space @NASA and @nasa. @jpl")
        assert media.hashtags == ["space"]
        assert media.mentions == ["nasa", "jpl"]

    def test_caption_absent_yields_empty_tags(self) -> None:
        assert make_media(caption=None).hashtags == []
        assert make_media(caption=None).mentions == []

    def test_media_urls_include_children(self) -> None:
        child = make_media(id="2", resources=[MediaResource(url="https://cdn.example/b.jpg")])
        parent = make_media(media_type=MediaType.CAROUSEL, children=[child])
        assert media_urls(parent) == ["https://cdn.example/a.jpg", "https://cdn.example/b.jpg"]

    def test_best_resource_picks_highest_pixel_count(self) -> None:
        media = make_media(
            resources=[
                MediaResource(url="https://cdn.example/s.jpg", width=320, height=320),
                MediaResource(url="https://cdn.example/l.jpg", width=1080, height=1080),
            ]
        )
        assert str(media.best_resource.url) == "https://cdn.example/l.jpg"

    def test_best_resource_none_when_no_resources(self) -> None:
        assert make_media(resources=[]).best_resource is None

    def test_permalink_requires_shortcode(self) -> None:
        assert make_media().permalink == "https://www.instagram.com/p/ABC/"
        assert make_media(shortcode=None).permalink is None


class TestMediaTimestamps:
    def test_naive_timestamp_is_treated_as_utc(self) -> None:
        # Deliberately naive: the point is that the model attaches UTC.
        media = make_media(timestamp=datetime(2026, 7, 31, 12, 0, 0))  # noqa: DTZ001
        assert media.timestamp == UTC_NOON

    def test_aware_timestamp_is_converted_to_utc(self) -> None:
        from datetime import timedelta

        offset = timezone(timedelta(hours=2))
        media = make_media(timestamp=datetime(2026, 7, 31, 14, 0, 0, tzinfo=offset))
        assert media.timestamp == UTC_NOON


class TestMediaFilenames:
    def test_filename_is_deterministic_and_typed(self) -> None:
        assert make_media().filename() == "20260731_120000_ABC.jpg"
        assert make_media(media_type=MediaType.REEL).filename() == "20260731_120000_ABC.mp4"

    def test_carousel_children_get_indexed_names(self) -> None:
        assert make_media().filename(index=3) == "20260731_120000_ABC_03.jpg"

    def test_illegal_characters_are_replaced(self) -> None:
        assert "/" not in make_media(shortcode="a/b").filename()

    def test_filename_falls_back_to_id(self) -> None:
        assert make_media(shortcode=None).filename() == "20260731_120000_1.jpg"


class TestMediaTree:
    def test_flatten_is_depth_first_and_includes_self(self) -> None:
        grandchild = make_media(id="3")
        child = make_media(id="2", children=[grandchild])
        parent = make_media(id="1", media_type=MediaType.CAROUSEL, children=[child])
        assert [m.id for m in parent.flatten()] == ["1", "2", "3"]

    def test_with_username_propagates_to_children(self) -> None:
        parent = make_media(media_type=MediaType.CAROUSEL, children=[make_media(id="2")])
        tagged = parent.with_username("nasa")
        assert [m.username for m in tagged.flatten()] == ["nasa", "nasa"]

    def test_with_username_does_not_mutate_original(self) -> None:
        parent = make_media()
        parent.with_username("nasa")
        assert parent.username is None


class TestMediaType:
    @pytest.mark.parametrize(
        ("media_type", "is_video"),
        [
            (MediaType.IMAGE, False),
            (MediaType.VIDEO, True),
            (MediaType.REEL, True),
            (MediaType.CAROUSEL, False),
        ],
    )
    def test_is_video(self, media_type: MediaType, is_video: bool) -> None:
        assert media_type.is_video is is_video


class TestValidation:
    def test_dimensions_reject_non_positive(self) -> None:
        with pytest.raises(ValidationError):
            Dimensions(width=0, height=10)

    def test_media_requires_owner_and_timestamp(self) -> None:
        with pytest.raises(ValidationError):
            Media(id="1", media_type=MediaType.IMAGE)  # type: ignore[call-arg]

    def test_resource_rejects_non_url(self) -> None:
        with pytest.raises(ValidationError):
            MediaResource(url="not-a-url")


class TestSerialisation:
    def test_round_trip_preserves_tree(self) -> None:
        original = make_media(
            media_type=MediaType.CAROUSEL,
            caption="#a @b",
            children=[make_media(id="2")],
        )
        restored = Media.model_validate(original.model_dump(mode="json"))
        assert restored.id == original.id
        assert [c.id for c in restored.children] == ["2"]
        assert restored.hashtags == ["a"]

    def test_dump_includes_computed_fields(self) -> None:
        dumped = make_media(caption="#x").model_dump(mode="json")
        assert dumped["hashtags"] == ["x"]
        assert dumped["media_urls"] == ["https://cdn.example/a.jpg"]


class TestProfile:
    def test_profile_picture_media_is_downloadable(self) -> None:
        profile = Profile(
            user_id="25025320",
            username="instagram",
            profile_pic_url_hd="https://cdn.example/pp.jpg",
        )
        media = profile.profile_picture_media()
        assert media is not None
        assert media.media_type is MediaType.PROFILE_PICTURE
        assert media_urls(media) == ["https://cdn.example/pp.jpg"]

    def test_profile_picture_media_none_without_url(self) -> None:
        assert Profile(user_id="1", username="x").profile_picture_media() is None


class TestPage:
    def test_page_length_and_defaults(self) -> None:
        page: Page[Media] = Page(items=[make_media()], page_info=PageInfo(has_next_page=True))
        assert len(page) == 1
        assert page.page_info.end_cursor is None


class TestConfig:
    def test_defaults_are_sane(self) -> None:
        config = ScraperConfig()
        assert config.page_size == 12
        assert config.transport.tiers[0].value == "anonymous"

    def test_config_is_frozen(self) -> None:
        with pytest.raises(ValidationError):
            ScraperConfig().page_size = 30  # type: ignore[misc]

    def test_unknown_key_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ScraperConfig(nonsense=1)  # type: ignore[call-arg]

    @pytest.mark.parametrize(
        "proxy",
        [
            "http://host:8080",
            "https://host:8443",
            "socks5://host:1080",
            "socks5h://host:1080",
            "http://user:pass@host:8080",
        ],
    )
    def test_supported_proxy_schemes_are_accepted(self, proxy: str) -> None:
        assert TransportConfig(proxy=proxy).proxy == proxy

    @pytest.mark.parametrize(
        "proxy",
        # socks4 is rejected on purpose: httpx supports only socks5/socks5h,
        # so accepting it would crash tier 1 after passing validation.
        ["not-a-url", "ftp://host:21", "http://", "://host", "", "socks4://host:1080"],
    )
    def test_bad_proxy_is_rejected_at_construction(self, proxy: str) -> None:
        # ValidationError subclasses ValueError, which the CLI catches, so a
        # typo is one red line rather than an ImportError from inside httpx.
        with pytest.raises(ValidationError):
            TransportConfig(proxy=proxy)

    def test_default_output_directory_is_named_after_the_tool(self) -> None:
        assert ScraperConfig().storage.output_dir == Path("bowerbird")
        assert ScraperConfig().storage.profile_dir("nasa") == Path("bowerbird/nasa")

    def test_profile_dir_is_namespaced_by_username(self) -> None:
        assert ScraperConfig().storage.profile_dir("nasa").name == "nasa"

    def test_profile_dir_cannot_escape_the_output_directory(self) -> None:
        # A single post's owner handle comes straight from Instagram's payload
        # and never passes through normalize_username.
        storage = ScraperConfig().storage
        root = storage.output_dir.resolve()
        for hostile in ("../../../tmp/pwned", "..", "a/b", "/etc/passwd"):
            directory = storage.profile_dir(hostile)
            assert directory.parent == storage.output_dir
            # One component, and it resolves to a child of the output root.
            assert directory.resolve().parent == root


def media_urls(media: Media) -> list[str]:
    """Read the computed ``media_urls`` field as a plain list of strings."""
    return list(media.media_urls)
