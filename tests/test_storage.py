"""Tests for metadata storage, resume state, caching, cookies and helpers."""

from __future__ import annotations

from pathlib import Path

import orjson
import pytest
from conftest import carousel_node, image_node, video_node

from instadata.auth import FileCookieProvider, format_netscape_cookies
from instadata.cache import FileCache, JsonCache, MemoryCache
from instadata.errors import AuthenticationError, ConfigurationError
from instadata.extractors.graphql import parse_media_node
from instadata.storage import FileStateStore, JobState, JsonLinesMetadataStore
from instadata.utils.files import atomic_write_bytes, file_digest, sanitize_path_component
from instadata.utils.urls import extract_shortcode, normalize_username, url_extension


class TestMetadataStore:
    async def test_records_are_appended_as_jsonl(self, tmp_path: Path) -> None:
        store = JsonLinesMetadataStore(tmp_path / "metadata.jsonl", flush_every=1)
        await store.save(parse_media_node(image_node("1")))
        await store.save(parse_media_node(video_node("2")))
        await store.aclose()

        lines = (tmp_path / "metadata.jsonl").read_bytes().splitlines()
        assert len(lines) == 2
        assert orjson.loads(lines[0])["id"] == "1"

    async def test_every_required_field_is_serialised(self, tmp_path: Path) -> None:
        store = JsonLinesMetadataStore(tmp_path / "m.jsonl", flush_every=1)
        await store.save(parse_media_node(image_node(), username="instagram"))
        await store.aclose()

        record = orjson.loads((tmp_path / "m.jsonl").read_bytes().splitlines()[0])
        for field in (
            "id",
            "shortcode",
            "owner_id",
            "username",
            "caption",
            "hashtags",
            "mentions",
            "timestamp",
            "like_count",
            "comment_count",
            "dimensions",
            "duration",
            "location",
            "music",
            "thumbnail_url",
            "media_urls",
            "media_type",
        ):
            assert field in record, field

    async def test_duplicates_are_dropped(self, tmp_path: Path) -> None:
        store = JsonLinesMetadataStore(tmp_path / "m.jsonl", flush_every=1)
        await store.save(parse_media_node(image_node("1")))
        await store.save(parse_media_node(image_node("1")))
        await store.aclose()
        assert len((tmp_path / "m.jsonl").read_bytes().splitlines()) == 1

    async def test_existing_ids_are_indexed_across_runs(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        first = JsonLinesMetadataStore(path, flush_every=1)
        await first.save(parse_media_node(image_node("1")))
        await first.aclose()

        second = JsonLinesMetadataStore(path)
        assert await second.has("1") is True
        assert await second.has("999") is False

    async def test_buffer_is_flushed_on_close(self, tmp_path: Path) -> None:
        store = JsonLinesMetadataStore(tmp_path / "m.jsonl", flush_every=100)
        await store.save(parse_media_node(image_node()))
        assert not (tmp_path / "m.jsonl").exists()
        await store.aclose()
        assert (tmp_path / "m.jsonl").exists()

    async def test_torn_line_from_a_hard_kill_is_tolerated(self, tmp_path: Path) -> None:
        path = tmp_path / "m.jsonl"
        path.write_bytes(b'{"id": "1"}\n{"id": "2", "trunc')
        store = JsonLinesMetadataStore(path)
        assert await store.has("1") is True
        assert await store.has("2") is False


class TestStateStore:
    async def test_fresh_job_starts_empty(self, tmp_path: Path) -> None:
        state = await FileStateStore(tmp_path).load_state("nasa:posts")
        assert state.cursor is None
        assert state.pages_done == 0

    async def test_state_round_trips(self, tmp_path: Path) -> None:
        store = FileStateStore(tmp_path)
        state = JobState(job_key="nasa:posts", user_id="1", cursor="C1", pages_done=530)
        await store.save_state(state)

        loaded = await store.load_state("nasa:posts")
        assert loaded.cursor == "C1"
        assert loaded.pages_done == 530

    async def test_advanced_increments_progress(self) -> None:
        state = (
            JobState(job_key="k").advanced(cursor="C1", items=12).advanced(cursor="C2", items=12)
        )
        assert (state.pages_done, state.items_seen, state.cursor) == (2, 24, "C2")

    async def test_finished_marks_completion(self) -> None:
        assert JobState(job_key="k").finished().completed is True

    async def test_corrupt_state_file_is_discarded(self, tmp_path: Path) -> None:
        store = FileStateStore(tmp_path)
        await store.save_state(JobState(job_key="k", cursor="C"))
        next(tmp_path.glob("*.state.json")).write_bytes(b"{not json")
        assert (await store.load_state("k")).cursor is None

    async def test_clear_removes_state(self, tmp_path: Path) -> None:
        store = FileStateStore(tmp_path)
        await store.save_state(JobState(job_key="k", cursor="C"))
        await store.clear("k")
        assert (await store.load_state("k")).cursor is None


class TestCache:
    async def test_memory_cache_round_trip(self) -> None:
        cache = MemoryCache()
        await cache.set("k", b"v")
        assert await cache.get("k") == b"v"
        await cache.delete("k")
        assert await cache.get("k") is None

    async def test_memory_cache_honours_ttl(self) -> None:
        now = [0.0]
        cache = MemoryCache(clock=lambda: now[0])
        await cache.set("k", b"v", ttl=10)
        now[0] = 11
        assert await cache.get("k") is None

    async def test_file_cache_survives_a_new_instance(self, tmp_path: Path) -> None:
        await FileCache(tmp_path).set("user:nasa", b"528817151")
        assert await FileCache(tmp_path).get("user:nasa") == b"528817151"

    async def test_file_cache_key_is_path_safe(self, tmp_path: Path) -> None:
        await FileCache(tmp_path).set("../../etc/passwd", b"x")
        assert all(p.parent == tmp_path for p in tmp_path.iterdir())

    async def test_corrupt_cache_entry_is_discarded(self, tmp_path: Path) -> None:
        cache = FileCache(tmp_path, memory_front=False)
        await cache.set("k", b"v")
        next(tmp_path.glob("*.json")).write_bytes(b"{bad")
        assert await cache.get("k") is None

    async def test_json_cache_namespaces_keys(self, tmp_path: Path) -> None:
        backend = MemoryCache()
        profiles = JsonCache(backend, namespace="profile")
        cursors = JsonCache(backend, namespace="cursor")
        await profiles.set_json("nasa", {"user_id": "1"})
        assert await cursors.get_json("nasa") is None
        assert await profiles.get_json("nasa") == {"user_id": "1"}


class TestCookies:
    async def test_json_dict_format_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "cookies.json"
        path.write_bytes(orjson.dumps({"sessionid": "abc", "csrftoken": "xyz"}))
        assert (await FileCookieProvider(path).load())["sessionid"] == "abc"

    async def test_extension_list_format_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "cookies.json"
        path.write_bytes(orjson.dumps([{"name": "sessionid", "value": "abc", "domain": ".x"}]))
        assert (await FileCookieProvider(path).load()) == {"sessionid": "abc"}

    async def test_netscape_format_is_read(self, tmp_path: Path) -> None:
        path = tmp_path / "cookies.txt"
        path.write_text(format_netscape_cookies({"sessionid": "abc"}))
        assert (await FileCookieProvider(path).load()) == {"sessionid": "abc"}

    async def test_missing_optional_file_is_anonymous(self, tmp_path: Path) -> None:
        assert await FileCookieProvider(tmp_path / "nope.json").load() == {}

    async def test_missing_required_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError):
            await FileCookieProvider(tmp_path / "nope.json", required=True).load()

    async def test_required_file_without_session_raises(self, tmp_path: Path) -> None:
        path = tmp_path / "cookies.json"
        path.write_bytes(orjson.dumps({"csrftoken": "xyz"}))
        with pytest.raises(AuthenticationError):
            await FileCookieProvider(path, required=True).load()

    async def test_save_merges_instead_of_overwriting(self, tmp_path: Path) -> None:
        path = tmp_path / "cookies.json"
        path.write_bytes(orjson.dumps({"sessionid": "abc", "csrftoken": "old"}))
        provider = FileCookieProvider(path)
        await provider.load()
        await provider.save({"csrftoken": "new"})

        reloaded = await FileCookieProvider(path).load()
        assert reloaded == {"sessionid": "abc", "csrftoken": "new"}

    async def test_saved_cookie_file_is_not_group_or_world_readable(self, tmp_path: Path) -> None:
        # A session id is full account access, and the browser tier saves one
        # without the user explicitly asking.
        path = tmp_path / "cookies.json"
        await FileCookieProvider(path).save({"sessionid": "secret"})
        assert path.stat().st_mode & 0o077 == 0


