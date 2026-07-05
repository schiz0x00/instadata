"""Command line interface.

Thin by design: parse arguments, build a :class:`ScraperConfig`, run one
coroutine, render the report. All behaviour lives in the library, so anything
the CLI can do is equally available to an importing program.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, NoReturn

import anyio
import orjson
import typer
from pydantic import ValidationError
from rich.console import Console
from rich.table import Table

from .. import __version__
from ..errors import ScraperError
from ..models.config import (
    DEFAULT_OUTPUT_DIR,
    DownloadConfig,
    ScraperConfig,
    StorageConfig,
    TransportConfig,
    TransportTier,
)
from ..scraper import InstagramScraper, ScrapeReport
from ..utils.humanize import format_bytes
from ..utils.logging import configure_logging, logger

__all__ = ["app", "main"]

#: Root help body. Kept here rather than in a docstring because the command
#: docstrings are reStructuredText for the API docs, and their ``literal``
#: markup renders as stray backticks in a terminal.
_ROOT_HELP = """
Fast, resumable Instagram media scraper. HTTP first; a browser only if forced.

[bold]What it downloads[/bold]

  Posts, reels and carousels from a profile timeline, single posts by URL or
  shortcode, stories, highlight reels, and profile pictures. Each item is
  written as an individual file plus a JSON Lines metadata record.

[bold]Session requirements[/bold]

  [green]No cookies[/green]  public profile timelines, whoami
  [yellow]--cookies[/yellow]   stories, highlights, private accounts, single posts

  Credentials are never accepted. Export cookies from a browser you already
  logged in with, as JSON or Netscape cookies.txt.

[bold]Behaviour worth knowing[/bold]

  Resumable    progress saves after every page; Ctrl-C and rerun continues
  Incremental  re-running a finished profile fetches only what is new
  Adaptive     pacing tightens on throttling, relaxes after clean responses
  Escalating   anonymous HTTP, then Chrome TLS, then session, then Chromium

[bold]How "only what is new" works[/bold]

  metadata.jsonl doubles as the archive of finished posts. A record is written
  only once every file of that post has landed, so a post listed there is
  never re-requested. The timeline is newest-first, so the walk stops after a
  couple of pages with nothing new rather than reading the whole account.

  A post whose files did not all download is deliberately left out, so the
  next run retries it. Use [yellow]--full[/yellow] to walk the whole account and backfill
  older failures. [yellow]--no-metadata[/yellow] removes the archive and with it both of
  these: every run then re-walks the account in full.
"""

#: Each line is its own paragraph on purpose: typer rewraps consecutive lines
#: in an epilog into one block, so a blank line is what keeps them separate.
_ROOT_EPILOG = """
[bold]Examples[/bold]

[cyan]bowerbird profile nasa[/cyan]

[cyan]bowerbird profile nasa --limit 50 --output ./out --workers 16[/cyan]

[cyan]bowerbird post https://instagram.com/p/DbbY9pdm6Q2/ --cookies c.json[/cyan]

[cyan]bowerbird story nasa --cookies c.json[/cyan]

[cyan]bowerbird whoami nasa --json[/cyan]

