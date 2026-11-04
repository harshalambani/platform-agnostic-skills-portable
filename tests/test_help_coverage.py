"""
tests/test_help_coverage.py — help documentation coverage + freshness.

Guards the single-source-of-truth help system:
  1. Every UI skill (one discovered by the registry) has a non-empty `help:`
     block in its skill.yaml.
  2. Each help input/output name matches a real manifest input / is described.
  3. `scripts/gen_docs.py --check` reports no stale generated docs — i.e. a
     `help:` block was edited but the docs were not regenerated.

These run in source mode and need no LLM, no native binaries, and no gradio.
"""
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import registry  # noqa: E402


@pytest.fixture(scope="module")
def skills():
    return registry.discover(refresh=True)


def test_every_ui_skill_has_help(skills):
    missing = [s.display_name for s in skills if not s.help or s.help.is_empty()]
    assert not missing, f"UI skills missing a help: block: {missing}"


def test_help_inputs_match_manifest_inputs(skills):
    problems = []
    for s in skills:
        if not s.help:
            continue
        input_names = {i.name for i in s.inputs}
        for hi in s.help.inputs:
            if hi.name not in input_names:
                problems.append(f"{s.display_name}: help input '{hi.name}' "
                                f"is not a declared input {sorted(input_names)}")
    assert not problems, "\n".join(problems)


def test_help_has_outputs_and_steps(skills):
    thin = []
    for s in skills:
        h = s.help
        if not h:
            continue
        if not h.overview:
            thin.append(f"{s.display_name}: no overview")
        if not h.steps:
            thin.append(f"{s.display_name}: no steps")
        if not h.output_files:
            thin.append(f"{s.display_name}: no output files described")
    assert not thin, "\n".join(thin)


def test_no_skill_ships_a_scaffold_marker(skills):
    """No REGISTERED skill's help block may still carry the Skill Scaffolder's
    placeholder marker -- that means someone shipped a scaffolded skill
    (agents.skill_scaffold) without filling in the generated placeholders.
    The marker legitimately appears only under src/agents/skill_scaffold/templates/
    and as a constant in test files (never in a registered skill.yaml)."""
    marker = "TODO(scaffold)"
    offenders = []
    for s in skills:
        h = s.help
        if not h:
            continue
        haystack = " ".join([
            s.description or "",
            h.overview, h.when_to_use, h.tips,
            " ".join(h.steps),
            " ".join(f"{i.tooltip} {i.accepts} {i.gotchas}" for i in h.inputs),
            " ".join(f.tooltip for f in h.output_files),
            " ".join(f"{t.problem} {t.fix}" for t in h.troubleshooting),
        ])
        if marker in haystack:
            offenders.append(s.display_name)
    assert not offenders, (
        f"skill(s) still ship the Skill Scaffolder's placeholder marker: {offenders} "
        "-- fill in the generated placeholders before this skill ships."
    )


def test_generated_docs_are_fresh():
    """gen_docs --check must pass; if this fails, run scripts/gen_docs.py."""
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "gen_docs.py"), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "Generated docs are stale — run `python scripts/gen_docs.py`.\n"
        + result.stdout + result.stderr
    )
