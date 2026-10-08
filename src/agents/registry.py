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
from dataclasses import dataclass
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
    type: str
    label: str
    file_types: tuple[str, ...] = ()
    required: bool = True
    options: tuple[str, ...] = ()
    match: str = ""
    options_from: str = ""   # named dynamic option source (e.g. "itr_entities"),
                              # resolved by the UI layer; empty = use static `options`
    book_from: str = ""      # (file/files inputs only) name of another `select`
                              # input in this skill whose value is an entity key;
                              # the UI layer auto-fills this file field from that
                              # entity's registered GnuCash book. Empty = no
                              # auto-fill. On a `files` input the named select is
                              # expected to be `multiselect: true`, and every
                              # picked entity's book is filled in, one per line.
    multiselect: bool = False  # (select inputs only) let the dropdown hold more
                              # than one value; its value is then a list of keys
                              # rather than a single key. Used by the Inter-entity
                              # Matrix, which reconciles every pair among N books.
    depends_on: tuple[str, ...] = ()  # (select inputs with options_from only) names
                              # of other inputs whose value feeds the option
                              # resolver; the UI refreshes the choices when
                              # any of them changes.
    fy_from: str = ""        # (file inputs, optional) name of another input
                              # in this skill whose value is a bare FY string (e.g.
                              # "2025-26"), used together with book_from to pick the
                              # right book when an entity has more than one. Empty =
                              # resolve to the entity's newest registered FY.
                              # (output_file inputs with entity_from, UI-18) the
                              # picker also offers only workbooks of that FY.
    entity_from: str = ""    # (output_file inputs only, optional) name of the entity
                              # select whose value scopes this picker: only files that
                              # entity owns are offered (see agents/entity_scope.py),
                              # and the choices follow the select. Needs a `match` in
                              # entity_scope.SCOPABLE_MATCHES. Empty = unscoped.
    default: str = ""       # (text inputs only) value the form opens with


@dataclass(frozen=True)
class SkillExtraOutput:
    """A further file a run may write beside the main output (UI-24), e.g. a
    journal CSV. Declared in skill.yaml under output.extra_outputs; the skill
    reports which of them THIS run actually wrote (agents.outputs)."""
    key: str
    label: str = ""
    download_label: str = "Download"


@dataclass(frozen=True)
class SkillCheck:
    """An optional "check the inputs only" action beside Run (UI-23). Declared
    in skill.yaml under check:. `function` is a callable in the skill's entry
    point module taking one dict {input name: value} and returning Markdown;
    it must not write any output file."""
    function: str
    label: str = "Check my files"


@dataclass(frozen=True)
class SkillOutput:
    """Output configuration from skill.yaml."""
    extension: str = ".txt"
    suffix: str = "output"
    download_label: str = "Download"
    type: str = "file"
    extra_outputs: tuple = ()


@dataclass(frozen=True)
class SkillRequires:
    """External dependency declarations."""
    native_binaries: tuple[str, ...] = ()
    external_tools: tuple[str, ...] = ()
    llm: bool = True
    network: bool = False   # this skill makes outbound internet calls (surfaced
                              # as a distinct UI badge from the LLM badge)


@dataclass(frozen=True)
class SkillHelpInput:
    """Per-input help authored in the manifest's help: block."""
    name: str
    tooltip: str = ""
    accepts: str = ""
    gotchas: str = ""
    # UI-21: optional "What does this file look like?" panel. `sample_html`
    # is a hand-drawn, made-up illustration (never a real document image);
    # `looks_like` is the plain description; `not_these` lists look-alikes
    # that must NOT be picked; `filename_note` is a free-text reminder.
    sample_html: str = ""
    looks_like: str = ""
    not_these: tuple[str, ...] = ()
    filename_note: str = ""


@dataclass(frozen=True)
class SkillHelpOutputFile:
    """One output file/artifact described in the help: block."""
    name: str
    tooltip: str = ""


@dataclass(frozen=True)
class SkillHelpFix:
    """One troubleshooting entry (problem -> fix)."""
    problem: str
    fix: str = ""


@dataclass(frozen=True)
class SkillHelp:
    """
    Optional authored help — single source of truth for the user guide, the
    standalone HTML, and the in-app inline panel + Help tab + tooltips.
    File types, suffix, native deps and llm flag are read from existing
    manifest keys, never duplicated here.
    """
    overview: str = ""
    when_to_use: str = ""
    inputs: tuple[SkillHelpInput, ...] = ()
    steps: tuple[str, ...] = ()
    output_folder: str = "Data/outputs/"
    output_files: tuple[SkillHelpOutputFile, ...] = ()
    tips: str = ""
    troubleshooting: tuple[SkillHelpFix, ...] = ()

    def is_empty(self) -> bool:
        return not (self.overview or self.when_to_use or self.inputs or self.steps
                    or self.output_files or self.tips or self.troubleshooting)


@dataclass(frozen=True)
class SkillInfo:
    """Complete parsed manifest for one skill."""
    name: str
    display_name: str
    description: str
    category: str
    version: str
    mode: str
    entry_point: str
    inputs: tuple[SkillInput, ...]
    run_args: dict[str, str]
    output: SkillOutput
    requires: SkillRequires
    package: str
    manifest_path: Path
    help: SkillHelp | None = None
    check: SkillCheck | None = None


# ---------------------------------------------------------------------------
# Parsing.
# ---------------------------------------------------------------------------

