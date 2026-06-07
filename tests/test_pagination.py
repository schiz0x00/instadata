"""Tests for timeline pagination."""

from __future__ import annotations

import orjson
import pytest
from conftest import FakeProvider, carousel_node, image_node, timeline_payload, video_node

from bowerbird.errors import ParsingError
from bowerbird.pagination import TimelinePaginator, collect


def variables(call: tuple[str, str, dict]) -> dict:
    """Decode the GraphQL variables of a recorded call."""
    return orjson.loads(call[2]["variables"])


class TestFetchPage:
    async def test_request_shape_matches_the_live_endpoint(self) -> None:
        provider = FakeProvider([timeline_payload([image_node()])])
        paginator = TimelinePaginator(provider, page_size=12)
        await paginator.fetch_page("25025320")

        method, url, params = provider.calls[0]
        assert method == "GET"
        assert url.endswith("/graphql/query/")
        assert params["doc_id"] == "7950326061742207"
        assert variables(provider.calls[0]) == {
            "id": "25025320",
            "include_clips_attribution_info": True,
            "first": 12,
        }

    async def test_cursor_is_sent_as_after(self) -> None:
        provider = FakeProvider([timeline_payload([image_node()])])
        await TimelinePaginator(provider).fetch_page("1", cursor="CUR")
        assert variables(provider.calls[0])["after"] == "CUR"

    async def test_retired_doc_id_falls_back_when_one_is_configured(self) -> None:
        provider = FakeProvider([{"data": {}}, timeline_payload([image_node()])])
        paginator = TimelinePaginator(provider, fallback_doc_ids=("999",))
        page = await paginator.fetch_page("1")
        assert len(page) == 1
        assert paginator.active_doc_id == "999"

    async def test_all_doc_ids_failing_raises(self) -> None:
        provider = FakeProvider([{"data": {}}] * 3)
        paginator = TimelinePaginator(provider, fallback_doc_ids=("999",))
        with pytest.raises(ParsingError, match="no working timeline doc_id"):
            await paginator.fetch_page("1")

    async def test_no_fallback_configured_fails_on_the_primary(self) -> None:
        provider = FakeProvider([{"data": {}}])
        with pytest.raises(ParsingError):
            await TimelinePaginator(provider).fetch_page("1")


class TestIterPages:
    async def test_walks_until_has_next_page_is_false(self) -> None:
        provider = FakeProvider(
            [
                timeline_payload([image_node("1")], has_next=True, cursor="C1"),
                timeline_payload([image_node("2")], has_next=True, cursor="C2"),
                timeline_payload([image_node("3")], has_next=False),
            ]
        )
        pages = [p async for p in TimelinePaginator(provider).iter_pages("1")]
        assert len(pages) == 3
        assert [variables(c).get("after") for c in provider.calls] == [None, "C1", "C2"]

    async def test_resume_starts_from_the_given_cursor(self) -> None:
        provider = FakeProvider([timeline_payload([image_node()])])
        _ = [p async for p in TimelinePaginator(provider).iter_pages("1", cursor="RESUME")]
        assert variables(provider.calls[0])["after"] == "RESUME"

    async def test_max_pages_stops_early(self) -> None:
        provider = FakeProvider(
            [
                timeline_payload([image_node(str(i))], has_next=True, cursor=f"C{i}")
                for i in range(5)
            ]
        )
        pages = [p async for p in TimelinePaginator(provider).iter_pages("1", max_pages=2)]
        assert len(pages) == 2

    async def test_repeated_cursor_does_not_loop_forever(self) -> None:
        provider = FakeProvider(
            [
                timeline_payload([image_node("1")], has_next=True, cursor="SAME"),
                timeline_payload([image_node("2")], has_next=True, cursor="SAME"),
            ]
        )
        pages = [p async for p in TimelinePaginator(provider).iter_pages("1", cursor="SAME")]
        assert len(pages) == 1

    async def test_missing_cursor_terminates(self) -> None:
        provider = FakeProvider([timeline_payload([image_node()], has_next=True, cursor=None)])
        pages = [p async for p in TimelinePaginator(provider).iter_pages("1")]
        assert len(pages) == 1


class TestIterMedia:
    async def test_flattens_pages_into_items(self) -> None:
        provider = FakeProvider(
            [
                timeline_payload([image_node("1"), video_node("2")], has_next=True, cursor="C"),
                timeline_payload([carousel_node("3")]),
            ]
        )
        items = await collect(TimelinePaginator(provider).iter_media("1"))
        assert [m.id for m in items] == ["1", "2", "3"]

    async def test_max_items_stops_mid_page(self) -> None:
        provider = FakeProvider(
            [timeline_payload([image_node("1"), image_node("2"), image_node("3")])]
        )
        items = await collect(TimelinePaginator(provider).iter_media("1", max_items=2))
        assert len(items) == 2

    async def test_username_is_stamped_onto_items(self) -> None:
        provider = FakeProvider([timeline_payload([carousel_node()])])
        items = await collect(TimelinePaginator(provider).iter_media("1", username="instagram"))
        assert items[0].username == "instagram"
        assert all(child.username == "instagram" for child in items[0].children)

    async def test_lazy_iteration_does_not_prefetch(self) -> None:
        provider = FakeProvider(
            [
                timeline_payload([image_node("1")], has_next=True, cursor="C1"),
                timeline_payload([image_node("2")]),
            ]
        )
        iterator = TimelinePaginator(provider).iter_media("1")
        first = await anext(iterator)
        assert first.id == "1"
        assert len(provider.calls) == 1  # second page not requested yet
        await iterator.aclose()
