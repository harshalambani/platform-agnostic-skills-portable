"""
tests/test_splash.py — Unit tests for ui/_splash.py (dismiss the PAL launcher
splash on window-ready instead of a fixed timer).

The real Win32 layer (EnumWindows, GetClassNameW, PostMessage, ...) is not
headless-testable and must never touch real windows in a test run, so every
test here monkeypatches _splash's thin per-call wrappers
(_enum_top_level_windows, _window_is_visible, _window_class_name,
_window_owner_pid, _process_image_name, _post_click) with synthetic fakes.
Nothing in this file launches the app or reads real windows/processes.

Run with:
    cd src && python -m pytest ../tests/test_splash.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ui import _splash  # noqa: E402


@pytest.fixture(autouse=True)
def _fast_search_budget(monkeypatch):
    """Shrink the retry budget so a "nothing found" test doesn't sit for the
    real ~2s production budget."""
    monkeypatch.setattr(_splash, "_SEARCH_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(_splash, "_POLL_INTERVAL_SECONDS", 0.01)


@pytest.fixture(autouse=True)
def _pretend_windows(monkeypatch):
    """Force the Windows gate open for every test by default.

    CI runs on Linux, where `dismiss_launcher_splash()` returns False before
    it looks at anything — which silently turns the monkeypatched-window
    tests below into tests of the platform gate and nothing else. Three of
    them assert False, so they would have gone green on Linux without ever
    exercising a line of the logic they name. Pin the platform instead, and
    let the one test that IS about the gate override this (it runs its own
    setattr afterwards, which wins).
    """
    monkeypatch.setattr(_splash.sys, "platform", "win32")


def test_noop_when_no_matching_window(monkeypatch):
    """No `_sp` window anywhere (source mode / splash disabled / no PAL):
    the function must no-op and report nothing was dismissed."""
    monkeypatch.setattr(_splash, "_enum_top_level_windows", lambda: [])
    posted = []
    monkeypatch.setattr(_splash, "_post_click", lambda hwnd: posted.append(hwnd))

    assert _splash.dismiss_launcher_splash() is False
    assert posted == []


def test_noop_on_non_windows(monkeypatch):
    """Gated to Windows only — must no-op immediately on any other platform,
    without even looking for windows."""
    monkeypatch.setattr(_splash.sys, "platform", "linux")
    called = []
    monkeypatch.setattr(
        _splash, "_enum_top_level_windows", lambda: called.append(1) or []
    )

    assert _splash.dismiss_launcher_splash() is False
    assert called == []


def test_internal_exception_is_swallowed(monkeypatch):
    """Any exception anywhere in the Win32 layer must never propagate out of
    dismiss_launcher_splash() — startup must never fail because of this."""
    def _boom():
        raise RuntimeError("simulated ctypes failure")

    monkeypatch.setattr(_splash, "_enum_top_level_windows", _boom)

    # Must not raise.
    assert _splash.dismiss_launcher_splash() is False


def test_click_posted_to_matching_window(monkeypatch):
    """A visible top-level `_sp` window owned by PASkillsPortable.exe gets a
    WM_LBUTTONDOWN + WM_LBUTTONUP click posted to it."""
    hwnd = 4242
    monkeypatch.setattr(_splash, "_enum_top_level_windows", lambda: [hwnd])
    monkeypatch.setattr(_splash, "_window_is_visible", lambda h: True)
    monkeypatch.setattr(_splash, "_window_class_name", lambda h: "_sp")
    monkeypatch.setattr(_splash, "_window_owner_pid", lambda h: 9999)
    monkeypatch.setattr(
        _splash, "_process_image_name", lambda pid: "PASkillsPortable.exe"
    )

    posted = []
    monkeypatch.setattr(_splash, "_post_click", lambda h: posted.append(h))

    assert _splash.dismiss_launcher_splash() is True
    assert posted == [hwnd]


def test_ignores_window_with_wrong_class_or_owner(monkeypatch):
    """A window that isn't `_sp`, or isn't owned by the launcher, must be
    left alone — no click posted, and the call reports nothing dismissed."""
    hwnd = 7
    monkeypatch.setattr(_splash, "_enum_top_level_windows", lambda: [hwnd])
    monkeypatch.setattr(_splash, "_window_is_visible", lambda h: True)
    monkeypatch.setattr(_splash, "_window_class_name", lambda h: "SomeOtherClass")
    monkeypatch.setattr(_splash, "_window_owner_pid", lambda h: 1234)
    monkeypatch.setattr(_splash, "_process_image_name", lambda pid: "notepad.exe")

    posted = []
    monkeypatch.setattr(_splash, "_post_click", lambda h: posted.append(h))

    assert _splash.dismiss_launcher_splash() is False
    assert posted == []
