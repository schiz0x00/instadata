"""Shortcode ↔ media id conversion.

A post's shortcode is its numeric media id written in a base64 alphabet, so the
two are interchangeable with arithmetic and no network call. That matters
because the endpoints that still return full post metadata are keyed by numeric
id, while every URL a user pastes carries the shortcode.
"""

from __future__ import annotations

__all__ = ["ALPHABET", "media_id_to_shortcode", "shortcode_to_media_id"]

#: Instagram's URL-safe base64 alphabet, most significant character first.
ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"

_INDEX = {char: position for position, char in enumerate(ALPHABET)}


def shortcode_to_media_id(shortcode: str) -> str:
    """Convert a post shortcode to its numeric media id.

    Args:
        shortcode: For example ``DbbY9pdm6Q2``. Anything after the eleventh
            character is ignored: Instagram appends carousel-slide suffixes
            there, and they are not part of the post's own id.

    Raises:
        ValueError: The shortcode contains a character outside the alphabet.
    """
    media_id = 0
    for char in shortcode[:11]:
        if char not in _INDEX:
            raise ValueError(f"invalid shortcode character {char!r} in {shortcode!r}")
        media_id = media_id * 64 + _INDEX[char]
    return str(media_id)


def media_id_to_shortcode(media_id: str | int) -> str:
    """Convert a numeric media id back to its shortcode.

    Raises:
        ValueError: ``media_id`` is not a non-negative integer.
    """
    value = int(media_id)
    if value < 0:
        raise ValueError(f"media id must be non-negative, got {media_id!r}")
    if value == 0:
        return ALPHABET[0]

    characters: list[str] = []
    while value:
        value, remainder = divmod(value, 64)
        characters.append(ALPHABET[remainder])
    return "".join(reversed(characters))
