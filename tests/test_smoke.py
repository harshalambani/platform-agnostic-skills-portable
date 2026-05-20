"""
tests/test_smoke.py — Phase-1 smoke tests.

These run against the project in source mode. They verify imports resolve,
the buildinfo module is shaped correctly, and the Gradio app object can be
constructed without raising. They do NOT touch a real LLM endpoint.
"""
import importlib
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"

# Make src/ importable in source mode (mirrors how the frozen build resolves it)
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def test_buildinfo_shape():
    bi = importlib.import_module("ui._buildinfo")
    assert hasattr(bi, "VERSION")
    assert hasattr(bi, "COMMIT_SHA")
    assert hasattr(bi, "BUILD_DIRTY")
    assert hasattr(bi, "BUILD_TIMESTAMP")
    assert isinstance(bi.VERSION, str)
    assert isinstance(bi.COMMIT_SHA, str)


def test_base_agent_imports():
    """The 'agents' package must be importable from src/."""
    mod = importlib.import_module("agents.base_agent")
    assert hasattr(mod, "load_model")
    assert hasattr(mod, "build_agent")


def test_skill_26as_imports():
    mod = importlib.import_module("agents.skill_26as.agent")
    assert hasattr(mod, "run")


def test_webui_constructs():
    """Gradio app object should construct without binding a port."""
    from ui import webui
    app = webui.build_app(launch=False)
    assert app is not None
