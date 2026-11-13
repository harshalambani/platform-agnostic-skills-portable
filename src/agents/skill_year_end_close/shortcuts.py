"""
shortcuts.py -- the only place a Windows .lnk is created or read.

Kept to two tiny functions so tests can stub them (the agent always calls
them through this module). Paths travel in environment variables, never
spliced into the PowerShell text, so a path cannot change what runs.
"""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

_CREATE = (
    "$s = (New-Object -ComObject WScript.Shell).CreateShortcut($env:YE_LNK); "
    "$s.TargetPath = $env:YE_TARGET; $s.Save()"
)
_READ = "(New-Object -ComObject WScript.Shell).CreateShortcut($env:YE_LNK).TargetPath"


def _ps(script: str, lnk: str, target: str = "") -> str:
    env = dict(os.environ, YE_LNK=str(lnk), YE_TARGET=str(target))
    done = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, env=env, timeout=60,
    )
    if done.returncode != 0:
        raise OSError((done.stderr or done.stdout or "shortcut command failed").strip())
    return (done.stdout or "").strip()


def create_shortcut(lnk_path: str | Path, target_path: str | Path) -> None:
    """Create a new .lnk at lnk_path pointing at target_path. The caller has
    already checked that lnk_path does not exist."""
    _ps(_CREATE, str(lnk_path), str(target_path))


def read_shortcut_target(lnk_path: str | Path) -> str:
    """The target a .lnk points at ("" when it has none)."""
    return _ps(_READ, str(lnk_path))
