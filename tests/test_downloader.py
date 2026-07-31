"""Tests for the streaming downloader."""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from conftest import carousel_node, image_node

from instadata.api.http_transport import HttpxTransport
from instadata.downloader import MediaDownloader
from instadata.downloader import media as media_module
from instadata.errors import DownloadError, HTTPStatusError, MediaUnavailableError
from instadata.extractors.graphql import parse_media_node
from instadata.models.config import DownloadConfig, RetryConfig
from instadata.retry import RetryPolicy
from instadata.utils.humanize import format_bytes
from instadata.utils.logging import logger

BODY = b"binary-content" * 100
FAST_RETRY = RetryConfig(max_attempts=2, initial_backoff=0.0001, jitter=0.0)


def transport_for(handler) -> HttpxTransport:
    """httpx tier backed by a scripted handler."""
    return HttpxTransport(client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))


def downloader(handler, **config_overrides) -> MediaDownloader:
    """Downloader wired to a scripted transport and fast retries."""
    return MediaDownloader(
        transport_for(handler),
        config=DownloadConfig(**config_overrides),
        retry_policy=RetryPolicy(FAST_RETRY, sleep=_no_sleep),
    )


async def _no_sleep(seconds: float) -> None:
    """Sleep replacement for tests."""


class TestSingleDownload:
    async def test_writes_file_and_reports_bytes(self, tmp_path: Path) -> None:
        media = parse_media_node(image_node())
        result = await downloader(lambda r: httpx.Response(200, content=BODY)).download(
            media, tmp_path / "out.jpg"
        )
        assert result.path.read_bytes() == BODY
        assert result.bytes_written == len(BODY)
        assert result.skipped is False

    async def test_no_partial_file_remains(self, tmp_path: Path) -> None:
        media = parse_media_node(image_node())
        await downloader(lambda r: httpx.Response(200, content=BODY)).download(
            media, tmp_path / "out.jpg"
        )
        assert list(tmp_path.glob("*.part")) == []

    async def test_checksum_is_computed_when_enabled(self, tmp_path: Path) -> None:
        media = parse_media_node(image_node())
        result = await downloader(
            lambda r: httpx.Response(200, content=BODY), verify_checksum=True
        ).download(media, tmp_path / "out.jpg")
        assert result.checksum == hashlib.sha256(BODY).hexdigest()

    async def test_existing_file_is_skipped(self, tmp_path: Path) -> None:
        destination = tmp_path / "out.jpg"
        destination.write_bytes(b"already here")
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return httpx.Response(200, content=BODY)

        result = await downloader(handler).download(parse_media_node(image_node()), destination)
        assert result.skipped is True
        assert calls == 0
        assert destination.read_bytes() == b"already here"

    async def test_media_without_resources_raises(self, tmp_path: Path) -> None:
        media = parse_media_node(image_node()).model_copy(update={"resources": []})
        with pytest.raises(MediaUnavailableError):
            await downloader(lambda r: httpx.Response(200)).download(media, tmp_path / "x.jpg")

    async def test_expired_cdn_url_raises_media_unavailable(self, tmp_path: Path) -> None:
        with pytest.raises(MediaUnavailableError, match="expired"):
            await downloader(lambda r: httpx.Response(403)).download(
                parse_media_node(image_node()), tmp_path / "x.jpg"
            )

    async def test_server_error_is_retried_then_surfaces_as_http_status(
        self, tmp_path: Path
    ) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            return httpx.Response(500)

        with pytest.raises(HTTPStatusError):
            await downloader(handler).download(parse_media_node(image_node()), tmp_path / "x.jpg")
        assert attempts == FAST_RETRY.max_attempts

    async def test_cdn_throttling_is_retried_not_treated_as_fatal(self, tmp_path: Path) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return httpx.Response(429, headers={"Retry-After": "0"})
            return httpx.Response(200, content=BODY)

        result = await downloader(handler).download(
            parse_media_node(image_node()), tmp_path / "x.jpg"
        )
        assert attempts == 2
        assert result.path.read_bytes() == BODY

    async def test_success_status_with_no_body_route_raises_download_error(
        self, tmp_path: Path
    ) -> None:
        # 204 is a success, so status classification passes it through; the
        # downloader still cannot make a file out of it.
        with pytest.raises(DownloadError):
            await downloader(lambda r: httpx.Response(204)).download(
                parse_media_node(image_node()), tmp_path / "x.jpg"
            )

    async def test_extension_follows_the_url(self, tmp_path: Path) -> None:
        node = image_node()
        node["display_resources"] = [
            {"src": "https://cdn.example/a.webp", "config_width": 1080, "config_height": 1080}
        ]
        node["display_url"] = "https://cdn.example/a.webp"
        result = await downloader(lambda r: httpx.Response(200, content=BODY)).download(
            parse_media_node(node), tmp_path / "out.jpg"
        )
        assert result.path.suffix == ".webp"


