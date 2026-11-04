"""
UI-13 -- the Assign Account picker can be cleared: Esc and an X in the box.

Shared engine, so every review tab gets it (the TDS journal review is checked too).
  - Esc with the dropdown open closes it (text and chosen value kept).
  - Esc with the dropdown shut empties the text and `chosen`.
  - An X inside the box, only while it has text: empties text + chosen, closes the
    dropdown, keeps focus in the box.
  - Neither reaches the filter boxes' own Esc handlers, and neither ever assigns.

Runtime behaviour runs in node against the harness's fake DOM.
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
from ui.tabs import gnucash_review as rv  # noqa: E402
from ui.tabs import tds_journal_review as tds  # noqa: E402

needs_node = pytest.mark.skipif(not js.have_node(), reason="node not installed")

_HEADER = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"
_ROWS = (
    "2025-05-01,UNKNOWN PAYEE ONE,Liabilities:Suspense,,100.00,900.00,suspense,no match\n"
    "2025-05-02,UNKNOWN PAYEE TWO,Liabilities:Suspense,,50.00,850.00,suspense,no match\n"
    "2025-05-03,COFFEE,Expense:Food,,20.00,830.00,high,rule\n"
    "2025-05-04,TEA,Expense:Food,,10.00,820.00,high,rule\n"
)

_PRE = """
const __alerts = [];
globalThis.alert = (m) => { __alerts.push(String(m)); };
"""
_HELPERS = """
const S = () => $id(APP + '-picker-search');
const DD = () => $id(APP + '-picker-dropdown');
const X = () => $id(APP + '-picker-clear');
const esc = () => dispatch(S(), 'keydown', { key: 'Escape' });
const isOpen = () => DD().classList.contains('open');
"""


def _html(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + _ROWS, encoding="utf-8")
    return rv._load_review_data(str(p), str(p))


def _run(tmp_path, scenario):
    return js.run_js(_html(tmp_path), "rv", _HELPERS + scenario, tmp_path, pre=_PRE)


# ---- 1. Escape ----------------------------------------------------------------------

@needs_node
def test_escape_with_dropdown_open_closes_it_and_keeps_the_choice(tmp_path):
    out = _run(tmp_path, """
        pick(APP, 0);
        const val = S().value;
        S().onfocus();
        const wasOpen = isOpen();
        esc();
        select(APP, 0); applySel(APP);
        return {val, wasOpen, openAfter: isOpen(), textAfter: S().value,
                assigned: view(APP).find(v => v.idx === 0).cells.join('|').includes(val)};
    """)
    assert out["wasOpen"] and not out["openAfter"]
    assert out["textAfter"] == out["val"] != ""      # NEGATIVE: text not cleared while open
    assert out["assigned"]                            # NEGATIVE: chosen not cleared while open


@needs_node
def test_escape_with_dropdown_shut_clears_text_and_chosen(tmp_path):
    out = _run(tmp_path, """
        pick(APP, 0);
        const shut = !isOpen();
        esc();
        select(APP, 0); applySel(APP);
        return {shut, text: S().value, alerts: __alerts.slice()};
    """)
    assert out["shut"]
    assert out["text"] == ""
    assert out["alerts"] == ["Pick a value first."]   # chosen is gone too


@needs_node
def test_escape_on_an_empty_shut_box_does_nothing(tmp_path):
    out = _run(tmp_path, """
        const ev = esc();
        return {text: S().value, open: isOpen(), stopped: ev.stopped};
    """)
    assert out == {"text": "", "open": False, "stopped": True}


# ---- 2. the X -----------------------------------------------------------------------

@needs_node
def test_x_is_hidden_on_an_empty_box_and_shown_only_with_text(tmp_path):
    out = _run(tmp_path, """
        const initial = X().style.display;
        S().value = 'food'; S().oninput();
        const typed = X().style.display;
        pick(APP, 0);
        const picked = X().style.display;
        esc();
        const cleared = X().style.display;
        return {initial, typed, picked, cleared};
    """)
    assert out["initial"] == "none"                   # NEGATIVE: never on an empty box
    assert out["typed"] == "" and out["picked"] == ""
    assert out["cleared"] == "none"


@needs_node
def test_x_clears_text_and_chosen_closes_dropdown_and_keeps_focus(tmp_path):
    out = _run(tmp_path, """
        pick(APP, 0);
        S().onfocus();
        document.activeElement = null;
        const ev = dispatch(X(), 'click', {});
        select(APP, 0); applySel(APP);
        return {text: S().value, open: isOpen(), focused: document.activeElement === S(),
                xHidden: X().style.display, alerts: __alerts.slice(), stopped: ev.stopped};
    """)
    assert out["text"] == "" and not out["open"]
    assert out["focused"]
    assert out["xHidden"] == "none"
    assert out["alerts"] == ["Pick a value first."]   # chosen is gone
    assert out["stopped"]


# ---- 3. negatives: never assigns, never touches rows / filters ---------------------

@needs_node
def test_escape_and_x_never_assign_or_mark_any_row_changed(tmp_path):
    out = _run(tmp_path, """
        const before = globalThis._rvSavePayload;
        const dBefore = globalThis._rvSavePayloadDirty;
        pick(APP, 0);
        const mid = globalThis._rvSavePayload;
        esc(); esc();
        pick(APP, 1); dispatch(X(), 'click', {});
        S().value = 'zz'; S().oninput(); esc(); esc();
        return {same: globalThis._rvSavePayload === before && mid === before,
                dirty: globalThis._rvSavePayloadDirty === dBefore,
                changed: payload('_rvSavePayload').changes || []};
    """)
    assert out["same"]                                # NEGATIVE: payload untouched
    assert out["dirty"]                               # NEGATIVE: nothing marked unsaved
    assert out["changed"] == []                       # NEGATIVE: no r._changed anywhere


@needs_node
def test_escape_and_x_never_clear_accounts_already_assigned(tmp_path):
    out = _run(tmp_path, """
        pick(APP, 0); select(APP, 0); applySel(APP);
        const assigned = view(APP).map(v => v.cells.join('|'));
        const payloadAfterApply = globalThis._rvSavePayload;
        esc(); pick(APP, 1); dispatch(X(), 'click', {}); esc();
        return {assigned, now: view(APP).map(v => v.cells.join('|')),
                payloadSame: globalThis._rvSavePayload === payloadAfterApply,
                nChanges: (payload('_rvSavePayload').changes || []).length};
    """)
    assert out["now"] == out["assigned"]              # NEGATIVE: assigned rows keep their account
    assert out["payloadSame"]
    assert out["nChanges"] == 1                       # the one real assignment is still there


@needs_node
def test_escape_and_x_never_change_the_filter_boxes(tmp_path):
    out = _run(tmp_path, """
        typeIn(APP, 'Description', 'PAYEE');
        typeIn(APP, 'Date', '2025');
        const f0 = [filterBox(APP, 'Description').value, filterBox(APP, 'Date').value];
        const rows0 = view(APP).length;
        pick(APP, 0);
        esc(); S().onfocus(); esc(); dispatch(X(), 'click', {});
        const f1 = [filterBox(APP, 'Description').value, filterBox(APP, 'Date').value];
        return {f0, f1, rows0, rows1: view(APP).length,
                xs: filterCols(APP).filter(c => filterX(APP, c))};
    """)
    assert out["f1"] == out["f0"] == ["PAYEE", "2025"]   # NEGATIVE
    assert out["rows1"] == out["rows0"]
    assert sorted(out["xs"]) == ["Date", "Description"]


@needs_node
def test_escape_and_x_do_not_propagate_to_other_handlers(tmp_path):
    out = _run(tmp_path, """
        let reached = 0;
        S().addEventListener('keydown', () => { reached++; });
        pick(APP, 0);
        const e1 = esc();
        S().onfocus();
        const e2 = esc();
        pick(APP, 0);
        X().addEventListener('click', () => { reached++; });
        const e3 = dispatch(X(), 'click', {});
        return {reached, stopped: [e1.stopped, e2.stopped, e3.stopped]};
    """)
    assert out["reached"] == 0                        # NEGATIVE: nobody downstream saw them
    assert out["stopped"] == [True, True, True]


@needs_node
def test_other_keys_do_nothing(tmp_path):
    out = _run(tmp_path, """
        pick(APP, 0);
        const v = S().value;
        const ev = dispatch(S(), 'keydown', { key: 'Enter' });
        dispatch(S(), 'keydown', { key: 'a' });
        return {kept: S().value === v, stopped: ev.stopped};
    """)
    assert out["kept"]
    assert not out["stopped"]                         # NEGATIVE: only Esc is swallowed


@needs_node
def test_apply_after_a_clear_says_pick_a_value_first_and_changes_nothing(tmp_path):
    out = _run(tmp_path, """
        pick(APP, 0);
        const before = globalThis._rvSavePayload;
        dispatch(X(), 'click', {});
        select(APP, 0); select(APP, 1);
        applySel(APP);
        return {alerts: __alerts.slice(), same: globalThis._rvSavePayload === before,
                changes: payload('_rvSavePayload').changes || []};
    """)
    assert out["alerts"] == ["Pick a value first."]
    assert out["same"] and out["changes"] == []


@needs_node
def test_typing_still_opens_the_dropdown_and_x_follows_the_text(tmp_path):
    out = _run(tmp_path, """
        S().value = 'zzzz-no-match'; S().oninput();
        return {open: isOpen(), x: X().style.display};
    """)
    assert out["open"]
    assert out["x"] == ""


# ---- 4. rendered in every tab that uses the engine ---------------------------------

def test_engine_markup_has_the_x_and_the_stylesheet(tmp_path):
    html = _html(tmp_path)
    assert 'id="rv-picker-clear"' in html
    tag = html.split('id="rv-picker-clear"')[1].split(">")[0]
    assert "display:none" in tag                      # hidden until the box has text
    assert ".picker-clear" in html and ".picker-search.has-text" in html


def test_tds_journal_review_renders_the_picker_x(tmp_path):
    p = tmp_path / "x-tds-journals-review.csv"
    p.write_text(
        "Credit Account,Debit Account,Amount,Confidence,Needs Review,Account Exists,Balanced\n"
        "Income:Example,Assets:TDS Receivable,10.00,High,No,Yes,Yes\n", encoding="utf-8")
    html = tds._load_review_data(str(p), "")
    assert 'id="tdsjr-picker-clear"' in html
    assert ".picker-clear" in html
