"""Tests for log formatting helpers and download progress reporting."""

from __future__ import annotations

import pytest

from instadata.downloader.media import _label, _total_size
from instadata.utils.humanize import format_bytes, format_duration, format_rate


class TestFormatBytes:
    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (0, "0 B"),
            (512, "512 B"),
            (1023, "1023 B"),
            (1024, "1.0 KiB"),
            (1536, "1.5 KiB"),
            (1048576, "1.0 MiB"),
            (13_107_200, "12.5 MiB"),
            (1073741824, "1.0 GiB"),
            (1099511627776, "1.0 TiB"),
        ],
    )
    def test_scales_to_the_right_unit(self, count: int, expected: str) -> None:
        assert format_bytes(count) == expected

    def test_absurd_sizes_do_not_run_out_of_units(self) -> None:
        assert format_bytes(1024**7).endswith("PiB")

    def test_rate_appends_per_second(self) -> None:
        assert format_rate(1536) == "1.5 KiB/s"


class TestFormatDuration:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0.42, "0.4s"), (9.9, "9.9s"), (59.9, "59.9s"), (95, "1m35s"), (3725, "1h02m")],
    )
    def test_scales_to_the_right_unit(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected


class TestTotalSize:
    def test_content_length_is_added_to_the_resume_offset(self) -> None:
        assert _total_size({"Content-Length": "100"}, 0) == 100
        assert _total_size({"Content-Length": "100"}, 900) == 1000

    def test_content_range_wins_because_it_carries_the_true_total(self) -> None:
        # On a 206 the Content-Length is only the remaining slice; trusting it
        # would report a nearly-complete resume as 100% of a tiny whole.
        headers = {"Content-Range": "bytes 900-999/1000", "Content-Length": "100"}
        assert _total_size(headers, 900) == 1000

    def test_header_lookup_is_case_insensitive(self) -> None:
        assert _total_size({"content-length": "42"}, 0) == 42

    @pytest.mark.parametrize(
        "headers",
        [{}, {"Content-Length": "banana"}, {"Content-Range": "bytes */*"}],
    )
    def test_unknown_total_is_none(self, headers: dict[str, str]) -> None:
        assert _total_size(headers, 0) is None


class TestLabel:
    def test_part_suffix_is_stripped_for_log_lines(self) -> None:
        from pathlib import Path

        assert _label(Path("/x/20260731_ABC.jpg.part"), ".part") == "20260731_ABC.jpg"
