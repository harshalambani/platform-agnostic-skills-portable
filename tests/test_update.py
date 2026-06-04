"""
tests/test_update.py — Unit tests for ui/_update.py (Phase 7, task 7-3).

Tests cover:
  - _parse_version() for various tag formats
  - Version comparison logic
  - format_banner() when update is/isn't available
  - Error handling in the check

Run with:
    cd src && python -m pytest ../tests/test_update.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ui._update import _parse_version, UpdateInfo, format_banner, _result


# ---------------------------------------------------------------------------
# _parse_version()
# ---------------------------------------------------------------------------

class TestParseVersion:
    def test_basic(self):
        assert _parse_version("0.4.1") == (0, 4, 1)

    def test_v_prefix(self):
        assert _parse_version("v0.4.1") == (0, 4, 1)

    def test_prerelease_stripped(self):
        assert _parse_version("v0.5.0-rc1") == (0, 5, 0)

    def test_build_metadata_stripped(self):
        assert _parse_version("0.5.0+abc123") == (0, 5, 0)

    def test_empty_string(self):
        assert _parse_version("") == (0, 0, 0)

    def test_garbage(self):
        assert _parse_version("not-a-version") == (0, 0, 0)

    def test_two_part(self):
        assert _parse_version("1.2") == (1, 2)

    def test_comparison(self):
        assert _parse_version("0.5.0") > _parse_version("0.4.1")
        assert _parse_version("1.0.0") > _parse_version("0.99.99")
        assert _parse_version("0.4.1") == _parse_version("v0.4.1")
        assert _parse_version("0.5.0-rc1") == _parse_version("0.5.0")


# ---------------------------------------------------------------------------
# format_banner()
# ---------------------------------------------------------------------------

class TestFormatBanner:
    def test_update_available(self):
        with patch("ui._update.get_result", return_value=UpdateInfo(
            available=True,
            latest_tag="v0.5.0",
            current_tag="0.4.1",
            download_url="https://github.com/x/releases/v0.5.0",
            checked=True,
        )):
            banner = format_banner()
            assert "v0.5.0" in banner
            assert "0.4.1" in banner
            assert "Download" in banner

    def test_no_update(self):
        with patch("ui._update.get_result", return_value=UpdateInfo(
            available=False,
            latest_tag="v0.4.1",
            current_tag="0.4.1",
            checked=True,
        )):
            assert format_banner() == ""

    def test_not_checked_yet(self):
        with patch("ui._update.get_result", return_value=UpdateInfo(checked=False)):
            assert format_banner() == ""

    def test_check_errored(self):
        with patch("ui._update.get_result", return_value=UpdateInfo(
            error="TimeoutError: timed out",
            checked=True,
        )):
            assert format_banner() == ""
