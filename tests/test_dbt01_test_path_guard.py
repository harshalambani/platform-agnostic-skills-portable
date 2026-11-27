"""DBT-01 part 5: test files do not edit sys.path.

pyproject.toml's pytest `pythonpath` puts the repo's source folders on sys.path for
every test. A test file that adds its own entry again would bring back the hidden
order-dependence this setting removed (a module found only because an earlier test
happened to insert its folder). This file fails when:

  * a test file edits sys.path and is not on ALLOWED, or an ALLOWED file's count moves;
  * two folders on `pythonpath` provide the same top-level module name;
  * the pythonpath setting is gone, points at a missing folder, or CI overrides it;
  * the CI test step stops failing on pytest exit codes 1 to 5.

The scanner is checked in both directions below, so a scanner that stopped seeing
edits would fail here instead of silently passing.
"""
from __future__ import annotations

import ast
import re
import tomllib
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
TESTS = ROOT / "tests"

# file (relative to the repo root) -> (number of sys.path edits, reason).
# Only tests whose job is to exercise path handling belong here.
ALLOWED = {
    "tests/test_dbt01_paths_helper.py": (
        2, "tests agents/_paths.py itself: swaps sys.path for a private copy, then checks "
           "what the helper adds to it"),
    "tests/test_dbt01_no_stray_path_inserts.py": (
        1, "tests the production-code guard: one syspath_prepend('') proves the helper "
           "never resolves against the working directory (this file arrives with part 4)"),
    "tests/test_aud07_p5_key_in_memory.py": (
        1, "monkeypatch.syspath_prepend of the ITR scripts folder inside one test, which "
           "pytest undoes afterwards; the module is imported inside that test on purpose"),
}


def _sys_aliases(tree: ast.AST) -> set:
    """Every local name that is the sys module (import sys, import sys as x)."""
    names = {"sys"}
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == "sys":
                    names.add(a.asname or "sys")
    return names


def _is_sys_path(node: ast.AST, aliases: set) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id in aliases)


