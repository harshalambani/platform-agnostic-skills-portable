"""
agents/registry.py — skill auto-discovery registry.

Scans agents/*/skill.yaml at import time, validates each manifest, and
exposes the results via discover(). The UI layer reads this to build
tabs dynamically instead of hard-coding imports per skill.

Public surface:
    SkillInfo          — frozen dataclass with all manifest fields.
    discover()         — returns a list of SkillInfo sorted by display_name.
    get(name)          — returns a single SkillInfo by name, or None.
    load_run_function(skill) — lazily imports and returns the skill's run().
"""
from __future__ import annotations

import importlib
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import yaml

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Locate the agents/ root — works in both source and frozen mode.
# ---------------------------------------------------------------------------
_MEIPASS = getattr(sys, "_MEIPASS", None)
if _MEIPASS:
    _AGENTS_ROOT = Path(_MEIPASS) / "agents"
else:
    _AGENTS_ROOT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# Data model.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SkillInput:
    """One input field declared in skill.yaml."""
    name: str
    type: str           # "file", "files", "directory", "text", "select"
    label: str
    file_types: tuple[str, ...] = ()
    required: bool = True
    options: tuple[str, ...] = ()   # choices for type="select"


@dataclass(frozen=True)
class SkillOutput:
    """Output configuration from skill.yaml."""
    extension: str = ".txt"
    suffix: str = "output"
    download_label: str = "Download"
    type: str = "file"          # "file" or "directory"


@dataclass(frozen=True)
class SkillRequires:
    """External dependency declarations."""
    native_binaries: tuple[str, ...] = ()   # e.g. ("tesseract", "poppler")
    external_tools: tuple[str, ...] = ()    # e.g. ("qpdf",)


@dataclass(frozen=True)
class SkillInfo:
    """Complete parsed manifest for one skill."""
    name: str
    display_name: str
    description: str
    category: str
    version: str
    mode: str                   # "agent" or "direct"
    entry_point: str            # e.g. "agent:run"
    inputs: tuple[SkillInput, ...]
    run_args: dict[str, str]
    output: SkillOutput
    requires: SkillRequires
    package: str                # e.g. "agents.skill_26as"
    manifest_path: Path         # absolute path to skill.yaml


# ---------------------------------------------------------------------------
# Parsing.
# ---------------------------------------------------------------------------

def _parse_manifest(path: Path) -> SkillInfo | None:
    """Parse a single skill.yaml. Returns None on validation failure."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Skipping %s: YAML parse error: %s", path, e)
        return None

    if not isinstance(raw, dict):
        log.warning("Skipping %s: top-level is not a mapping", path)
        return None

    # Required fields.
    missing = [k for k in ("name", "display_name", "description", "mode", "entry_point") if k not in raw]
    if missing:
        log.warning("Skipping %s: missing required fields: %s", path, missing)
        return None

    # Parse inputs.
    inputs = []
    for inp in raw.get("inputs") or []:
        inputs.append(SkillInput(
            name=inp["name"],
            type=inp.get("type", "text"),
            label=inp.get("label", inp["name"]),
            file_types=tuple(inp.get("file_types") or ()),
            required=inp.get("required", True),
            options=tuple(inp.get("options") or ()),
        ))

    # Parse output.
    out_raw = raw.get("output") or {}
    output = SkillOutput(
        extension=out_raw.get("extension", ".txt"),
        suffix=out_raw.get("suffix", "output"),
        download_label=out_raw.get("download_label", "Download"),
        type=out_raw.get("type", "file"),
    )

    # Parse requires.
    req_raw = raw.get("requires") or {}
    requires = SkillRequires(
        native_binaries=tuple(req_raw.get("native_binaries") or ()),
        external_tools=tuple(req_raw.get("external_tools") or ()),
    )

    # Derive package name from directory.
    skill_dir = path.parent
    package = f"agents.{skill_dir.name}"

    return SkillInfo(
        name=raw["name"],
        display_name=raw["display_name"],
        description=raw["description"],
        category=raw.get("category", "general"),
        version=raw.get("version", "0.0.0"),
        mode=raw["mode"],
        entry_point=raw["entry_point"],
        inputs=tuple(inputs),
        run_args=raw.get("run_args") or {},
        output=output,
        requires=requires,
        package=package,
        manifest_path=path,
    )


# ---------------------------------------------------------------------------
# Discovery cache.
# ---------------------------------------------------------------------------

_cache: list[SkillInfo] | None = None


def discover(*, refresh: bool = False) -> list[SkillInfo]:
    """
    Scan agents/*/skill.yaml and return all valid skills, sorted by
    display_name. Results are cached; pass refresh=True to re-scan.
    """
    global _cache
    if _cache is not None and not refresh:
        return list(_cache)

    skills: list[SkillInfo] = []
    if not _AGENTS_ROOT.is_dir():
        log.warning("Agents root not found: %s", _AGENTS_ROOT)
        _cache = skills
        return list(_cache)

    for skill_dir in sorted(_AGENTS_ROOT.iterdir()):
        manifest = skill_dir / "skill.yaml"
        if not manifest.is_file():
            continue
        info = _parse_manifest(manifest)
        if info is not None:
            skills.append(info)

    skills.sort(key=lambda s: s.display_name)
    _cache = skills
    log.info("Discovered %d skill(s): %s", len(skills), [s.name for s in skills])
    return list(_cache)


def get(name: str) -> SkillInfo | None:
    """Look up a skill by name (case-insensitive)."""
    name_lower = name.lower()
    for skill in discover():
        if skill.name.lower() == name_lower:
            return skill
    return None


# ---------------------------------------------------------------------------
# Lazy import of the skill's run() function.
# ---------------------------------------------------------------------------

def load_run_function(skill: SkillInfo) -> Callable[..., Any]:
    """
    Import the skill's entry_point and return the callable.

    entry_point is "module:function", e.g. "agent:run" resolves to
    agents.skill_<name>.agent.run().
    """
    module_part, func_name = skill.entry_point.split(":", 1)
    full_module = f"{skill.package}.{module_part}"
    mod = importlib.import_module(full_module)
    fn = getattr(mod, func_name)
    return fn
