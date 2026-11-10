"""
tests/test_readme_accuracy.py -- the README stays true to the repository.

The skill table is generated from the manifests (scripts/gen_docs.py), and the
README's other factual claims (files, commands, entry points) are checked here
so a rename or removal cannot leave the README describing something that is gone.
"""
import importlib.util
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import registry  # noqa: E402


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("gen_docs_under_test", ROOT / "scripts" / "gen_docs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def readme():
    return (ROOT / "README.md").read_text(encoding="utf-8")


def _table_rows(text):
    a = text.index("<!-- SKILLS-TABLE:START")
    b = text.index("<!-- SKILLS-TABLE:END -->")
    rows = [ln for ln in text[a:b].splitlines() if ln.startswith("| ")][1:]
    return [[c.strip() for c in r.strip("|").split("|")] for r in rows]


def test_readme_skill_table_is_current(gen):
    """gen_docs --check covers the README: the committed table equals a fresh one."""
    out = gen.build()
    assert gen.README in out
    assert (ROOT / "README.md").read_text(encoding="utf-8") == out[gen.README]


def _key(gen, skill):
    return (gen._GROUP_LABELS.get(skill.category, skill.category), skill.display_name)


def test_table_lists_every_ui_skill_and_nothing_else(gen, readme):
    skills = registry.discover(refresh=True)
    expected = sorted(_key(gen, s) for s in skills)
    listed = sorted((r[0], r[1]) for r in _table_rows(readme))
    assert listed == expected
    m = re.search(r"(\d+) skills appear in the app", readme)
    assert m and int(m.group(1)) == len(skills)


def test_table_llm_column_matches_manifests(gen, readme):
    by_key = {_key(gen, s): s for s in registry.discover(refresh=True)}
    for group, name, llm, _what in _table_rows(readme):
        assert (llm == "Yes") == bool(by_key[(group, name)].requires.llm), (group, name)


def test_stale_table_is_detected(gen, readme):
    """Negative proof: a hand-edited or outdated table is not equal to a fresh one,
    and a README without markers is refused instead of silently losing its table."""
    skills = registry.discover(refresh=True)
    views = [gen.skill_view(s) for s in skills]
    fresh = gen.render_readme(readme, views, gen.internal_steps())
    assert fresh == readme
    tampered = readme.replace("| Kotak |", "| Kotak (renamed) |")
    assert gen.render_readme(tampered, views, gen.internal_steps()) != tampered
    with pytest.raises(SystemExit):
        gen.render_readme("# no markers here", views, gen.internal_steps())


def test_readme_keeps_the_licence_and_notice_pointers(readme):
    assert "`LICENSE`" in readme and "`NOTICE`" in readme
    assert (ROOT / "LICENSE").exists() and (ROOT / "NOTICE").exists()


def test_readme_names_only_files_and_commands_that_exist(readme):
    # Relative links resolve.
    for target in re.findall(r"\]\(((?!http)[^)#]+)\)", readme):
        assert (ROOT / target).exists(), f"README links to missing {target}"
    # Paths in code spans resolve (skipping placeholders and generated or optional locations).
    for path in ("scripts/gen_docs.py", "bundling/build.py", ".github/workflows/release.yml",
                 "ui/webui.py",
                 "src/agents/registry.py", "src/agents/base_agent.py"):
        assert (ROOT / path).exists(), path
    assert "PASkillsPortable" in readme
    assert "PASkillsPortable.exe" in (ROOT / "bundling" / "build.py").read_text(encoding="utf-8")
    assert "python -m ui.webui" in readme
    assert (ROOT / "RunApp.bat").read_text(encoding="utf-8").count("python -m ui.webui") == 1


def test_readme_python_requirement_matches_pyproject(readme):
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    m = re.search(r'requires-python\s*=\s*"[^0-9]*([0-9.]+)"', pyproject)
    assert m and f"Python {m.group(1)} or newer" in readme


def test_readme_ci_test_command_matches_ci(readme):
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert r"python -m pytest ..\tests\ -q" in readme
    assert "python -m pytest ../tests/" in ci and "requirements-lock" in ci
    assert "from the dependency lock" in readme


def test_readme_has_no_stale_claims(readme):
    """Negative: wording that was once true and no longer is must not come back."""
    stale = [
        "16 skills", "6 internal pipeline steps", "11-step", "~160 tests", "Currently ~",
        "HSBC and ICICI are next in line", "mirrored from upstream",
        "Creates a GitHub Release with the zip attached",
    ]
    for phrase in stale:
        assert phrase not in readme, phrase


def test_readme_has_no_control_characters(readme):
    """Negative: a stray tab or backspace (an unescaped backslash path such as
    ..\tests or bundling\build.py written into a normal string) corrupts a command."""
    bad = [(i, repr(ch)) for i, ch in enumerate(readme) if ord(ch) < 32 and ch not in "\n"]
    assert not bad, bad[:5]