Run [cyan]instagram COMMAND --help[/cyan] for a command's own options.
"""

app = typer.Typer(
    name="bowerbird",
    help=_ROOT_HELP,
    epilog=_ROOT_EPILOG,
    no_args_is_help=True,
    add_completion=False,
    rich_markup_mode="rich",
)
console = Console()


def _examples(*lines: str) -> str:
    """Build a command epilog listing one example per line.

    Entries are separated by blank lines because typer rewraps consecutive
    epilog lines into a single paragraph; the blank line is what keeps each
    example on its own row.
    """
    return "\n\n".join(["[bold]Examples[/bold]", *(f"[cyan]{line}[/cyan]" for line in lines)])


def _version(value: bool) -> None:
    """Print the version and exit, as an eager ``--version`` callback."""
    if value:
        console.print(__version__)
        raise typer.Exit()


@app.callback()
def _root(
    version: Annotated[
        bool,
        typer.Option("--version", callback=_version, is_eager=True, help="Print the version."),
    ] = False,
) -> None:
    """Root callback, present so ``--version`` has somewhere to live."""


# --------------------------------------------------------------------------- #
# Shared options
# --------------------------------------------------------------------------- #

OutputOption = Annotated[Path, typer.Option("--output", "-o", help="Output directory.")]
WorkersOption = Annotated[int, typer.Option("--workers", "-w", min=1, help="Concurrent downloads.")]
ProxyOption = Annotated[
    str | None,
    typer.Option(
        "--proxy",
        help=(
            "Proxy for every tier and for downloads. "
            "http/https/socks5/socks5h, credentials allowed: "
            "http://user:pass@host:8080"
        ),
    ),
]
CookiesOption = Annotated[
    Path | None,
    typer.Option(
        "--cookies",
        help=(
            "Cookie file for authenticated access. JSON or Netscape cookies.txt. "
            "Needed for stories, highlights, private accounts and single posts."
        ),
    ),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Print the report as JSON.")]
MetadataOption = Annotated[
    bool,
    typer.Option(
        "--metadata/--no-metadata",
        help=(
            "Write metadata.jsonl. It is also the archive of finished posts, "
            "so --no-metadata makes every run re-walk the whole account and "
            "re-check each post against the files on disk."
        ),
    ),
]
VerboseOption = Annotated[bool, typer.Option("--verbose", "-v", help="Debug logging.")]
FullOption = Annotated[
    bool,
    typer.Option(
        "--full",
        help=(
            "Walk the entire timeline instead of stopping once pages have "
            "nothing new. Use to backfill posts an earlier run failed on."
        ),
    ),
]
NoBrowserOption = Annotated[
    bool, typer.Option("--no-browser", help="Never escalate to Chromium; fail instead.")
]


def build_config(
    *,
    output: Path,
    workers: int,
    proxy: str | None,
    cookies: Path | None,
    metadata: bool,
    no_browser: bool = False,
    full: bool = False,
) -> ScraperConfig:
    """Assemble a :class:`ScraperConfig` from CLI options.

    Config validation runs here, before any command body does work, so an
    unusable ``--proxy`` or ``--workers`` fails on one line rather than as a
    traceback out of a command function. Every command routes through this.
    """
    tiers = tuple(
        tier
        for tier in TransportConfig().tiers
        if not (no_browser and tier is TransportTier.BROWSER)
    )
    try:
        return ScraperConfig(
            transport=TransportConfig(proxy=proxy, tiers=tiers),
            download=DownloadConfig(workers=workers),
            storage=StorageConfig(output_dir=output, write_metadata=metadata),
            cookies_path=cookies,
            stop_after_known_pages=0 if full else ScraperConfig().stop_after_known_pages,
        )
    except ValidationError as exc:
        _fail(exc)


def _fail(exc: Exception) -> NoReturn:
    """Log one line for an expected failure and exit non-zero.

    Pydantic's own rendering carries a traceback pointer and a docs URL that
    are noise for a CLI typo, so only the messages are kept.
    """
    if isinstance(exc, ValidationError):
        for error in exc.errors():
            logger.error("invalid {}: {}", ".".join(str(p) for p in error["loc"]), error["msg"])
    else:
        logger.error("{}: {}", type(exc).__name__, exc)
    raise typer.Exit(1) from exc


def execute(coro: Any) -> Any:
    """Run a coroutine, turning expected failures into clean exits.

    Exits with status 1 on a scraper error and 130 on Ctrl-C, so shell
    pipelines and cron jobs can react to failures.

    ``ValueError`` is caught alongside ``ScraperError`` because argument
    validation raises it: a malformed username or shortcode is a user mistake
    and deserves one red line, not a traceback.
    """
    try:
        return anyio.run(lambda: coro)
    except KeyboardInterrupt:  # pragma: no cover - interactive
        console.print("[yellow]interrupted; progress saved, rerun to resume[/yellow]")
        raise typer.Exit(130) from None
    except (ScraperError, ValueError) as exc:
        _fail(exc)


def run(coro: Any, *, as_json: bool) -> None:
    """Run a scrape coroutine and render its report."""
    render(execute(coro), as_json=as_json)


def render(report: ScrapeReport, *, as_json: bool) -> None:
    """Print a scrape report as JSON or a table."""
    if as_json:
        payload = {
            "username": report.username,
            "posts_seen": report.posts_seen,
            "posts_skipped": report.posts_skipped,
            "files_downloaded": report.files_downloaded,
            "files_skipped": report.files_skipped,
            "bytes_downloaded": report.bytes_downloaded,
            "pages": report.pages,
            "completed": report.completed,
            "failures": report.failures,
        }
        console.print_json(orjson.dumps(payload).decode())
        return

    table = Table(title=f"@{report.username}", show_header=False, box=None)
    table.add_row("posts", str(report.posts_seen))
    if report.posts_skipped:
        table.add_row("posts already done", str(report.posts_skipped))
    table.add_row("files downloaded", str(report.files_downloaded))
    table.add_row("files skipped", str(report.files_skipped))
    table.add_row("downloaded", format_bytes(report.bytes_downloaded))
    table.add_row("pages", str(report.pages))
    table.add_row("completed", "yes" if report.completed else "no (resumable)")
    if report.failures:
        table.add_row("failures", str(len(report.failures)))
    console.print(table)
    for failure in report.failures[:10]:
        console.print(f"  [red]![/red] {failure}")


# --------------------------------------------------------------------------- #
# Commands
# --------------------------------------------------------------------------- #


@app.command(
    help=(
        "Download a profile's posts, reels and carousels."
        "\n\nWalks the timeline newest-first. No session needed for a public "
        "account. An interrupted run continues from its saved cursor; a "
        "finished one re-walks from the top and stops once pages have nothing "
        "new, so repeat runs cost a couple of requests rather than one per "
        "twelve posts. Pass --full to walk the whole account anyway."
    ),
    epilog=_examples(
        "bowerbird profile nasa",
        "bowerbird profile nasa --limit 50 --profile-picture",
        "bowerbird profile nasa            # again later: fetches only new posts",
        "bowerbird profile nasa --full     # backfill posts an earlier run failed",
    ),
)
def profile(
    username: Annotated[str, typer.Argument(help="Username, @handle or profile URL.")],
    output: OutputOption = DEFAULT_OUTPUT_DIR,
    workers: WorkersOption = 8,
    limit: Annotated[int | None, typer.Option("--limit", "-n", help="Stop after N posts.")] = None,
    resume: Annotated[
        bool, typer.Option("--resume/--no-resume", help="Continue from saved state.")
    ] = True,
    profile_picture: Annotated[
        bool, typer.Option("--profile-picture", help="Also fetch the avatar.")
    ] = False,
    proxy: ProxyOption = None,
    cookies: CookiesOption = None,
    metadata: MetadataOption = True,
    as_json: JsonOption = False,
    full: FullOption = False,
    no_browser: NoBrowserOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Download a profile's posts, reels and carousels."""
    configure_logging(verbose=verbose, quiet=as_json)
    config = build_config(
        output=output,
        workers=workers,
        proxy=proxy,
        cookies=cookies,
        metadata=metadata,
        no_browser=no_browser,
        full=full,
    )

    async def job() -> ScrapeReport:
        async with InstagramScraper.build(config) as scraper:
            return await scraper.scrape_profile(
                username,
                limit=limit,
                resume=resume,
                include_profile_picture=profile_picture,
            )

    run(job(), as_json=as_json)


