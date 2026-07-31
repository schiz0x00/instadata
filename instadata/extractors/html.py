"""Parsers for data embedded in server-rendered Instagram HTML.

Last-resort resolution path: when every JSON endpoint has been retired or
rejects us, the profile page itself still carries the numeric user id in an
inline script. Regex rather than a DOM parser, deliberately — we want one
value out of a 2 MB page, not a tree.
"""

from __future__ import annotations

import re
from typing import Any

import orjson

from ..errors import ParsingError

__all__ = ["extract_json_blobs", "extract_shared_data", "extract_user_id"]

_USER_ID_PATTERNS = (
    re.compile(r'"profile_id"\s*:\s*"(\d+)"'),
    re.compile(r'"user_id"\s*:\s*"(\d+)"'),
    re.compile(r'"profilePage_(\d+)"'),
    re.compile(r'"owner"\s*:\s*\{\s*"id"\s*:\s*"(\d+)"'),
    re.compile(r"instagram://user\?username=[^&]*&amp;id=(\d+)"),
)

_SHARED_DATA_RE = re.compile(r"window\._sharedData\s*=\s*(\{.*?\});</script>", re.DOTALL)
_ADDITIONAL_DATA_RE = re.compile(
    r'window\.__additionalDataLoaded\s*\(\s*[\'"][^\'"]+[\'"]\s*,\s*(\{.*?\})\s*\);',
    re.DOTALL,
)
_JSON_SCRIPT_RE = re.compile(
    r'<script type="application/json"[^>]*>(.*?)</script>',
    re.DOTALL,
)


def extract_user_id(html: str) -> str:
    """Pull the numeric user id out of a profile page.

    Raises:
        ParsingError: No id pattern matched, meaning the page layout changed
            or Instagram served a login wall instead of the profile.
    """
    for pattern in _USER_ID_PATTERNS:
        if match := pattern.search(html):
            return match.group(1)
    raise ParsingError("no user id found in profile HTML", path="html")


def extract_shared_data(html: str) -> dict[str, Any] | None:
    """Return the legacy ``window._sharedData`` blob, when still present."""
    for pattern in (_SHARED_DATA_RE, _ADDITIONAL_DATA_RE):
        if match := pattern.search(html):
            try:
                payload = orjson.loads(match.group(1))
            except orjson.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                return payload
    return None


def extract_json_blobs(html: str, needle: str | None = None) -> list[dict[str, Any]]:
    """Return every embedded ``application/json`` script payload.

    Args:
        html: Page source.
        needle: When given, only blobs whose raw text contains it are parsed,
            which avoids decoding dozens of unrelated megabyte-scale blobs.
    """
    blobs: list[dict[str, Any]] = []
    for match in _JSON_SCRIPT_RE.finditer(html):
        raw = match.group(1)
        if needle and needle not in raw:
            continue
        try:
            payload = orjson.loads(raw)
        except orjson.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            blobs.append(payload)
    return blobs
