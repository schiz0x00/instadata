"""CLI tests.

The library is exercised elsewhere; these cover only what the CLI itself owns:
argument wiring, exit codes and rendering.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import orjson
import pytest
from typer.testing import CliRunner

from instadata.cli.app import app, build_config, render
from instadata.errors import PrivateAccountError
from instadata.models.config import TransportTier
from instadata.scraper import ScrapeReport

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def help_text(*args: str) -> str:
    """Rendered help with styling removed.

    rich colours option names when it detects a CI terminal, which splits
    ``--proxy`` with escape sequences and breaks a plain substring check. The
    tests care what the help says, not how the terminal painted it.
    """
    return _ANSI.sub("", runner.invoke(app, [*args, "--help"]).stdout)


class FakeScraper:
    """Stand-in for :class:`InstagramScraper` recording how it was called."""

    calls: list[tuple[str, dict[str, Any]]] = []
    error: Exception | None = None
    config: Any = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    @classmethod
    def build(cls, config: Any = None) -> FakeScraper:
        cls.config = config
        return cls()

    async def __aenter__(self) -> FakeScraper:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        pass

    async def scrape_profile(self, username: str, **kwargs: Any) -> ScrapeReport:
        FakeScraper.calls.append(("profile", {"username": username, **kwargs}))
        if FakeScraper.error:
            raise FakeScraper.error
        return ScrapeReport(username=username, posts_seen=3, files_downloaded=4, completed=True)

    async def scrape_post(self, url: str, **kwargs: Any) -> ScrapeReport:
        FakeScraper.calls.append(("post", {"url": url}))
        return ScrapeReport(username="instagram", posts_seen=1, files_downloaded=1, completed=True)

    async def scrape_stories(self, username: str, *, highlights: bool = False) -> ScrapeReport:
        FakeScraper.calls.append(("stories", {"username": username, "highlights": highlights}))
        return ScrapeReport(username=username, completed=True)


@pytest.fixture(autouse=True)
def fake_scraper(monkeypatch: pytest.MonkeyPatch) -> None:
    """Route every CLI command at the fake scraper."""
    import sys

    # Fetched from sys.modules: the package exports an ``app`` Typer instance,
    # so both the dotted string and ``from ... import app`` resolve to that
    # object rather than to the module.
    cli_module = sys.modules["instadata.cli.app"]

    FakeScraper.calls = []
    FakeScraper.error = None
    monkeypatch.setattr(cli_module, "InstagramScraper", FakeScraper)


class TestProfileCommand:
    def test_runs_and_reports(self) -> None:
        result = runner.invoke(app, ["profile", "instagram"])
        assert result.exit_code == 0
        assert FakeScraper.calls[0][1]["username"] == "instagram"

    def test_options_are_forwarded(self, tmp_path: Path) -> None:
        result = runner.invoke(
            app,
            [
                "profile",
                "nasa",
                "--limit",
                "5",
                "--no-resume",
                "--output",
                str(tmp_path),
                "--workers",
                "3",
                "--profile-picture",
            ],
        )
        assert result.exit_code == 0
        call = FakeScraper.calls[0][1]
        assert call["limit"] == 5
        assert call["resume"] is False
        assert call["include_profile_picture"] is True
        assert FakeScraper.config.download.workers == 3
        assert FakeScraper.config.storage.output_dir == tmp_path

    def test_json_output_is_machine_readable(self) -> None:
        result = runner.invoke(app, ["profile", "instagram", "--json"])
        assert result.exit_code == 0
        payload = orjson.loads(result.stdout)
        assert payload["posts_seen"] == 3
        assert payload["completed"] is True

    def test_scraper_error_exits_nonzero(self) -> None:
        FakeScraper.error = PrivateAccountError("nasa is private")
        result = runner.invoke(app, ["profile", "nasa"])
        assert result.exit_code == 1

    def test_invalid_username_exits_cleanly_without_a_traceback(self) -> None:
        # normalize_username raises ValueError, not ScraperError; the CLI has
        # to catch it or the user gets a traceback for a typo.
        FakeScraper.error = ValueError("invalid instagram username: 'bad!!name'")
        result = runner.invoke(app, ["profile", "nasa"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output

    def test_the_real_scraper_rejects_a_malformed_username_before_any_network_call(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        from instadata.scraper import InstagramScraper

        cli_module = sys.modules["instadata.cli.app"]
        monkeypatch.setattr(cli_module, "InstagramScraper", InstagramScraper)

        result = runner.invoke(app, ["profile", "bad!!name"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output

    def test_bad_proxy_exits_cleanly_without_a_traceback(self) -> None:
        # build_config runs in the command body, outside execute()'s guard, so
        # a config ValidationError needs catching where the config is built.
        result = runner.invoke(app, ["profile", "nasa", "--proxy", "ftp://host:21"])
        assert result.exit_code == 1
        assert "Traceback" not in result.output

    def test_proxy_reaches_the_transport_config(self) -> None:
        runner.invoke(app, ["profile", "nasa", "--proxy", "socks5://host:1080"])
        assert FakeScraper.config.transport.proxy == "socks5://host:1080"

    def test_no_browser_removes_the_browser_tier(self) -> None:
        runner.invoke(app, ["profile", "instagram", "--no-browser"])
        assert TransportTier.BROWSER not in FakeScraper.config.transport.tiers

    def test_cookies_path_is_wired(self, tmp_path: Path) -> None:
        cookies = tmp_path / "c.json"
        cookies.write_text("{}")
        runner.invoke(app, ["profile", "instagram", "--cookies", str(cookies)])
        assert FakeScraper.config.cookies_path == cookies


class TestOtherCommands:
    def test_post_command(self) -> None:
        result = runner.invoke(app, ["post", "https://www.instagram.com/p/ABC/"])
        assert result.exit_code == 0
        assert FakeScraper.calls[0][0] == "post"

    def test_reel_command_reuses_the_post_route(self) -> None:
        result = runner.invoke(app, ["reel", "https://www.instagram.com/reel/ABC/"])
        assert result.exit_code == 0
        assert FakeScraper.calls[0][0] == "post"

    def test_story_command(self) -> None:
        result = runner.invoke(app, ["story", "instagram"])
        assert result.exit_code == 0
        assert FakeScraper.calls[0][1]["highlights"] is False

    def test_highlights_command(self) -> None:
        result = runner.invoke(app, ["highlights", "instagram"])
        assert result.exit_code == 0
        assert FakeScraper.calls[0][1]["highlights"] is True

    def test_help_lists_every_command(self) -> None:
        out = help_text()
        for command in ("profile", "post", "reel", "story", "highlights", "whoami"):
            assert command in out

    def test_root_help_covers_capabilities_auth_and_examples(self) -> None:
        out = help_text()
        for expected in (
            "What it downloads",
            "Session requirements",
            "Behaviour worth knowing",
            "Examples",
            "--cookies",
            "Resumable",
        ):
            assert expected in out, expected

    def test_help_documents_incremental_behaviour_and_its_escape_hatches(self) -> None:
        # This help went stale once already: it still described the pre-archive
        # design after the walk had been made incremental.
        root = help_text()
        assert "Incremental" in root
        assert "metadata.jsonl" in root
        assert "--full" in root
        assert "--no-metadata" in root

        profile_help = help_text("profile")
        assert "--full" in profile_help
        assert "nothing new" in profile_help

    def test_no_metadata_flag_warns_that_it_disables_the_archive(self) -> None:
        # --no-metadata silently turns a 2-request re-check into a full walk;
        # that coupling has to be visible where the flag is documented.
        out = help_text("profile")
        assert "archive" in out

    def test_help_text_carries_no_restructuredtext_markup(self) -> None:
        # Command docstrings are RST for the API docs; the CLI help strings are
        # separate so ``literal`` markup never reaches a terminal.
        commands = ["profile", "post", "reel", "story", "highlights", "whoami"]
        for name in [None, *commands]:
            assert "``" not in help_text(*([] if name is None else [name])), name

    @pytest.mark.parametrize(
        "command", ["profile", "post", "reel", "story", "highlights", "whoami"]
    )
    def test_every_command_help_shows_examples(self, command: str) -> None:
        out = help_text(command)
        assert "Examples" in out
        assert "instadata " in out

    def test_whoami_accepts_a_proxy_like_every_other_command(self) -> None:
        # It was the one command that hardcoded proxy=None.
        assert "--proxy" in help_text("whoami")

    def test_version_flag_prints_the_version(self) -> None:
        from instadata import __version__

        result = runner.invoke(app, ["--version"])
        assert result.exit_code == 0
        assert __version__ in result.stdout


class TestConfigBuilding:
    def test_defaults_keep_every_tier(self) -> None:
        config = build_config(
            output=Path("data"), workers=8, proxy=None, cookies=None, metadata=True
        )
        assert len(config.transport.tiers) == 4
        assert config.storage.write_metadata is True

    def test_proxy_and_metadata_flags(self) -> None:
        config = build_config(
            output=Path("d"), workers=1, proxy="http://p:8080", cookies=None, metadata=False
        )
        assert config.transport.proxy == "http://p:8080"
        assert config.storage.write_metadata is False


class TestRender:
    def test_table_render_does_not_raise(self) -> None:
        render(ScrapeReport(username="x", failures=["a: boom"]), as_json=False)

    def test_json_render_includes_failures(self, capsys: pytest.CaptureFixture[str]) -> None:
        render(ScrapeReport(username="x", failures=["a: boom"]), as_json=True)
        assert "boom" in capsys.readouterr().out
