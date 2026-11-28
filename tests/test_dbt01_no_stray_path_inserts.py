"""DBT-01 PR 4: production code reaches sys.path through agents/_paths.py only.

Every remaining direct edit of sys.path in production code is listed below with the
reason it cannot use the helper (it runs before `agents` can be imported, or as a
stand-alone subprocess). A new insert/append anywhere else fails this test, and so
does a listed file whose count changes, so the list cannot go stale. Also checks that
the helper never resolves a folder against the current working directory.
"""
from __future__ import annotations

import ast
import os
import sys
import warnings
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents import _paths  # noqa: E402

# Subprocess scripts: run as `python scripts/x.py`, so `agents` is not importable
# until their own bootstrap line has put src/ (or their own folder) on sys.path.
_SUBPROCESS = "stand-alone script run as a subprocess; the bootstrap runs before `agents` can be imported"
# A flat module in a skill's scripts/ folder that is also run directly (has __main__).
_FLAT_MAIN = "flat module in a scripts/ folder that is also run directly; cannot import `agents` first"
# The fallback branch of `try: from agents... except ImportError`.
_STANDALONE_FALLBACK = "stand-alone fallback taken only when `agents` is not importable"

ALLOWED: dict[str, tuple[int, str]] = {
    "bundling/rthook_agents.py": (2, "PyInstaller runtime hook (moves _MEIPASS to the front); runs before any import of the app"),
    "scripts/gen_docs.py": (1, "developer script run from the repo root; makes `agents` importable"),
    "src/run_4c_e2e.py": (1, "developer end-to-end script; makes `agents` importable"),
    "ui/webui.py": (3, "app entry point: puts the project root and src/ on the path so `ui` and `agents` can be imported at all"),
    "ui/_firstwindow.py": (1, "start-up window loader; runs before `agents` is imported"),
    "src/agents/skill_26as/scripts/extract_26as_to_xlsx.py": (1, _SUBPROCESS),
    "src/agents/skill_bob/scripts/extract_bob_statement.py": (1, _SUBPROCESS),
    "src/agents/skill_cc_sort/scripts/extract_sort_cc_pdfs.py": (1, _SUBPROCESS),
    "src/agents/skill_cc_transactions/scripts/create_cc_transaction_list.py": (1, _SUBPROCESS),
    "src/agents/skill_hsbc/scripts/ocr_to_tsv.py": (1, _SUBPROCESS),
    "src/agents/skill_hsbc/scripts/parse_tsv.py": (1, _SUBPROCESS),
    "src/agents/skill_kotak/scripts/extract_kotak_statement.py": (1, _SUBPROCESS),
    "src/agents/skill_krc/scripts/parse_krc_ledger.py": (1, _SUBPROCESS),
    "src/agents/skill_krc_gnucash/scripts/build_krc_gnucash.py": (1, _SUBPROCESS),
    "src/agents/skill_krc_recon/scripts/parse_krc_bills.py": (1, _SUBPROCESS),
    "src/agents/skill_26as_journal/scripts/build_tds_journals.py": (3, _SUBPROCESS + " (own folder, src/, fallback)"),
    "src/agents/skill_gnucash_intercompany/scripts/reconcile_intercompany.py": (2, _FLAT_MAIN),
    "src/agents/skill_gnucash_intercompany_matrix/scripts/matrix_recon.py": (2, _FLAT_MAIN),
    "src/agents/skill_gnucash_intercompany_matrix/scripts/matrix_report.py": (1, "flat module in a scripts/ folder with no `agents` import; reaches the pairwise engine by path"),
    "src/agents/skill_itr_workbook/scripts/apply_mapping_corrections.py": (1, _FLAT_MAIN),
    "src/agents/skill_itr_workbook/scripts/bootstrap_mappings.py": (1, _FLAT_MAIN),
    "src/agents/skill_itr_workbook/scripts/foreign_dividend_journal.py": (1, _FLAT_MAIN + " (lazy `agents` import)"),
    "src/agents/skill_itr_workbook/scripts/parse_gnucash.py": (1, _STANDALONE_FALLBACK),
    "src/agents/skill_itr_workbook/scripts/suggest.py": (4, _FLAT_MAIN + " (lazy `agents` imports)"),
    "src/agents/skill_itr_workbook/agent.py": (2, "run as a script (argparse main): its own scripts/ folder, and the lazy `agents` import"),
    "src/agents/skill_gnucash_reconciler/agent.py": (1, _STANDALONE_FALLBACK),
    "src/agents/skill_gnucash_xml_extractor/agent.py": (1, _STANDALONE_FALLBACK),
}

