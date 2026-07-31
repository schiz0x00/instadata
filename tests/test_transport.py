"""Tests for status classification, the httpx tier and tier escalation."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from instadata.api.base import check_graphql_errors, classify_status, parse_json
from instadata.api.http_transport import HttpxTransport
from instadata.api.provider import EscalatingTransportProvider
from instadata.errors import (
    AllTiersFailedError,
    AuthenticationError,
    HTTPStatusError,
    NetworkError,
    NotFoundError,
    ParsingError,
    PrivateAccountError,
    RateLimitError,
)
from instadata.models.config import (
    RateLimitConfig,
    RetryConfig,
    ScraperConfig,
    TransportTier,
)
from instadata.retry import NullRateLimiter, RetryPolicy

FAST = ScraperConfig(
    retry=RetryConfig(max_attempts=2, initial_backoff=0.0001, jitter=0.0),
    rate_limit=RateLimitConfig(initial_delay=0.0, min_delay=0.0, jitter=0.0),
)


class TestParseJson:
    def test_plain_json(self) -> None:
        assert parse_json(b'{"a": 1}') == {"a": 1}

    def test_anti_hijacking_prefix_is_stripped(self) -> None:
        assert parse_json(b'for (;;);{"fbid": null}') == {"fbid": None}

    def test_html_body_raises(self) -> None:
        with pytest.raises(ParsingError, match="not JSON"):
            parse_json(b"<html>login</html>")


class TestClassifyStatus:
    def test_success_is_silent(self) -> None:
        classify_status(200, "u", b"{}")

    def test_429_is_a_rate_limit_with_retry_after(self) -> None:
        with pytest.raises(RateLimitError) as info:
            classify_status(429, "u", b"", {"Retry-After": "30"})
        assert info.value.retry_after == 30.0

    def test_401_escalates(self) -> None:
        with pytest.raises(AuthenticationError):
            classify_status(401, "u", b"")

    def test_403_is_auth_by_default(self) -> None:
        with pytest.raises(AuthenticationError):
            classify_status(403, "u", b"forbidden")

    def test_403_mentioning_waiting_is_a_rate_limit(self) -> None:
        with pytest.raises(RateLimitError):
            classify_status(403, "u", b"Please wait a few minutes before you try again.")

    def test_403_containing_the_substring_rate_is_still_auth(self) -> None:
        # "corporate" contains "rate". Matching on that turns a credential
        # failure into a back-off that never escalates to the next tier.
        with pytest.raises(AuthenticationError):
            classify_status(403, "u", b"This corporate account is not accessible.")

    def test_404_is_not_found(self) -> None:
        with pytest.raises(NotFoundError):
            classify_status(404, "u", b"")

    def test_500_is_a_status_error(self) -> None:
        with pytest.raises(HTTPStatusError) as info:
            classify_status(500, "u", b"oops")
        assert info.value.status_code == 500


class TestGraphqlErrorChecks:
    def test_clean_payload_passes(self) -> None:
        check_graphql_errors({"data": {"user": {}}})

    def test_logged_out_error_becomes_auth_error(self) -> None:
        payload = {"errors": [{"message": "Unauthorized logged out query.", "code": 1675002}]}
        with pytest.raises(AuthenticationError):
            check_graphql_errors(payload)

    def test_private_message_becomes_private_error(self) -> None:
        with pytest.raises(PrivateAccountError):
            check_graphql_errors({"errors": [{"message": "This account is private"}]})

    def test_rate_limit_message_becomes_rate_limit_error(self) -> None:
        with pytest.raises(RateLimitError):
            check_graphql_errors({"errors": [{"message": "Please wait a few minutes"}]})

    def test_status_fail_envelope_is_handled(self) -> None:
        with pytest.raises(AuthenticationError):
            check_graphql_errors({"status": "fail", "message": "login_required"})

    def test_unknown_error_becomes_parsing_error(self) -> None:
        with pytest.raises(ParsingError):
            check_graphql_errors({"errors": [{"message": "something new"}]})


def mock_transport(handler) -> HttpxTransport:
    """Build an httpx tier backed by a scripted handler."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return HttpxTransport(client=client)


