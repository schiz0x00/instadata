"""Configuration objects.

Every tunable lives here as a validated, immutable value object. Components
receive the slice they need by constructor injection, never by importing a
module-level singleton, so tests can vary one knob without touching globals.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "DEFAULT_OUTPUT_DIR",
    "DEFAULT_USER_AGENT",
    "INSTAGRAM_APP_ID",
    "DownloadConfig",
    "RateLimitConfig",
    "RetryConfig",
    "ScraperConfig",
    "StorageConfig",
    "TransportConfig",
    "TransportTier",
]

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/150.0.0.0 Safari/537.36"
)
INSTAGRAM_APP_ID = "936619743392459"

#: Where a run writes when ``--output`` is not given, relative to the working
#: directory. Named after the tool so a bare run is self-describing rather than
#: dropping a generic ``data/`` into whatever directory you happened to be in.
DEFAULT_OUTPUT_DIR = Path("bowerbird")


class TransportTier(StrEnum):
    """Rungs of the escalation ladder, cheapest first.

    Declaration order is escalation order; the escalator relies on it.
    """

    ANONYMOUS = "anonymous"
    IMPERSONATED = "impersonated"
    AUTHENTICATED = "authenticated"
    BROWSER = "browser"


class FrozenConfig(BaseModel):
    """Immutable configuration base."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class RetryConfig(FrozenConfig):
    """Exponential backoff with full jitter."""

    max_attempts: Annotated[int, Field(ge=1)] = 5
    initial_backoff: Annotated[float, Field(gt=0)] = 1.0
    max_backoff: Annotated[float, Field(gt=0)] = 60.0
    backoff_multiplier: Annotated[float, Field(gt=1)] = 2.0
    jitter: Annotated[float, Field(ge=0, le=1)] = Field(
        default=1.0,
        description="Fraction of the computed delay randomised away, 0 disables jitter.",
    )


class RateLimitConfig(FrozenConfig):
    """Adaptive pacing between API requests.

    The limiter starts at ``initial_delay`` and moves within
    ``[min_delay, max_delay]``: it multiplies the delay on a throttling
    response and decays it back down after sustained success.
    """

    initial_delay: Annotated[float, Field(ge=0)] = 1.5
    min_delay: Annotated[float, Field(ge=0)] = 0.5
    max_delay: Annotated[float, Field(gt=0)] = 90.0
    increase_factor: Annotated[float, Field(gt=1)] = 2.0
    decrease_factor: Annotated[float, Field(gt=0, le=1)] = 0.9
    successes_before_decrease: Annotated[int, Field(ge=1)] = 10
    jitter: Annotated[float, Field(ge=0, le=1)] = 0.3


class TransportConfig(FrozenConfig):
    """HTTP behaviour shared by every tier."""

    timeout: Annotated[float, Field(gt=0)] = 30.0
    connect_timeout: Annotated[float, Field(gt=0)] = 10.0
    max_connections: Annotated[int, Field(ge=1)] = 20
    http2: bool = True
    proxy: str | None = Field(
        default=None,
        description=(
            "Proxy URL applied to every tier, e.g. http://host:8080, "
            "socks5://host:1080, or http://user:pass@host:8080."
        ),
    )
    user_agent: str = DEFAULT_USER_AGENT
    app_id: str = INSTAGRAM_APP_ID
    impersonate: str = Field(
        default="chrome",
        description="curl_cffi TLS/JA3 fingerprint profile.",
    )
    tiers: tuple[TransportTier, ...] = Field(
        default=(
            TransportTier.ANONYMOUS,
            TransportTier.IMPERSONATED,
            TransportTier.AUTHENTICATED,
            TransportTier.BROWSER,
        ),
        description="Enabled tiers, in escalation order.",
    )

    @field_validator("proxy")
    @classmethod
    def _check_proxy(cls, value: str | None) -> str | None:
        """Reject an unusable proxy URL at construction.

        Without this a bad scheme surfaces as a raw ``ImportError`` from
        httpx's SOCKS path or a ``ValueError`` from deep inside a transport,
        neither of which the escalation ladder or the CLI catch.
        """
        if value is None:
            return None
        from ..utils.urls import validate_proxy_url

        return validate_proxy_url(value)


class DownloadConfig(FrozenConfig):
    """Media download behaviour."""

    workers: Annotated[int, Field(ge=1)] = 8
    chunk_size: Annotated[int, Field(ge=1024)] = 1 << 16
    timeout: Annotated[float, Field(gt=0)] = 120.0
    resume: bool = Field(
        default=True,
        description="Continue partial files with a Range request when the server allows it.",
    )
    verify_checksum: bool = Field(
        default=False,
        description=(
            "Compute a sha256 of each finished file and report it on the "
            "download result. Off by default: it re-reads every byte written "
            "and nothing in the pipeline compares the digest."
        ),
    )
    skip_existing: bool = True
    temp_suffix: str = ".part"


class StorageConfig(FrozenConfig):
    """Where output lands on disk."""

    output_dir: Path = DEFAULT_OUTPUT_DIR
    metadata_filename: str = "metadata.jsonl"
    state_filename: str = "state.json"
    write_metadata: bool = True

    def profile_dir(self, username: str) -> Path:
        """Directory holding one profile's media and metadata.

        The name is sanitised here rather than at the call sites: not every
        username reaching this method came from
        :func:`~bowerbird.utils.urls.normalize_username`. A single
        post's owner handle comes straight out of Instagram's payload, and a
        ``../`` in it would otherwise write outside the output directory.
        """
        from ..utils.files import sanitize_path_component

        return self.output_dir / sanitize_path_component(username)


class ScraperConfig(FrozenConfig):
    """Root configuration, composed of one section per concern."""

    transport: TransportConfig = TransportConfig()
    retry: RetryConfig = RetryConfig()
    rate_limit: RateLimitConfig = RateLimitConfig()
    download: DownloadConfig = DownloadConfig()
    storage: StorageConfig = StorageConfig()

    stop_after_known_pages: Annotated[int, Field(ge=0)] = Field(
        default=2,
        description=(
            "Stop a top-of-timeline walk after this many consecutive pages "
            "containing nothing new. The timeline is newest-first, so once a "
            "page is entirely already-downloaded everything below it is too. "
            "0 disables the early stop and always walks the whole account."
        ),
    )

    cache_dir: Path = Path.home() / ".cache" / "bowerbird"
    cookies_path: Path | None = None
    page_size: Annotated[int, Field(ge=1, le=50)] = Field(
        default=12,
        description="Requested items per page. Instagram caps timeline pages at 12.",
    )
