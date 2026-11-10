"""UI-19: the two Delete confirmations are ticked in order. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ui.tabs import itr_entities as ui_mod       # noqa: E402


def _seed(tmp_path):
    root = tmp_path / "Data"
    p = root / "itr" / "entities.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    ent = {"name": "Synthetic Person", "pan": "AAAAA0000A", "status": "Individual",
           "residency": "Resident", "default_regime": "new"}
    p.write_text(yaml.safe_dump({"SYN-A": ent, "SYN-B": dict(ent, name="Other")}), encoding="utf-8")
    return root, p


# ---- the gate rules ----

def test_box_two_is_unusable_until_box_one_is_ticked():
    v2, i2, d = ui_mod._confirm_gate(False, False)
    assert (v2, i2, d) == (False, False, False)


def test_box_two_cannot_be_ticked_first():                      # NEGATIVE
    v2, i2, d = ui_mod._confirm_gate(False, True)
    assert v2 is False and i2 is False and d is False


def test_ticking_box_one_enables_box_two_but_not_delete():
    v2, i2, d = ui_mod._confirm_gate(True, False)
    assert (v2, i2, d) == (False, True, False)


def test_both_ticked_enables_delete():
    assert ui_mod._confirm_gate(True, True) == (True, True, True)


def test_unticking_box_one_clears_box_two_and_disables_delete():   # NEGATIVE
    v2, i2, d = ui_mod._confirm_gate(False, True)
    assert v2 is False and d is False


# ---- the server-side check stays ----

@pytest.mark.parametrize("c1,c2", [(True, False), (False, True), (False, False)])
def test_delete_does_nothing_unless_both_boxes_are_ticked(tmp_path, c1, c2):   # NEGATIVE
    root, p = _seed(tmp_path)
    before = p.read_text(encoding="utf-8")
    with patch("ui._config.data_root_dir", return_value=root):
        msg = ui_mod._delete_entity("SYN-A", c1, c2)
    assert msg.startswith("Error") and "nothing was deleted" in msg
    assert p.read_text(encoding="utf-8") == before
    assert list(p.parent.glob("entities.yaml.bak-*")) == []


def test_delete_with_both_ticked_still_works(tmp_path):
    root, p = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        msg = ui_mod._delete_entity("SYN-A", True, True)
    assert msg.startswith("**Deleted**")
    assert "SYN-A" not in yaml.safe_load(p.read_text(encoding="utf-8"))


# ---- the built tab ----

def _built_tab():
    import gradio as gr
    with gr.Blocks() as demo:
        with gr.Tab("t") as tab:
            ui_mod.render(container_tab=tab)
    return demo


def _by_label(demo, label):
    return next(b for b in demo.blocks.values() if getattr(b, "label", None) == label)


def test_built_tab_starts_with_box_two_and_delete_disabled(tmp_path):
    root, _ = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        demo = _built_tab()
    box2 = _by_label(demo, "I confirm I want to delete this entity")
    box1 = _by_label(demo, "I understand this removes the entity from entities.yaml")
    delete = next(b for b in demo.blocks.values() if getattr(b, "value", None) == "Delete Entity")
    assert box1.interactive is not False
    assert box2.interactive is False
    assert delete.interactive is False


def test_switching_entity_resets_both_boxes_and_delete(tmp_path):     # NEGATIVE
    root, _ = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        demo = _built_tab()
        box1 = _by_label(demo, "I understand this removes the entity from entities.yaml")
        box2 = _by_label(demo, "I confirm I want to delete this entity")
        delete = next(b for b in demo.blocks.values() if getattr(b, "value", None) == "Delete Entity")
        fn = next(f for f in demo.fns.values()
                  if f.name == "_on_select")
        out = fn.fn("SYN-B")
    pos = {o._id: n for n, o in enumerate(fn.outputs)}
    assert out[pos[box1._id]]["value"] is False
    assert out[pos[box2._id]]["value"] is False and out[pos[box2._id]]["interactive"] is False
    assert out[pos[delete._id]]["interactive"] is False
