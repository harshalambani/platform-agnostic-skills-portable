"""
scaffold.py -- deterministic engine for the Skill Scaffolder.

No LLM, no network, no timestamps. Renders the template files under
templates/ by plain {{TOKEN}} string substitution and writes a brand-new
skill package under an agents root (normally src/agents/), plus a starter
test module under a tests root (normally tests/). Every prose field in the
generated output carries a fixed TODO marker (a placeholder that fails a
guard test, never plausible-sounding generated prose) -- see AGENT.md for
the rationale and tests/test_help_coverage.py for the guard.

scaffold() itself is a pure filesystem operation: it never shells out, never
touches the network, and never inspects sys.frozen. The frozen-build guard
and the scripts/gen_docs.py subprocess call both live in agent.py / __main__.py
(the two entry points), via the is_frozen()/run_gen_docs() helpers below --
that keeps this module's own unit tests hermetic (see tests/test_skill_scaffold.py).
"""
from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

_NAME_RE = re.compile(r"^skill_[a-z0-9_]+$")

_TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

# Project root: src/agents/skill_scaffold/scaffold.py -> skill_scaffold -> agents -> src -> project root
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
DEFAULT_TESTS_ROOT = PROJECT_ROOT / "tests"

CATEGORIES = (
    "dev", "utilities", "banks", "credit_card", "gnucash",
    "itr", "26as", "krc", "intercompany",
)
MODES = ("direct", "agent")
OUTPUT_TYPES = ("file", "directory")

FROZEN_MESSAGE = (
    "Error: the Skill Scaffolder is a source-checkout-only dev tool -- the "
    "shipped app's agents tree is read-only, so there is nowhere for it to "
    "write a new skill package. Run this from a source checkout instead."
)


def is_frozen() -> bool:
    """True under a PyInstaller-frozen build (sys.frozen / sys._MEIPASS)."""
    return bool(getattr(sys, "frozen", False)) or getattr(sys, "_MEIPASS", None) is not None


def run_gen_docs(project_root: Path) -> int:
    """Run scripts/gen_docs.py as a subprocess (sys.executable); return its exit code."""
    result = subprocess.run(
        [sys.executable, str(Path(project_root) / "scripts" / "gen_docs.py")],
        cwd=str(project_root),
        capture_output=True,
        text=True,
    )
    return result.returncode


@dataclass(frozen=True)
class ScaffoldResult:
    ok: bool
    message: str
    created_files: tuple[Path, ...] = ()


def _entry_func(mode: str) -> str:
    return "run_ui" if mode == "agent" else "run"


def _output_suffix(skill_dir: str) -> str:
    stem = skill_dir[len("skill_"):] if skill_dir.startswith("skill_") else skill_dir
    return stem.replace("_", "-") or "output"


def validate_skill_dir(skill_dir: str) -> str | None:
    """Return an error string if skill_dir is not a safe, valid package name; else None."""
    if not skill_dir or not isinstance(skill_dir, str):
        return "skill_dir is required."
    if "/" in skill_dir or "\\" in skill_dir or ".." in skill_dir:
        return f"skill_dir {skill_dir!r} must not contain path separators or '..'."
    if re.match(r"^[a-zA-Z]:", skill_dir):
        return f"skill_dir {skill_dir!r} must not include a drive letter."
    if not _NAME_RE.match(skill_dir):
        return (
            f"skill_dir {skill_dir!r} is invalid -- must match ^skill_[a-z0-9_]+$ "
            "(lowercase ascii, starts with 'skill_')."
        )
    return None


