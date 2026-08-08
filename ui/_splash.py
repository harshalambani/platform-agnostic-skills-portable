"""
ui/_splash.py — dismiss the PortableApps.com Launcher splash the moment the
app's real window is ready, instead of letting it run out a fixed timer.

Why this exists
----------------
`SplashTime` in PASkillsPortable.ini (see bundling/templates/) is a TIMED
overlay: newadvsplash takes a duration and nothing tells it the app is ready,
and the window is topmost. A fixed duration necessarily encodes one machine's
hardware. Calibrate it against a fast laptop and it overshoots on a slow one,
parking a topmost, unresponsive-looking splash on top of an already-usable
window. Calibrate it against a slow one and it undershoots on a fast one,
clearing into a blank desktop before the app is there. There is no single
number that is right on every machine.

The fix is to stop trusting the timer and tell the splash directly, the
moment our own window is actually up (see webui.py's `window.events.shown`
wiring). That turns `SplashTime` from an estimate into a maximum: it only
needs to be long enough to cover the slowest cold start we've ever measured,
because on every faster run this module ends the splash early.

How dismissal actually works (measured against the real 3.5.1 build)
----------------------------------------------------------------------
The splash is a separate top-level window (class name `_sp`) owned by the
LAUNCHER process (PASkillsPortable.exe), not by this app's own process. Two
things were verified against the live window before writing this:

  * `PostMessage(hwnd, WM_CLOSE, ...)` is IGNORED. The window does not close.
  * `PostMessage(hwnd, WM_LBUTTONDOWN, ...)` followed by `WM_LBUTTONUP` DOES
    dismiss it (observed gone in ~0.18s), and the launcher process and app
    window are unaffected afterwards.

This isn't a coincidence: `bundling/launcher-gen/2.2.4/Other/Source/Segments/
SplashScreen.nsh` invokes the `newadvsplash` NSIS plugin WITHOUT `/NOCANCEL`,
and that plugin's documented default is "exit on user click" (see
`bundling/launcher-gen/2.2.4/App/NSIS/Docs/NewAdvSplash/newadvsplash.txt`).
So what we're doing here is synthesizing the click the plugin already listens
for — there is no private/undocumented API involved, just the ordinary
behaviour of a splash screen you're allowed to dismiss by clicking it.

`_sp` is an internal PAL/NSIS implementation detail, not a public contract.
If a future PortableApps.com Launcher version renames the class, adds
`/NOCANCEL`, or otherwise changes this behaviour, this module simply finds no
matching window, no-ops, and the splash falls back to running out
`SplashTime` in full — exactly today's (pre-this-change) behaviour. That is
why `SplashTime` must still be set generously (see the .ini template): it is
the safety net this module leans on whenever it can't do its job.

Failure handling
-----------------
Every code path in `dismiss_launcher_splash()` is best-effort. There is
nothing here worth failing startup over — at worst we lose the "dismiss
early" behaviour and get the old fixed-timer splash back. The function is
gated to Windows only, is wrapped so no exception can ever propagate out of
it, and caps its own search in time so it cannot hang startup waiting on
windows that don't exist (e.g. running from source, splash disabled in the
build, or the app launched directly rather than via the PAL launcher).

Testability
-----------
The individual Win32 calls below are split into small module-level
functions (`_enum_top_level_windows`, `_window_class_name`, ...) purely so
tests can monkeypatch each one with a synthetic fake instead of poking real
OS windows. None of them are meant to be called from outside this module.
"""

from __future__ import annotations

import sys
import time

# Win32 message constants (winuser.h). Defined locally so this module has no
# new runtime dependency beyond the standard library's ctypes.
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202

_LAUNCHER_EXE_NAME = "PASkillsPortable.exe"
_SPLASH_CLASS_NAME = "_sp"
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000

# Upper bound on how long we're willing to spend looking for the splash
# window. This must never become a place startup can stall: if the window
# isn't found almost immediately, it's very likely not there at all (source
# mode, splash disabled, or launched without PAL), so a short budget is both
# safe and sufficient for the case where it *is* there (EnumWindows is fast).
_SEARCH_BUDGET_SECONDS = 2.0
_POLL_INTERVAL_SECONDS = 0.1


def dismiss_launcher_splash() -> bool:
    """Best-effort: click through the PAL splash window if one is showing.

    Returns True if a matching splash window was found and a click was
    posted to it, False otherwise (including on any error). The return value
    exists for tests/logging only — callers must not branch on it, since a
    False result is the expected, harmless outcome on non-Windows platforms,
    when running from source, or whenever PAL isn't involved at all.
    """
    try:
        return _dismiss_launcher_splash_impl()
    except Exception:  # noqa: BLE001 — must never affect app startup
        return False


def _dismiss_launcher_splash_impl() -> bool:
    if not sys.platform.startswith("win"):
        return False

    deadline = time.monotonic() + _SEARCH_BUDGET_SECONDS
    while True:
        hwnds = _find_splash_hwnds()
        if hwnds:
            for hwnd in hwnds:
                # Synthesize the click the newadvsplash plugin's default
                # "exit on click" behaviour already listens for. WM_CLOSE is
                # ignored by this window (measured) so it is not tried here.
                _post_click(hwnd)
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(_POLL_INTERVAL_SECONDS)


def _find_splash_hwnds() -> list[int]:
    """Return hwnds of visible top-level `_sp` windows owned by the launcher."""
    found: list[int] = []
    for hwnd in _enum_top_level_windows():
        try:
            if not _window_is_visible(hwnd):
                continue
            if _window_class_name(hwnd) != _SPLASH_CLASS_NAME:
                continue
            pid = _window_owner_pid(hwnd)
            if _process_image_name(pid).lower() != _LAUNCHER_EXE_NAME.lower():
                continue
            found.append(hwnd)
        except Exception:  # noqa: BLE001 — one bad window must not abort the scan
            continue
    return found


# ---------------------------------------------------------------------------
# Thin Win32 wrappers. Each does exactly one ctypes call so tests can
# monkeypatch them individually without touching real windows.
# ---------------------------------------------------------------------------

def _enum_top_level_windows() -> list[int]:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    hwnds: list[int] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _enum_proc(hwnd, _lparam):
        hwnds.append(hwnd)
        return True

    ctypes.windll.user32.EnumWindows(_enum_proc, 0)
    return hwnds


def _window_is_visible(hwnd: int) -> bool:
    import ctypes  # noqa: PLC0415

    return bool(ctypes.windll.user32.IsWindowVisible(hwnd))


def _window_class_name(hwnd: int) -> str:
    import ctypes  # noqa: PLC0415

    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _window_owner_pid(hwnd: int) -> int:
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    pid = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def _process_image_name(pid: int) -> str:
    """Resolve the exe filename for *pid*, without a psutil dependency."""
    import ctypes  # noqa: PLC0415
    from ctypes import wintypes  # noqa: PLC0415

    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(260)
        size = wintypes.DWORD(len(buf))
        ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
        if not ok:
            return ""
        return buf.value.rsplit("\\", 1)[-1]
    finally:
        kernel32.CloseHandle(handle)


def _post_click(hwnd: int) -> None:
    import ctypes  # noqa: PLC0415

    ctypes.windll.user32.PostMessageW(hwnd, WM_LBUTTONDOWN, 1, 0)
    ctypes.windll.user32.PostMessageW(hwnd, WM_LBUTTONUP, 0, 0)
