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

from ui import _update
from ui._update import _parse_version, _pick_windows_asset, UpdateInfo, format_banner, check_now, _result


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


# ---------------------------------------------------------------------------
# _pick_windows_asset()
# ---------------------------------------------------------------------------

class TestPickWindowsAsset:
    def test_matches_win_named_exe(self):
        assets = [
            {"name": "PASkillsPortable_0.5.0_win.exe",
             "browser_download_url": "https://github.com/x/y/releases/download/v0.5.0/win.exe"},
            {"name": "source.tar.gz",
             "browser_download_url": "https://github.com/x/y/archive/v0.5.0.tar.gz"},
        ]
        assert _pick_windows_asset(assets) == (
            "https://github.com/x/y/releases/download/v0.5.0/win.exe"
        )

    def test_matches_portable_named_zip(self):
        assets = [
            {"name": "PASkillsPortable_0.5.0.zip",
             "browser_download_url": "https://github.com/x/y/releases/download/v0.5.0/portable.zip"},
        ]
        assert _pick_windows_asset(assets) != ""

    def test_no_match_returns_empty(self):
        assets = [
            {"name": "source.tar.gz",
             "browser_download_url": "https://github.com/x/y/archive/v0.5.0.tar.gz"},
            {"name": "checksums.txt",
             "browser_download_url": "https://github.com/x/y/releases/download/v0.5.0/checksums.txt"},
        ]
        assert _pick_windows_asset(assets) == ""

    def test_empty_assets(self):
        assert _pick_windows_asset([]) == ""

    def test_tampered_url_rejected(self):
        assets = [
            {"name": "app_win.exe",
             "browser_download_url": "https://evil.example.com/app_win.exe"},
        ]
        assert _pick_windows_asset(assets) == ""


# ---------------------------------------------------------------------------
# check_now()
# ---------------------------------------------------------------------------

class _FakeResponse:
    def __init__(self, payload: bytes):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _fake_release_json(*, tag_name="v0.5.0", assets=None, html_url=None):
    import json as _json
    return _json.dumps({
        "tag_name": tag_name,
        "html_url": html_url if html_url is not None else f"https://github.com/x/y/releases/tag/{tag_name}",
        "assets": assets or [],
    }).encode("utf-8")


class TestCheckNow:
    def setup_method(self):
        # Reset the cached singleton so tests don't leak state into each other.
        _update._result = UpdateInfo()

    def test_newer_version_available(self):
        payload = _fake_release_json(
            tag_name="v99.0.0",
            assets=[{
                "name": "app_win.exe",
                "browser_download_url": "https://github.com/x/y/releases/download/v99.0.0/app_win.exe",
            }],
        )
        with patch("ui._update.request.urlopen", return_value=_FakeResponse(payload)):
            info = check_now()
        assert info.checked
        assert info.available
        assert info.latest_tag == "v99.0.0"
        assert info.asset_url == "https://github.com/x/y/releases/download/v99.0.0/app_win.exe"
        # Cached singleton reflects the fresh result.
        assert _update.get_result() == info

    def test_same_version_not_available(self):
        payload = _fake_release_json(tag_name=f"v{_update._buildinfo.VERSION.split('+')[0]}")
        with patch("ui._update.request.urlopen", return_value=_FakeResponse(payload)):
            info = check_now()
        assert info.checked
        assert not info.available

    def test_no_matching_asset_leaves_asset_url_empty(self):
        payload = _fake_release_json(tag_name="v99.0.0", assets=[
            {"name": "source.tar.gz", "browser_download_url": "https://github.com/x/y/archive/v99.0.0.tar.gz"},
        ])
        with patch("ui._update.request.urlopen", return_value=_FakeResponse(payload)):
            info = check_now()
        assert info.asset_url == ""

    def test_error_case_returns_safely(self):
        with patch("ui._update.request.urlopen", side_effect=OSError("network down")):
            info = check_now()
        assert info.checked
        assert info.error
        assert not info.available

    def test_tampered_html_url_falls_back(self):
        payload = _fake_release_json(tag_name="v99.0.0", html_url="https://evil.example.com/fake")
        with patch("ui._update.request.urlopen", return_value=_FakeResponse(payload)):
            info = check_now()
        assert info.download_url.startswith("https://github.com/")

    def test_tampered_asset_url_falls_back_empty(self):
        payload = _fake_release_json(tag_name="v99.0.0", assets=[
            {"name": "app_win.exe", "browser_download_url": "https://evil.example.com/app_win.exe"},
        ])
        with patch("ui._update.request.urlopen", return_value=_FakeResponse(payload)):
            info = check_now()
        assert info.asset_url == ""
