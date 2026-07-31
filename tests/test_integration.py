"""End-to-end pipeline tests.

The whole stack runs — resolver, provider, paginator, downloader, stores — with
only the network faked at the httpx transport boundary. That is the layer worth
faking: everything above it is the code under test.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlparse

import httpx
import orjson
import pytest
from conftest import carousel_node, image_node, profile_payload, timeline_payload, video_node

from instadata.api.client import InstagramClient
from instadata.api.http_transport import HttpxTransport
from instadata.api.provider import EscalatingTransportProvider
from instadata.api.resolver import CachingUserResolver, WebProfileInfoStrategy
from instadata.cache import JsonCache, MemoryCache
from instadata.downloader import MediaDownloader
from instadata.errors import AuthenticationError, PrivateAccountError
from instadata.models.config import (
    DownloadConfig,
    RateLimitConfig,
    RetryConfig,
    ScraperConfig,
    StorageConfig,
    TransportTier,
)
from instadata.pagination import TimelinePaginator
from instadata.retry import NullRateLimiter, RetryPolicy
from instadata.scraper import InstagramScraper
from instadata.storage import FileStateStore

MEDIA_BODY = b"media-bytes" * 50


def fast_config(tmp_path: Path) -> ScraperConfig:
    """Configuration with no sleeping and everything under ``tmp_path``."""
    return ScraperConfig(
        retry=RetryConfig(max_attempts=2, initial_backoff=0.0001, jitter=0.0),
        rate_limit=RateLimitConfig(initial_delay=0.0, min_delay=0.0, jitter=0.0),
        download=DownloadConfig(workers=4, verify_checksum=False),
        storage=StorageConfig(output_dir=tmp_path / "out"),
        cache_dir=tmp_path / "cache",
    )


class FakeInstagram:
    """Scripted Instagram, served through an httpx ``MockTransport``.

    Args:
        pages: Timeline payloads returned in order.
        private: Report the account as private.
    """

    def __init__(self, pages: list[dict], *, private: bool = False) -> None:
        self.pages = list(pages)
        self.private = private
        self.requests: list[str] = []
        self.media_requests = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Route one request to its scripted response."""
        url = str(request.url)
        self.requests.append(url)

        if "web_profile_info" in url:
            return httpx.Response(200, json=profile_payload(private=self.private))
        if "/graphql/query/" in url:
            page = self.pages.pop(0) if self.pages else timeline_payload([])
            return httpx.Response(200, json=page)
        if url.startswith("https://cdn.example/"):
            self.media_requests += 1
            return httpx.Response(200, content=MEDIA_BODY)
        return httpx.Response(404, json={"message": "not found"})


class StubCookies:
    """Cookie provider returning a fixed jar."""

    def __init__(self, jar: dict[str, str]) -> None:
        self.jar = jar

    async def load(self) -> dict[str, str]:
        """Return the fixed jar."""
        return dict(self.jar)

    async def save(self, cookies: dict[str, str]) -> None:
        """Discard."""


class CursoredInstagram(FakeInstagram):
    """Timeline fake that honours the ``after`` cursor.

    :class:`FakeInstagram` replays a scripted list of pages and ignores the
    cursor entirely, which makes it blind to any bug about *which* page a run
    asks for. This one pages through a real post list, newest first, exactly
    as Instagram does.

    Args:
        posts: Post ids, newest first.
        page_size: Items per page.
    """

    def __init__(self, posts: list[str], *, page_size: int = 2) -> None:
        super().__init__([])
        self.posts = list(posts)
        self.page_size = page_size

    def handler(self, request: httpx.Request) -> httpx.Response:
        """Serve one cursor-aware page, or defer to the base fake."""
        url = str(request.url)
        if "/graphql/query/" not in url:
            return super().handler(request)

        self.requests.append(url)
        variables = orjson.loads(parse_qs(urlparse(url).query)["variables"][0])
        after = variables.get("after")
        start = self.posts.index(after) + 1 if after in self.posts else 0
        page = self.posts[start : start + self.page_size]
        return httpx.Response(
            200,
            json=timeline_payload(
                [image_node(post, post) for post in page],
                has_next=start + self.page_size < len(self.posts),
                cursor=page[-1] if page else None,
            ),
        )


