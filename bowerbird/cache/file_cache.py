"""Cache backends.

A username's numeric id never changes, so resolving it twice is pure waste —
and every resolution is a request that can be rate limited. The file cache is
deliberately dumb: one JSON file per key, atomic writes, optional TTL. No
database, because the access pattern is a handful of reads per run.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any

import anyio
import orjson

from ..utils.files import atomic_write_bytes, ensure_dir, sanitize_path_component
from ..utils.logging import logger

__all__ = ["FileCache", "JsonCache", "MemoryCache", "NullCache"]


class MemoryCache:
    """In-process cache. Used by tests and as a front for :class:`FileCache`."""

    def __init__(self, clock: Any = time.monotonic) -> None:
        self._clock = clock
        self._data: dict[str, tuple[bytes, float | None]] = {}

    async def get(self, key: str) -> bytes | None:
        """Return the value, or ``None`` when absent or expired."""
        entry = self._data.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if expires_at is not None and self._clock() >= expires_at:
            del self._data[key]
            return None
        return value

    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        """Store a value with an optional TTL in seconds."""
        self._data[key] = (value, None if ttl is None else self._clock() + ttl)

    async def delete(self, key: str) -> None:
        """Remove a key, ignoring a miss."""
        self._data.pop(key, None)


class NullCache:
    """Cache that stores nothing. Used to force fresh resolution."""

    async def get(self, key: str) -> bytes | None:
        """Always miss."""
        return None

    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        """Discard the value."""

    async def delete(self, key: str) -> None:
        """No-op."""


class FileCache:
    """Durable cache backed by one file per key.

    Keys are sanitised for use as filenames and hashed when long, so an
    arbitrary key never escapes the cache directory.

    Args:
        directory: Cache root. Created on first write.
        default_ttl: Applied when :meth:`set` is called without a TTL.
        memory_front: Serve repeat reads from memory within one process.
    """

    def __init__(
        self,
        directory: Path,
        *,
        default_ttl: float | None = None,
        memory_front: bool = True,
    ) -> None:
        self._directory = Path(directory)
        self._default_ttl = default_ttl
        self._memory = MemoryCache() if memory_front else None

    @property
    def directory(self) -> Path:
        """Cache root directory."""
        return self._directory

    def _path(self, key: str) -> Path:
        """Filesystem path for a cache key."""
        safe = sanitize_path_component(key)
        if len(safe) > 100 or safe != key:
            safe = f"{safe[:60]}_{hashlib.sha256(key.encode()).hexdigest()[:16]}"
        return self._directory / f"{safe}.json"

    async def get(self, key: str) -> bytes | None:
        """Return the cached value, or ``None`` when absent or expired."""
        if self._memory is not None and (hit := await self._memory.get(key)) is not None:
            return hit

        path = self._path(key)
        if not path.exists():
            return None
        try:
            async with await anyio.open_file(path, "rb") as handle:
                envelope = orjson.loads(await handle.read())
        except (orjson.JSONDecodeError, OSError) as exc:
            logger.debug("discarding unreadable cache entry {}: {}", path, exc)
            path.unlink(missing_ok=True)
            return None

        expires_at = envelope.get("expires_at")
        if expires_at is not None and time.time() >= expires_at:
            path.unlink(missing_ok=True)
            return None

        value = str(envelope.get("value", "")).encode("utf-8")
        if self._memory is not None:
            await self._memory.set(key, value)
        return value

    async def set(self, key: str, value: bytes, *, ttl: float | None = None) -> None:
        """Store a value, expiring after ``ttl`` seconds when given."""
        effective = self._default_ttl if ttl is None else ttl
        envelope = {
            "key": key,
            "value": value.decode("utf-8"),
            "stored_at": time.time(),
            "expires_at": None if effective is None else time.time() + effective,
        }
        ensure_dir(self._directory)
        await atomic_write_bytes(self._path(key), orjson.dumps(envelope))
        if self._memory is not None:
            await self._memory.set(key, value, ttl=effective)

    async def delete(self, key: str) -> None:
        """Remove a key from both layers, ignoring a miss."""
        self._path(key).unlink(missing_ok=True)
        if self._memory is not None:
            await self._memory.delete(key)


class JsonCache:
    """Typed convenience wrapper over any :class:`Cache` backend.

    Callers cache dicts and models, not bytes; this keeps ``orjson`` calls out
    of every call site.

    Args:
        backend: Any object satisfying the ``Cache`` protocol.
        namespace: Prefix keeping different value kinds from colliding.
    """

    def __init__(self, backend: Any, *, namespace: str = "") -> None:
        self._backend = backend
        self._namespace = namespace

    def _key(self, key: str) -> str:
        """Namespace-qualified cache key."""
        return f"{self._namespace}:{key}" if self._namespace else key

    async def get_json(self, key: str) -> Any | None:
        """Return a decoded JSON value, or ``None`` on a miss."""
        raw = await self._backend.get(self._key(key))
        if raw is None:
            return None
        try:
            return orjson.loads(raw)
        except orjson.JSONDecodeError:
            await self._backend.delete(self._key(key))
            return None

    async def set_json(self, key: str, value: Any, *, ttl: float | None = None) -> None:
        """Store a JSON-serialisable value."""
        await self._backend.set(self._key(key), orjson.dumps(value), ttl=ttl)

    async def delete(self, key: str) -> None:
        """Remove a namespaced key."""
        await self._backend.delete(self._key(key))