class TestUtils:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("nasa", "nasa"),
            ("@NASA", "nasa"),
            ("https://www.instagram.com/nasa/", "nasa"),
            ("instagram.com/nasa", "nasa"),
        ],
    )
    def test_username_normalisation(self, value: str, expected: str) -> None:
        assert normalize_username(value) == expected

    def test_invalid_username_raises(self) -> None:
        with pytest.raises(ValueError):
            normalize_username("has spaces!")

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            ("https://www.instagram.com/p/DbbY9pdm6Q2/", "DbbY9pdm6Q2"),
            ("https://www.instagram.com/reel/DbbY9pdm6Q2/?igsh=x", "DbbY9pdm6Q2"),
            ("DbbY9pdm6Q2", "DbbY9pdm6Q2"),
            ("https://www.instagram.com/nasa/", None),
        ],
    )
    def test_shortcode_extraction(self, value: str, expected: str | None) -> None:
        assert extract_shortcode(value) == expected

    def test_url_extension_ignores_query_string(self) -> None:
        assert url_extension("https://cdn.example/a.mp4?_nc_ohc=abc&oe=123") == ".mp4"

    def test_unknown_extension_falls_back(self) -> None:
        assert url_extension("https://cdn.example/a.bin", default=".jpg") == ".jpg"

    def test_path_component_is_sanitised(self) -> None:
        assert "/" not in sanitize_path_component("a/b")
        assert sanitize_path_component("   ") == "_"

    async def test_atomic_write_leaves_no_temp_file(self, tmp_path: Path) -> None:
        target = tmp_path / "f.bin"
        await atomic_write_bytes(target, b"data")
        assert target.read_bytes() == b"data"
        assert list(tmp_path.iterdir()) == [target]

    async def test_file_digest_matches_hashlib(self, tmp_path: Path) -> None:
        import hashlib

        target = tmp_path / "f.bin"
        target.write_bytes(b"x" * 5000)
        assert await file_digest(target) == hashlib.sha256(b"x" * 5000).hexdigest()

    async def test_carousel_children_serialise_with_their_own_urls(self, tmp_path: Path) -> None:
        store = JsonLinesMetadataStore(tmp_path / "m.jsonl", flush_every=1)
        await store.save(parse_media_node(carousel_node(children=2)))
        await store.aclose()
        record = orjson.loads((tmp_path / "m.jsonl").read_bytes().splitlines()[0])
        assert len(record["children"]) == 2
        assert len(record["media_urls"]) >= 3


