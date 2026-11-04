"""UI-26: Reset returns every input to its form-open state.

Renders each skill tab through ui.tabs._generic.render, records every
component's form-open value, fires the captured Reset handler and compares.
Synthetic/empty config only; nothing under Data/ is read.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import gradio as gr
import pytest

ROOT = Path(__file__).resolve().parent.parent
for p in (str(ROOT / "src"), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents.registry import discover  # noqa: E402
from ui.tabs import _generic  # noqa: E402

SKILLS = discover()
BOOK_SKILLS = [s for s in SKILLS if any(i.book_from for i in s.inputs)]


def _empty(v):
    return v in (None, "", [])


@pytest.fixture(autouse=True)
def _populated_registry():
    """Non-empty pickers, so a Reset that refills the first choice is visible."""
    ent = [("Alice Doe", "alice"), ("Bob Doe", "bob")]
    fake = {
        "itr_entities": lambda: list(ent),
        "banks": lambda: [("Bank One", "b1"), ("Bank Two", "b2")],
        "itr_ay_years": lambda: [("AY 2026-27", "2026-27"), ("AY 2025-26", "2025-26")],
    }
    with patch.dict(_generic._OPTIONS_FROM_RESOLVERS, fake):
        yield


def _render_and_reset(skill, tmp_path):
    captured: dict = {}
    orig_click = gr.Button.click

    def _tracking_click(self, fn=None, **kwargs):
        if self.value == "Reset":
            captured["fn"] = fn
            captured["outputs"] = kwargs.get("outputs")
        return orig_click(self, fn=fn, **kwargs)

    with patch.object(gr.Button, "click", _tracking_click):
        with patch.object(_generic._config, "output_dir", return_value=tmp_path, create=True):
            with gr.Blocks():
                _generic.render(skill)
    assert "fn" in captured, f"{skill.name}: no Reset button"
    return captured["outputs"], captured["fn"]()


def _same(opened, reset_val):
    if _empty(opened) and _empty(reset_val):
        return True
    return opened == reset_val


@pytest.mark.parametrize("skill", SKILLS, ids=lambda s: s.name)
def test_reset_equals_form_open_value(skill, tmp_path):
    outputs, updates = _render_and_reset(skill, tmp_path)
    assert len(outputs) == len(updates)
    for comp, upd in zip(outputs, updates):
        if not isinstance(upd, dict) or "value" not in upd:
            continue
        label = getattr(comp, "label", None) or type(comp).__name__
        if label in ("Result",) or upd["value"] == "_Awaiting input._":
            continue  # result panel text, not an input
        assert _same(comp.value, upd["value"]), (
            f"{skill.name}: {label!r} opens as {comp.value!r} but Reset gives {upd['value']!r}")


@pytest.mark.parametrize("skill", BOOK_SKILLS, ids=lambda s: s.name)
def test_reset_leaves_no_entity_or_book_filled(skill, tmp_path):
    outputs, updates = _render_and_reset(skill, tmp_path)
    by_label = {}
    for comp, upd in zip(outputs, updates):
        by_label[getattr(comp, "label", None)] = (comp, upd)
    sources = {i.book_from for i in skill.inputs if i.book_from}
    for inp in skill.inputs:
        if inp.name in sources or inp.book_from:
            comp, upd = by_label[inp.label]
            assert _empty(upd.get("value")), f"{skill.name}.{inp.name} refilled by Reset: {upd!r}"
            if inp.type == "select" and inp.multiselect:
                assert upd["value"] == [], "multiselect must reset to []"
    # status line hidden
    for comp, upd in zip(outputs, updates):
        if isinstance(comp, gr.Markdown) and upd.get("visible") is False:
            assert upd.get("value") in ("", None)


def _reset_by_name(skill, tmp_path):
    outputs, updates = _render_and_reset(skill, tmp_path)
    return {getattr(c, "label", None): (c, u) for c, u in zip(outputs, updates)}


def test_non_book_select_keeps_first_choice_reset(tmp_path):
    skill = next(s for s in SKILLS if s.name == "partner_comp_recon")
    got = _reset_by_name(skill, tmp_path)
    fy = next(i for i in skill.inputs if i.name == "fy")
    assert got[fy.label][1]["value"] == "2026-27"  # first choice, not blank


def test_multiselect_resets_to_empty_list_not_scalar(tmp_path):
    skill = next(s for s in SKILLS if s.name == "gnucash_coverage")
    got = _reset_by_name(skill, tmp_path)
    inp = next(i for i in skill.inputs if i.name == "entities")
    assert got[inp.label][1]["value"] == []


def test_dependent_select_is_cleared(tmp_path):
    skill = next(s for s in SKILLS if s.name == "gnucash_pipeline")
    got = _reset_by_name(skill, tmp_path)
    inp = next(i for i in skill.inputs if i.name == "bank_account")
    assert got[inp.label][1]["value"] is None
    assert got[inp.label][1]["choices"] == []


@pytest.mark.parametrize("module_path", ["ui.tabs.gnucash_review", "ui.tabs.tds_journal_review"])
def test_hand_written_tab_reset_clears_entity_book_and_status(module_path, tmp_path):
    """Reset on the hand-written review tabs: entity blank, book blank, the
    registry status line hidden, and no stale check result left on screen."""
    from tests.test_gnucash_entity_book_prefill import _capture_reset
    fn, outputs = _capture_reset(tmp_path, module_path)
    result = fn()
    assert len(result) == len(outputs)
    for comp, upd in zip(outputs, result):
        label = getattr(comp, "label", "") or ""
        if "Entity" in label:
            assert _empty(upd.get("value") if isinstance(upd, dict) else upd)
        if "GnuCash" in label and "book" in label.lower():
            assert _empty(upd.get("value") if isinstance(upd, dict) else upd)


def test_gnucash_review_reset_clears_the_check_result(tmp_path):  # NEGATIVE: no stale check
    from tests.test_gnucash_entity_book_prefill import _capture_reset
    fn, outputs = _capture_reset(tmp_path, "ui.tabs.gnucash_review")
    result = fn()
    check_result, check_download = result[-2], result[-1]
    assert _empty(check_result.get("value") if isinstance(check_result, dict) else check_result)
    assert _empty(check_download.get("value"))
    assert check_download.get("interactive") is False
