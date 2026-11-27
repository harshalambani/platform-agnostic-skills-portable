"""DBT-01: agents/_paths.py is the one place that resolves the src folder, the
agents folder and a skill's scripts/ folder. Synthetic, no user data."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import _paths  # noqa: E402

SRC = ROOT / "src"
AGENTS = SRC / "agents"
ITR = "skill_itr_workbook"
ITR_SCRIPTS = AGENTS / ITR / "scripts"


@pytest.fixture
def clean_path(monkeypatch):
    """A private copy of sys.path, restored afterwards."""
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)


def _forget(monkeypatch, folder: Path):
    monkeypatch.setattr(sys, "path", [p for p in sys.path if p != str(folder)])


# ---- source resolution -------------------------------------------------------

def test_source_resolution_points_into_the_checkout(clean_path):
    assert _paths.src_dir() == SRC
    assert _paths.agents_dir() == AGENTS
    assert _paths.ensure_src() == SRC
    assert _paths.ensure_agents_dir() == AGENTS
    assert _paths.ensure_scripts(ITR) == ITR_SCRIPTS
    for folder in (SRC, AGENTS, ITR_SCRIPTS):
        assert str(folder) in sys.path


def test_each_call_is_idempotent(clean_path, monkeypatch):
    _forget(monkeypatch, ITR_SCRIPTS)
    first = _paths.ensure_scripts(ITR)
    snapshot = list(sys.path)
    assert _paths.ensure_scripts(ITR) == first
    assert sys.path == snapshot
    assert sys.path.count(str(ITR_SCRIPTS)) == 1
    for fn in (_paths.ensure_src, _paths.ensure_agents_dir):
        fn()
        snap = list(sys.path)
        fn()
        assert sys.path == snap


# ---- frozen resolution (sys._MEIPASS simulated) -------------------------------

def test_frozen_resolution_goes_through_meipass(clean_path, monkeypatch, tmp_path):
    meipass = tmp_path / "bundle"
    scripts = meipass / "agents" / ITR / "scripts"
    scripts.mkdir(parents=True)
    monkeypatch.setattr(sys, "_MEIPASS", str(meipass), raising=False)
    assert _paths.src_dir() == meipass
    assert _paths.agents_dir() == meipass / "agents"
    assert _paths.ensure_src() == meipass
    assert _paths.ensure_agents_dir() == meipass / "agents"
    assert _paths.ensure_scripts(ITR) == scripts
    assert str(scripts) in sys.path
    assert str(ITR_SCRIPTS) not in sys.path[:1]     # the source tree is not used when frozen


def test_frozen_lookup_does_not_fall_back_to_the_source_tree(clean_path, monkeypatch, tmp_path):   # NEGATIVE
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path / "empty-bundle"), raising=False)
    with pytest.raises(FileNotFoundError):
        _paths.ensure_scripts(ITR)


# ---- an unknown skill fails clearly --------------------------------------------

@pytest.mark.parametrize("name", ["skill_does_not_exist", "skill_itr_workbok"])
def test_an_unknown_skill_name_fails_and_adds_nothing(clean_path, name):         # NEGATIVE
    before = list(sys.path)
    with pytest.raises(FileNotFoundError, match=name):
        _paths.ensure_scripts(name)
    assert sys.path == before


@pytest.mark.parametrize("name", ["", "..", ".", "../agents", "skill_itr_workbook/..", "a\\b", None])
def test_a_name_that_is_not_a_folder_name_is_refused(clean_path, name):          # NEGATIVE
    before = list(sys.path)
    with pytest.raises(ValueError):
        _paths.ensure_scripts(name)
    assert sys.path == before


def test_a_skill_without_a_scripts_folder_is_refused(clean_path):                # NEGATIVE
    # skill_bank_skills exists but ships no scripts/ folder
    assert (AGENTS / "skill_bank_skills").is_dir() and not (AGENTS / "skill_bank_skills" / "scripts").exists()
    before = list(sys.path)
    with pytest.raises(FileNotFoundError):
        _paths.ensure_scripts("skill_bank_skills")
    assert sys.path == before


# ---- the five callers still reach the ITR scripts -------------------------------

def _unload(monkeypatch, *modules):
    for m in modules:
        monkeypatch.delitem(sys.modules, m, raising=False)


def test_book_registry_still_imports_configs(clean_path, monkeypatch):
    from ui import _book_registry
    _forget(monkeypatch, ITR_SCRIPTS)
    _unload(monkeypatch, "configs")
    mod = _book_registry._configs_mod()
    assert mod.__name__ == "configs"
    assert Path(mod.__file__).resolve().parent == ITR_SCRIPTS


def test_advance_tax_tab_still_imports_advance_tax(clean_path, monkeypatch):
    from ui.tabs import advance_tax
    _forget(monkeypatch, ITR_SCRIPTS)
    _unload(monkeypatch, "advance_tax")
    mod = advance_tax._adv_mod()
    assert Path(mod.__file__).resolve().parent == ITR_SCRIPTS


def test_itr_entities_tab_still_imports_configs(clean_path, monkeypatch):
    from ui.tabs import itr_entities
    _forget(monkeypatch, ITR_SCRIPTS)
    _unload(monkeypatch, "configs")
    mod = itr_entities._configs_mod()
    assert Path(mod.__file__).resolve().parent == ITR_SCRIPTS


def test_itr_mapping_review_tab_still_imports_its_modules(clean_path, monkeypatch):
    from ui.tabs import itr_mapping_review
    _forget(monkeypatch, ITR_SCRIPTS)
    _unload(monkeypatch, "configs", "tags", "apply_mapping_corrections")
    mods = itr_mapping_review._itr_modules()
    assert len(mods) == 3
    for m in mods:
        assert Path(m.__file__).resolve().parent == ITR_SCRIPTS


def test_coverage_agent_asks_the_helper_for_the_itr_scripts(clean_path, monkeypatch):
    from agents.skill_gnucash_coverage import agent
    from ui import _book_registry
    monkeypatch.setattr(_book_registry, "list_books", lambda *a, **k: {})
    asked = []
    real = _paths.ensure_scripts
    monkeypatch.setattr(_paths, "ensure_scripts", lambda s: (asked.append(s), real(s))[1])
    _forget(monkeypatch, ITR_SCRIPTS)
    out = agent._boundary_has_adjacent_evidence(
        "2025-03", "2024-25", "2024-04", "2025-03", "synthetic", None, "Assets:Bank", Path("x.gnucash"))
    assert out is False
    assert asked == [ITR]
    assert str(ITR_SCRIPTS) in sys.path


def test_the_four_copies_are_gone():                                             # NEGATIVE
    for rel in ("ui/_book_registry.py", "ui/tabs/advance_tax.py", "ui/tabs/itr_entities.py",
                "ui/tabs/itr_mapping_review.py", "src/agents/skill_gnucash_coverage/agent.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "def _ensure_itr_scripts_importable" not in text, rel
        assert "_SCRIPTS_ON_PATH" not in text, rel
        assert '_paths.ensure_scripts("skill_itr_workbook")' in text, rel
