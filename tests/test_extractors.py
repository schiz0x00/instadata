"""Unit tests for the payload parsers."""

from __future__ import annotations

from datetime import UTC

import pytest
from conftest import carousel_node, image_node, profile_payload, timeline_payload, video_node

from instadata.errors import ParsingError
from instadata.extractors.graphql import (
    parse_media_node,
    parse_profile,
    parse_timeline_page,
)
from instadata.extractors.html import extract_json_blobs, extract_user_id
from instadata.extractors.v1 import parse_reels_tray, parse_v1_item
from instadata.models import MediaType


class TestParseMediaNode:
    def test_image_node_maps_every_field(self) -> None:
        media = parse_media_node(image_node())
        assert media.media_type is MediaType.IMAGE
        assert media.shortcode == "AAA"
        assert media.owner_id == "25025320"
        assert media.like_count == 42
        assert media.comment_count == 7
        assert media.hashtags == ["space"]
        assert media.mentions == ["nasa"]
        assert media.timestamp.tzinfo == UTC
        assert media.accessibility_caption == "Photo of a rocket"

    def test_image_resources_are_deduplicated_and_ranked(self) -> None:
        media = parse_media_node(image_node())
        assert len(media.resources) == 3
        assert media.best_resource is not None
        assert "1080" in str(media.best_resource.url)

    def test_clips_are_classified_as_reels(self) -> None:
        media = parse_media_node(video_node())
        assert media.media_type is MediaType.REEL
        assert media.view_count == 1234
        assert media.music is not None
        assert media.music.title == "Track"

    def test_video_url_is_the_first_resource(self) -> None:
        media = parse_media_node(video_node())
        assert str(media.resources[0].url).endswith(".mp4")

    def test_carousel_children_are_parsed_and_inherit_context(self) -> None:
        media = parse_media_node(carousel_node(children=3))
        assert media.media_type is MediaType.CAROUSEL
        assert len(media.children) == 3
        assert all(child.caption == "swipe" for child in media.children)
        assert all(child.timestamp == media.timestamp for child in media.children)

    def test_carousel_exposes_every_child_url(self) -> None:
        media = parse_media_node(carousel_node(children=3))
        urls = list(media.media_urls)
        assert any(url.endswith(".mp4") for url in urls)
        assert len(urls) >= 4

    def test_username_is_propagated_to_children(self) -> None:
        media = parse_media_node(carousel_node(), username="instagram")
        assert all(child.username == "instagram" for child in media.children)

    def test_missing_id_raises(self) -> None:
        node = image_node()
        del node["id"]
        with pytest.raises(ParsingError, match="id"):
            parse_media_node(node)

    def test_missing_timestamp_raises(self) -> None:
        node = image_node()
        del node["taken_at_timestamp"]
        with pytest.raises(ParsingError, match="timestamp"):
            parse_media_node(node)

    def test_missing_owner_raises(self) -> None:
        node = image_node()
        del node["owner"]
        with pytest.raises(ParsingError, match="owner"):
            parse_media_node(node)

    def test_unknown_typename_falls_back_to_is_video(self) -> None:
        node = image_node() | {"__typename": "XDTSomethingNew", "is_video": True}
        assert parse_media_node(node).media_type is MediaType.VIDEO


class TestParseTimelinePage:
    def test_page_carries_items_and_cursor(self) -> None:
        payload = timeline_payload([image_node(), video_node()], has_next=True, cursor="CUR")
        page = parse_timeline_page(payload, username="instagram")
        assert len(page) == 2
        assert page.page_info.has_next_page is True
        assert page.page_info.end_cursor == "CUR"
        assert page.total_count == 8543

    def test_last_page_reports_no_next(self) -> None:
        page = parse_timeline_page(timeline_payload([image_node()]))
        assert page.page_info.has_next_page is False
        assert page.page_info.end_cursor is None

    def test_empty_timeline_is_valid(self) -> None:
        assert len(parse_timeline_page(timeline_payload([]))) == 0

    def test_newer_envelope_shape_is_accepted(self) -> None:
        payload = {
            "data": {
                "xdt_api__v1__feed__user_timeline_graphql_connection": {
                    "edges": [{"node": image_node()}],
                    "page_info": {"has_next_page": False, "end_cursor": None},
                }
            }
        }
        assert len(parse_timeline_page(payload)) == 1

    def test_unknown_envelope_raises(self) -> None:
        with pytest.raises(ParsingError, match="timeline container"):
            parse_timeline_page({"data": {"nothing": {}}})