_SKIP_DIRS = {"tests", "venv", "vendor", "__pycache__", ".git", "node_modules", "build", "dist", "staging"}
_SCAN_TOP = ("src", "ui", "scripts", "bundling")


def _is_sys_path(node: ast.AST) -> bool:
    return (isinstance(node, ast.Attribute) and node.attr == "path"
            and isinstance(node.value, ast.Name) and node.value.id in ("sys", "_sys"))


def path_edits(source: str) -> int:
    """How many places in `source` change sys.path (insert, append, extend, +=, =, slice)."""
    count = 0
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        tree = ast.parse(source)
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("insert", "append", "extend", "remove", "pop")
                and _is_sys_path(node.func.value)):
            count += 1
        elif isinstance(node, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                if _is_sys_path(t) or (isinstance(t, ast.Subscript) and _is_sys_path(t.value)):
                    count += 1
    return count


def _production_files():
    for top in _SCAN_TOP:
        base = ROOT / top
        if not base.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
            for name in filenames:
                if name.endswith(".py"):
                    yield Path(dirpath) / name


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


def _scan() -> dict[str, int]:
    found = {}
    for p in _production_files():
        if _rel(p) == "src/agents/_paths.py":
            continue
        n = path_edits(p.read_text(encoding="utf-8"))
        if n:
            found[_rel(p)] = n
    return found


# ---- the guard itself --------------------------------------------------------

def test_no_sys_path_edit_outside_the_listed_stubs():                    # NEGATIVE
    found = _scan()
    stray = {f: n for f, n in found.items() if f not in ALLOWED}
    assert not stray, (
        "production code must use agents/_paths.py (ensure_src / ensure_agents_dir / "
        f"ensure_scripts) instead of editing sys.path: {stray}")


def test_every_listed_stub_still_has_exactly_its_listed_edits():
    found = _scan()
    drift = {f: (found.get(f, 0), n) for f, (n, _why) in ALLOWED.items() if found.get(f, 0) != n}
    assert not drift, f"allow-list out of date (found, listed): {drift}"


def test_every_listed_stub_has_a_reason():
    assert all(why.strip() for _n, why in ALLOWED.values())


@pytest.mark.parametrize("snippet", [
    "import sys\nsys.path.insert(0, '/x')\n",
    "import sys\nsys.path.append('/x')\n",
    "import sys\nsys.path += ['/x']\n",
    "import sys\nsys.path = ['/x']\n",
    "import sys\nsys.path[:] = ['/x']\n",
    "import sys as _sys\ndef f():\n    _sys.path.insert(0, 'x')\n",
])
def test_the_scanner_does_catch_each_form(snippet):                      # NEGATIVE of the scanner
    assert path_edits(snippet) == 1


def test_the_scanner_ignores_reads_and_comments():
    assert path_edits("import sys\n# sys.path.insert(0, 'x')\nx = sys.path[0]\nif 'a' in sys.path: pass\n") == 0


def test_a_new_insert_in_a_normal_module_would_be_flagged():             # NEGATIVE
    src = "import sys\nfrom pathlib import Path\nsys.path.insert(0, str(Path(__file__).parent))\n"
    fake = "src/agents/skill_new/agent.py"
    assert path_edits(src) == 1 and fake not in ALLOWED


# ---- the helper never uses the working directory ----------------------------

def test_helper_ignores_the_current_directory(tmp_path, monkeypatch):    # NEGATIVE
    decoy = tmp_path / "agents" / "skill_itr_workbook" / "scripts"
    decoy.mkdir(parents=True)
    (tmp_path / "src").mkdir()
    monkeypatch.chdir(tmp_path)
    monkeypatch.syspath_prepend("")                  # '' = the current directory, as in an interactive run
    real_src = Path(_paths.__file__).resolve().parent.parent
    assert _paths.src_dir() == real_src
    assert _paths.agents_dir() == real_src / "agents"
    got = _paths.ensure_scripts("skill_itr_workbook")
    assert got == real_src / "agents" / "skill_itr_workbook" / "scripts"
    assert str(decoy) not in sys.path and str(tmp_path) not in sys.path
    assert all(Path(p).resolve() != decoy for p in sys.path if p)
    # a skill that exists only in the decoy is refused, not found through the cwd
    (tmp_path / "agents" / "skill_only_in_decoy" / "scripts").mkdir(parents=True)
    with pytest.raises(FileNotFoundError):
        _paths.ensure_scripts("skill_only_in_decoy")
