"""
UI-08 -- review grid filter row (shared engine, ui/_review_engine.py).

1. The filter row used to scroll away: only the title cells were sticky. Now the
   title row and the filter row stick together as one block (sticky on thead).
2. The cursor was pulled back: a remembered "active filter" re-focused that box
   on EVERY render, so after typing in a filter a row click or Apply moved focus
   into it. Now focus returns to a filter box only if it had focus when the
   render began.

Runtime behaviour (focus, filtering, sorting) runs in node against the harness's
fake DOM. NOT runtime-tested: the sticky CSS itself (no browser here) -- that is
asserted on the generated stylesheet only.
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
)


def _html(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + _ROWS, encoding="utf-8")
    return rv._load_review_data(str(p), str(p))


# ---- the stylesheet (string-level only) --------------------------------------------

def _css(html):
    return html[:html.index("</style>")]


def test_thead_is_one_sticky_block_with_an_opaque_background():
    css = _css(eng._CSS + eng._BODY).replace("%%APP%%", "x")
    block = re.search(r"#x-app thead \{([^}]*)\}", css).group(1)
    assert "position: sticky" in block and "top: 0" in block
    assert re.search(r"background:\s*#", block)             # rows never show through
    z = int(re.search(r"z-index:\s*(\d+)", block).group(1))
    assert z >= 3


def test_title_cells_are_not_separately_sticky_at_top_zero():
    """NEGATIVE: the old per-th sticky made the filter row (not sticky) scroll
    away and could double-stack against the thead block."""
    css = _css(eng._CSS).replace("%%APP%%", "x")
    th = re.search(r"#x-app thead th \{([^}]*)\}", css).group(1)
    assert "sticky" not in th


def test_body_rows_stay_below_the_header_block():
    css = _css(eng._CSS).replace("%%APP%%", "x")
    for sel in (r"#x-app tbody tr\b", r"#x-app tbody td\b"):
        for m in re.finditer(sel + r"[^{]*\{([^}]*)\}", css):
            z = re.search(r"z-index:\s*(\d+)", m.group(1))
            assert not z or int(z.group(1)) < 3


# ---- focus (runtime, node) -----------------------------------------------------------

@needs_node
def test_typing_in_a_filter_keeps_the_cursor_there_as_the_list_narrows(tmp_path):
    html = _html(tmp_path)
    out = js.run_js(html, "rv", """
        typeIn(APP, 'Description', 'PAY');
        const a = focusedCol(), n1 = view(APP).length;
        typeIn(APP, 'Description', 'PAYEE O');
        return {a, b: focusedCol(), n1, fresh: document.activeElement === filterBox(APP, 'Description'), n2: view(APP).length,
                caret: filterBox(APP, 'Description').selectionStart};
    """, tmp_path)
    assert out["a"] == "Description" and out["b"] == "Description"
    assert out["fresh"]                      # focus is on the NEW box, not the replaced one
    assert out["n1"] == 2 and out["n2"] == 1                    # it did narrow
    assert out["caret"] == len("PAYEE O")


@needs_node
def test_after_typing_then_clearing_a_row_click_does_not_move_focus_into_the_filter(tmp_path):
    """NEGATIVE: the old sticky marker re-focused the filter on every render."""
    html = _html(tmp_path)
    out = js.run_js(html, "rv", """
        typeIn(APP, 'Description', 'PAY');
        typeIn(APP, 'Description', '');
        clickAway();
        select(APP, 0);
        const afterClick = focusedCol();
        return {afterClick};
    """, tmp_path)
    assert out["afterClick"] is None


@needs_node
def test_apply_after_typing_does_not_move_focus_into_the_filter(tmp_path):
    """NEGATIVE: Apply re-renders; focus must stay wherever the user put it."""
    html = _html(tmp_path)
    out = js.run_js(html, "rv", """
        typeIn(APP, 'Description', 'PAY');
        clickAway();
        select(APP, 0); pick(APP, 0);
        clickAway();
        applySel(APP);
        return {f: focusedCol()};
    """, tmp_path)
    assert out["f"] is None


@needs_node
def test_status_change_after_typing_does_not_pull_focus_back(tmp_path):
    """NEGATIVE: any other re-render path, same rule."""
    html = _html(tmp_path)
    out = js.run_js(html, "rv", """
        typeIn(APP, 'Description', 'PAY');
        clickAway();
        setStatus(APP, '');
        return {f: focusedCol()};
    """, tmp_path)
    assert out["f"] is None


@needs_node
def test_no_active_filter_marker_is_left_in_the_engine():
    assert "activeFilterCol" not in eng._BODY


# ---- clicks and typing in a filter never sort ------------------------------------------

@needs_node
def test_clicking_or_typing_in_a_filter_box_never_sorts(tmp_path):
    """NEGATIVE: filter boxes live in the filter row, not in a title cell, and
    neither a click nor typing changes the sort column / direction."""
    html = _html(tmp_path)
    out = js.run_js(html, "rv", """
        const th = () => $id(APP + '-thead').innerHTML;
        const before = th();
        const box = filterBox(APP, 'Description');
        let stopped = false;
        box.onclick({ stopPropagation() { stopped = true; } });
        typeIn(APP, 'Description', 'PAY');
        typeIn(APP, 'Description', '');
        return {same: th() === before, stopped,
                inHead: before.indexOf('<input') !== -1};
    """, tmp_path)
    assert out["same"] and out["stopped"] and not out["inHead"]


@needs_node
def test_clicking_a_title_cell_still_sorts(tmp_path):
    """Control: the sort path itself still works (th onclick is not broken)."""
    assert "th.onclick" in eng._BODY and "sortAsc = !sortAsc" in eng._BODY


# ---- every tab that uses the engine still renders ----------------------------------------

@pytest.mark.parametrize("modname", ["gnucash_review", "itr_mapping_review",
                                     "krc_gnucash_review", "tds_journal_review"])
def test_every_engine_tab_module_still_imports_and_the_engine_builds(modname):
    import importlib
    mod = importlib.import_module(f"ui.tabs.{modname}")
    assert mod is not None
    spec = eng.ReviewSpec(app_id="zz", columns=[eng.Column("A", "A")],
                          target_col="A", payload_var="_zzPayload")
    html = eng.build_html(spec, [{"A": "x"}])
    assert "%%" not in html                                  # every token filled
    assert "thead" in html and "filter-row" in html
