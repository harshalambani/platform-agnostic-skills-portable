"""AUD-14: LangSmith / LangChain tracing is forced off at app start, whatever
the environment says. Synthetic only; no network."""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))


def test_tracing_is_false_after_startup_import_even_if_the_environment_said_true(monkeypatch):
    monkeypatch.setenv("LANGSMITH_TRACING", "true")
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "true")
    from ui import _tracing_off
    importlib.reload(_tracing_off)
    import os
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


@pytest.mark.parametrize("value", ["", "1", "TRUE", "yes"])
def test_other_environment_values_are_overridden_too(monkeypatch, value):
    monkeypatch.setenv("LANGSMITH_TRACING", value)
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", value)
    from ui import _tracing_off
    importlib.reload(_tracing_off)
    import os
    assert os.environ["LANGSMITH_TRACING"] == "false"
    assert os.environ["LANGCHAIN_TRACING_V2"] == "false"


def test_webui_imports_it_before_gradio_or_any_langchain_import():
    text = (ROOT / "ui" / "webui.py").read_text(encoding="utf-8")
    first_trace = text.index("from ui import _tracing_off")
    heavy = re.search(r"^\s*(import gradio|from agents|import langchain|from langchain)", text, re.M)
    first_heavy = heavy.start()
    assert first_trace < first_heavy
    assert not re.search(r"^\s*(import|from)\s+langchain", text[:first_trace], re.M)
