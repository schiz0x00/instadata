"""Pipeline orchestration.

Ties the stages together — resolve, paginate, extract, download, store — and
owns the one thing none of them can own alone: progress. State is written
after every completed page, so an interrupted run resumes at the next page
rather than at the top of the account.

The orchestrator holds no network or parsing logic of its own. Everything it
does is delegation, which is what keeps it testable with fakes.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Self

from .api.client import InstagramClient
from .downloader.media import MediaDownloader
from .errors import MediaUnavailableError, ScraperError
from .models.config import RateLimitConfig, ScraperConfig
from .models.media import Media
from .models.profile import Profile
from .retry.rate_limiter import AdaptiveRateLimiter
from .storage.metadata import JsonLinesMetadataStore, NullMetadataStore
from .storage.state import FileStateStore, JobState, NullStateStore
from .utils.files import ensure_dir
from .utils.logging import logger
from .utils.urls import normalize_username

__all__ = ["InstagramScraper", "ProgressCallback", "ScrapeReport"]

#: Called after each item with ``(media, files_written)``.
ProgressCallback = Callable[[Media, int], None]


@dataclass(slots=True)
class ScrapeReport:
    """Outcome of one scrape run.

    Attributes:
        username: Account scraped.
        posts_seen: Top-level posts yielded by pagination.
        posts_skipped: Posts recognised as already complete from the metadata
            record, and therefore never handed to the downloader at all.
        files_downloaded: Files newly written to disk.
        files_skipped: Files already present and reused.
        bytes_downloaded: Bytes pulled over the network.
        pages: Pages of the timeline consumed this run.
        failures: One message per item that could not be downloaded.
        resumed_from: Cursor the run started at, when resuming.
        completed: Whether the timeline was walked to its end.
    """

    username: str
    posts_seen: int = 0
    posts_skipped: int = 0
    files_downloaded: int = 0
    files_skipped: int = 0
    bytes_downloaded: int = 0
    pages: int = 0
    failures: list[str] = field(default_factory=list)
    resumed_from: str | None = None
    completed: bool = False

    @property
    def files_total(self) -> int:
        """Files touched, downloaded or skipped."""
        return self.files_downloaded + self.files_skipped


class InstagramScraper:
    """Scrapes a profile end to end.

    Args:
        client: API surface. Owns the transport ladder.
        downloader: Writes media to disk.
        config: Root configuration.
        metadata_store: Where records go. ``None`` builds one per profile.
        state_store: Where resume state goes. ``None`` builds one per profile.
    """

    def __init__(
        self,
        client: InstagramClient,
        downloader: MediaDownloader,
        *,
        config: ScraperConfig | None = None,
        metadata_store: JsonLinesMetadataStore | NullMetadataStore | None = None,
        state_store: FileStateStore | NullStateStore | None = None,
    ) -> None:
        self._client = client
        self._downloader = downloader
        self._config = config or client.config
        self._metadata_store = metadata_store
        self._state_store = state_store

    @classmethod
    def build(cls, config: ScraperConfig | None = None) -> Self:
        """Wire a scraper from configuration, using the standard components.

        The downloader gets its own transport and its own pacing budget. The
        CDN is a different host from the API with a different quota, so
        spending the API's 1.5s-per-request budget on media would serialise
        thousands of files behind a limit that does not apply to them — but
        running wholly unpaced is how a bulk pull earns a CDN 429. So it gets
        an adaptive limiter that starts at full speed and only tightens if the
        CDN actually pushes back.
        """
        config = config or ScraperConfig()
        client = InstagramClient.build(config)
        from .api.http_transport import AnonymousTransport

        # CDN media needs no session, so it always rides the cheapest tier.
        downloader = MediaDownloader(
            AnonymousTransport(config.transport),
            config=config.download,
            rate_limiter=AdaptiveRateLimiter(
                RateLimitConfig(initial_delay=0.0, min_delay=0.0, max_delay=30.0)
            ),
        )
        return cls(client, downloader, config=config)

    # ---------------------------------------------------------------- public

    async def scrape_profile(
        self,
        username: str,
        *,
        limit: int | None = None,
        resume: bool = True,
        include_profile_picture: bool = False,
        on_progress: ProgressCallback | None = None,
    ) -> ScrapeReport:
        """Scrape a profile's posts to disk.

        Args:
            username: Handle, ``@handle`` or profile URL.
            limit: Stop after this many top-level posts.
            resume: Continue from saved state when present.
            include_profile_picture: Also download the avatar.
            on_progress: Called after each post with the file count written.

        Returns:
            A :class:`ScrapeReport` summarising the run.
        """
        username = normalize_username(username)
        profile = await self._client.get_profile(username)
        directory = ensure_dir(self._config.storage.profile_dir(username))

        metadata = self._metadata_store or self._build_metadata_store(directory)
        state_store = self._state_store or (
            FileStateStore(directory) if resume else NullStateStore()
        )

        job_key = f"{username}:posts"
        state = await state_store.load_state(job_key) if resume else JobState(job_key=job_key)

        if state.completed:
            # A finished timeline's cursor points at its oldest post, and new
            # posts arrive at the newest end. Resuming there asks Instagram for
            # everything after the last post and is answered with nothing, so a
            # re-run would silently report success having fetched no new posts.
            # Restart from the top instead: pages are re-requested, but every
            # file already on disk is skipped by name without a CDN request.
            logger.info("{} finished previously; re-walking for new posts", username)
            state = JobState(job_key=job_key)

        report = ScrapeReport(username=username, resumed_from=state.cursor)
        if state.cursor:
            logger.info("resuming {} from page {}", username, state.pages_done + 1)

        if include_profile_picture:
            await self._download_profile_picture(profile, directory, report)

        stop_after = self._config.stop_after_known_pages
        known_pages = 0

        try:
            async for page in self._client.iter_post_pages(username, cursor=state.cursor):
                report.pages += 1
                processed = 0
                fresh = 0
                for media in page.items:
                    if await self._already_done(media, metadata):
                        report.posts_seen += 1
                        report.posts_skipped += 1
                    else:
                        fresh += 1
                        await self._process(media, directory, metadata, report, on_progress)
                    processed += 1
                    if limit is not None and report.posts_seen >= limit:
                        break

                # Only advance past a page every item of which was handled.
                # Storing this page's end_cursor after a mid-page `--limit`
                # break would resume the next run *after* items it never
                # looked at, losing them silently and permanently. Re-reading
                # one page is cheap; the reprocessed items are already on disk
                # and get skipped.
                if processed == len(page.items):
                    state = state.advanced(cursor=page.page_info.end_cursor, items=processed)
                    await state_store.save_state(state)
                await metadata.flush()

                if limit is not None and report.posts_seen >= limit:
                    logger.info("reached limit of {} posts", limit)
                    break

                # The timeline is newest-first, so a page with nothing new on
                # it means everything below is already downloaded. Stopping
                # here is what keeps a routine "any new posts?" run to a couple
                # of requests instead of one per twelve posts in the account.
                known_pages = known_pages + 1 if fresh == 0 and page.items else 0
                if stop_after and known_pages >= stop_after:
                    logger.info(
                        "{}: {} pages with nothing new, stopping (rest is already downloaded)",
                        username,
                        known_pages,
                    )
                    report.completed = True
                    break
            else:
                report.completed = True
        finally:
            await metadata.aclose()

        if report.completed:
            await state_store.save_state(state.finished())
            logger.info(
                "finished {}: {} posts, {} new",
                username,
                report.posts_seen,
                report.posts_seen - report.posts_skipped,
            )
        return report

    async def scrape_post(
        self, url_or_shortcode: str, *, directory: Path | None = None
    ) -> ScrapeReport:
        """Download a single post, reel or carousel."""
        media = await self._client.get_post(url_or_shortcode)
        username = media.username or "unknown"
        target = ensure_dir(directory or self._config.storage.profile_dir(username))
        report = ScrapeReport(username=username)
        metadata = self._metadata_store or self._build_metadata_store(target)
        try:
            await self._process(media, target, metadata, report, None)
        finally:
            await metadata.aclose()
        report.completed = True
        return report

    async def scrape_stories(self, username: str, *, highlights: bool = False) -> ScrapeReport:
        """Download a user's stories, or every highlight reel.

        Both require a session; the client raises
        :class:`~bowerbird.errors.AuthenticationError` without one.
        """
        username = normalize_username(username)
        items = (
            await self._client.get_highlights(username)
            if highlights
            else await self._client.get_stories(username)
        )
        folder = "highlights" if highlights else "stories"
        directory = ensure_dir(self._config.storage.profile_dir(username) / folder)
        report = ScrapeReport(username=username)
        metadata = self._metadata_store or self._build_metadata_store(directory)
        try:
            for media in items:
                await self._process(media, directory, metadata, report, None)
        finally:
            await metadata.aclose()
        report.completed = True
        return report

    async def aclose(self) -> None:
        """Close the client's transports."""
        await self._client.aclose()

    async def __aenter__(self) -> Self:
        """Enter an async context, returning this scraper."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the scraper on context exit."""
        await self.aclose()

    # --------------------------------------------------------------- private

    def _build_metadata_store(self, directory: Path) -> JsonLinesMetadataStore | NullMetadataStore:
        """Build the default metadata store for an output directory."""
        if not self._config.storage.write_metadata:
            return NullMetadataStore()
        return JsonLinesMetadataStore(directory / self._config.storage.metadata_filename)

    async def _already_done(
        self,
        media: Media,
        metadata: JsonLinesMetadataStore | NullMetadataStore,
    ) -> bool:
        """Whether this post was fully downloaded by an earlier run.

        The metadata record is the archive: it is written only once every one
        of a post's files has landed, so its presence means there is no work
        left. Consulting it costs an in-memory set lookup and saves both the
        per-file ``stat`` calls and, via the caller's early stop, the timeline
        requests for everything below.

        Honours ``skip_existing``: turning that off means "redo the work", and
        that has to apply here too or the downloader would never be reached.
        """
        if not self._config.download.skip_existing:
            return False
        return await metadata.has(media.id)

    async def _process(
        self,
        media: Media,
        directory: Path,
        metadata: JsonLinesMetadataStore | NullMetadataStore,
        report: ScrapeReport,
        on_progress: ProgressCallback | None,
    ) -> None:
        """Download one post's files and record it.

        A single failed post never aborts the run: it is logged, counted and
        skipped. Losing 700 pages of progress to one dead CDN URL would be a
        far worse failure mode.
        """
        report.posts_seen += 1
        try:
            results = await self._downloader.download_media(media, directory)
        except (MediaUnavailableError, ScraperError) as exc:
            report.failures.append(f"{media.shortcode or media.id}: {exc}")
            logger.warning("skipping {}: {}", media.shortcode or media.id, exc)
            return

        for result in results:
            if result.skipped:
                report.files_skipped += 1
            else:
                report.files_downloaded += 1
                report.bytes_downloaded += result.bytes_written

        # Record only a post whose every file landed. A carousel that lost one
        # slide to an expired URL still returns the other nine, and archiving
        # it would mark the post done and never retry the missing slide.
        expected = len(media.downloadable())
        if len(results) < expected:
            report.failures.append(
                f"{media.shortcode or media.id}: {len(results)}/{expected} files, "
                "not recorded so the next run retries it"
            )
        else:
            await metadata.save(media)

        if on_progress is not None:
            on_progress(media, len(results))

    async def _download_profile_picture(
        self,
        profile: Profile,
        directory: Path,
        report: ScrapeReport,
    ) -> None:
        """Download the avatar, tolerating its absence."""
        media = profile.profile_picture_media()
        if media is None:
            return
        try:
            results = await self._downloader.download_media(media, directory)
        except ScraperError as exc:
            report.failures.append(f"profile picture: {exc}")
            return
        report.files_downloaded += sum(1 for r in results if not r.skipped)
        report.files_skipped += sum(1 for r in results if r.skipped)