def build_scraper(
    fake: FakeInstagram,
    tmp_path: Path,
    *,
    cookies: StubCookies | None = None,
    config: ScraperConfig | None = None,
) -> InstagramScraper:
    """Wire the real stack against a fake network."""
    config = config or fast_config(tmp_path)
    client_transport = HttpxTransport(
        config.transport,
        client=httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)),
    )
    provider = EscalatingTransportProvider(
        config,
        factory=lambda tier: client_transport if tier is TransportTier.ANONYMOUS else None,
        rate_limiter=NullRateLimiter(),
        retry_policy=RetryPolicy(config.retry, sleep=_no_sleep),
    )
    client = InstagramClient(
        provider=provider,
        resolver=CachingUserResolver(
            [WebProfileInfoStrategy(provider)],
            cache=JsonCache(MemoryCache(), namespace="profile"),
        ),
        paginator=TimelinePaginator(provider, page_size=config.page_size),
        config=config,
        cookies=cookies,
    )
    downloader = MediaDownloader(
        client_transport,
        config=config.download,
        retry_policy=RetryPolicy(config.retry, sleep=_no_sleep),
    )
    return InstagramScraper(client, downloader, config=config)


async def _no_sleep(seconds: float) -> None:
    """Sleep replacement for tests."""


class TestProfileScrape:
    async def test_full_pipeline_writes_media_and_metadata(self, tmp_path: Path) -> None:
        fake = FakeInstagram(
            [
                timeline_payload([image_node("1"), video_node("2")], has_next=True, cursor="C1"),
                timeline_payload([carousel_node("3", children=2)]),
            ]
        )
        report = await build_scraper(fake, tmp_path).scrape_profile("instagram")

        assert report.posts_seen == 3
        assert report.completed is True
        assert report.pages == 2

        output = tmp_path / "out" / "instagram"
        files = sorted(p.name for p in output.iterdir())
        assert "metadata.jsonl" in files
        # image + reel + two carousel slides. The carousel container itself is
        # not a file; its cover is slide one.
        assert sum(1 for name in files if name.endswith((".jpg", ".mp4"))) == 4

    async def test_metadata_records_every_post(self, tmp_path: Path) -> None:
        fake = FakeInstagram([timeline_payload([image_node("1"), video_node("2")])])
        await build_scraper(fake, tmp_path).scrape_profile("instagram")

        lines = (tmp_path / "out" / "instagram" / "metadata.jsonl").read_bytes().splitlines()
        records = [orjson.loads(line) for line in lines]
        assert {r["id"] for r in records} == {"1", "2"}
        assert all(r["username"] == "instagram" for r in records)

    async def test_video_posts_download_the_stream_not_the_poster(self, tmp_path: Path) -> None:
        fake = FakeInstagram([timeline_payload([video_node("2", "BBB")])])
        await build_scraper(fake, tmp_path).scrape_profile("instagram")
        files = list((tmp_path / "out" / "instagram").glob("*.mp4"))
        assert len(files) == 1

    async def test_limit_stops_early(self, tmp_path: Path) -> None:
        fake = FakeInstagram(
            [timeline_payload([image_node(str(i)) for i in range(12)], has_next=True, cursor="C")]
        )
        report = await build_scraper(fake, tmp_path).scrape_profile("instagram", limit=5)
        assert report.posts_seen == 5
        assert report.completed is False

    async def test_private_account_fails_before_downloading(self, tmp_path: Path) -> None:
        fake = FakeInstagram([timeline_payload([image_node()])], private=True)
        with pytest.raises(PrivateAccountError):
            await build_scraper(fake, tmp_path).scrape_profile("instagram")
        assert fake.media_requests == 0

    async def test_private_account_is_attempted_when_a_session_is_configured(
        self, tmp_path: Path
    ) -> None:
        # Passing cookies for an account that follows a private profile is the
        # whole point of --cookies; refusing anyway made the flag a no-op.
        fake = FakeInstagram([timeline_payload([image_node()])], private=True)
        scraper = build_scraper(fake, tmp_path, cookies=StubCookies({"sessionid": "abc"}))
        report = await scraper.scrape_profile("instagram")

        assert report.posts_seen == 1
        assert report.files_downloaded == 1

    async def test_private_account_with_a_sessionless_cookie_file_still_refuses(
        self, tmp_path: Path
    ) -> None:
        fake = FakeInstagram([timeline_payload([image_node()])], private=True)
        scraper = build_scraper(fake, tmp_path, cookies=StubCookies({"csrftoken": "x"}))
        with pytest.raises(PrivateAccountError):
            await scraper.scrape_profile("instagram")

    async def test_user_id_is_resolved_once_per_run(self, tmp_path: Path) -> None:
        fake = FakeInstagram(
            [
                timeline_payload([image_node("1")], has_next=True, cursor="C1"),
                timeline_payload([image_node("2")]),
            ]
        )
        await build_scraper(fake, tmp_path).scrape_profile("instagram")
        assert sum(1 for url in fake.requests if "web_profile_info" in url) == 1

    async def test_dead_media_does_not_abort_the_run(self, tmp_path: Path) -> None:
        fake = FakeInstagram([timeline_payload([image_node("1"), image_node("2", "GONE")])])

        original = fake.handler

        def handler(request: httpx.Request) -> httpx.Response:
            if "GONE" in str(request.url):
                return httpx.Response(403)
            return original(request)

        fake.handler = handler  # type: ignore[method-assign]
        report = await build_scraper(fake, tmp_path).scrape_profile("instagram")

        assert report.posts_seen == 2
        assert report.files_downloaded == 1
        assert len(report.failures) == 1


