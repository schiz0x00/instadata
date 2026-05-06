"""Tests for retry policy and adaptive rate limiting."""

from __future__ import annotations

import pytest

from bowerbird.errors import (
    AuthenticationError,
    HTTPStatusError,
    NetworkError,
    ParsingError,
    RateLimitError,
)
from bowerbird.models.config import RateLimitConfig, RetryConfig
from bowerbird.retry import AdaptiveRateLimiter, RetryPolicy, is_retryable

NO_JITTER = RetryConfig(jitter=0.0, initial_backoff=1.0, backoff_multiplier=2.0, max_attempts=4)


class TestIsRetryable:
    @pytest.mark.parametrize(
        ("exc", "expected"),
        [
            (NetworkError("boom"), True),
            (RateLimitError("slow down"), True),
            (HTTPStatusError(503, "u"), True),
            (HTTPStatusError(500, "u"), True),
            (HTTPStatusError(404, "u"), False),
            (AuthenticationError("nope"), False),
            (ParsingError("bad shape"), False),
        ],
    )
    def test_classification(self, exc: Exception, expected: bool) -> None:
        assert is_retryable(exc) is expected


class TestBackoff:
    def test_delay_grows_exponentially(self) -> None:
        policy = RetryPolicy(NO_JITTER)
        assert [policy.backoff_for(n) for n in (1, 2, 3)] == [1.0, 2.0, 4.0]

    def test_delay_is_capped(self) -> None:
        policy = RetryPolicy(NO_JITTER.model_copy(update={"max_backoff": 3.0}))
        assert policy.backoff_for(10) == 3.0

    def test_retry_after_wins_over_curve(self) -> None:
        assert RetryPolicy(NO_JITTER).backoff_for(1, retry_after=42.0) == 42.0

    def test_jitter_stays_within_bounds(self) -> None:
        policy = RetryPolicy(RetryConfig(jitter=0.5, initial_backoff=10.0))
        delays = [policy.backoff_for(1) for _ in range(50)]
        assert all(5.0 <= d <= 10.0 for d in delays)
        assert len(set(delays)) > 1


class TestRetryPolicyRun:
    async def test_returns_on_first_success(self, instant_sleep) -> None:
        policy = RetryPolicy(NO_JITTER, sleep=instant_sleep)
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            return "ok"

        assert await policy.run(operation) == "ok"
        assert calls == 1
        assert instant_sleep.delays == []

    async def test_retries_until_success(self, instant_sleep) -> None:
        policy = RetryPolicy(NO_JITTER, sleep=instant_sleep)
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls < 3:
                raise NetworkError("flaky")
            return "ok"

        assert await policy.run(operation) == "ok"
        assert calls == 3
        assert instant_sleep.delays == [1.0, 2.0]

    async def test_gives_up_after_max_attempts(self, instant_sleep) -> None:
        policy = RetryPolicy(NO_JITTER, sleep=instant_sleep)
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            raise NetworkError("always down")

        with pytest.raises(NetworkError):
            await policy.run(operation)
        assert calls == NO_JITTER.max_attempts

    async def test_non_retryable_fails_immediately(self, instant_sleep) -> None:
        policy = RetryPolicy(NO_JITTER, sleep=instant_sleep)
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            raise AuthenticationError("logged out")

        with pytest.raises(AuthenticationError):
            await policy.run(operation)
        assert calls == 1
        assert instant_sleep.delays == []

    async def test_retry_after_header_is_honoured(self, instant_sleep) -> None:
        policy = RetryPolicy(NO_JITTER, sleep=instant_sleep)
        calls = 0

        async def operation() -> str:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RateLimitError("slow down", retry_after=7.0)
            return "ok"

        assert await policy.run(operation) == "ok"
        assert instant_sleep.delays == [7.0]


class TestAdaptiveRateLimiter:
    def _limiter(self, **overrides) -> AdaptiveRateLimiter:
        config = RateLimitConfig(
            initial_delay=1.0,
            min_delay=0.5,
            max_delay=10.0,
            increase_factor=2.0,
            decrease_factor=0.5,
            successes_before_decrease=2,
            jitter=0.0,
            **overrides,
        )
        return AdaptiveRateLimiter(config)

    def test_throttling_increases_delay(self) -> None:
        limiter = self._limiter()
        limiter.record_throttled()
        assert limiter.current_delay == 2.0

    def test_delay_is_capped(self) -> None:
        limiter = self._limiter()
        for _ in range(10):
            limiter.record_throttled()
        assert limiter.current_delay == 10.0

    def test_retry_after_raises_the_floor(self) -> None:
        limiter = self._limiter()
        limiter.record_throttled(retry_after=6.0)
        assert limiter.current_delay == 6.0

    def test_delay_relaxes_only_after_a_streak(self) -> None:
        limiter = self._limiter()
        limiter.record_throttled()
        limiter.record_success()
        assert limiter.current_delay == 2.0
        limiter.record_success()
        assert limiter.current_delay == 1.0

    def test_relaxation_stops_at_min_delay(self) -> None:
        limiter = self._limiter()
        for _ in range(20):
            limiter.record_success()
        assert limiter.current_delay == 0.5

    def test_throttling_resets_the_success_streak(self) -> None:
        limiter = self._limiter()
        limiter.record_success()
        limiter.record_throttled()
        limiter.record_success()
        assert limiter.current_delay == 2.0

    async def test_acquire_waits_for_the_configured_gap(self) -> None:
        delays: list[float] = []
        now = [100.0]

        async def sleep(seconds: float) -> None:
            delays.append(seconds)
            now[0] += seconds

        limiter = AdaptiveRateLimiter(
            RateLimitConfig(initial_delay=2.0, jitter=0.0),
            sleep=sleep,
            clock=lambda: now[0],
        )
        await limiter.acquire()
        assert delays == []
        await limiter.acquire()
        assert delays == [2.0]

    async def test_acquire_does_not_wait_when_enough_time_passed(self) -> None:
        delays: list[float] = []
        now = [0.0]

        async def sleep(seconds: float) -> None:
            delays.append(seconds)

        limiter = AdaptiveRateLimiter(
            RateLimitConfig(initial_delay=1.0, jitter=0.0),
            sleep=sleep,
            clock=lambda: now[0],
        )
        await limiter.acquire()
        now[0] = 30.0
        await limiter.acquire()
        assert delays == []