class TestHttpxTransport:
    async def test_successful_request_returns_parsed_json(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.headers["X-IG-App-ID"]
            return httpx.Response(200, json={"data": {"ok": True}})

        transport = mock_transport(handler)
        response = await transport.request("GET", "https://www.instagram.com/graphql/query/")
        assert response.json() == {"data": {"ok": True}}
        assert response.tier is TransportTier.ANONYMOUS
        await transport.aclose()

    async def test_connection_failure_becomes_network_error(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        transport = mock_transport(handler)
        with pytest.raises(NetworkError):
            await transport.request("GET", "https://www.instagram.com/")
        await transport.aclose()

    async def test_status_is_classified(self) -> None:
        transport = mock_transport(lambda request: httpx.Response(429))
        with pytest.raises(RateLimitError):
            await transport.request("GET", "https://www.instagram.com/")
        await transport.aclose()

    async def test_stream_yields_chunks(self) -> None:
        body = b"x" * 5000
        transport = mock_transport(lambda request: httpx.Response(200, content=body))
        chunks = [c async for c in transport.stream("https://cdn.example/a.jpg", chunk_size=1024)]
        assert b"".join(chunks) == body
        await transport.aclose()


class FakeTransport:
    """Transport double that replays a scripted sequence per tier."""

    def __init__(self, tier: TransportTier, responses: list[object]) -> None:
        self._tier = tier
        self.responses = list(responses)
        self.calls = 0

    @property
    def tier(self) -> TransportTier:
        return self._tier

    async def request(self, method, url, *, params=None, headers=None, data=None):
        self.calls += 1
        response = self.responses.pop(0) if self.responses else self.responses
        if isinstance(response, BaseException):
            raise response
        return _Payload(response)

    async def stream(self, url, *, headers=None, chunk_size=65536):
        yield b""

    async def aclose(self) -> None:
        pass


class _Payload:
    """Minimal Response stand-in."""

    def __init__(self, payload: object) -> None:
        self._payload = payload
        self.status_code = 200
        self.headers: dict[str, str] = {}
        self.content = b"{}"

    def json(self) -> object:
        return self._payload


def provider_with(tiers: dict[TransportTier, FakeTransport]) -> EscalatingTransportProvider:
    """Build a provider whose factory serves the given doubles."""
    return EscalatingTransportProvider(
        FAST,
        factory=lambda tier: tiers.get(tier),
        rate_limiter=NullRateLimiter(),
        retry_policy=RetryPolicy(FAST.retry, sleep=_no_sleep),
    )


async def _no_sleep(seconds: float) -> None:
    """Sleep replacement used by provider tests."""


class TestDefaultFactory:
    def test_authenticated_tier_is_skipped_without_a_cookie_file(self) -> None:
        # A null provider carries no session, so this tier would be a
        # byte-for-byte repeat of tier 2 costing an extra round trip.
        from instadata.api.provider import default_transport_factory
        from instadata.auth import NullCookieProvider

        factory = default_transport_factory(ScraperConfig(), NullCookieProvider())
        assert factory(TransportTier.AUTHENTICATED) is None

    def test_authenticated_tier_is_built_when_cookies_are_configured(self, tmp_path: Path) -> None:
        from instadata.api.provider import default_transport_factory
        from instadata.auth import FileCookieProvider

        path = tmp_path / "cookies.json"
        config = ScraperConfig(cookies_path=path)
        factory = default_transport_factory(config, FileCookieProvider(path))
        assert factory(TransportTier.AUTHENTICATED) is not None


class TestEscalation:
    async def test_cheapest_tier_wins_and_nothing_else_is_built(self) -> None:
        anonymous = FakeTransport(TransportTier.ANONYMOUS, [{"data": 1}])
        impersonated = FakeTransport(TransportTier.IMPERSONATED, [{"data": 2}])
        provider = provider_with(
            {TransportTier.ANONYMOUS: anonymous, TransportTier.IMPERSONATED: impersonated}
        )
        assert await provider.request_json("GET", "https://x") == {"data": 1}
        assert impersonated.calls == 0
        assert provider.current_tier is TransportTier.ANONYMOUS

    async def test_auth_error_escalates_to_the_next_tier(self) -> None:
        anonymous = FakeTransport(TransportTier.ANONYMOUS, [AuthenticationError("logged out")] * 2)
        impersonated = FakeTransport(TransportTier.IMPERSONATED, [{"data": 2}])
        provider = provider_with(
            {TransportTier.ANONYMOUS: anonymous, TransportTier.IMPERSONATED: impersonated}
        )
        assert await provider.request_json("GET", "https://x") == {"data": 2}
        assert provider.current_tier is TransportTier.IMPERSONATED

    async def test_provider_locks_onto_the_working_tier(self) -> None:
        anonymous = FakeTransport(TransportTier.ANONYMOUS, [AuthenticationError("nope")])
        impersonated = FakeTransport(TransportTier.IMPERSONATED, [{"data": 1}, {"data": 2}])
        provider = provider_with(
            {TransportTier.ANONYMOUS: anonymous, TransportTier.IMPERSONATED: impersonated}
        )
        await provider.request_json("GET", "https://x")
        await provider.request_json("GET", "https://x")
        assert anonymous.calls == 1  # not retried after escalation
        assert impersonated.calls == 2

    async def test_in_band_logged_out_error_also_escalates(self) -> None:
        logged_out = {"errors": [{"message": "Unauthorized logged out query."}]}
        anonymous = FakeTransport(TransportTier.ANONYMOUS, [logged_out])
        impersonated = FakeTransport(TransportTier.IMPERSONATED, [{"data": "ok"}])
        provider = provider_with(
            {TransportTier.ANONYMOUS: anonymous, TransportTier.IMPERSONATED: impersonated}
        )
        assert await provider.request_json("GET", "https://x") == {"data": "ok"}

    async def test_all_tiers_failing_raises_with_detail(self) -> None:
        provider = provider_with(
            {
                TransportTier.ANONYMOUS: FakeTransport(
                    TransportTier.ANONYMOUS, [AuthenticationError("a")] * 2
                ),
                TransportTier.IMPERSONATED: FakeTransport(
                    TransportTier.IMPERSONATED, [AuthenticationError("b")] * 2
                ),
            }
        )
        with pytest.raises(AllTiersFailedError) as info:
            await provider.request_json("GET", "https://x")
        assert set(info.value.failures) == {"anonymous", "impersonated"}

    async def test_domain_errors_are_not_escalated(self) -> None:
        anonymous = FakeTransport(TransportTier.ANONYMOUS, [NotFoundError("gone")])
        impersonated = FakeTransport(TransportTier.IMPERSONATED, [{"data": 1}])
        provider = provider_with(
            {TransportTier.ANONYMOUS: anonymous, TransportTier.IMPERSONATED: impersonated}
        )
        with pytest.raises(NotFoundError):
            await provider.request_json("GET", "https://x")
        assert impersonated.calls == 0

    async def test_rate_limit_is_retried_in_place_before_escalating(self) -> None:
        anonymous = FakeTransport(TransportTier.ANONYMOUS, [RateLimitError("slow"), {"data": "ok"}])
        impersonated = FakeTransport(TransportTier.IMPERSONATED, [{"data": "wrong"}])
        provider = provider_with(
            {TransportTier.ANONYMOUS: anonymous, TransportTier.IMPERSONATED: impersonated}
        )
        assert await provider.request_json("GET", "https://x") == {"data": "ok"}
        assert impersonated.calls == 0

    async def test_unavailable_tier_is_skipped(self) -> None:
        impersonated = FakeTransport(TransportTier.IMPERSONATED, [{"data": 1}])
        provider = provider_with({TransportTier.IMPERSONATED: impersonated})
        assert await provider.request_json("GET", "https://x") == {"data": 1}
