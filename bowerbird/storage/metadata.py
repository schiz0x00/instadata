"""Metadata persistence.

JSON Lines, not a database: the write pattern is append-only, the read pattern
is "load the ids I already have", and a plain file survives being copied,
grepped and piped into anything. A database would be a dependency and a
migration story bought for nothing.

Writes are buffered and flushed in batches, because ``fsync`` per post is what
turns a 100k-post run into an IO-bound one.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import anyio
import orjson

from ..errors import DownloadError
from ..models.media import Media
from ..utils.files import ensure_dir
from ..utils.logging import logger

__all__ = ["JsonLinesMetadataStore", "NullMetadataStore"]


class JsonLinesMetadataStore:
    """Append-only JSONL store, one media record per line.

    Existing ids are indexed on first use so a resumed run can skip work
    without re-reading the file per post.

    Args:
        path: Output file. Parent directories are created on demand.
        flush_every: Records buffered before hitting the disk.
    """

    def __init__(self, path: Path, *, flush_every: int = 25) -> None:
        self._path = Path(path)
        self._flush_every = max(1, flush_every)
        self._buffer: list[bytes] = []
        self._known: set[str] | None = None
        self._lock = anyio.Lock()

    @property
    def path(self) -> Path:
        """Backing file path."""
        return self._path

    async def _index(self) -> set[str]:
        """Ids already stored, read once per instance."""
        if self._known is not None:
            return self._known

        known: set[str] = set()
        if self._path.exists():
            async with await anyio.open_file(self._path, "rb") as handle:
                async for line in handle:
                    if not line.strip():
                        continue
                    try:
                        record = orjson.loads(line)
                    except orjson.JSONDecodeError:
                        continue  # tolerate a torn final line from a hard kill
                    if media_id := record.get("id"):
                        known.add(str(media_id))
            logger.debug("metadata index: {} existing records in {}", len(known), self._path)
        self._known = known
        return known

    async def has(self, media_id: str) -> bool:
        """Whether this id was already stored."""
        return str(media_id) in await self._index()

    async def save(self, media: Media) -> None:
        """Buffer one record, flushing when the batch is full.

        Duplicate ids are dropped, which makes re-running a partially
        completed job idempotent.
        """
        known = await self._index()
        if media.id in known:
            return

        async with self._lock:
            known.add(media.id)
            self._buffer.append(orjson.dumps(media.model_dump(mode="json")))
            if len(self._buffer) >= self._flush_every:
                await self._flush_locked()

    async def save_many(self, items: list[Media]) -> None:
        """Buffer several records in one pass."""
        for media in items:
            await self.save(media)

    async def flush(self) -> None:
        """Write buffered records to disk."""
        async with self._lock:
            await self._flush_locked()

    async def _flush_locked(self) -> None:
        """Append the buffer. Caller must hold the lock.

        Appends rather than rewrites: a crash mid-flush costs the tail of one
        batch, never the whole file.
        """
        if not self._buffer:
            return
        ensure_dir(self._path.parent)
        payload = b"\n".join(self._buffer) + b"\n"
        try:
            async with await anyio.open_file(self._path, "ab") as handle:
                await handle.write(payload)
        except OSError as exc:
            raise DownloadError(f"cannot write metadata to {self._path}: {exc}") from exc
        logger.debug("flushed {} metadata records", len(self._buffer))
        self._buffer.clear()

    async def load_all(self) -> list[dict[str, Any]]:
        """Read every stored record.

        For inspection and exports; the scrape path never calls this.
        """
        if not self._path.exists():
            return []
        records: list[dict[str, Any]] = []
        async with await anyio.open_file(self._path, "rb") as handle:
            async for line in handle:
                if line.strip():
                    try:
                        records.append(orjson.loads(line))
                    except orjson.JSONDecodeError:
                        continue
        return records

    async def aclose(self) -> None:
        """Flush anything still buffered."""
        await self.flush()

    async def __aenter__(self) -> JsonLinesMetadataStore:
        """Enter an async context, returning this store."""
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Flush on context exit."""
        await self.aclose()


class NullMetadataStore:
    """Discards everything. Used by ``--no-metadata`` and by tests."""

    async def save(self, media: Media) -> None:
        """Discard the record."""

    async def has(self, media_id: str) -> bool:
        """Always report a miss."""
        return False

    async def flush(self) -> None:
        """No-op."""

    async def aclose(self) -> None:
        """No-op."""
