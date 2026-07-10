"""
tests/test_password_input.py — Regression guard for the "password" skill-input
type added to ui/tabs/_generic.py.

Verifies that an input declared with type: "password" in a skill.yaml
renders as a gr.Textbox(type="password") — Gradio's masked entry field —
rather than a plain, always-visible text box.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _make_password_skill():
    inp = SimpleNamespace(
        name="password",
        type="password",
        label="PDF password(s), comma-separated",
        required=False,
        file_types=None,
        options=[],
        match="",
    )
    output = SimpleNamespace(
        type="directory",
        suffix="out",
        extension=".txt",
        download_label="Download",
    )
    requires = SimpleNamespace(native_binaries=[], external_tools=[], llm=False)
    return SimpleNamespace(
        name="test_password_skill",
        display_name="Test Password Skill",
        description="test",
        inputs=[inp],
        output=output,
        requires=requires,
        run_args={},
        mode="direct",
        entry_point="agent:run",
        help=None,
    )


def test_generic_renders_password_input_as_masked_textbox(monkeypatch):
    import gradio as gr

    from ui.tabs import _generic

    monkeypatch.setattr(_generic, "_refresh_models", lambda **kw: [("model-x", "model-x")])
    monkeypatch.setattr(_generic, "_default_model_value", lambda choices: "model-x")

    skill = _make_password_skill()

    captured = {}
    original_textbox = gr.Textbox

    def _tracking_textbox(*args, **kwargs):
        if kwargs.get("label") == skill.inputs[0].label:
            captured["kwargs"] = kwargs
        return original_textbox(*args, **kwargs)

    monkeypatch.setattr(gr, "Textbox", _tracking_textbox)

    with gr.Blocks():
        with gr.Tab("Test"):
            _generic.render(skill)

    assert captured, "password input should render as a gr.Textbox"
    assert captured["kwargs"].get("type") == "password", (
        "password input must render with type='password' so Gradio masks it"
    )


def test_cc_sort_skill_declares_password_type():
    """The Credit Card Sort manifest must use the masked 'password' input type,
    not plain 'text', for its PDF-password field."""
    from agents import registry

    skill = registry.get("CC Sort")
    assert skill is not None, "CC Sort skill should be discoverable"
    password_inputs = [i for i in skill.inputs if i.name == "password"]
    assert password_inputs, "CC Sort skill should declare a 'password' input"
    assert password_inputs[0].type == "password", (
        "CC Sort's password input must be type='password', not 'text', "
        "so it renders masked in the UI"
    )
