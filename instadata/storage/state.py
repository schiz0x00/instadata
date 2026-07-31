"""Resume state.

The cursor is the whole game: without it, an interruption at page 530 costs
530 pages of requests to get back to where it was — which is also 530 chances
to get rate limited. State is written after every completed page and written
atomically, so a kill during the write cannot corrupt it.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import orjson
from pydantic import BaseModel, ConfigDict, Field

from ..utils.files import atomic_write_bytes, sanitize_path_component
from ..utils.logging import logger

__all__ = ["FileStateStore", "JobState", "NullStateStore"]


class JobState(BaseModel):
    """Progress of one scrape job.

    Attributes:
        job_key: Identifies the job, normally ``<username>:<target>``.
        user_id: Resolved numeric id, cached so a resume skips resolution.
        cursor: Cursor to pass to the next page request.
        pages_done: Pages completed, for reporting.
        items_seen: Media items yielded so far.
        completed: Whether the timeline was walked to its end.
        updated_at: Last write time, UTC.
    """

    model_config = ConfigDict(extra="ignore")

    job_key: str
    user_id: str | None = None
    cursor: str | None = None
    pages_done: int = 0
    items_seen: int = 0
    completed: bool = False
    updated_at: datetime = Field(default_factory=lambda: datetime.now(tz=UTC))

    def advanced(self, *, cursor: str | None, items: int) -> JobState:
        """Return the state one page further along."""
        return self.model_copy(
            update={
                "cursor": cursor,
                "pages_done": self.pages_done + 1,
                "items_seen": self.items_seen + items,
                "updated_at": datetime.now(tz=UTC),
            }
        )

    def finished(self) -> JobState:
        """Return the state marked complete."""
        return self.model_copy(update={"completed": True, "updated_at": datetime.now(tz=UTC)})


class FileStateStore:
    """Stores one JSON file per job, written atomically.

    Args:
        directory: Where state files live. Usually the profile's output
            directory, so deleting a profile's data also clears its progress.
    """

    def __init__(self, directory: Path) -> None:
        self._directory = Path(directory)

    @property
    def directory(self) -> Path:
        """State directory."""
        return self._directory

    def _path(self, job_key: str) -> Path:
        """State file path for a job key."""
        return self._directory / f"{sanitize_path_component(job_key)}.state.json"

    async def load(self, job_key: str) -> dict[str, Any] | None:
        """Return saved state, or ``None`` for a fresh job.

        A corrupt state file is discarded rather than fatal: restarting a
        scrape beats refusing to run.
        """
        path = self._path(job_key)
        if not path.exists():
            return None
        try:
            async with await anyio.open_file(path, "rb") as handle:
                payload = orjson.loads(await handle.read())
        except (orjson.JSONDecodeError, OSError) as exc:
            logger.warning("discarding unreadable state file {}: {}", path, exc)
            return None
        return payload if isinstance(payload, dict) else None

    async def load_state(self, job_key: str) -> JobState:
        """Return typed state, defaulting to a fresh job."""
        raw = await self.load(job_key)
        if raw is None:
            return JobState(job_key=job_key)
        try:
            return JobState.model_validate(raw)
        except ValueError as exc:
            logger.warning("state for {} is invalid, starting fresh: {}", job_key, exc)
            return JobState(job_key=job_key)

    async def save(self, job_key: str, state: Mapping[str, Any]) -> None:
        """Persist state atomically."""
        await atomic_write_bytes(
            self._path(job_key),
            orjson.dumps(dict(state), option=orjson.OPT_INDENT_2),
        )

    async def save_state(self, state: JobState) -> None:
        """Persist typed state."""
        await self.save(state.job_key, state.model_dump(mode="json"))

    async def clear(self, job_key: str) -> None:
        """Delete a job's state, forcing a restart from the newest post."""
        self._path(job_key).unlink(missing_ok=True)


class NullStateStore:
    """Keeps no state. Used by ``--no-resume`` and by tests."""

    async def load(self, job_key: str) -> dict[str, Any] | None:
        """Always report no saved state."""
        return None

    async def load_state(self, job_key: str) -> JobState:
        """Return a fresh job state."""
        return JobState(job_key=job_key)

    async def save(self, job_key: str, state: Mapping[str, Any]) -> None:
        """Discard the state."""

    async def save_state(self, state: JobState) -> None:
        """Discard the state."""

    async def clear(self, job_key: str) -> None:
        """No-op."""
