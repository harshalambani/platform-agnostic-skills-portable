"""
tests/test_skill_scaffold.py -- the Skill Scaffolder (agents.skill_scaffold).

All five tests operate against a tmp_path agents root via
`monkeypatch.setattr(registry, "_AGENTS_ROOT", tmp)` -- registry.discover()
reads that module global at call time, so this is enough to sandbox
discovery without ever touching the real src/agents/ tree. scaffold()
itself also takes agents_root/tests_root explicitly, so real src/agents/
and tests/ are never written to by this file -- see
test_src_agents_never_polluted below, which asserts that directly.

These run in source mode and need no LLM, no network, no gradio, no native
binaries.
"""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import registry  # noqa: E402
from agents.skill_scaffold import scaffold  # noqa: E402

# Snapshot of real src/agents/ contents, taken once at import time, so we can
# assert it is byte-for-byte unchanged after every test in this module runs.
_REAL_AGENTS_ROOT = SRC / "agents"
_REAL_TESTS_ROOT = PROJECT_ROOT / "tests"


def _spec(skill_dir="skill_widget_maker", display_name="Widget Maker"):
    return dict(
        skill_dir=skill_dir,
        display_name=display_name,
        category="dev",
        mode="direct",
        output_type="file",
        needs_llm=False,
    )


# ---------------------------------------------------------------------------
# a. Byte-stable rendering
# ---------------------------------------------------------------------------

def test_byte_stable_rendering(tmp_path):
    root_a = tmp_path / "root_a" / "agents"
    root_b = tmp_path / "root_b" / "agents"
    tests_a = tmp_path / "root_a" / "tests"
    tests_b = tmp_path / "root_b" / "tests"
    root_a.mkdir(parents=True)
    root_b.mkdir(parents=True)
    tests_a.mkdir(parents=True)
    tests_b.mkdir(parents=True)

    spec = _spec()
    result_a = scaffold.scaffold(agents_root=root_a, tests_root=tests_a, **spec)
    result_b = scaffold.scaffold(agents_root=root_b, tests_root=tests_b, **spec)

    assert result_a.ok, result_a.message
    assert result_b.ok, result_b.message
    assert len(result_a.created_files) == len(result_b.created_files)

    for file_a in result_a.created_files:
        rel = file_a.relative_to(root_a) if file_a.is_relative_to(root_a) else file_a.relative_to(tests_a)
        base_b = root_b if file_a.is_relative_to(root_a) else tests_b
        file_b = base_b / rel
        assert file_b.is_file(), f"missing counterpart for {rel}"
        assert file_a.read_bytes() == file_b.read_bytes(), f"byte mismatch for {rel}"


# ---------------------------------------------------------------------------
# b. registry.discover(refresh=True) finds the generated skill
# ---------------------------------------------------------------------------

def test_discover_finds_generated_skill(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    tests_root = tmp_path / "tests"
    agents_root.mkdir(parents=True)
    tests_root.mkdir(parents=True)

    result = scaffold.scaffold(agents_root=agents_root, tests_root=tests_root, **_spec())
    assert result.ok, result.message

    monkeypatch.setattr(registry, "_AGENTS_ROOT", agents_root)
    found = registry.discover(refresh=True)
    names = {s.name for s in found}
    assert "Widget Maker" in names


# ---------------------------------------------------------------------------
# c. Generated skill satisfies the three test_help_coverage assertions
# ---------------------------------------------------------------------------

def test_generated_skill_satisfies_help_coverage(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    tests_root = tmp_path / "tests"
    agents_root.mkdir(parents=True)
    tests_root.mkdir(parents=True)

    result = scaffold.scaffold(agents_root=agents_root, tests_root=tests_root, **_spec())
    assert result.ok, result.message

    monkeypatch.setattr(registry, "_AGENTS_ROOT", agents_root)
    found = registry.discover(refresh=True)
    skill = next(s for s in found if s.name == "Widget Maker")

    # mirrors test_every_ui_skill_has_help
    assert skill.help is not None and not skill.help.is_empty()

    # mirrors test_help_inputs_match_manifest_inputs
    input_names = {i.name for i in skill.inputs}
    for hi in skill.help.inputs:
        assert hi.name in input_names

    # mirrors test_help_has_outputs_and_steps
    assert skill.help.overview
    assert skill.help.steps
    assert skill.help.output_files


# ---------------------------------------------------------------------------
# d. TODO guard fires on a fresh scaffold
# ---------------------------------------------------------------------------

def test_marker_present_in_fresh_scaffold(tmp_path, monkeypatch):
    agents_root = tmp_path / "agents"
    tests_root = tmp_path / "tests"
    agents_root.mkdir(parents=True)
    tests_root.mkdir(parents=True)

    result = scaffold.scaffold(agents_root=agents_root, tests_root=tests_root, **_spec())
    assert result.ok, result.message

    monkeypatch.setattr(registry, "_AGENTS_ROOT", agents_root)
    found = registry.discover(refresh=True)
    skill = next(s for s in found if s.name == "Widget Maker")

    marker = "TODO(scaffold)"
    haystack = " ".join([
        skill.description or "",
        skill.help.overview, skill.help.when_to_use, skill.help.tips,
        " ".join(skill.help.steps),
        " ".join(f"{i.tooltip} {i.accepts} {i.gotchas}" for i in skill.help.inputs),
        " ".join(f.tooltip for f in skill.help.output_files),
        " ".join(f"{t.problem} {t.fix}" for t in skill.help.troubleshooting),
    ])
    assert marker in haystack, "a freshly scaffolded skill must still carry the placeholder marker"


# ---------------------------------------------------------------------------
# e. Refuses to overwrite / rejects invalid names
# ---------------------------------------------------------------------------

def test_refuses_existing_directory(tmp_path):
    agents_root = tmp_path / "agents"
    tests_root = tmp_path / "tests"
    agents_root.mkdir(parents=True)
    tests_root.mkdir(parents=True)

    spec = _spec()
    first = scaffold.scaffold(agents_root=agents_root, tests_root=tests_root, **spec)
    assert first.ok, first.message

    second = scaffold.scaffold(agents_root=agents_root, tests_root=tests_root, **spec)
    assert not second.ok
    assert "already exists" in second.message


@pytest.mark.parametrize("bad_name", [
    "",
    "widget_maker",           # missing skill_ prefix
    "skill_Widget",           # uppercase
    "skill_../etc",           # traversal
    "skill_foo/bar",          # path separator
    "C:\\skill_foo",          # drive letter / backslash
    "skill_..",               # traversal-shaped
])
def test_rejects_invalid_names(tmp_path, bad_name):
    agents_root = tmp_path / "agents"
    tests_root = tmp_path / "tests"
    agents_root.mkdir(parents=True)
    tests_root.mkdir(parents=True)

    spec = _spec(skill_dir=bad_name)
    result = scaffold.scaffold(agents_root=agents_root, tests_root=tests_root, **spec)
    assert not result.ok
    assert "Refused" in result.message


# ---------------------------------------------------------------------------
# Real src/agents/ and tests/ must never be touched by this module.
# ---------------------------------------------------------------------------

def test_src_agents_never_polluted():
    assert not (_REAL_AGENTS_ROOT / "skill_widget_maker").exists()
    assert not (_REAL_TESTS_ROOT / "test_skill_widget_maker.py").exists()
