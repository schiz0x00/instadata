"""Filesystem helpers: atomic writes, digests, safe path components.

Every write in this package lands through a temporary file plus
:func:`os.replace`, so an interrupted run never leaves a truncated file that a
later run would mistake for a complete download.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections.abc import Iterable
from pathlib import Path

import anyio

__all__ = [
    "atomic_replace",
    "atomic_write_bytes",
    "digest_bytes",
    "ensure_dir",
    "file_digest",
    "sanitize_path_component",
    "temp_path_for",
]

_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10))}
_MAX_COMPONENT = 200


def sanitize_path_component(name: str) -> str:
    """Return ``name`` reduced to a safe single path component.

    Strips separators and control characters, collapses whitespace, avoids
    Windows reserved names, and truncates to a length every common filesystem
    accepts.
    """
    cleaned = _ILLEGAL.sub("_", name).strip(" .")
    cleaned = re.sub(r"\s+", " ", cleaned) or "_"
    if cleaned.split(".")[0].upper() in _RESERVED:
        cleaned = f"_{cleaned}"
    return cleaned[:_MAX_COMPONENT]


def ensure_dir(path: Path) -> Path:
    """Create ``path`` and its parents when missing, then return it."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def temp_path_for(destination: Path, suffix: str = ".part") -> Path:
    """Sibling temporary path for ``destination``.

    A sibling, not a system temp file, so the final :func:`os.replace` stays
    on one filesystem and is therefore atomic.
    """
    return destination.with_name(destination.name + suffix)


def digest_bytes(data: bytes, algorithm: str = "sha256") -> str:
    """Hex digest of an in-memory buffer."""
    return hashlib.new(algorithm, data).hexdigest()


async def file_digest(path: Path, algorithm: str = "sha256", chunk_size: int = 1 << 20) -> str:
    """Hex digest of a file, read in chunks so large videos stay off the heap."""
    hasher = hashlib.new(algorithm)
    async with await anyio.open_file(path, "rb") as handle:
        while chunk := await handle.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


def atomic_replace(source: Path, destination: Path) -> None:
    """Move ``source`` onto ``destination`` atomically."""
    ensure_dir(destination.parent)
    os.replace(source, destination)


async def atomic_write_bytes(
    destination: Path,
    chunks: Iterable[bytes] | bytes,
    *,
    mode: int | None = None,
) -> Path:
    """Write bytes to ``destination`` via a temporary sibling file.

    Args:
        destination: Final path.
        chunks: A buffer, or an iterable of buffers written in order.
        mode: Permission bits applied to the temporary file *before* it is
            moved into place, so the final path is never briefly readable at
            the default umask. Used for files holding credentials.

    Returns:
        The destination path.
    """
    ensure_dir(destination.parent)
    temp = temp_path_for(destination, f".tmp{os.getpid()}")
    payload: Iterable[bytes] = [chunks] if isinstance(chunks, bytes) else chunks
    try:
        async with await anyio.open_file(temp, "wb") as handle:
            for chunk in payload:
                await handle.write(chunk)
        if mode is not None:
            os.chmod(temp, mode)
        atomic_replace(temp, destination)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise
    return destination
