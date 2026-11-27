"""One place that knows where the source tree, the agents/ package and a skill's
flat scripts/ folder live, in a source checkout and in the packaged app.

Frozen builds ship agents/ as data under sys._MEIPASS (see agents/registry.py);
a source run resolves through this file's own location. Every ensure_* function
puts its folder on sys.path once (a second call changes nothing) and returns it.
"""
from __future__ import annotations

import sys
from pathlib import Path


def _frozen_root() -> Path | None:
    meipass = getattr(sys, "_MEIPASS", None)
    return Path(meipass) if meipass else None


def _on_path(folder: Path) -> Path:
    text = str(folder)
    if text not in sys.path:
        sys.path.insert(0, text)
    return folder


def src_dir() -> Path:
    """The folder that contains the `agents` package."""
    frozen = _frozen_root()
    return frozen if frozen is not None else Path(__file__).resolve().parent.parent


def agents_dir() -> Path:
    """The agents/ package folder itself."""
    frozen = _frozen_root()
    return frozen / "agents" if frozen is not None else Path(__file__).resolve().parent


def ensure_src() -> Path:
    return _on_path(src_dir())


def ensure_agents_dir() -> Path:
    return _on_path(agents_dir())


def ensure_scripts(skill: str) -> Path:
    """Put <agents>/<skill>/scripts on sys.path and return it. An unknown skill
    name (or one that has no scripts/ folder) raises, so a typo cannot quietly
    add a path that does not exist."""
    if (not isinstance(skill, str) or not skill
            or skill in (".", "..") or "/" in skill or "\\" in skill):
        raise ValueError(f"not a skill folder name: {skill!r}")
    scripts = agents_dir() / skill / "scripts"
    if not scripts.is_dir():
        raise FileNotFoundError(f"no scripts folder for skill {skill!r}: {scripts}")
    return _on_path(scripts)
