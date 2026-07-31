"""Exception hierarchy.

Every failure crossing a module boundary is one of these. Transport-level
exceptions (``httpx``, ``curl_cffi``, ``playwright``) are translated at the
transport boundary so upper layers never import a transport library to catch
its errors.
"""

from __future__ import annotations

__all__ = [
    "AllTiersFailedError",
    "AuthenticationError",
    "ChecksumMismatchError",
    "ConfigurationError",
    "DownloadError",
    "HTTPStatusError",
    "MediaUnavailableError",
    "NetworkError",
    "NotFoundError",
    "ParsingError",
    "PrivateAccountError",
    "RateLimitError",
    "ScraperError",
    "TransportError",
]


class ScraperError(Exception):
    """Base class for every error raised by this package."""


class ConfigurationError(ScraperError):
    """Invalid or missing configuration supplied by the caller."""


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #


class TransportError(ScraperError):
    """Base class for anything that went wrong while talking to Instagram."""


class NetworkError(TransportError):
    """Connection reset, DNS failure, TLS failure, timeout.

    Always retryable: no server-side state was observed.
    """


class HTTPStatusError(TransportError):
    """Unexpected HTTP status code.

    Attributes:
        status_code: The status code returned by the server.
        url: Request URL, with query string, for log correlation.
        body: Truncated response body, useful for post-mortems.
    """

    def __init__(self, status_code: int, url: str, body: str = "") -> None:
        self.status_code = status_code
        self.url = url
        self.body = body[:512]
        super().__init__(f"HTTP {status_code} for {url}: {self.body!r}")


class AuthenticationError(TransportError):
    """Instagram rejected the session: 401, or a logged-out GraphQL error.

    Signals the escalation ladder to move to the next, more authenticated tier.
    """


class RateLimitError(TransportError):
    """Instagram is throttling us: 429, or 403 with a rate-limit body.

    Attributes:
        retry_after: Seconds the server asked us to wait, when advertised.
    """

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        self.retry_after = retry_after
        super().__init__(message)


class AllTiersFailedError(TransportError):
    """Every transport tier in the escalation ladder failed.

    Attributes:
        failures: Per-tier failure, in escalation order.
    """

    def __init__(self, failures: dict[str, BaseException]) -> None:
        self.failures = failures
        detail = ", ".join(f"{tier}: {exc!r}" for tier, exc in failures.items())
        super().__init__(f"all transport tiers failed ({detail})")


# --------------------------------------------------------------------------- #
# Domain
# --------------------------------------------------------------------------- #


class NotFoundError(ScraperError):
    """The requested user, post or story does not exist."""


class PrivateAccountError(ScraperError):
    """The account exists but its media is not visible to this session."""


class MediaUnavailableError(ScraperError):
    """The post exists but its media cannot be fetched.

    Deleted, age-gated, region-blocked, or an expired CDN URL.
    """


class ParsingError(ScraperError):
    """Instagram's response did not match the shape we expect.

    Raised whenever a required field is missing. Instagram changes its
    payloads without notice; a loud parse failure beats silently persisting
    half-empty rows.

    Attributes:
        path: Dotted path of the offending field, e.g. ``data.user.id``.
    """

    def __init__(self, message: str, path: str | None = None) -> None:
        self.path = path
        super().__init__(f"{message} (at {path})" if path else message)


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #


class DownloadError(ScraperError):
    """A media file could not be written to disk."""


class ChecksumMismatchError(DownloadError):
    """Downloaded bytes did not match the expected digest.

    Attributes:
        expected: Digest we were told to expect.
        actual: Digest we computed.
    """

    def __init__(self, path: str, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(f"checksum mismatch for {path}: expected {expected}, got {actual}")