def _render(text: str, tokens: dict[str, str]) -> str:
    for key, value in tokens.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def scaffold(
    *,
    skill_dir: str,
    display_name: str,
    category: str,
    mode: str,
    output_type: str,
    needs_llm: bool,
    agents_root: Path,
    tests_root: Path = DEFAULT_TESTS_ROOT,
) -> ScaffoldResult:
    """
    Deterministically scaffold a new skill package at agents_root/skill_dir,
    plus a starter test module at tests_root/test_<skill_dir>.py.

    Refuses (ok=False, no filesystem writes at all) when: skill_dir fails
    validate_skill_dir(), category/mode/output_type are not one of the
    allowed values, display_name is blank, the resolved target escapes
    agents_root, or either the target skill directory or the test module
    already exists. Never overwrites, never merges, never deletes.

    Rendering is byte-stable: the same arguments always produce byte-identical
    files (no timestamps, no dict-iteration-order dependence -- see
    tests/test_skill_scaffold.py's byte-stability test).
    """
    err = validate_skill_dir(skill_dir)
    if err:
        return ScaffoldResult(ok=False, message=f"Refused: {err}")

    if category not in CATEGORIES:
        return ScaffoldResult(ok=False, message=f"Refused: category must be one of {CATEGORIES}.")
    if mode not in MODES:
        return ScaffoldResult(ok=False, message=f"Refused: mode must be one of {MODES}.")
    if output_type not in OUTPUT_TYPES:
        return ScaffoldResult(ok=False, message=f"Refused: output_type must be one of {OUTPUT_TYPES}.")
    if not display_name or not display_name.strip():
        return ScaffoldResult(ok=False, message="Refused: display_name is required.")

    agents_root = Path(agents_root).resolve()
    target = (agents_root / skill_dir).resolve()
    try:
        target.relative_to(agents_root)
    except ValueError:
        return ScaffoldResult(ok=False, message=f"Refused: target path {target} escapes {agents_root}.")

    if target.exists():
        return ScaffoldResult(
            ok=False,
            message=(
                f"Refused: {target} already exists -- the scaffolder never "
                "overwrites or merges into an existing skill directory."
            ),
        )

    tests_root = Path(tests_root).resolve()
    test_dest = tests_root / f"test_{skill_dir}.py"
    if test_dest.exists():
        return ScaffoldResult(
            ok=False,
            message=f"Refused: {test_dest} already exists -- the scaffolder never overwrites an existing test module.",
        )

    entry_func = _entry_func(mode)
    tokens = {
        "SKILL_DIR": skill_dir,
        "NAME": display_name.strip(),
        "DISPLAY_NAME": display_name.strip(),
        "CATEGORY": category,
        "MODE": mode,
        "ENTRY_FUNC": entry_func,
        "ENTRY_POINT": f"agent:{entry_func}",
        "OUTPUT_TYPE": output_type,
        "OUTPUT_SUFFIX": _output_suffix(skill_dir),
        "OUTPUT_EXTENSION": ".txt" if output_type == "file" else "",
        "OUTPUT_IS_DIR": "True" if output_type == "directory" else "False",
        "NEEDS_LLM": "true" if needs_llm else "false",
    }

    plan = [
        (_TEMPLATES_DIR / "__init__.py.tmpl", target / "__init__.py"),
        (_TEMPLATES_DIR / "skill.yaml.tmpl", target / "skill.yaml"),
        (_TEMPLATES_DIR / "agent.py.tmpl", target / "agent.py"),
        (_TEMPLATES_DIR / "AGENT.md.tmpl", target / "AGENT.md"),
        (_TEMPLATES_DIR / "test_module.py.tmpl", test_dest),
    ]

    for tmpl_path, _dest in plan:
        if not tmpl_path.is_file():
            return ScaffoldResult(ok=False, message=f"Refused: missing template {tmpl_path}.")

    target.mkdir(parents=True, exist_ok=False)
    created: list[Path] = []
    try:
        for tmpl_path, dest_path in plan:
            rendered = _render(tmpl_path.read_text(encoding="utf-8"), tokens)
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            dest_path.write_text(rendered, encoding="utf-8", newline="\n")
            created.append(dest_path)
    except OSError as e:
        return ScaffoldResult(
            ok=False,
            message=f"Error while writing files: {e}",
            created_files=tuple(created),
        )

    message = (
        f"Scaffolded '{display_name}' at {target} ({len(created)} file(s)) "
        f"plus a starter test at {test_dest}."
    )
    return ScaffoldResult(ok=True, message=message, created_files=tuple(created))