class TestResume:
    async def test_state_is_written_after_every_page(self, tmp_path: Path) -> None:
        fake = FakeInstagram(
            [
                timeline_payload([image_node("1")], has_next=True, cursor="C1"),
                timeline_payload([image_node("2")]),
            ]
        )
        await build_scraper(fake, tmp_path).scrape_profile("instagram")

        state = await FileStateStore(tmp_path / "out" / "instagram").load_state("instagram:posts")
        assert state.pages_done == 2
        assert state.completed is True

    async def test_interrupted_run_resumes_from_the_saved_cursor(self, tmp_path: Path) -> None:
        first = FakeInstagram(
            [timeline_payload([image_node("1")], has_next=True, cursor="CURSOR-1")]
        )
        await build_scraper(first, tmp_path).scrape_profile("instagram", limit=1)

        second = FakeInstagram([timeline_payload([image_node("2")])])
        report = await build_scraper(second, tmp_path).scrape_profile("instagram")

        graphql = [url for url in second.requests if "/graphql/query/" in url]
        assert "CURSOR-1" in graphql[0]
        assert report.resumed_from == "CURSOR-1"

    async def test_limit_stopping_mid_page_does_not_skip_the_rest_of_it(
        self, tmp_path: Path
    ) -> None:
        # Saving this page's end_cursor after a mid-page break would resume the
        # next run past the seven items it never looked at.
        page = lambda: [  # noqa: E731
            timeline_payload([image_node(str(i)) for i in range(12)], has_next=True, cursor="C")
        ]
        first = await build_scraper(FakeInstagram(page()), tmp_path).scrape_profile(
            "instagram", limit=5
        )
        assert first.posts_seen == 5

        state = await FileStateStore(tmp_path / "out" / "instagram").load_state("instagram:posts")
        assert state.cursor is None

        second = FakeInstagram(page())
        report = await build_scraper(second, tmp_path).scrape_profile("instagram")
        assert report.posts_seen == 12
        # No "after" variable at all: the resumed run restarts the same page.
        assert "after" not in next(url for url in second.requests if "graphql" in url)

    async def test_fully_consumed_page_still_advances_the_cursor(self, tmp_path: Path) -> None:
        fake = FakeInstagram(
            [timeline_payload([image_node("1")], has_next=True, cursor="CURSOR-1")]
        )
        await build_scraper(fake, tmp_path).scrape_profile("instagram", limit=1)

        state = await FileStateStore(tmp_path / "out" / "instagram").load_state("instagram:posts")
        assert state.cursor == "CURSOR-1"

    async def test_no_resume_starts_from_the_top(self, tmp_path: Path) -> None:
        first = FakeInstagram(
            [timeline_payload([image_node("1")], has_next=True, cursor="CURSOR-1")]
        )
        await build_scraper(first, tmp_path).scrape_profile("instagram", limit=1)

        second = FakeInstagram([timeline_payload([image_node("2")])])
        await build_scraper(second, tmp_path).scrape_profile("instagram", resume=False)
        assert "CURSOR-1" not in next(url for url in second.requests if "graphql" in url)

    async def test_completed_run_picks_up_posts_added_since(self, tmp_path: Path) -> None:
        # The finished cursor points at the oldest post; new posts arrive at
        # the newest end. Resuming from it asked for everything *after* the
        # last post and got nothing, so a re-run reported success having
        # fetched no new posts at all.
        await build_scraper(CursoredInstagram(["p3", "p2", "p1"]), tmp_path).scrape_profile(
            "instagram"
        )

        fake = CursoredInstagram(["p4", "p3", "p2", "p1"])
        report = await build_scraper(fake, tmp_path).scrape_profile("instagram")

        assert report.posts_seen == 4
        assert report.files_downloaded == 1
        # The three known posts are recognised from the metadata archive and
        # never reach the downloader, so they cost neither a stat nor a request.
        assert report.posts_skipped == 3
        assert fake.media_requests == 1  # only the new post costs a CDN pull

    async def test_completed_rerun_reports_no_resume_point(self, tmp_path: Path) -> None:
        fake = CursoredInstagram(["p2", "p1"])
        await build_scraper(fake, tmp_path).scrape_profile("instagram")
        report = await build_scraper(CursoredInstagram(["p2", "p1"]), tmp_path).scrape_profile(
            "instagram"
        )
        assert report.resumed_from is None

    async def test_interrupted_run_still_resumes_from_its_cursor(self, tmp_path: Path) -> None:
        # The completed-restart must not swallow the interrupted case, which is
        # what the cursor exists for.
        fake = CursoredInstagram(["p4", "p3", "p2", "p1"])
        first = await build_scraper(fake, tmp_path).scrape_profile("instagram", limit=2)
        assert first.completed is False

        second = CursoredInstagram(["p4", "p3", "p2", "p1"])
        report = await build_scraper(second, tmp_path).scrape_profile("instagram")
        assert report.resumed_from == "p3"

    async def test_update_run_stops_once_pages_have_nothing_new(self, tmp_path: Path) -> None:
        posts = [f"p{i}" for i in range(20, 0, -1)]
        await build_scraper(CursoredInstagram(posts), tmp_path).scrape_profile("instagram")

        fake = CursoredInstagram(["new", *posts])
        report = await build_scraper(fake, tmp_path).scrape_profile("instagram")

        # 2-post pages, stop_after_known_pages=2: page 1 has the new post,
        # pages 2 and 3 have nothing. Ten pages would be a full walk.
        assert report.files_downloaded == 1
        assert report.pages == 3
        assert sum("graphql" in url for url in fake.requests) == 3

    async def test_no_op_update_costs_only_the_stop_window(self, tmp_path: Path) -> None:
        posts = [f"p{i}" for i in range(20, 0, -1)]
        await build_scraper(CursoredInstagram(posts), tmp_path).scrape_profile("instagram")

        fake = CursoredInstagram(posts)
        report = await build_scraper(fake, tmp_path).scrape_profile("instagram")
        assert report.files_downloaded == 0
        assert report.pages == 2
        assert fake.media_requests == 0
        assert report.completed is True

    async def test_full_walks_the_whole_account(self, tmp_path: Path) -> None:
        posts = [f"p{i}" for i in range(20, 0, -1)]
        config = fast_config(tmp_path).model_copy(update={"stop_after_known_pages": 0})

        await build_scraper(CursoredInstagram(posts), tmp_path).scrape_profile("instagram")
        fake = CursoredInstagram(posts)
        report = await build_scraper(fake, tmp_path, config=config).scrape_profile("instagram")
        assert report.pages == 10

    async def test_a_partially_downloaded_carousel_is_retried_next_run(
        self, tmp_path: Path
    ) -> None:
        # The gap the early stop must not paper over: archiving a post that
        # lost one slide would mark it done and never fetch the slide again.
        fake = FakeInstagram([timeline_payload([carousel_node("3", children=2)])])
        original = fake.handler

        def handler(request: httpx.Request) -> httpx.Response:
            # The last slide of the fixture carousel is the video.
            if request.url.path.endswith(".mp4"):
                return httpx.Response(404)
            return original(request)

        fake.handler = handler  # type: ignore[method-assign]
        first = await build_scraper(fake, tmp_path).scrape_profile("instagram")
        assert first.files_downloaded == 1  # the image slide only
        assert first.failures  # recorded as incomplete

        records = tmp_path / "out" / "instagram" / "metadata.jsonl"
        assert not records.exists() or records.read_bytes() == b"", (
            "an incomplete post must not enter the archive"
        )

        second = FakeInstagram([timeline_payload([carousel_node("3", children=2)])])
        report = await build_scraper(second, tmp_path).scrape_profile("instagram")
        assert report.posts_skipped == 0, "post must be retried, not skipped"
        assert report.files_downloaded == 1  # the video slide that failed before
        assert report.files_skipped == 1  # the image slide already on disk

    async def test_rerun_skips_files_already_on_disk(self, tmp_path: Path) -> None:
        pages = lambda: [timeline_payload([image_node("1"), video_node("2")])]  # noqa: E731

        first = FakeInstagram(pages())
        await build_scraper(first, tmp_path).scrape_profile("instagram", resume=False)

        second = FakeInstagram(pages())
        report = await build_scraper(second, tmp_path).scrape_profile("instagram", resume=False)
        assert report.files_downloaded == 0
        assert report.posts_skipped == 2
        assert second.media_requests == 0

    async def test_metadata_is_not_duplicated_on_rerun(self, tmp_path: Path) -> None:
        for _ in range(2):
            fake = FakeInstagram([timeline_payload([image_node("1")])])
            await build_scraper(fake, tmp_path).scrape_profile("instagram", resume=False)

        lines = (tmp_path / "out" / "instagram" / "metadata.jsonl").read_bytes().splitlines()
        assert len(lines) == 1


