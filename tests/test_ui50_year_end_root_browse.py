"""tests/test_ui50_year_end_root_browse.py -- UI-50: Browse... pickers on the two year-end roots.

Synthetic folders only (tmp_path); the native picker is monkeypatched; the live config.yaml is never used."""
from __future__ import annotations

import sys
from pathlib import Path

import gradio as gr
import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ui import _filedialog                                   # noqa: E402
from ui.tabs import itr_entities as ui_mod                   # noqa: E402

KEYS = ("drive_accounts_root", "gnucash_profile_root")


def _patch(monkeypatch, result, calls=None):
    def fake(box_key, *, title="", fallback_dir=None, path=None):
        if calls is not None:
            calls.append((box_key, fallback_dir))
        return result
    monkeypatch.setattr(_filedialog, "pick_folder", fake)


def _value(upd):
    return upd.get("value") if isinstance(upd, dict) else getattr(upd, "value", None)


def _is_noop(upd):
    return isinstance(upd, dict) and "value" not in upd or _value(upd) is None


@pytest.mark.parametrize("key", KEYS)
def test_a_pick_fills_the_box(tmp_path, monkeypatch, key):
    d = tmp_path / "picked"
    d.mkdir()
    _patch(monkeypatch, str(d))
    assert _value(ui_mod._browse_year_end_root(key, "")) == str(d)


@pytest.mark.parametrize("result", [None, ""])
def test_cancel_leaves_the_box_as_it_was(monkeypatch, result):               # NEGATIVE
    _patch(monkeypatch, result)
    assert _is_noop(ui_mod._browse_year_end_root("drive_accounts_root", "typed value"))


def test_the_picker_starts_in_the_boxs_existing_folder_and_ignores_a_missing_one(tmp_path, monkeypatch):
    calls = []
    _patch(monkeypatch, None, calls)
    ui_mod._browse_year_end_root("drive_accounts_root", str(tmp_path))
    ui_mod._browse_year_end_root("drive_accounts_root", str(tmp_path / "nope"))
    assert calls[0][1] == str(tmp_path) and calls[1][1] is None


def test_browse_writes_nothing_to_config_yaml(tmp_path, monkeypatch):           # NEGATIVE
    from ui import _config as cfgmod
    cfg = tmp_path / "config.yaml"
    cfg.write_text("keep: 1\n", encoding="utf-8")
    monkeypatch.setattr(cfgmod, "PORTABLE_CONFIG_PATH", cfg)
    d = tmp_path / "picked"
    d.mkdir()
    _patch(monkeypatch, str(d))
    for k in KEYS:
        ui_mod._browse_year_end_root(k, "")
    assert cfg.read_text(encoding="utf-8") == "keep: 1\n"


def test_picking_for_one_root_does_not_touch_the_other(tmp_path, monkeypatch):    # NEGATIVE
    d = tmp_path / "picked"
    d.mkdir()
    _patch(monkeypatch, str(d))
    with gr.Blocks() as demo:
        ui_mod.render()
    buttons = [c for c in demo.blocks.values() if isinstance(c, gr.Button) and c.value == "Browse…"]
    assert len(buttons) >= 2
    drive = next(c for c in demo.blocks.values() if getattr(c, "label", "") == "Drive Accounts root")
    prof = next(c for c in demo.blocks.values() if getattr(c, "label", "") == "GnuCash profile root")
    wired = {}
    for fn in demo.fns.values():
        outs = [o for o in fn.outputs]
        if len(outs) == 1 and outs[0] in (drive, prof) and len(fn.inputs) == 1:
            wired[outs[0]] = fn
    assert set(wired) == {drive, prof}
    assert wired[drive].outputs == [drive] and wired[prof].outputs == [prof]    # each button writes only its own box
    assert wired[drive].inputs == [drive] and wired[prof].inputs == [prof]


def test_the_folder_name_field_has_no_browse_button():                              # NEGATIVE
    with gr.Blocks() as demo:
        ui_mod.render()
    name = next(c for c in demo.blocks.values()
                if "Folder name (Drive Accounts and GnuCash profile folders)" in str(getattr(c, "label", "")))
    for fn in demo.fns.values():
        if name in fn.outputs and len(fn.inputs) == 1 and fn.inputs[0] is name:
            pytest.fail("the folder-name field is wired to a Browse-style button")
    browse = [c for c in demo.blocks.values() if isinstance(c, gr.Button) and c.value == "Browse…"]
    # the entity form itself has none; the only Browse buttons on this tab are the two root pickers
    assert len(browse) == 2


def test_save_still_rejects_a_nonexistent_folder_after_a_browse(tmp_path, monkeypatch):    # NEGATIVE
    cfg = tmp_path / "config.yaml"
    cfg.write_text("keep: 1\n", encoding="utf-8")
    good = tmp_path / "good"
    good.mkdir()
    _patch(monkeypatch, str(good))
    ok = _value(ui_mod._browse_year_end_root("drive_accounts_root", ""))
    gone = str(tmp_path / "does_not_exist")
    msg = ui_mod._save_year_end_roots(ok, gone, settings_path=cfg)
    assert msg.startswith("**Not saved")
    assert cfg.read_text(encoding="utf-8") == "keep: 1\n"


def test_the_accordion_text_says_browse_or_paste():
    src = (ROOT / "ui" / "tabs" / "itr_entities.py").read_text(encoding="utf-8")
    assert "Use Browse… or paste a full path" in src
