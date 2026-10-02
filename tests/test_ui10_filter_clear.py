"""
UI-10 -- column filters are easy to clear (shared engine, all four review tabs).

1. An X inside a filter box, only while that box has text.
2. Esc in a filter box empties that box (and goes no further).
3. "N filters - Clear all" beside the rows counter, only while a box has text; it
   empties every column box and leaves the "Filter:" dropdown alone.
4. A filtered box and its column header are marked.

Runtime behaviour runs in node against the harness's fake DOM. NOT runtime-tested:
the look of the marking (no browser here) -- only the stylesheet text.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _review_js as js  # noqa: E402
from ui import _review_engine as eng  # noqa: E402
from ui.tabs import gnucash_review as rv  # noqa: E402

needs_node = pytest.mark.skipif(not js.have_node(), reason="node not installed")

_HEADER = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"
_ROWS = (
    "2025-05-01,UNKNOWN PAYEE ONE,Liabilities:Suspense,,100.00,900.00,suspense,no match\n"
    "2025-05-02,UNKNOWN PAYEE TWO,Liabilities:Suspense,,50.00,850.00,suspense,no match\n"
    "2025-05-03,COFFEE,Expense:Food,,20.00,830.00,high,rule\n"
    "2025-05-04,TEA,Expense:Food,,10.00,820.00,high,rule\n"
)


def _html(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + _ROWS, encoding="utf-8")
    return rv._load_review_data(str(p), str(p))


def _run(tmp_path, scenario):
    return js.run_js(_html(tmp_path), "rv", scenario, tmp_path)


# ---- 1. the X ----------------------------------------------------------------------

@needs_node
def test_x_shows_only_on_a_box_with_text_and_clears_only_that_box(tmp_path):
    out = _run(tmp_path, """
        const none = filterCols(APP).map(c => !!filterX(APP, c));
        typeIn(APP, 'Description', 'PAYEE');
        typeIn(APP, 'Date', '2025');
        const shown = filterCols(APP).filter(c => filterX(APP, c));
        filterX(APP, 'Description').onclick({stopPropagation(){}});
        return {none, shown, desc: filterBox(APP, 'Description').value,
                date: filterBox(APP, 'Date').value,
                after: filterCols(APP).filter(c => filterX(APP, c)),
                rows: view(APP).length};
    """)
    assert not any(out["none"])                       # NEGATIVE: never on an empty box
    assert sorted(out["shown"]) == ["Date", "Description"]
    assert out["desc"] == ""
    assert out["date"] == "2025"                      # NEGATIVE: the other box untouched
    assert out["after"] == ["Date"]                   # the X is gone from the emptied box
    assert out["rows"] == 4                           # Date "2025" matches all four


@needs_node
def test_x_leaves_focus_in_the_emptied_box(tmp_path):
    out = _run(tmp_path, """
        typeIn(APP, 'Description', 'PAYEE');
        clickAway();                                  // e.g. mouse is on the X
        filterX(APP, 'Description').onclick({stopPropagation(){}});
        return {col: focusedCol(), active: document.activeElement === filterBox(APP, 'Description')};
    """)
    assert out["col"] == "Description" and out["active"]


@needs_node
def test_x_keeps_the_dropdown_selection_and_pending_changes(tmp_path):
    out = _run(tmp_path, """
        $id(APP + '-status').value = 'suspense';
        setStatus(APP, 'suspense');
        select(APP, 0); pick(APP, 0); applySel(APP);
        select(APP, 1);
        typeIn(APP, 'Description', 'PAYEE');
        filterX(APP, 'Description').onclick({stopPropagation(){}});
        return {stats: $id(APP + '-stats').textContent,
                status: $id(APP + '-status').value, rows: view(APP).length};
    """)
    assert "1 changed" in out["stats"] and "1 selected" in out["stats"]
    assert out["status"] == "suspense"
    assert out["rows"] == 1          # NEGATIVE: still the suspense bucket (1 left after the assign), not all 4


# ---- 2. Esc ------------------------------------------------------------------------

@needs_node
def test_esc_empties_that_box_and_stops_there(tmp_path):
    out = _run(tmp_path, """
        typeIn(APP, 'Description', 'PAYEE');
        typeIn(APP, 'Date', '2025');
        const ev = pressEsc(APP, 'Description');
        return {ev: {stopped: ev.stopped, prevented: ev.prevented},
                desc: filterBox(APP, 'Description').value, date: filterBox(APP, 'Date').value,
                col: focusedCol()};
    """)
    assert out["ev"] == {"stopped": True, "prevented": True}
    assert out["desc"] == "" and out["date"] == "2025"
    assert out["col"] == "Description"


@needs_node
def test_esc_on_an_empty_box_does_nothing_harmful(tmp_path):
    """NEGATIVE: nothing is cleared elsewhere, nothing prevented, no crash, and the
    event still does not bubble."""
    out = _run(tmp_path, """
        typeIn(APP, 'Date', '2025');
        filterBox(APP, 'Description').focus();
        const ev = pressEsc(APP, 'Description');
        return {prevented: ev.prevented, stopped: ev.stopped,
                date: filterBox(APP, 'Date').value, rows: view(APP).length};
    """)
    assert out["prevented"] is False and out["stopped"] is True
    assert out["date"] == "2025" and out["rows"] == 4


@needs_node
def test_other_keys_are_ignored(tmp_path):
    out = _run(tmp_path, """
        typeIn(APP, 'Description', 'PAYEE');
        const ev = {key: 'a', stopPropagation(){ this.s = true; }, preventDefault(){ this.p = true; }};
        filterBox(APP, 'Description').onkeydown(ev);
        return {v: filterBox(APP, 'Description').value, s: !!ev.s, p: !!ev.p};
    """)
    assert out == {"v": "PAYEE", "s": False, "p": False}


# ---- 3. Clear all ------------------------------------------------------------------

@needs_node
def test_clear_all_link_only_while_a_box_has_text_and_counts_filters(tmp_path):
    out = _run(tmp_path, """
        const l = clearAllLink(APP);
        const idle = {d: l.style.display, t: l.textContent};
        typeIn(APP, 'Description', 'PAYEE');
        const one = {d: l.style.display, t: l.textContent};
        typeIn(APP, 'Date', '2025');
        const two = {d: l.style.display, t: l.textContent};
        filterX(APP, 'Date').onclick({stopPropagation(){}});
        filterX(APP, 'Description').onclick({stopPropagation(){}});
        return {idle, one, two, end: {d: l.style.display, t: l.textContent}};
    """)
    assert out["idle"] == {"d": "none", "t": ""}                    # NEGATIVE
    assert out["one"]["d"] != "none" and out["one"]["t"].startswith("1 filter ")
    assert out["two"]["t"].startswith("2 filters ") and "Clear all" in out["two"]["t"]
    assert out["end"] == {"d": "none", "t": ""}


@needs_node
def test_clear_all_empties_every_box_but_not_the_dropdown_or_work(tmp_path):
    out = _run(tmp_path, """
        $id(APP + '-status').value = 'suspense';
        setStatus(APP, 'suspense');
        select(APP, 0); pick(APP, 0); applySel(APP);
        select(APP, 1);
        typeIn(APP, 'Description', 'PAYEE');
        typeIn(APP, 'Date', '2025');
        clickAway();
        clearAllLink(APP).onclick({preventDefault(){ this.p = true; }});
        return {vals: filterCols(APP).map(c => filterBox(APP, c).value),
                xs: filterCols(APP).filter(c => filterX(APP, c)),
                status: $id(APP + '-status').value, rows: view(APP).length,
                stats: $id(APP + '-stats').textContent,
                link: clearAllLink(APP).style.display, col: focusedCol()};
    """)
    assert set(out["vals"]) == {""}
    assert out["xs"] == []
    assert out["status"] == "suspense" and out["rows"] == 1       # NEGATIVE: dropdown kept (1 suspense row left), not all 4
    assert "1 changed" in out["stats"] and "1 selected" in out["stats"]
    assert out["link"] == "none"
    assert out["col"] in ("Description", "Date")                  # an emptied box, not the page


@needs_node
def test_clear_all_with_nothing_to_clear_is_a_no_op(tmp_path):
    out = _run(tmp_path, """
        const before = view(APP).length;
        clearAllLink(APP).onclick({preventDefault(){}});
        return {before, after: view(APP).length, col: focusedCol()};
    """)
    assert out["before"] == out["after"] == 4 and out["col"] is None


# ---- 4. marking --------------------------------------------------------------------

@needs_node
def test_filtered_box_and_header_are_marked_then_unmarked(tmp_path):
    out = _run(tmp_path, """
        const none = theadHtml(APP);
        typeIn(APP, 'Description', 'PAYEE');
        const withText = {html: theadHtml(APP), cls: filterBox(APP, 'Description').className,
                          other: filterBox(APP, 'Date').className};
        filterX(APP, 'Description').onclick({stopPropagation(){}});
        return {none, withText, cleared: theadHtml(APP),
                clsAfter: filterBox(APP, 'Description').className};
    """)
    assert "filter-mark" not in out["none"] and 'class="filtered"' not in out["none"]
    assert out["withText"]["cls"] == "has-text" and out["withText"]["other"] == ""
    assert out["withText"]["html"].count("filter-mark") == 1       # only that header
    assert 'data-col="Description" class="filtered"' in out["withText"]["html"]
    # NEGATIVE: marking disappears once the box is cleared
    assert "filter-mark" not in out["cleared"] and out["clsAfter"] == ""


def test_marking_styles_differ_from_the_focus_state():
    css = eng._CSS.replace("%%APP%%", "x")
    focus = re.search(r"filter-row input:focus \{([^}]*)\}", css).group(1)
    marked = re.search(r"filter-row input\.has-text \{([^}]*)\}", css).group(1)
    assert "#2563eb" in focus
    assert "#2563eb" not in marked and "border-color" in marked and "background" in marked


# ---- UI-08 still holds -------------------------------------------------------------

def test_filter_row_is_still_sticky_with_the_title_row():
    css = eng._CSS.replace("%%APP%%", "x")
    block = re.search(r"#x-app thead \{([^}]*)\}", css).group(1)
    assert "position: sticky" in block and "top: 0" in block


@needs_node
def test_typing_still_keeps_focus_and_a_row_click_does_not_steal_it(tmp_path):
    out = _run(tmp_path, """
        typeIn(APP, 'Description', 'PAYEE');
        const typing = focusedCol();
        clickAway();
        select(APP, 0);
        return {typing, after: focusedCol()};
    """)
    assert out["typing"] == "Description"
    assert out["after"] is None      # NEGATIVE: no sticky refocus (UI-08), nor leftover focusAfter
