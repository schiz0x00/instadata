"""Production Instagram media scraper.

Cheapest-transport-first: anonymous HTTP, then TLS-impersonated HTTP, then an
authenticated session, and only then a real browser.
"""

from .errors import (
    AllTiersFailedError,
    AuthenticationError,
    ChecksumMismatchError,
    ConfigurationError,
    DownloadError,
    HTTPStatusError,
    MediaUnavailableError,
    NetworkError,
    NotFoundError,
    ParsingError,
    PrivateAccountError,
    RateLimitError,
    ScraperError,
    TransportError,
)
from .models import Media, MediaType, Profile, ScraperConfig

__version__ = "0.1.0"

__all__ = [
    "AllTiersFailedError",
    "AuthenticationError",
    "ChecksumMismatchError",
    "ConfigurationError",
    "DownloadError",
    "HTTPStatusError",
    "Media",
    "MediaType",
    "MediaUnavailableError",
    "NetworkError",
    "NotFoundError",
    "ParsingError",
    "PrivateAccountError",
    "Profile",
    "RateLimitError",
    "ScraperConfig",
    "ScraperError",
    "TransportError",
    "__version__",
]
