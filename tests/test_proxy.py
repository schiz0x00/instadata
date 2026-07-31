"""Proxy wiring: credential splitting and per-tier application."""

from __future__ import annotations

from typing import Any

import pytest

from instadata.browser.transport import BrowserTransport
from instadata.models.config import TransportConfig
from instadata.utils.urls import split_proxy_credentials, validate_proxy_url


class TestSplitProxyCredentials:
    def test_plain_proxy_has_no_credentials(self) -> None:
        assert split_proxy_credentials("http://host:8080") == ("http://host:8080", None, None)

    def test_credentials_are_lifted_out_of_the_url(self) -> None:
        assert split_proxy_credentials("http://usr:pwd@host:8080") == (
            "http://host:8080",
            "usr",
            "pwd",
        )

    def test_username_without_password(self) -> None:
        assert split_proxy_credentials("socks5://usr@host:1080") == (
            "socks5://host:1080",
            "usr",
            None,
        )

    def test_port_is_optional(self) -> None:
        assert split_proxy_credentials("http://usr:pwd@host") == ("http://host", "usr", "pwd")

    def test_scheme_is_preserved(self) -> None:
        server, _, _ = split_proxy_credentials("socks5h://u:p@host:1080")
        assert server == "socks5h://host:1080"


class TestValidateProxyUrl:
    def test_unsupported_scheme_names_the_supported_ones(self) -> None:
        with pytest.raises(ValueError, match="socks5"):
            validate_proxy_url("ftp://host:21")

    def test_missing_host_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="no host"):
            validate_proxy_url("http://")


class FakeBrowser:
    """Records the kwargs Playwright would have been launched with."""

    def __init__(self) -> None:
        self.kwargs: dict[str, Any] = {}

    async def launch(self, **kwargs: Any) -> FakeBrowser:
        self.kwargs = kwargs
        return self

    async def new_context(self) -> FakeContext:
        return FakeContext()


class FakeContext:
    async def new_page(self) -> FakePage:
        return FakePage()

    async def add_cookies(self, cookies: list[dict[str, str]]) -> None:
        pass

    async def cookies(self) -> list[dict[str, str]]:
        return []


class FakePage:
    def __init__(self) -> None:
        self.context = FakeContext()

    async def goto(self, url: str, **kwargs: Any) -> None:
        pass


class TestBrowserProxy:
    async def _launch_kwargs(self, proxy: str | None) -> dict[str, Any]:
        browser = FakeBrowser()
        transport = BrowserTransport(TransportConfig(proxy=proxy), launcher=browser.launch)
        await transport._ensure_page()
        return browser.kwargs

    async def test_credentials_become_separate_fields(self) -> None:
        # Chromium ignores credentials inside --proxy-server; embedding them
        # in `server` produced a 407 on every request.
        kwargs = await self._launch_kwargs("http://usr:pwd@host:8080")
        assert kwargs["proxy"] == {
            "server": "http://host:8080",
            "username": "usr",
            "password": "pwd",
        }

    async def test_plain_proxy_passes_only_the_server(self) -> None:
        kwargs = await self._launch_kwargs("socks5://host:1080")
        assert kwargs["proxy"] == {"server": "socks5://host:1080"}

    async def test_no_proxy_means_no_proxy_argument(self) -> None:
        assert "proxy" not in await self._launch_kwargs(None)
