"""Browser transport: tier 4, the recovery mechanism.

Launching Chromium costs hundreds of megabytes of RAM and seconds per page,
so this tier exists to unblock the cheap tiers, not to do their work. It runs
requests from inside a real page context via ``fetch``, which means Instagram
sees a genuine browser session — and, as a side effect, hands us fresh cookies
that the HTTP tiers can then reuse.

Chromium is imported lazily and launched on first use; a run that never
escalates never pays for it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from typing import Any, Self
from urllib.parse import urlencode

from ..api.base import SimpleResponse, classify_status
from ..api.endpoints import BASE_URL
from ..errors import NetworkError, TransportError
from ..interfaces import CookieProvider
from ..models.config import TransportConfig, TransportTier
from ..utils.logging import logger
from ..utils.urls import split_proxy_credentials

__all__ = ["BrowserTransport"]

_FETCH_SCRIPT = """
async ([url, method, headers, body]) => {
    const response = await fetch(url, {
        method,
        headers,
        body: body || undefined,
        credentials: 'include',
    });
    const text = await response.text();
    const out = {};
    response.headers.forEach((value, key) => { out[key] = value; });
    return { status: response.status, headers: out, body: text };
}
"""


class BrowserTransport:
    """Runs requests inside a stealth Chromium page.

    Args:
        config: Proxy and timeout settings; the user agent comes from the
            browser build, not from config.
        cookies: Optional provider. Cookies harvested from the browser are
            written back here so cheaper tiers can be retried with a valid
            session on the next run.
        headless: Run without a visible window.
        launcher: Injectable ``cloakbrowser.launch_async`` replacement, so
            tests never start a real browser.
    """

    def __init__(
        self,
        config: TransportConfig | None = None,
        *,
        cookies: CookieProvider | None = None,
        headless: bool = True,
        launcher: Any = None,
    ) -> None:
        self._config = config or TransportConfig()
        self._cookies = cookies
        self._headless = headless
        self._launcher = launcher
        self._browser: Any = None
        self._page: Any = None

    @property
    def tier(self) -> TransportTier:
        """Rung of the escalation ladder this transport occupies."""
        return TransportTier.BROWSER

    async def _ensure_page(self) -> Any:
        """Launch Chromium and open a warmed-up Instagram page, once."""
        if self._page is not None:
            return self._page

        launcher = self._launcher
        if launcher is None:
            from cloakbrowser import launch_async  # imported lazily: heavy

            launcher = launch_async

        logger.info("escalating to browser tier, launching chromium")
        kwargs: dict[str, Any] = {"headless": self._headless}
        if self._config.proxy:
            # Chromium's --proxy-server ignores credentials embedded in the
            # URL, so Playwright takes them as separate fields. Passing the
            # whole URL as `server` yields a 407 on every request.
            server, username, password = split_proxy_credentials(self._config.proxy)
            proxy: dict[str, str] = {"server": server}
            if username is not None:
                proxy["username"] = username
            if password is not None:
                proxy["password"] = password
            kwargs["proxy"] = proxy
        self._browser = await launcher(**kwargs)
        context = await self._browser.new_context()

        if self._cookies is not None and (jar := await self._cookies.load()):
            await context.add_cookies(
                [
                    {"name": name, "value": value, "domain": ".instagram.com", "path": "/"}
                    for name, value in jar.items()
                ]
            )

        self._page = await context.new_page()
        # Same-origin navigation first: fetch() from about:blank would be
        # cross-origin and would not carry Instagram's cookies.
        await self._page.goto(f"{BASE_URL}/", wait_until="domcontentloaded")
        await self._harvest_cookies()
        return self._page

    async def _harvest_cookies(self) -> None:
        """Persist the browser's cookies so cheaper tiers can reuse them."""
        if self._cookies is None or self._page is None:
            return
        try:
            jar = await self._page.context.cookies()
        except Exception as exc:  # pragma: no cover - browser teardown races
            logger.debug("cookie harvest failed: {}", exc)
            return
        harvested = {c["name"]: c["value"] for c in jar if "instagram" in c.get("domain", "")}
        if harvested:
            await self._cookies.save(harvested)
            logger.debug("harvested {} cookies from browser", len(harvested))

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, str] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, str] | None = None,
    ) -> SimpleResponse:
        """Perform a request from inside the page via ``fetch``.

        Raises:
            NetworkError: The browser could not run the request.
            AuthenticationError | RateLimitError | NotFoundError |
            HTTPStatusError: Per :func:`~instadata.api.base.classify_status`.
        """
        page = await self._ensure_page()
        full_url = f"{url}?{urlencode(dict(params))}" if params else url
        request_headers = {
            "X-IG-App-ID": self._config.app_id,
            "X-Requested-With": "XMLHttpRequest",
            **dict(headers or {}),
        }
        body = urlencode(dict(data)) if data else None
        if body is not None:
            request_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

        try:
            result = await page.evaluate(
                _FETCH_SCRIPT, [full_url, method.upper(), request_headers, body]
            )
        except Exception as exc:
            raise NetworkError(f"browser fetch failed for {full_url}: {exc}") from exc

        await self._harvest_cookies()
        content = str(result["body"]).encode("utf-8")
        response_headers = {str(k): str(v) for k, v in dict(result["headers"]).items()}
        logger.debug("[browser] {} {} -> {}", method, full_url, result["status"])
        classify_status(int(result["status"]), full_url, content, response_headers)
        return SimpleResponse(
            status_code=int(result["status"]),
            headers=response_headers,
            content=content,
            url=full_url,
            tier=self.tier,
        )

    async def stream(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        chunk_size: int = 65536,
    ) -> AsyncIterator[bytes]:
        """Not supported: media bytes must never route through the browser.

        A CDN download needs no session, so escalating one here would burn
        memory for nothing.

        Raises:
            TransportError: Always.
        """
        raise TransportError("browser tier does not stream media; use an HTTP tier")
        yield b""  # pragma: no cover - unreachable, keeps this an async generator

    async def aclose(self) -> None:
        """Close the page and the browser, if either was ever launched."""
        if self._browser is not None:
            await self._harvest_cookies()
            try:
                await self._browser.close()
            finally:
                self._browser = None
                self._page = None

    async def __aenter__(self) -> Self:
        """Enter an async context, returning this transport."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Close the browser on context exit."""
        await self.aclose()
