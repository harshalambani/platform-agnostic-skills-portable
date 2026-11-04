"""
ui/_update.py — GitHub release version checker.

Checks the GitHub API for the latest release of the PA Skills Portable
repo and compares it to the running version. Used by the Home tab to
show an update banner when a newer version is available.

The check runs in a background thread to avoid blocking UI startup.
Results are cached for the lifetime of the process.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request

from . import _buildinfo


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

# GitHub owner/repo — override via config if needed.
_REPO = "harshalambani/platform-agnostic-skills-portable"
_API_URL = f"https://api.github.com/repos/{_REPO}/releases/latest"
_TIMEOUT = 5.0  # seconds


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------

def _parse_version(tag: str) -> tuple[int, ...]:
    """
    Parse a version tag like 'v0.4.1' or '0.4.1-rc1' into a tuple of ints.
    Pre-release suffixes are stripped — we only compare major.minor.patch.
    Returns (0, 0, 0) if parsing fails.
    """
    tag = tag.lstrip("v").strip()
    # Strip pre-release suffix (-rc1, -beta, etc.)
    tag = re.split(r"[-+]", tag, maxsplit=1)[0]
    try:
        return tuple(int(x) for x in tag.split("."))
    except (ValueError, AttributeError):
        return (0, 0, 0)


# ---------------------------------------------------------------------------
# Update check result
# ---------------------------------------------------------------------------

@dataclass
class UpdateInfo:
    available: bool = False        # True if a newer version exists
    latest_tag: str = ""           # e.g. "v0.5.0"
    current_tag: str = ""          # e.g. "0.4.1"
    download_url: str = ""         # URL to the release page
    asset_url: str = ""            # direct URL to the Windows portable asset, if found
    error: str = ""                # non-empty if the check failed
    checked: bool = False          # True once the check has completed


def _pick_windows_asset(assets: list[dict]) -> str:
    """Return the browser_download_url of the Windows portable asset, or ''.

    Matches an .exe/.zip asset case-insensitively; prefers one whose name
    also mentions 'win' or 'portable' when there's more than one candidate.
    Returns '' if nothing matches, or if the picked URL doesn't point at
    github.com (finding #12).
    """
    zips_and_exes = [
        a for a in assets
        if isinstance(a, dict) and a.get("name", "").lower().endswith((".exe", ".zip"))
    ]
    named = [
        a for a in zips_and_exes
        if re.search(r"win|portable", a.get("name", ""), re.IGNORECASE)
    ]
    pool = named or zips_and_exes
    if not pool:
        return ""
    url = pool[0].get("browser_download_url", "")
    if not url.startswith("https://github.com/"):
        return ""
    return url


# ---------------------------------------------------------------------------
# Cached singleton
# ---------------------------------------------------------------------------

_result = UpdateInfo()
_lock = threading.Lock()
_started = False


def _fetch() -> UpdateInfo:
    """Hit the GitHub API and build a fresh UpdateInfo. No shared state touched.

    SECURITY NOTE (finding #9): all exceptions (including TLS/network failures)
    are caught and returned as UpdateInfo.error — this must never raise, since
    callers include a daemon background thread that must never crash the app.
    """
    try:
        req = request.Request(
            _API_URL,
            headers={
                "User-Agent": "PA-Skills-Portable/update-check",
                "Accept": "application/vnd.github+json",
            },
        )
        with request.urlopen(req, timeout=_TIMEOUT) as resp:
            data: dict[str, Any] = json.loads(
                resp.read().decode("utf-8", errors="replace")
            )

        latest_tag = data.get("tag_name", "")
        html_url = data.get("html_url", "")

        # SECURITY (finding #12): validate the download URL points to GitHub.
        # Defence-in-depth: if the repo is compromised or the API response is
        # tampered, we don't want to render an arbitrary URL as a download link.
        if html_url and not html_url.startswith("https://github.com/"):
            html_url = f"https://github.com/{_REPO}/releases"

        asset_url = _pick_windows_asset(data.get("assets", []) or [])

        current = _parse_version(_buildinfo.VERSION)
        latest = _parse_version(latest_tag)

        return UpdateInfo(
            available=latest > current,
            latest_tag=latest_tag,
            current_tag=_buildinfo.VERSION,
            download_url=html_url,
            asset_url=asset_url,
            checked=True,
        )

    except Exception as e:  # noqa: BLE001 — intentional broad catch (finding #9)
        return UpdateInfo(
            error=f"{type(e).__name__}: {e}",
            current_tag=_buildinfo.VERSION,
            checked=True,
        )


def _do_check() -> None:
    """Run the GitHub API check and cache the result (called in a background thread)."""
    global _result
    info = _fetch()
    with _lock:
        _result = info


def check_now() -> UpdateInfo:
    """Run a fresh, synchronous check (respects the same timeout), update the
    cached singleton, and return the fresh result.

    Unlike start_check(), this is not idempotent — call it from a "Check for
    updates" button so the user gets a live result instead of whatever was
    cached at process startup.
    """
    global _result
    info = _fetch()
    with _lock:
        _result = info
    return info


def start_check() -> None:
    """
    Kick off the background update check (idempotent — only runs once).
    Call this early (e.g., during app construction) so the result is
    ready by the time the user looks at the Home tab.
    """
    global _started
    if _started:
        return
    _started = True
    t = threading.Thread(target=_do_check, daemon=True, name="update-check")
    t.start()


def get_result() -> UpdateInfo:
    """Return the current update-check result (may not be ready yet)."""
    with _lock:
        return _result


def format_banner() -> str:
    """
    Return a Markdown banner string for the Home tab.

    Returns an empty string if no update is available or if the check
    hasn't completed / errored silently.
    """
    info = get_result()
    if not info.checked:
        return ""
    if info.available:
        return (
            f"> **Update available:** {info.latest_tag} "
            f"(you have {info.current_tag}). "
            f"[Download →]({info.download_url})"
        )
    return ""