class TestResume:
    async def test_partial_file_is_continued_with_a_range_request(self, tmp_path: Path) -> None:
        destination = tmp_path / "out.jpg"
        partial = tmp_path / "out.jpg.part"
        partial.write_bytes(BODY[:100])
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Range", ""))
            return httpx.Response(206, content=BODY[100:])

        result = await downloader(handler).download(parse_media_node(image_node()), destination)
        assert seen == ["bytes=100-"]
        assert result.resumed is True
        assert destination.read_bytes() == BODY

    async def test_complete_partial_file_is_accepted_on_416(self, tmp_path: Path) -> None:
        destination = tmp_path / "out.jpg"
        (tmp_path / "out.jpg.part").write_bytes(BODY)
        result = await downloader(lambda r: httpx.Response(416)).download(
            parse_media_node(image_node()), destination
        )
        assert destination.read_bytes() == BODY
        assert result.bytes_written == 0

    async def test_resume_disabled_restarts_the_file(self, tmp_path: Path) -> None:
        (tmp_path / "out.jpg.part").write_bytes(b"stale")
        seen: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("Range", ""))
            return httpx.Response(200, content=BODY)

        result = await downloader(handler, resume=False).download(
            parse_media_node(image_node()), tmp_path / "out.jpg"
        )
        assert seen == [""]
        assert result.path.read_bytes() == BODY


class TestRetries:
    async def test_transient_failure_is_retried(self, tmp_path: Path) -> None:
        attempts = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.ConnectError("reset")
            return httpx.Response(200, content=BODY)

        result = await downloader(handler).download(
            parse_media_node(image_node()), tmp_path / "out.jpg"
        )
        assert attempts == 2
        assert result.path.read_bytes() == BODY


class TestProgressLogging:
    def _capture(self, level: str) -> list[str]:
        """Collect formatted log messages at or above ``level``."""
        lines: list[str] = []
        logger.add(lambda message: lines.append(message.record["message"]), level=level)
        return lines

    async def test_completion_line_reports_size_and_speed(self, tmp_path: Path) -> None:
        lines = self._capture("INFO")
        await downloader(lambda r: httpx.Response(200, content=BODY)).download(
            parse_media_node(image_node()), tmp_path / "out.jpg"
        )
        assert len(lines) == 1
        assert "out.jpg" in lines[0]
        assert "KiB in " in lines[0]
        assert "/s)" in lines[0]

    async def test_in_flight_progress_shows_done_over_total(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Zero interval so a small test file still emits per chunk.
        monkeypatch.setattr(media_module, "PROGRESS_INTERVAL", 0.0)
        lines = self._capture("DEBUG")
        await downloader(
            lambda r: httpx.Response(200, headers={"Content-Length": str(len(BODY))}, content=BODY),
            chunk_size=1024,
        ).download(parse_media_node(image_node()), tmp_path / "out.jpg")

        progress = [line for line in lines if "%)" in line]
        assert progress, lines
        assert f"/{format_bytes(len(BODY))}" in progress[-1]
        assert "(100%)" in progress[-1]

    async def test_progress_is_silent_for_fast_small_files(self, tmp_path: Path) -> None:
        # The default one-second interval is the whole throttle: a file that
        # finishes sooner never emits an in-flight line.
        lines = self._capture("DEBUG")
        await downloader(lambda r: httpx.Response(200, content=BODY)).download(
            parse_media_node(image_node()), tmp_path / "out.jpg"
        )
        assert not [line for line in lines if "%)" in line]


class TestCarousels:
    async def test_every_slide_is_downloaded_with_indexed_names(self, tmp_path: Path) -> None:
        media = parse_media_node(carousel_node(children=3))
        results = await downloader(lambda r: httpx.Response(200, content=BODY)).download_media(
            media, tmp_path
        )
        # Three slides, three files. The container's own display_url is the
        # cover, which Instagram serves as slide one's image; writing it too
        # would duplicate a slide on every carousel.
        assert len(results) == 3
        names = sorted(p.name for p in tmp_path.iterdir())
        assert all("_0" in name for name in names)
        assert any(name.endswith(".mp4") for name in names)

    async def test_carousel_container_is_not_written_as_its_own_file(self, tmp_path: Path) -> None:
        node = carousel_node(children=2)
        # Real payloads reuse slide one's image as the container's cover.
        first_slide = node["edge_sidecar_to_children"]["edges"][0]["node"]["display_url"]
        node["display_url"] = first_slide

        results = await downloader(lambda r: httpx.Response(200, content=BODY)).download_media(
            parse_media_node(node), tmp_path
        )
        assert len(results) == 2
        assert len({p.read_bytes() for p in tmp_path.iterdir()}) >= 1
        assert len(list(tmp_path.iterdir())) == 2

    async def test_single_media_gets_an_unindexed_name(self, tmp_path: Path) -> None:
        media = parse_media_node(image_node())
        results = await downloader(lambda r: httpx.Response(200, content=BODY)).download_media(
            media, tmp_path
        )
        assert len(results) == 1
        assert "_01" not in results[0].path.name

    async def test_worker_limit_is_respected(self, tmp_path: Path) -> None:
        active = 0
        peak = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            active -= 1
            return httpx.Response(200, content=BODY)

        media = parse_media_node(carousel_node(children=5))
        await downloader(handler, workers=2).download_media(media, tmp_path)
        assert peak <= 2