class TestParseProfile:
    def test_profile_fields_are_mapped(self) -> None:
        profile = parse_profile(profile_payload())
        assert profile.user_id == "25025320"
        assert profile.username == "instagram"
        assert profile.follower_count == 690_000_000
        assert profile.media_count == 8543
        assert profile.is_verified is True

    def test_private_flag_is_read(self) -> None:
        assert parse_profile(profile_payload(private=True)).is_private is True

    def test_missing_user_raises(self) -> None:
        with pytest.raises(ParsingError):
            parse_profile({"data": {}})


class TestV1Parsers:
    def _story_item(self) -> dict:
        return {
            "pk": "555",
            "code": "SSS",
            "taken_at": 1785528674,
            "media_type": 2,
            "user": {"pk": "25025320", "username": "instagram"},
            "image_versions2": {
                "candidates": [{"url": "https://cdn.example/s.jpg", "width": 1080, "height": 1920}]
            },
            "video_versions": [{"url": "https://cdn.example/s.mp4", "width": 720, "height": 1280}],
            "original_width": 1080,
            "original_height": 1920,
        }

    def test_story_item_is_typed_as_story(self) -> None:
        media = parse_v1_item(self._story_item(), is_story=True)
        assert media.media_type is MediaType.STORY
        assert str(media.resources[0].url).endswith(".mp4")
        assert media.dimensions is not None
        assert media.dimensions.height == 1920

    def test_reels_tray_is_flattened(self) -> None:
        payload = {"reels_media": [{"user": {"pk": "25025320"}, "items": [self._story_item()]}]}
        assert len(parse_reels_tray(payload)) == 1

    def test_reels_tray_filter_is_an_exact_id_match(self) -> None:
        # A prefix match would hand user 123 everything user 1234 posted.
        payload = {"reels_media": [{"user": {"pk": "1234"}, "items": [self._story_item()]}]}
        assert parse_reels_tray(payload, user_id="123") == []
        assert len(parse_reels_tray(payload, user_id="1234")) == 1

    def test_carousel_v1_children_are_parsed(self) -> None:
        item = self._story_item() | {"media_type": 8, "carousel_media": [self._story_item()]}
        media = parse_v1_item(item)
        assert media.media_type is MediaType.CAROUSEL
        assert len(media.children) == 1

    def test_missing_taken_at_raises(self) -> None:
        item = self._story_item()
        del item["taken_at"]
        with pytest.raises(ParsingError, match="taken_at"):
            parse_v1_item(item)


class TestHtmlExtractors:
    def test_user_id_from_profile_id_marker(self) -> None:
        assert extract_user_id('window.x = {"profile_id":"25025320"};') == "25025320"

    def test_user_id_from_owner_marker(self) -> None:
        assert extract_user_id('{"owner": {"id": "17841400000"}}') == "17841400000"

    def test_missing_user_id_raises(self) -> None:
        with pytest.raises(ParsingError):
            extract_user_id("<html>login required</html>")

    def test_json_blobs_are_filtered_by_needle(self) -> None:
        html = (
            '<script type="application/json">{"user": {"id": "1"}}</script>'
            '<script type="application/json">{"unrelated": true}</script>'
        )
        blobs = extract_json_blobs(html, needle='"user"')
        assert blobs == [{"user": {"id": "1"}}]

    def test_malformed_blob_is_skipped(self) -> None:
        html = '<script type="application/json">{not json}</script>'
        assert extract_json_blobs(html) == []