def _parse_check(raw: Any) -> SkillCheck | None:
    if not isinstance(raw, dict) or not raw.get("function"):
        return None
    return SkillCheck(function=str(raw["function"]).strip(),
                      label=str(raw.get("label") or "Check my files").strip())


def _parse_help(raw: Any) -> SkillHelp | None:
    """Parse an optional help: block. Returns None when absent/empty."""
    if not isinstance(raw, dict):
        return None

    h_inputs = []
    for inp in raw.get("inputs") or []:
        if not isinstance(inp, dict) or "name" not in inp:
            continue
        h_inputs.append(SkillHelpInput(
            name=inp["name"],
            tooltip=(inp.get("tooltip") or "").strip(),
            accepts=(inp.get("accepts") or "").strip(),
            gotchas=(inp.get("gotchas") or "").strip(),
            sample_html=(inp.get("sample_html") or "").strip(),
            looks_like=(inp.get("looks_like") or "").strip(),
            not_these=tuple(
                str(x).strip() for x in (inp.get("not_these") or []) if str(x).strip()
            ),
            filename_note=(inp.get("filename_note") or "").strip(),
        ))

    out_raw = raw.get("outputs") or {}
    h_files = []
    for f in out_raw.get("files") or []:
        if not isinstance(f, dict) or "name" not in f:
            continue
        h_files.append(SkillHelpOutputFile(
            name=f["name"],
            tooltip=(f.get("tooltip") or "").strip(),
        ))

    h_fixes = []
    for t in raw.get("troubleshooting") or []:
        if not isinstance(t, dict) or "problem" not in t:
            continue
        h_fixes.append(SkillHelpFix(
            problem=str(t["problem"]).strip(),
            fix=(t.get("fix") or "").strip(),
        ))

    block = SkillHelp(
        overview=(raw.get("overview") or "").strip(),
        when_to_use=(raw.get("when_to_use") or "").strip(),
        inputs=tuple(h_inputs),
        steps=tuple(str(s).strip() for s in (raw.get("steps") or [])),
        output_folder=(out_raw.get("folder") or "Data/outputs/").strip(),
        output_files=tuple(h_files),
        tips=(raw.get("tips") or "").strip(),
        troubleshooting=tuple(h_fixes),
    )
    return None if block.is_empty() else block


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

    missing = [k for k in ("name", "display_name", "description", "mode", "entry_point") if k not in raw]
    if missing:
        log.debug("Skipping %s: missing required fields: %s", path, missing)
        return None

    inputs = []
    for inp in raw.get("inputs") or []:
        inputs.append(SkillInput(
            name=inp["name"],
            type=inp.get("type", "text"),
            label=inp.get("label", inp["name"]),
            file_types=tuple(inp.get("file_types") or ()),
            required=inp.get("required", True),
            options=tuple(inp.get("options") or ()),
            match=inp.get("match", ""),
            options_from=inp.get("options_from", ""),
            book_from=inp.get("book_from", ""),
            fy_from=inp.get("fy_from", ""),
            entity_from=inp.get("entity_from", ""),
            default=str(inp.get("default", "")),
            depends_on=tuple(inp.get("depends_on") or ()),
            multiselect=bool(inp.get("multiselect", False)),
        ))

    out_raw = raw.get("output") or {}
    output = SkillOutput(
        extension=out_raw.get("extension", ".txt"),
        suffix=out_raw.get("suffix", "output"),
        download_label=out_raw.get("download_label", "Download"),
        type=out_raw.get("type", "file"),
        extra_outputs=tuple(
            SkillExtraOutput(
                key=str(x["key"]),
                label=str(x.get("label", x["key"])),
                download_label=str(x.get("download_label", "Download")),
            )
            for x in (out_raw.get("extra_outputs") or ())
        ),
    )

    req_raw = raw.get("requires") or {}
    requires = SkillRequires(
        native_binaries=tuple(req_raw.get("native_binaries") or ()),
        external_tools=tuple(req_raw.get("external_tools") or ()),
        llm=bool(req_raw.get("llm", True)),
        network=bool(req_raw.get("network", False)),
    )

    help_block = _parse_help(raw.get("help"))

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
        help=help_block,
        check=_parse_check(raw.get("check")),
    )


# ---------------------------------------------------------------------------
# Discovery cache.
# ---------------------------------------------------------------------------

_cache: list[SkillInfo] | None = None


def discover(*, refresh: bool = False) -> list[SkillInfo]:
    """Scan agents/*/skill.yaml; cached, sorted by display_name."""
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


def discover_parser_scripts() -> list[Path]:
    """Return embedded parser scripts (parse_*.py / extract_*.py)."""
    if not _AGENTS_ROOT.is_dir():
        return []
    found: set[Path] = set()
    for scripts_dir in _AGENTS_ROOT.glob("*/scripts"):
        for pattern in ("parse_*.py", "extract_*.py"):
            found.update(scripts_dir.glob(pattern))
    return sorted(found)


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

def load_check_function(skill: SkillInfo) -> Callable[..., Any] | None:
    """The skill's declared check callable, or None when it declares none."""
    if skill.check is None:
        return None
    module_part, _ = skill.entry_point.split(":", 1)
    mod = importlib.import_module(f"{skill.package}.{module_part}")
    return getattr(mod, skill.check.function)


def load_run_function(skill: SkillInfo) -> Callable[..., Any]:
    """Import the skill's entry_point and return the callable."""
    module_part, func_name = skill.entry_point.split(":", 1)
    full_module = f"{skill.package}.{module_part}"
    mod = importlib.import_module(full_module)
    fn = getattr(mod, func_name)
    return fn
