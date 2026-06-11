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
    error: str = ""                # non-empty if the check failed
    checked: bool = False          # True once the check has completed


# ---------------------------------------------------------------------------
# Cached singleton
# ---------------------------------------------------------------------------

_result = UpdateInfo()
_lock = threading.Lock()
_started = False


def _do_check() -> None:
    """Run the GitHub API check (called in a background thread)."""
    global _result
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

        current = _parse_version(_buildinfo.VERSION)
        latest = _parse_version(latest_tag)

        with _lock:
            _result = UpdateInfo(
                available=latest > current,
                latest_tag=latest_tag,
                current_tag=_buildinfo.VERSION,
                download_url=html_url,
                checked=True,
            )

    except Exception as e:  # noqa: BLE001 — intentional broad catch (finding #9)
        # SECURITY NOTE (finding #9): all exceptions (including TLS/network failures)
        # are stored in UpdateInfo.error, which is available via get_result().
        # The update check runs in a daemon thread and must never crash the app;
        # errors are surfaced to the caller, not silently dropped.
        with _lock:
            _result = UpdateInfo(
                error=f"{type(e).__name__}: {e}",
                current_tag=_buildinfo.VERSION,
                checked=True,
            )


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