class TestShortcodeConversion:
    """Shortcode ↔ media id, verified against a live post on 2026-07-31."""

    def test_known_pair_converts_both_ways(self) -> None:
        from instadata.utils.shortcode import media_id_to_shortcode, shortcode_to_media_id

        assert shortcode_to_media_id("DbbY9pdm6Q2") == "3952862887472243766"
        assert media_id_to_shortcode("3952862887472243766") == "DbbY9pdm6Q2"

    def test_round_trip_is_stable(self) -> None:
        from instadata.utils.shortcode import media_id_to_shortcode, shortcode_to_media_id

        for code in ("Dbd9uTISQNu", "ABCDEFGHIJK", "A"):
            assert media_id_to_shortcode(shortcode_to_media_id(code)).endswith(code.lstrip("A"))

    def test_carousel_suffix_is_ignored(self) -> None:
        from instadata.utils.shortcode import shortcode_to_media_id

        assert shortcode_to_media_id("DbbY9pdm6Q2_extra") == shortcode_to_media_id("DbbY9pdm6Q2")

    def test_invalid_character_raises(self) -> None:
        from instadata.utils.shortcode import shortcode_to_media_id

        with pytest.raises(ValueError):
            shortcode_to_media_id("bad!code")

    def test_negative_media_id_raises(self) -> None:
        from instadata.utils.shortcode import media_id_to_shortcode

        with pytest.raises(ValueError):
            media_id_to_shortcode(-1)