def path_edits(source: str) -> list:
    """Line numbers of every statement that changes sys.path, under any alias:
    insert/append/extend/remove/pop/clear calls, +=, = , slice assignment, and
    monkeypatch.setattr(sys, "path", ...) / monkeypatch.syspath_prepend(...)."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        tree = ast.parse(source.lstrip("﻿"))
    al = _sys_aliases(tree)
    hits = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
            f = n.func
            if f.attr in ("insert", "append", "extend", "remove", "pop", "clear") \
                    and _is_sys_path(f.value, al):
                hits.append(n.lineno)
            elif f.attr == "syspath_prepend":
                hits.append(n.lineno)
            elif f.attr == "setattr" and len(n.args) >= 2 \
                    and isinstance(n.args[0], ast.Name) and n.args[0].id in al \
                    and isinstance(n.args[1], ast.Constant) and n.args[1].value == "path":
                hits.append(n.lineno)
        elif isinstance(n, ast.AugAssign) and _is_sys_path(n.target, al):
            hits.append(n.lineno)
        elif isinstance(n, ast.Assign):
            for t in n.targets:
                if _is_sys_path(t, al) or (isinstance(t, ast.Subscript) and _is_sys_path(t.value, al)):
                    hits.append(n.lineno)
    return sorted(hits)


def _test_files():
    return sorted(p for p in TESTS.rglob("*.py") if "__pycache__" not in p.parts)


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


# ---- the guard -------------------------------------------------------------------

def test_no_test_file_edits_sys_path_outside_the_list():
    stray = {}
    for p in _test_files():
        rel = _rel(p)
        hits = path_edits(p.read_text(encoding="utf-8-sig"))
        if hits and rel not in ALLOWED:
            stray[rel] = hits
    assert not stray, (
        "These test files edit sys.path. Put the folder on `pythonpath` in pyproject.toml "
        "or fix the import instead: " + repr(stray))


def test_each_allowed_file_has_exactly_its_listed_edits():
    for rel, (count, reason) in ALLOWED.items():
        assert reason.strip(), f"{rel}: every allowed file needs a reason"
        p = ROOT / rel
        if not p.exists():
            continue    # a listed file that is not in this checkout yet (kept for the next part)
        assert len(path_edits(p.read_text(encoding="utf-8-sig"))) == count, (
            f"{rel}: expected {count} sys.path edit(s). If a test really needs more, "
            "change the count here with the reason.")


def test_every_listed_file_is_one_that_uses_it():
    # A stale entry would let a future edit in that file slip through.
    for rel, (count, _r) in ALLOWED.items():
        p = ROOT / rel
        if p.exists():
            assert path_edits(p.read_text(encoding="utf-8-sig")), f"{rel}: listed but has no edit; remove it"


# ---- the scanner itself (negative: the wrong thing must be seen) -------------------

@pytest.mark.parametrize("src", [
    "import sys\nsys.path.insert(0, 'x')\n",
    "import sys\nsys.path.append('x')\n",
    "import sys\nsys.path.extend(['x'])\n",
    "import sys as _s\n_s.path.insert(0, 'x')\n",
    "import sys\nsys.path += ['x']\n",
    "import sys\nsys.path = ['x']\n",
    "import sys\nsys.path[:0] = ['x']\n",
    "import sys\nfor p in ('a', 'b'):\n    if p not in sys.path:\n        sys.path.insert(0, p)\n",
    "def test_x(monkeypatch):\n    monkeypatch.syspath_prepend('x')\n",
    "import sys\ndef test_x(monkeypatch):\n    monkeypatch.setattr(sys, 'path', [])\n",
    "import sys\ndef f():\n    sys.path.remove('x')\n",
])
def test_the_scanner_sees_every_way_to_edit_sys_path(src):
    assert path_edits(src), src


@pytest.mark.parametrize("src", [
    "import sys\nprint(sys.path)\n",
    "import sys\nassert 'x' in sys.path\n",
    "import sys\n# sys.path.insert(0, 'x')\n",
    "s = \"import sys; sys.path.insert(0, 'x')\"\n",
    "import os\nos.path.insert = 1\nos.path.join('a')\n",
    "import sys\nx = list(sys.path)\n",
])
def test_the_scanner_ignores_reads_comments_strings_and_os_path(src):
    assert path_edits(src) == [], src


def test_a_new_insert_in_a_normal_test_would_fail_the_guard(tmp_path):
    f = tmp_path / "test_new.py"
    f.write_text("import sys\nsys.path.insert(0, '/x')\n\ndef test_a():\n    assert True\n",
                 encoding="utf-8")
    assert path_edits(f.read_text(encoding="utf-8")) == [2]
    assert "tests/test_new.py" not in ALLOWED


# ---- the pytest setting -----------------------------------------------------------

def _pytest_cfg() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["pytest"]["ini_options"]


def test_pythonpath_is_set_and_every_folder_exists():
    paths = _pytest_cfg().get("pythonpath")
    assert paths, "pytest `pythonpath` is missing from pyproject.toml"
    assert len(paths) == len(set(paths)), "a folder is listed twice"
    for rel in paths:
        assert not Path(rel).is_absolute() and ".." not in Path(rel).parts, rel
        assert (ROOT / rel).is_dir(), f"pythonpath folder does not exist: {rel}"


def test_the_source_trees_are_still_collected():
    cfg = _pytest_cfg()
    assert "tests" in cfg["testpaths"]
    assert "." in cfg["pythonpath"] and "src" in cfg["pythonpath"]


# ---- no two pythonpath folders provide the same module name ------------------------

# The one name two folders share: a `scripts` folder in the repo root and one inside
# skill_itr_workbook, both plain folders with no __init__. Nothing imports `scripts`
# by that bare name (checked below), so neither one can shadow the other.
SHARED_OK = {"scripts"}


def _top_level_names(folder: Path) -> set:
    names = set()
    for e in folder.iterdir():
        if e.name.startswith((".", "__")):
            continue
        if e.is_file() and e.suffix == ".py":
            names.add(e.stem)
        elif e.is_dir() and (e / "__init__.py").exists():
            names.add(e.name)
        # A folder with no __init__.py is only a namespace package, and a regular package
        # of the same name always wins over it (tests/skill_bob never hides agents/skill_bob).
    return names


def test_no_module_name_comes_from_two_pythonpath_folders():
    seen: dict = {}
    for rel in _pytest_cfg()["pythonpath"]:
        for name in _top_level_names(ROOT / rel):
            seen.setdefault(name, []).append(rel)
    clash = {n: v for n, v in seen.items() if len(v) > 1 and n not in SHARED_OK}
    assert not clash, f"two pythonpath folders provide the same module name: {clash}"


def test_nothing_imports_the_shared_name_bare():
    for p in _test_files() + sorted((ROOT / "src").rglob("*.py")) + sorted((ROOT / "ui").rglob("*.py")):
        if "__pycache__" in p.parts or p.name == Path(__file__).name:
            continue
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", SyntaxWarning)
            tree = ast.parse(p.read_text(encoding="utf-8-sig"))
        for n in ast.walk(tree):
            mods = []
            if isinstance(n, ast.Import):
                mods = [a.name for a in n.names]
            elif isinstance(n, ast.ImportFrom) and n.level == 0 and n.module:
                mods = [n.module]
            for m in mods:
                assert m.split(".")[0] not in SHARED_OK, f"{_rel(p)}:{n.lineno} imports {m!r}"


def test_the_clash_check_would_notice_a_clash(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    (a / "same.py").write_text("x = 1\n", encoding="utf-8")
    (b / "same.py").write_text("x = 2\n", encoding="utf-8")
    assert _top_level_names(a) & _top_level_names(b) == {"same"}


# ---- CI uses the same setting and tolerates nothing --------------------------------

def _ci_text() -> str:
    return (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")


def _test_step() -> str:
    """The text of the step that runs the main suite."""
    ci = _ci_text()
    i = ci.index("- name: Run tests (non-build-time-only)")
    j = ci.index("      - name:", i + 10)
    return ci[i:j]


def test_ci_does_not_set_its_own_path_or_rootdir():
    ci = _ci_text()
    step = _test_step()
    assert "python -m pytest ../tests/" in step
    for bad in ("PYTHONPATH", "--rootdir", "--import-mode"):
        assert bad not in ci, f"ci.yml overrides how tests find modules: {bad}"
    for bad in ("-c ", "-o ", "-p ", "--confcutdir"):
        assert bad not in step, f"the CI test step overrides how tests find modules: {bad}"


def test_ci_pytest_exit_codes_are_all_failures():
    ci = _test_step()
    m = re.search(r'case "\$rc" in(.*?)esac', ci, re.S)
    assert m, "the CI test step no longer maps pytest exit codes"
    body = m.group(1)
    assert re.search(r"^\s*0\)", body, re.M)
    assert "exit 1" in body and "1)" in body and "5)" in body and "*)" in body
    # every arm except the "0" one must exit 1
    arms = [a for a in re.split(r";;", body) if a.strip()]
    for arm in arms:
        if re.match(r"\s*0\)", arm):
            continue
        assert "exit 1" in arm, arm
    assert "continue-on-error" not in ci
    assert "pytest" not in "".join(l for l in ci.splitlines() if l.rstrip().endswith("|| true")), \
        "a pytest line ends in || true"
