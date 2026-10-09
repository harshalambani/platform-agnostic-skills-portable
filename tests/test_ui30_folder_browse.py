"""
tests/test_ui30_folder_browse.py -- UI-30: a Browse... button beside every
`directory` textbox, a native folder picker with per-box memory, and a Run-time
refusal of a folder that does not exist.

The native Win32 dialog is monkeypatched; only temp directories are used.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import gradio as gr
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for p in (str(SRC), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from ui import _filedialog  # noqa: E402
from ui.tabs import _generic as generic_mod  # noqa: E402

BOX = "Test Skill.folder"


@pytest.fixture
def cfg_path(tmp_path: Path) -> Path:
    p = tmp_path / "config.yaml"
    p.write_text(yaml.safe_dump({"active_endpoint": "local"}), encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# pick_folder: sets, remembers, reopens
# ---------------------------------------------------------------------------

def test_browse_returns_and_remembers_the_folder(monkeypatch, cfg_path, tmp_path):
    chosen = tmp_path / "statements"
    chosen.mkdir()
    monkeypatch.setattr(_filedialog, "_native_folder_dialog", lambda **kw: str(chosen))
    assert _filedialog.pick_folder(BOX, path=cfg_path) == str(chosen)
    assert _filedialog.last_dir_for(BOX, path=cfg_path) == str(chosen)


def test_browse_reopens_at_the_remembered_folder(monkeypatch, cfg_path, tmp_path):
    chosen = tmp_path / "statements"
    chosen.mkdir()
    _filedialog.remember_dir(BOX, str(chosen), path=cfg_path)
    seen = {}

    def fake(**kw):
        seen.update(kw)
        return None

    monkeypatch.setattr(_filedialog, "_native_folder_dialog", fake)
    _filedialog.pick_folder(BOX, path=cfg_path)
    assert seen["initialdir"] == str(chosen)


def test_fallback_dir_is_used_only_when_nothing_is_remembered(monkeypatch, cfg_path, tmp_path):
    fb = tmp_path / "fallback"
    fb.mkdir()
    mem = tmp_path / "remembered"
    mem.mkdir()
    seen = []
    monkeypatch.setattr(_filedialog, "_native_folder_dialog",
                        lambda **kw: seen.append(kw["initialdir"]))
    _filedialog.pick_folder(BOX, fallback_dir=str(fb), path=cfg_path)
    _filedialog.remember_dir(BOX, str(mem), path=cfg_path)
    _filedialog.pick_folder(BOX, fallback_dir=str(fb), path=cfg_path)
    assert seen == [str(fb), str(mem)]


# --- negatives -------------------------------------------------------------

def test_cancel_returns_none_and_remembers_nothing(monkeypatch, cfg_path):
    monkeypatch.setattr(_filedialog, "_native_folder_dialog", lambda **kw: None)
    assert _filedialog.pick_folder(BOX, path=cfg_path) is None
    assert _filedialog.last_dir_for(BOX, path=cfg_path) is None


def test_a_pick_that_is_not_a_folder_is_not_taken(monkeypatch, cfg_path, tmp_path):
    monkeypatch.setattr(_filedialog, "_native_folder_dialog",
                        lambda **kw: str(tmp_path / "nope"))
    assert _filedialog.pick_folder(BOX, path=cfg_path) is None
    assert _filedialog.last_dir_for(BOX, path=cfg_path) is None


def test_a_vanished_remembered_folder_is_not_used(monkeypatch, cfg_path, tmp_path):
    gone = tmp_path / "gone"
    gone.mkdir()
    _filedialog.remember_dir(BOX, str(gone), path=cfg_path)
    gone.rmdir()
    seen = {}
    monkeypatch.setattr(_filedialog, "_native_folder_dialog",
                        lambda **kw: seen.update(kw))
    _filedialog.pick_folder(BOX, path=cfg_path)
    assert not seen["initialdir"]


# ---------------------------------------------------------------------------
# rendering and click wiring
# ---------------------------------------------------------------------------

def _dir_skill():
    inp = SimpleNamespace(
        name="folder", type="directory", label="Statements folder", required=True,
        file_types=None, options=[], match="", options_from="", book_from="",
        fy_from="", multiselect=False, entity_from="", group="")
    file_inp = SimpleNamespace(
        name="one", type="file", label="One file", required=False,
        file_types=None, options=[], match="", options_from="", book_from="",
        fy_from="", multiselect=False, entity_from="", group="")
    output = SimpleNamespace(type="directory", suffix="out", extension=".txt",
                             download_label="Download")
    requires = SimpleNamespace(native_binaries=[], external_tools=[], llm=False, network=False)
    return SimpleNamespace(
        name="Test Skill", display_name="Test Skill", description="t",
        inputs=[inp, file_inp], output=output, requires=requires,
        run_args={"folder": "{inputs.folder}"}, mode="direct",
        entry_point="agent:run", help=None)


def _render(monkeypatch, skill):
    monkeypatch.setattr(generic_mod, "_refresh_models", lambda **kw: [("m", "m")])
    monkeypatch.setattr(generic_mod, "_default_model_value", lambda c: "m")
    with gr.Blocks() as demo:
        with gr.Tab("T"):
            generic_mod.render(skill)
    return demo


def test_every_directory_box_has_a_browse_button(monkeypatch):
    demo = _render(monkeypatch, _dir_skill())
    buttons = [b for b in demo.blocks.values()
               if isinstance(b, gr.Button) and b.value == "Browse\u2026"]
    # one for the directory box, one for the (unchanged) file box
    assert len(buttons) == 2
    assert all(b.scale == 0 and b.min_width == 110 for b in buttons)


def _browse_fns(demo):
    return [f.fn for f in demo.fns.values() if getattr(f.fn, "__name__", "") == "_browse_folder"]


def test_browse_click_sets_the_box_and_cancel_keeps_it(monkeypatch):
    demo = _render(monkeypatch, _dir_skill())
    fns = _browse_fns(demo)
    assert len(fns) == 1
    monkeypatch.setattr(_filedialog, "pick_folder", lambda *a, **kw: "C:/picked")
    assert fns[0]("C:/typed")["value"] == "C:/picked"
    # NEGATIVE: a cancel leaves the box exactly as typed (no value key at all)
    monkeypatch.setattr(_filedialog, "pick_folder", lambda *a, **kw: None)
    assert fns[0]("C:/typed") == gr.update()


def test_file_box_still_uses_its_own_browse_not_the_folder_picker(monkeypatch):
    demo = _render(monkeypatch, _dir_skill())
    calls = []
    monkeypatch.setattr(_filedialog, "pick_folder", lambda *a, **kw: calls.append(a))
    names = {getattr(f.fn, "__name__", "") for f in demo.fns.values()}
    assert "_browse_folder" in names
    assert not calls       # nothing opened by rendering alone


# ---------------------------------------------------------------------------
# Run-time refusal
# ---------------------------------------------------------------------------

def _run(monkeypatch, tmp_path, value):
    skill = _dir_skill()
    called = []
    monkeypatch.setattr("agents.registry.load_run_function",
                        lambda s: (lambda **kw: called.append(kw) or "done"))
    monkeypatch.setattr("ui._config.output_dir", lambda: tmp_path / "out")
    (tmp_path / "out").mkdir(exist_ok=True)
    handler = generic_mod._make_run_handler(skill)
    msgs = [m[0] for m in handler(value, None, "m")]
    return called, msgs


def test_run_with_a_missing_folder_is_refused_and_nothing_runs(monkeypatch, tmp_path):
    called, msgs = _run(monkeypatch, tmp_path, str(tmp_path / "does-not-exist"))
    assert not called
    assert any("Statements folder" in m for m in msgs)


def test_run_with_a_blank_required_folder_is_refused(monkeypatch, tmp_path):
    called, msgs = _run(monkeypatch, tmp_path, "")
    assert not called
    assert any("Statements folder" in m for m in msgs)


def test_run_with_a_real_folder_proceeds(monkeypatch, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    called, _msgs = _run(monkeypatch, tmp_path, str(real))
    assert called and called[0]["folder"] == str(real)
