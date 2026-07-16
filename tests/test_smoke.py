"""
tests/test_smoke.py — Smoke tests (Phase 1 through 4A).

These run against the project in source mode. They verify imports resolve,
the buildinfo module is shaped correctly, the skill registry discovers all
skills, and the Gradio app object can be constructed without raising.
They do NOT touch a real LLM endpoint, do NOT require Tesseract/Poppler
binaries to be present, and do NOT spin a port.
"""
import importlib
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"

# Make src/ importable in source mode (mirrors how the frozen build resolves it)
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


# ---------------------------------------------------------------------------
# Build info
# ---------------------------------------------------------------------------

def test_buildinfo_shape():
    bi = importlib.import_module("ui._buildinfo")
    assert hasattr(bi, "VERSION")
    assert hasattr(bi, "COMMIT_SHA")
    assert hasattr(bi, "BUILD_DIRTY")
    assert hasattr(bi, "BUILD_TIMESTAMP")
    assert isinstance(bi.VERSION, str)
    assert isinstance(bi.COMMIT_SHA, str)


# ---------------------------------------------------------------------------
# Base agent
# ---------------------------------------------------------------------------

def test_base_agent_imports():
    """The 'agents' package must be importable from src/."""
    mod = importlib.import_module("agents.base_agent")
    assert hasattr(mod, "load_model")
    assert hasattr(mod, "build_agent")
    assert hasattr(mod, "run_direct")


# ---------------------------------------------------------------------------
# Individual skill imports
# ---------------------------------------------------------------------------

def test_skill_26as_imports():
    mod = importlib.import_module("agents.skill_26as.agent")
    assert hasattr(mod, "run")


def test_skill_bob_imports():
    mod = importlib.import_module("agents.skill_bob.agent")
    assert hasattr(mod, "run")


def test_skill_hsbc_imports():
    """Does NOT require Tesseract on PATH."""
    mod = importlib.import_module("agents.skill_hsbc.agent")
    assert hasattr(mod, "run")


def test_skill_cc_sort_imports():
    mod = importlib.import_module("agents.skill_cc_sort.agent")
    assert hasattr(mod, "run")


def test_skill_cc_transactions_imports():
    mod = importlib.import_module("agents.skill_cc_transactions.agent")
    assert hasattr(mod, "run")


# ---------------------------------------------------------------------------
# Skill registry (Phase 4A)
# ---------------------------------------------------------------------------

def test_registry_discovers_all_skills():
    """Registry must find all 16 skills via skill.yaml manifests."""
    from agents.registry import discover
    skills = discover(refresh=True)
    names = {s.name for s in skills}
    # Phase 1-3 financial skills
    assert "26AS" in names
    assert "BoB" in names
    assert "HSBC" in names
    assert "CC Sort" in names
    assert "CC Transactions" in names
    # Phase 4C general skills
    assert "summarize" in names
    assert "translate" in names
    assert "CSV Analyzer" in names
    # Phase 6 skill
    assert "MSG Parser" in names
    # v1.1 dev-time skill (exposed as a top-level tab)
    assert "Parser Generator" in names
    # GnuCash intercompany reconciliation skills
    assert "gnucash_intercompany" in names
    assert "gnucash_intercompany_matrix" in names
    # ITR Workbook scaffold (batch 1: eguile HTML parser + verification only)
    assert "ITR Workbook" in names
    assert len(skills) == 20


def test_registry_get_by_name():
    from agents.registry import get
    skill = get("26AS")
    assert skill is not None
    assert skill.mode == "direct"
    assert skill.entry_point == "agent:run"


def test_registry_get_case_insensitive():
    from agents.registry import get
    assert get("bob") is not None
    assert get("BOB") is not None
    assert get("BoB") is not None


def test_registry_skill_has_inputs():
    from agents.registry import get
    skill = get("26AS")
    assert len(skill.inputs) >= 1
    assert skill.inputs[0].type == "file"


def test_registry_load_run_function():
    """load_run_function must return a callable for each skill."""
    from agents.registry import discover, load_run_function
    for skill in discover():
        fn = load_run_function(skill)
        assert callable(fn), f"run function for {skill.name} is not callable"


def test_registry_skill_output_config():
    from agents.registry import get
    skill = get("26AS")
    assert skill.output.extension == ".xlsx"
    assert skill.output.suffix == "26AS"


def test_registry_native_requirements():
    from agents.registry import get
    hsbc = get("HSBC")
    assert "tesseract" in hsbc.requires.native_binaries
    assert "poppler" in hsbc.requires.native_binaries

    bob = get("BoB")
    assert len(bob.requires.native_binaries) == 0


def test_registry_external_tools():
    from agents.registry import get
    cc_sort = get("CC Sort")
    assert "qpdf" in cc_sort.requires.external_tools


# ---------------------------------------------------------------------------
# Native resolver
# ---------------------------------------------------------------------------

def test_native_resolver_inspection():
    """ui._native.native_status() must work without binaries being installed."""
    from ui import _native
    status = _native.native_status()
    assert status.mode in ("source", "frozen")
    assert isinstance(status.ok, bool)


def test_native_resolver_idempotent():
    """ensure_native_path() must be safe to call repeatedly."""
    from ui import _native
    a = _native.ensure_native_path()
    b = _native.ensure_native_path()
    assert a == b


# ---------------------------------------------------------------------------
# Gradio app construction (Phase 4A: dynamic tabs)
# ---------------------------------------------------------------------------

def test_webui_constructs():
    """Gradio app object should construct with dynamic registry-driven tabs."""
    try:
        import gradio  # noqa: F401
        if not hasattr(gradio, "Blocks"):
            pytest.skip("gradio not fully installed")
    except ImportError:
        pytest.skip("gradio not installed")
    from ui import webui
    app = webui.build_app(launch=False)
    assert app is not None

    # ITR (Workbook + Mapping) is nested inside GnuCash as its own "ITR"
    # sub-tab (mirroring "Banks"), not a flat top-level tab -- see
    # GROUP_ORDER / _known_cats in webui.py.
    tab_labels = {getattr(b, "label", None) for b in app.blocks.values()}
    assert "ITR" in tab_labels
    assert "ITR Workbook" in tab_labels
    assert "ITR Mapping" in tab_labels
    assert "GnuCash" in tab_labels
