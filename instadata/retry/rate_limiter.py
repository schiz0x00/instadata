"""Adaptive rate limiting.

Instagram publishes no quota, so the pacing is closed-loop: start at a
conservative delay, multiply it whenever the server throttles us, and decay it
back down only after a run of clean responses. A fixed sleep is either too
slow all day or too fast right before a ban.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable

import anyio

from ..models.config import RateLimitConfig
from ..utils.logging import logger

__all__ = ["AdaptiveRateLimiter", "NullRateLimiter"]


class AdaptiveRateLimiter:
    """Paces requests, adapting the gap to observed throttling.

    Serialises pacing behind a lock: with N download workers sharing one
    limiter, the pacing applies to the fleet, not per worker.

    Args:
        config: Delay bounds and adaptation factors.
        sleep: Injectable sleep for tests.
        clock: Injectable monotonic clock for tests.
    """

    def __init__(
        self,
        config: RateLimitConfig | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._config = config or RateLimitConfig()
        self._sleep = sleep or anyio.sleep
        self._clock = clock or anyio.current_time
        self._delay = self._config.initial_delay
        self._consecutive_successes = 0
        self._last_request_at: float | None = None
        self._lock = anyio.Lock()

    @property
    def current_delay(self) -> float:
        """Current target gap between requests, in seconds."""
        return self._delay

    async def acquire(self) -> None:
        """Block until enough time has passed since the previous request."""
        async with self._lock:
            now = self._clock()
            if self._last_request_at is not None:
                elapsed = now - self._last_request_at
                wait = self._jittered(self._delay) - elapsed
                if wait > 0:
                    await self._sleep(wait)
            self._last_request_at = self._clock()

    def record_success(self) -> None:
        """Report a clean response; relax pacing after a sustained streak."""
        self._consecutive_successes += 1
        if self._consecutive_successes < self._config.successes_before_decrease:
            return
        self._consecutive_successes = 0
        relaxed = max(self._delay * self._config.decrease_factor, self._config.min_delay)
        if relaxed < self._delay:
            logger.debug("rate limit relaxed {:.2f}s -> {:.2f}s", self._delay, relaxed)
        self._delay = relaxed

    def record_throttled(self, retry_after: float | None = None) -> None:
        """Report throttling; tighten pacing immediately.

        A server-supplied ``Retry-After`` raises the floor directly, since it
        is a better estimate than our multiplier.
        """
        self._consecutive_successes = 0
        tightened = self._delay * self._config.increase_factor
        if retry_after is not None:
            tightened = max(tightened, retry_after)
        self._delay = min(tightened, self._config.max_delay)
        logger.warning("throttled, pacing raised to {:.2f}s", self._delay)

    def _jittered(self, delay: float) -> float:
        """Randomise ``delay`` upward so requests never form a clean cadence."""
        if not self._config.jitter:
            return delay
        return delay * random.uniform(1.0, 1.0 + self._config.jitter)


class NullRateLimiter:
    """No-op limiter, for tests and for transports that pace themselves."""

    async def acquire(self) -> None:
        """Return immediately."""

    def record_success(self) -> None:
        """Ignore."""

    def record_throttled(self, retry_after: float | None = None) -> None:
        """Ignore."""