class TestClientSurface:
    async def test_iter_posts_streams_lazily(self, tmp_path: Path) -> None:
        fake = FakeInstagram(
            [
                timeline_payload([image_node("1")], has_next=True, cursor="C1"),
                timeline_payload([image_node("2")]),
            ]
        )
        scraper = build_scraper(fake, tmp_path)
        client = scraper._client

        iterator = client.iter_posts("instagram")
        first = await anext(iterator)
        assert first.id == "1"
        assert sum(1 for url in fake.requests if "graphql" in url) == 1
        await iterator.aclose()

    async def test_post_falls_back_to_media_info_when_the_page_is_refused(
        self, tmp_path: Path
    ) -> None:
        # Instagram answers logged-out post pages with 403 as readily as with
        # an empty 200. Both mean "this route is done", not "give up".
        item = {
            "pk": "77",
            "code": "ABCDE",
            "taken_at": 1785528674,
            "media_type": 1,
            "user": {"pk": "25025320", "username": "instagram"},
            "image_versions2": {
                "candidates": [{"url": "https://cdn.example/x.jpg", "width": 10, "height": 10}]
            },
        }

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if "/p/ABCDE/" in url:
                return httpx.Response(403, content=b"login required")
            if "/media/" in url and "/info/" in url:
                return httpx.Response(200, json={"items": [item]})
            return httpx.Response(404)

        fake = FakeInstagram([])
        fake.handler = handler  # type: ignore[method-assign]
        client = build_scraper(fake, tmp_path, cookies=StubCookies({"sessionid": "abc"}))._client

        media = await client.get_post("https://www.instagram.com/p/ABCDE/")
        assert media.id == "77"

    async def test_post_without_a_session_reports_the_auth_requirement(
        self, tmp_path: Path
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(403, content=b"login required")

        fake = FakeInstagram([])
        fake.handler = handler  # type: ignore[method-assign]
        client = build_scraper(fake, tmp_path)._client

        with pytest.raises(AuthenticationError, match="needs a session"):
            await client.get_post("https://www.instagram.com/p/ABCDE/")

    async def test_profile_picture_is_downloadable(self, tmp_path: Path) -> None:
        fake = FakeInstagram([timeline_payload([])])
        report = await build_scraper(fake, tmp_path).scrape_profile(
            "instagram", include_profile_picture=True
        )
        assert report.files_downloaded == 1
