"""Streaming media downloader.

Rules this module exists to enforce:

* Bytes go straight from socket to disk. A 4K reel is never held in memory.
* Writes land in a ``.part`` sibling and are renamed only once complete, so a
  killed process never leaves a half file that a resume would trust.
* Interrupted transfers resume with a ``Range`` request when the CDN allows it.
* Optional digest verification for callers that cannot tolerate silent
  corruption.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import anyio
import httpx

from ..api.base import classify_status
from ..errors import (
    DownloadError,
    MediaUnavailableError,
    NetworkError,
    RateLimitError,
    ScraperError,
)
from ..interfaces import RateLimiter
from ..models.config import DownloadConfig
from ..models.media import Media, MediaResource
from ..retry.policy import RetryPolicy
from ..retry.rate_limiter import NullRateLimiter
from ..utils.files import atomic_replace, ensure_dir, file_digest, temp_path_for
from ..utils.humanize import format_bytes, format_duration, format_rate
from ..utils.logging import logger
from ..utils.urls import is_expired_cdn_url, url_extension

__all__ = ["PROGRESS_INTERVAL", "DownloadResult", "MediaDownloader"]

#: Seconds between in-flight progress lines, per file. One line per file per
#: second stays readable with eight workers; per-chunk would not.
PROGRESS_INTERVAL = 1.0


class DownloadResult:
    """Outcome of one download.

    Attributes:
        path: Where the file landed.
        bytes_written: Bytes pulled over the network this attempt; ``0`` when
            the file was already present.
        skipped: Whether an existing complete file was reused.
        resumed: Whether the transfer continued a partial file.
        checksum: Digest of the finished file, when verification ran.
    """

    __slots__ = ("bytes_written", "checksum", "path", "resumed", "skipped")

    def __init__(
        self,
        path: Path,
        *,
        bytes_written: int = 0,
        skipped: bool = False,
        resumed: bool = False,
        checksum: str | None = None,
    ) -> None:
        self.path = path
        self.bytes_written = bytes_written
        self.skipped = skipped
        self.resumed = resumed
        self.checksum = checksum

    def __repr__(self) -> str:
        """Compact debug representation."""
        state = "skipped" if self.skipped else ("resumed" if self.resumed else "fresh")
        return f"<DownloadResult {self.path.name} {state} {self.bytes_written}B>"


class MediaDownloader:
    """Downloads media files with retries, resume and atomic writes.

    Args:
        transport: Anything exposing ``stream_response`` (httpx tier) or
            ``stream``. Media comes from the CDN and needs no session, so the
            cheapest transport is always the right one here.
        config: Worker count, chunk size, resume and verification switches.
        retry_policy: Applied per file.
        rate_limiter: Shared pacing. Pass the provider's limiter so CDN pulls
            spend the same budget as API calls; the worker semaphore caps
            concurrency but says nothing about rate. Defaults to no pacing,
            which is what a lone downloader with its own transport wants.
    """

    def __init__(
        self,
        transport: Any,
        *,
        config: DownloadConfig | None = None,
        retry_policy: RetryPolicy | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._transport = transport
        self._config = config or DownloadConfig()
        self._retry = retry_policy or RetryPolicy()
        self._rate_limiter = rate_limiter or NullRateLimiter()
        self._semaphore = anyio.Semaphore(self._config.workers)

    @property
    def config(self) -> DownloadConfig:
        """Downloader configuration."""
        return self._config

    async def download_media(self, media: Media, directory: Path) -> list[DownloadResult]:
        """Download every file belonging to ``media``, children included.

        Runs the whole tree concurrently under the shared worker semaphore, so
        a 10-slide carousel does not serialise behind itself.

        Only leaves become files; see :meth:`Media.downloadable`.

        Returns:
            One result per file that landed, in tree order. Shorter than
            ``media.downloadable()`` when some files failed, which is how the
            caller tells a partial post from a complete one.
        """
        items = media.downloadable()
        results: list[DownloadResult | None] = [None] * len(items)
        failures: list[BaseException] = []

        async def run(index: int, item: Media) -> None:
            slide = index + 1 if len(items) > 1 else None
            destination = directory / item.filename(index=slide)
            try:
                results[index] = await self.download(item, destination)
            except ScraperError as exc:
                # One dead slide must not cancel its siblings: a carousel with
                # nine live URLs and one expired signature is still worth nine
                # files. Only a wholly failed item is reported upward.
                failures.append(exc)
                logger.warning("slide {} of {} failed: {}", index + 1, media.id, exc)

        async with anyio.create_task_group() as group:
            for index, item in enumerate(items):
                group.start_soon(run, index, item)

        downloaded = [r for r in results if r is not None]
        if failures and not downloaded:
            raise failures[0]
        return downloaded

    async def download(self, media: Media, destination: Path) -> DownloadResult:
        """Download the best representation of one item to ``destination``.

        Raises:
            MediaUnavailableError: The item carries no usable URL, or the CDN
                refused every attempt.
            DownloadError: The bytes could not be written.
        """
        resource = media.best_resource
        if resource is None:
            raise MediaUnavailableError(f"media {media.id} has no downloadable resource")

        destination = _with_url_extension(destination, str(resource.url))

        if self._config.skip_existing and destination.exists() and destination.stat().st_size > 0:
            logger.debug("skipping existing {}", destination.name)
            return DownloadResult(destination, skipped=True)

        async with self._semaphore:
            return await self._retry.run(
                lambda: self._download_once(resource, destination),
                description=f"download {destination.name}",
            )

    async def _download_once(self, resource: MediaResource, destination: Path) -> DownloadResult:
        """One download attempt, resuming a partial file when possible."""
        url = str(resource.url)
        temp = temp_path_for(destination, self._config.temp_suffix)
        ensure_dir(destination.parent)

        offset = temp.stat().st_size if self._config.resume and temp.exists() else 0
        headers: dict[str, str] = {"Range": f"bytes={offset}-"} if offset else {}

        await self._rate_limiter.acquire()
        started = time.monotonic()
        try:
            written = await self._stream_to_file(url, temp, headers, offset)
        except MediaUnavailableError:
            temp.unlink(missing_ok=True)
            raise
        except RateLimitError as exc:
            self._rate_limiter.record_throttled(exc.retry_after)
            raise
        except (NetworkError, ScraperError):
            raise
        except OSError as exc:
            raise DownloadError(f"cannot write {temp}: {exc}") from exc
        self._rate_limiter.record_success()

        elapsed = max(time.monotonic() - started, 1e-9)
        checksum = await file_digest(temp) if self._config.verify_checksum else None
        atomic_replace(temp, destination)
        logger.info(
            "{} {} in {} ({}){}",
            destination.name,
            format_bytes(written),
            format_duration(elapsed),
            format_rate(written / elapsed),
            f", resumed from {format_bytes(offset)}" if offset > 0 else "",
        )
        return DownloadResult(
            destination,
            bytes_written=written,
            resumed=offset > 0,
            checksum=checksum,
        )

    async def _stream_to_file(
        self,
        url: str,
        temp: Path,
        headers: Mapping[str, str],
        offset: int,
    ) -> int:
        """Stream ``url`` into ``temp``, appending when resuming.

        Returns:
            Bytes written this attempt.

        Raises:
            MediaUnavailableError: The CDN signature expired or the media died.
            RateLimitError | HTTPStatusError: Any other non-success status,
                classified so the retry policy can tell a transient 429 or 503
                apart from a permanent failure.
        """
        stream_response = getattr(self._transport, "stream_response", None)
        if stream_response is None:
            return await self._stream_generic(url, temp, headers, offset)

        async with stream_response(url, headers=headers) as response:
            status = response.status_code
            if is_expired_cdn_url(status):
                raise MediaUnavailableError(f"CDN refused {url} with {status}; URL likely expired")
            if status == 416:
                # Partial file is already the whole file.
                return 0
            if status not in (200, 206):
                # Shared classification, so a CDN 429 or 503 raises the same
                # retryable error an API call would. Raising a bare
                # DownloadError here would make every one of them permanent.
                classify_status(status, url, b"", response.headers)
                raise DownloadError(f"unexpected status {status} for {url}")

            # A 200 answering a Range request means the CDN ignored it, so the
            # partial file is worthless and the transfer restarts from zero.
            append = offset > 0 and status == 206
            mode = "ab" if append else "wb"
            base = offset if append else 0
            progress = _Progress(_label(temp, self._config.temp_suffix), base, response.headers)

            written = 0
            try:
                async with await anyio.open_file(temp, mode) as handle:
                    async for chunk in response.aiter_bytes(self._config.chunk_size):
                        await handle.write(chunk)
                        written += len(chunk)
                        progress.advance(len(chunk))
            # ponytail: the one httpx import above api/. Translating this
            # inside HttpxTransport.stream_response would mean wrapping the
            # yielded response object; not worth it for a single except.
            except httpx.HTTPError as exc:
                raise NetworkError(f"stream interrupted for {url}: {exc}") from exc
            return written

    async def _stream_generic(
        self,
        url: str,
        temp: Path,
        headers: Mapping[str, str],
        offset: int,
    ) -> int:
        """Fallback path for transports exposing only a chunk iterator.

        Cannot inspect the status before the body starts, so it cannot resume;
        it restarts the file instead of appending to avoid corrupting it. For
        the same reason there is no total size to report progress against.
        """
        progress = _Progress(_label(temp, self._config.temp_suffix), 0, {})
        written = 0
        async with await anyio.open_file(temp, "wb") as handle:
            async for chunk in self._transport.stream(
                url, headers=dict(headers), chunk_size=self._config.chunk_size
            ):
                await handle.write(chunk)
                written += len(chunk)
                progress.advance(len(chunk))
        return written

    async def verify(self, path: Path, expected: str) -> bool:
        """Whether ``path`` matches an expected sha256 digest."""
        return await file_digest(path) == expected


class _Progress:
    """Throttled in-flight progress logging for one transfer.

    Emits at most one line per :data:`PROGRESS_INTERVAL`, which is what keeps
    eight concurrent workers from turning a scrape into a wall of text. Small
    files never emit at all: they finish before the first interval elapses,
    so the threshold is time rather than an explicit size cutoff.

    Args:
        label: Filename to show.
        base: Bytes already on disk, when resuming; progress counts from here.
        headers: Response headers, mined for the total size.
    """

    __slots__ = ("_base", "_done", "_label", "_last_log", "_started", "_total")

    def __init__(self, label: str, base: int, headers: Mapping[str, str]) -> None:
        self._label = label
        self._base = base
        self._total = _total_size(headers, base)
        self._done = base
        self._started = time.monotonic()
        self._last_log = self._started

    def advance(self, count: int) -> None:
        """Record ``count`` more bytes, logging if the interval has elapsed."""
        self._done += count
        now = time.monotonic()
        if now - self._last_log < PROGRESS_INTERVAL:
            return
        self._last_log = now

        rate = (self._done - self._base) / max(now - self._started, 1e-9)
        if self._total:
            logger.debug(
                "{} {}/{} ({:.0f}%) at {}",
                self._label,
                format_bytes(self._done),
                format_bytes(self._total),
                100.0 * self._done / self._total,
                format_rate(rate),
            )
        else:
            # No Content-Length: chunked or a transport that hides it.
            logger.debug("{} {} at {}", self._label, format_bytes(self._done), format_rate(rate))


def _total_size(headers: Mapping[str, str], base: int) -> int | None:
    """Full size of the resource, or ``None`` when the server does not say.

    ``Content-Range`` wins because on a 206 the ``Content-Length`` is only the
    remaining slice, which would report a resumed 90%-complete file as 100%
    of a much smaller whole.
    """
    lookup = {key.lower(): value for key, value in headers.items()}
    if content_range := lookup.get("content-range"):
        _, _, total = content_range.partition("/")
        if total.strip().isdigit():
            return int(total.strip())
    length = lookup.get("content-length")
    if length is not None and length.strip().isdigit():
        return base + int(length.strip())
    return None


def _label(temp: Path, suffix: str) -> str:
    """Final filename for a ``.part`` path, for log lines."""
    return temp.name.removesuffix(suffix)


def _with_url_extension(destination: Path, url: str) -> Path:
    """Correct the destination extension using the URL's own suffix.

    Instagram serves ``.webp`` and ``.heic`` alongside ``.jpg``; naming a webp
    file ``.jpg`` breaks downstream tooling for no reason.
    """
    extension = url_extension(url)
    if extension and destination.suffix.lower() != extension:
        return destination.with_suffix(extension)
    return destination
