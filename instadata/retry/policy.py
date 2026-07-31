"""Retry policy.

A small object so callers express intent (``await policy.run(fn)``) rather
than assembling retry primitives at every call site, and so tests can inject a
zero-delay policy. Hand-rolled rather than wrapping a retry library: the whole
policy is thirty lines, and a dependency would only be re-exporting them.
"""

from __future__ import annotations

import random
from collections.abc import Awaitable, Callable
from typing import TypeVar

import anyio

from ..errors import HTTPStatusError, NetworkError, RateLimitError, ScraperError
from ..models.config import RetryConfig
from ..utils.logging import logger

__all__ = ["RetryPolicy", "is_retryable"]

T = TypeVar("T")

RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


def is_retryable(exc: BaseException) -> bool:
    """Whether another attempt could plausibly succeed.

    Retryable: network failures, throttling, 5xx and a handful of transient
    4xx. Not retryable: authentication, parsing and domain errors, which need
    escalation or code changes rather than patience.
    """
    if isinstance(exc, (NetworkError, RateLimitError)):
        return True
    if isinstance(exc, HTTPStatusError):
        return exc.status_code in RETRYABLE_STATUS
    return False


class RetryPolicy:
    """Exponential backoff with full jitter, honouring ``Retry-After``.

    Args:
        config: Attempt count and backoff shape.
        sleep: Injectable sleep, so tests run instantly.
        predicate: Overrides which exceptions are considered retryable.
    """

    def __init__(
        self,
        config: RetryConfig | None = None,
        *,
        sleep: Callable[[float], Awaitable[None]] | None = None,
        predicate: Callable[[BaseException], bool] = is_retryable,
    ) -> None:
        self._config = config or RetryConfig()
        self._sleep = sleep or anyio.sleep
        self._predicate = predicate

    @property
    def config(self) -> RetryConfig:
        """The policy's configuration."""
        return self._config

    def backoff_for(self, attempt: int, retry_after: float | None = None) -> float:
        """Delay in seconds before ``attempt`` (1-based) is retried.

        A server-supplied ``Retry-After`` wins over the computed delay: the
        server knows its own cooldown better than our curve does.
        """
        if retry_after is not None:
            return min(retry_after, self._config.max_backoff)
        raw = self._config.initial_backoff * (self._config.backoff_multiplier ** (attempt - 1))
        delay = min(raw, self._config.max_backoff)
        if self._config.jitter:
            floor = delay * (1.0 - self._config.jitter)
            delay = random.uniform(floor, delay)
        return delay

    async def run(
        self,
        operation: Callable[[], Awaitable[T]],
        *,
        description: str = "request",
    ) -> T:
        """Call ``operation`` until it succeeds or attempts run out.

        Raises:
            ScraperError: The last failure, re-raised unchanged so callers
                still see the specific error type.
        """
        last: BaseException | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                return await operation()
            except ScraperError as exc:
                last = exc
                if not self._predicate(exc) or attempt == self._config.max_attempts:
                    raise
                retry_after = getattr(exc, "retry_after", None)
                delay = self.backoff_for(attempt, retry_after)
                logger.warning(
                    "{} failed ({}), attempt {}/{}, retrying in {:.1f}s",
                    description,
                    type(exc).__name__,
                    attempt,
                    self._config.max_attempts,
                    delay,
                )
                await self._sleep(delay)
        raise last  # pragma: no cover - loop always raises or returns
