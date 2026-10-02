"""
UI-06 regression -- the Description double-click never fired in a real browser.

handleRowClick() used to call renderTable(), which rebuilds every td, so the
second click of a double-click landed on a NEW node and Chromium/WebView2 never
fired dblclick. The old tests called td.ondblclick() directly, which bypassed the
real event sequence and hid it.

Fix: a click toggles the 'selected' class on the existing rows and refreshes the
counter (no tbody rebuild); the edit is opened by ONE delegated dblclick listener
on the persistent tbody.

Realism of these tests: node + a fake DOM with real event dispatch (bubbling,
stopPropagation, on<type> and addEventListener handlers) and Chromium's rule
that the dblclick is dropped when the node under the pointer after the first
click is not the node the first click hit. NOT a real browser: no layout, no
real CDP mouse events (the coordinator re-verifies in headless Edge).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _review_js as js  # noqa: E402
from ui import _review_engine as eng  # noqa: E402

needs_node = pytest.mark.skipif(not js.have_node(), reason="node not installed")

APP = "t6"
ORIG = "NEFT-ACME LANDLORD-RENT JUNE"


def _html(extra_rows=()):
    spec = eng.ReviewSpec(
        app_id=APP,
        columns=[
            eng.Column("Date", "Date"),
            eng.Column("Description", "Description", edit_key="EditedDesc"),
            eng.Column("Account", "Account"),
        ],
        target_col="Account",
        payload_var="__t6_payload",
        allow_delete=True,
    )
    rows = [
        {"Date": "2025-06-01", "Description": ORIG, "Account": "Expenses:Rent"},
        {"Date": "2025-06-02", "Description": "SECOND ROW", "Account": "Expenses:Food"},
        {"Date": "2025-06-03", "Description": "THIRD ROW", "Account": "Expenses:Food"},
        {"Date": "2025-06-04", "Description": "LOCKED ROW", "Account": "Expenses:Food", "_locked": True},
        *extra_rows,
    ]
    return eng.build_html(spec, rows)


def _run(tmp_path, scenario):
    return js.run_js(_html(), APP, scenario, tmp_path)


# ---- 1. a click must not rebuild the rows --------------------------------------------

@needs_node
def test_click_keeps_every_tr_and_td_node(tmp_path):
    out = _run(tmp_path, """
        const snap = () => $id(APP + '-tbody').children.map(tr => [tr, ...tr.children]);
        const before = snap();
        click(APP, 1, 'Description');                                  // plain
        click(APP, 2, 'Description', {shiftKey: true});                // shift
        click(APP, 0, 'Description', {ctrlKey: true});                 // ctrl
        const after = snap();
        return {same: before.length === after.length &&
                      before.every((g, i) => g.length === after[i].length &&
                                             g.every((n, j) => n === after[i][j]))};
    """)
    assert out["same"] is True


@needs_node
def test_selection_is_updated_in_place_with_counter(tmp_path):
    out = _run(tmp_path, """
        const sel = () => view(APP).filter(v => v.cls.includes('selected')).map(v => v.idx);
        const stats = () => $id(APP + '-stats').textContent;
        click(APP, 1, 'Date');
        const plain = {sel: sel(), stats: stats()};
        click(APP, 2, 'Date', {ctrlKey: true});                        // add
        const ctrlAdd = {sel: sel(), stats: stats()};
        click(APP, 1, 'Date', {ctrlKey: true});                        // toggle off
        const ctrlOff = {sel: sel(), stats: stats()};
        click(APP, 0, 'Date');                                         // plain replaces
        click(APP, 2, 'Date', {shiftKey: true});                       // range 0..2
        const range = {sel: sel(), stats: stats()};
        click(APP, 1, 'Date');                                         // plain again
        const reset = {sel: sel(), stats: stats()};
        return {plain, ctrlAdd, ctrlOff, range, reset};
    """)
    assert out["plain"]["sel"] == [1] and "1 selected" in out["plain"]["stats"]
    assert out["ctrlAdd"]["sel"] == [1, 2] and "2 selected" in out["ctrlAdd"]["stats"]
    assert out["ctrlOff"]["sel"] == [2] and "1 selected" in out["ctrlOff"]["stats"]
    assert out["range"]["sel"] == [0, 1, 2] and "3 selected" in out["range"]["stats"]
    assert out["reset"]["sel"] == [1]                       # NEGATIVE: not left at 3
    assert "3 selected" not in out["reset"]["stats"]


# ---- 2. a real double-click opens exactly one input ----------------------------------

@needs_node
def test_real_double_click_opens_exactly_one_input_on_an_unselected_row(tmp_path):
    out = _run(tmp_path, """
        const r = realDblClick(APP, 0, 'Description');
        const td = cellAt(APP, 0, 'Description');
        const inputs = inputsIn(td);
        return {r, n: inputs.length, value: inputs[0] && inputs[0].value,
                otherInputs: ['Date', 'Account'].map(k => inputsIn(cellAt(APP, 0, k)).length)};
    """)
    assert out["r"] == {"fired": True, "replaced": False}
    assert out["n"] == 1 and out["value"] == ORIG
    assert out["otherInputs"] == [0, 0]                      # NEGATIVE: only Description


@needs_node
def test_real_double_click_works_on_an_already_selected_row_and_with_a_modifier(tmp_path):
    out = _run(tmp_path, """
        click(APP, 1, 'Description');                          // row 1 is selected first
        const a = realDblClick(APP, 1, 'Description');
        const na = inputsIn(cellAt(APP, 1, 'Description')).length;
        return {a, na, selected: view(APP).find(v => v.idx === 1).cls.includes('selected')};
    """)
    assert out["a"]["fired"] is True and out["na"] == 1
    assert out["selected"] is True


@needs_node
def test_a_second_double_click_while_editing_does_not_stack_inputs(tmp_path):
    """NEGATIVE: exactly one input, however many dblclicks arrive."""
    out = _run(tmp_path, """
        realDblClick(APP, 0, 'Description');
        const td = cellAt(APP, 0, 'Description');
        dispatch(td, 'dblclick', {});
        dispatch(inputsIn(td)[0], 'dblclick', {});             // inside the box itself
        return {n: inputsIn(td).length};
    """)
    assert out["n"] == 1


@needs_node
def test_only_the_edit_column_opens_an_editor(tmp_path):
    """NEGATIVE: a double-click on Date / Account (no edit_key) opens nothing."""
    out = _run(tmp_path, """
        realDblClick(APP, 0, 'Date'); realDblClick(APP, 0, 'Account');
        return {n: ['Date', 'Description', 'Account'].map(k => inputsIn(cellAt(APP, 0, k)).length)};
    """)
    assert out["n"] == [0, 0, 0]


# ---- 3. locked and deleted rows ----------------------------------------------------------

@needs_node
def test_double_click_on_a_locked_row_opens_no_input(tmp_path):
    out = _run(tmp_path, """
        realDblClick(APP, 3, 'Description');
        return {n: inputsIn(cellAt(APP, 3, 'Description')).length,
                locked: view(APP).find(v => v.idx === 3).cls.includes('locked')};
    """)
    assert out["locked"] is True and out["n"] == 0


@needs_node
def test_double_click_on_a_deleted_row_opens_no_input(tmp_path):
    out = _run(tmp_path, """
        click(APP, 2, 'Date');
        $id(APP + '-remove-sel').onclick();                    // marks the selected row deleted
        const deleted = view(APP).find(v => v.idx === 2).cls.includes('row-deleted');
        realDblClick(APP, 2, 'Description');
        return {deleted, n: inputsIn(cellAt(APP, 2, 'Description')).length};
    """)
    assert out["deleted"] is True and out["n"] == 0


# ---- 4. Esc / Enter ----------------------------------------------------------------------

@needs_node
def test_escape_leaves_description_and_the_edit_field_unchanged(tmp_path):
    out = _run(tmp_path, """
        realDblClick(APP, 0, 'Description');
        const inp = inputsIn(cellAt(APP, 0, 'Description'))[0];
        inp.value = 'SOMETHING ELSE ENTIRELY';
        inp.onkeydown({key: 'Escape'});
        const cell = view(APP).find(v => v.idx === 0).cells[1];
        const pl = payload('__t6_payload');
        return {cell, n: inputsIn(cellAt(APP, 0, 'Description')).length,
                changes: (pl.changes || []).length, edited: JSON.stringify(pl).includes('SOMETHING ELSE')};
    """)
    assert ORIG in out["cell"] and "SOMETHING ELSE" not in out["cell"]
    assert "&#9998;" not in out["cell"]                      # no 'edited' marker
    assert out["n"] == 0 and out["edited"] is False


@needs_node
def test_enter_commits_to_the_edit_field_and_keeps_the_original(tmp_path):
    out = _run(tmp_path, """
        realDblClick(APP, 0, 'Description');
        const inp = inputsIn(cellAt(APP, 0, 'Description'))[0];
        inp.value = 'Rent - June';
        inp.onkeydown({key: 'Enter', preventDefault(){}});
        const td = cellAt(APP, 0, 'Description');
        return {cell: td.innerHTML, title: td.title, n: inputsIn(td).length};
    """)
    assert "Rent - June" in out["cell"] and "&#9998;" in out["cell"]
    assert ORIG in out["title"]                              # original still reachable
    assert out["n"] == 0


@needs_node
def test_editing_still_works_after_a_commit(tmp_path):
    """The delegated listener survives the re-render that follows a commit."""
    out = _run(tmp_path, """
        realDblClick(APP, 0, 'Description');
        let inp = inputsIn(cellAt(APP, 0, 'Description'))[0];
        inp.value = 'First'; inp.onkeydown({key: 'Enter', preventDefault(){}});
        realDblClick(APP, 1, 'Description');
        return {n: inputsIn(cellAt(APP, 1, 'Description')).length};
    """)
    assert out["n"] == 1