@app.command(
    help=(
        "Download a single post, reel or carousel by URL or shortcode."
        "\n\nNeeds --cookies: Instagram embeds no media JSON in logged-out "
        "post pages, so this falls back to an endpoint requiring a session."
    ),
    epilog=_examples(
        "bowerbird post https://instagram.com/p/DbbY9pdm6Q2/ --cookies c.json",
        "bowerbird post DbbY9pdm6Q2 --cookies c.json",
    ),
)
def post(
    url: Annotated[str, typer.Argument(help="Post/reel URL or shortcode.")],
    output: OutputOption = DEFAULT_OUTPUT_DIR,
    workers: WorkersOption = 8,
    proxy: ProxyOption = None,
    cookies: CookiesOption = None,
    metadata: MetadataOption = True,
    as_json: JsonOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Download a single post, reel or carousel."""
    configure_logging(verbose=verbose, quiet=as_json)
    config = build_config(
        output=output, workers=workers, proxy=proxy, cookies=cookies, metadata=metadata
    )

    async def job() -> ScrapeReport:
        async with InstagramScraper.build(config) as scraper:
            return await scraper.scrape_post(url)

    run(job(), as_json=as_json)


@app.command(
    help=(
        "Download a single reel. Alias of post; reels share its route."
        "\n\nProvided for discoverability only. Identical behaviour and "
        "options to post."
    ),
    epilog=_examples(
        "bowerbird reel https://instagram.com/reel/Dbd9uTISQNu/ --cookies c.json",
    ),
)
def reel(
    url: Annotated[str, typer.Argument(help="Reel URL or shortcode.")],
    output: OutputOption = DEFAULT_OUTPUT_DIR,
    workers: WorkersOption = 8,
    proxy: ProxyOption = None,
    cookies: CookiesOption = None,
    metadata: MetadataOption = True,
    as_json: JsonOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Download a single reel. Alias of ``post``; reels share its route."""
    post(
        url=url,
        output=output,
        workers=workers,
        proxy=proxy,
        cookies=cookies,
        metadata=metadata,
        as_json=as_json,
        verbose=verbose,
    )


@app.command(
    help=(
        "Download a user's active stories. Requires --cookies."
        "\n\nStories are served to logged-in clients only. "
        "Lands in <output>/<user>/stories/."
    ),
    epilog=_examples(
        "bowerbird story nasa --cookies c.json",
    ),
)
def story(
    username: Annotated[str, typer.Argument(help="Username whose stories to fetch.")],
    output: OutputOption = DEFAULT_OUTPUT_DIR,
    workers: WorkersOption = 8,
    cookies: CookiesOption = None,
    proxy: ProxyOption = None,
    metadata: MetadataOption = True,
    as_json: JsonOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Download a user's active stories. Requires ``--cookies``."""
    configure_logging(verbose=verbose, quiet=as_json)
    config = build_config(
        output=output, workers=workers, proxy=proxy, cookies=cookies, metadata=metadata
    )

    async def job() -> ScrapeReport:
        async with InstagramScraper.build(config) as scraper:
            return await scraper.scrape_stories(username, highlights=False)

    run(job(), as_json=as_json)


@app.command(
    help=(
        "Download every highlight reel for a user. Requires --cookies."
        "\n\nFetches the whole tray, batched. "
        "Lands in <output>/<user>/highlights/."
    ),
    epilog=_examples(
        "bowerbird highlights nasa --cookies c.json",
    ),
)
def highlights(
    username: Annotated[str, typer.Argument(help="Username whose highlights to fetch.")],
    output: OutputOption = DEFAULT_OUTPUT_DIR,
    workers: WorkersOption = 8,
    cookies: CookiesOption = None,
    proxy: ProxyOption = None,
    metadata: MetadataOption = True,
    as_json: JsonOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Download every highlight reel for a user. Requires ``--cookies``."""
    configure_logging(verbose=verbose, quiet=as_json)
    config = build_config(
        output=output, workers=workers, proxy=proxy, cookies=cookies, metadata=metadata
    )

    async def job() -> ScrapeReport:
        async with InstagramScraper.build(config) as scraper:
            return await scraper.scrape_stories(username, highlights=True)

    run(job(), as_json=as_json)


@app.command(
    help=(
        "Resolve a username to its numeric id and print the profile record."
        "\n\nOne request, no downloads. The cheapest way to check that a "
        "handle, proxy or cookie file works before a real run."
    ),
    epilog=_examples(
        "bowerbird whoami nasa",
        "bowerbird whoami nasa --json",
    ),
)
def whoami(
    username: Annotated[str, typer.Argument(help="Username to resolve.")],
    cookies: CookiesOption = None,
    proxy: ProxyOption = None,
    as_json: JsonOption = False,
    verbose: VerboseOption = False,
) -> None:
    """Resolve a username to its numeric id and print the profile record."""
    configure_logging(verbose=verbose, quiet=as_json)
    config = build_config(
        output=DEFAULT_OUTPUT_DIR, workers=1, proxy=proxy, cookies=cookies, metadata=False
    )

    async def job() -> dict[str, Any]:
        from ..api.client import InstagramClient

        async with InstagramClient.build(config) as client:
            return (await client.get_profile(username)).model_dump(mode="json")

    payload = execute(job())

    if as_json:
        console.print_json(orjson.dumps(payload).decode())
        return
    table = Table(show_header=False, box=None)
    for key in ("user_id", "username", "full_name", "is_private", "media_count", "follower_count"):
        table.add_row(key, str(payload.get(key)))
    console.print(table)


def main() -> None:
    """Console-script entry point."""
    app()
