"""
UI-04 -- the SUSPENSE badge was set once at load and never cleared, so a row
the user had just reassigned still said SUSPENSE (and still filtered as
"Suspense") until a save + reload.

These run the real client code under node against a fake DOM.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _review_js as js  # noqa: E402
from ui import _review_engine as eng  # noqa: E402
from ui.tabs import gnucash_review as rv  # noqa: E402

pytestmark = pytest.mark.skipif(not js.have_node(), reason="node not installed")

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


def _by_idx(view):
    return {r["idx"]: r for r in view}


def _has_badge(row, text="SUSPENSE"):
    return any(text in c for c in row["cells"])


def test_assigned_suspense_row_loses_badge_without_save_or_reload(tmp_path):
    html = _html(tmp_path)
    out = js.run_js(html, "rv", """
        const before = view(APP);
        select(APP, 0); pick(APP, 0); applySel(APP);
        return {before, after: view(APP)};
    """, tmp_path)
    b, a = _by_idx(out["before"]), _by_idx(out["after"])
    assert _has_badge(b[0]) and _has_badge(b[1])
    assert not _has_badge(a[0]), "assigned row must lose the SUSPENSE badge"


def test_unedited_suspense_rows_keep_the_badge(tmp_path):
    """NEGATIVE: only the assigned row is cleared -- its untouched neighbour is
    still SUSPENSE, and so is the row that was never selected."""
    html = _html(tmp_path)
    out = js.run_js(html, "rv", """
        select(APP, 0); pick(APP, 0); applySel(APP);
        return view(APP);
    """, tmp_path)
    a = _by_idx(out)
    assert _has_badge(a[1]), "unedited suspense row lost its badge"
    assert not _has_badge(a[2]), "a high-confidence row never had one"


def test_assigned_row_moves_out_of_the_suspense_filter(tmp_path):
    html = _html(tmp_path)
    out = js.run_js(html, "rv", """
        select(APP, 0); pick(APP, 0); applySel(APP);
        setStatus(APP, 'suspense');
        const sus = view(APP).map(r => r.idx);
        setStatus(APP, 'override');
        return {sus, ov: view(APP).map(r => r.idx)};
    """, tmp_path)
    assert out["sus"] == [1]
    assert out["ov"] == [0]


def test_apply_to_matching_clears_every_matched_row_only(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER
                 + "2025-05-01,SAME NARRATION,Liabilities:Suspense,,1.00,9.00,suspense,x\n"
                 + "2025-05-02,SAME NARRATION,Liabilities:Suspense,,2.00,8.00,suspense,x\n"
                 + "2025-05-03,OTHER,Liabilities:Suspense,,3.00,7.00,suspense,x\n",
                 encoding="utf-8")
    html = rv._load_review_data(str(p), str(p))
    out = js.run_js(html, "rv", """
        select(APP, 0); pick(APP, 0);
        $id(APP + '-apply-match').onclick();
        return view(APP);
    """, tmp_path)
    a = _by_idx(out)
    assert not _has_badge(a[0]) and not _has_badge(a[1])
    assert _has_badge(a[2])


def test_contra_transfer_badge_is_not_touched_by_an_assign(tmp_path):
    """NEGATIVE: only the Account-column badge is stale; the Date-column
    TRANSFER chip belongs to the row and stays."""
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + _ROWS, encoding="utf-8")
    (tmp_path / "GnuCash_import_ready.contra.json").write_text(
        '{"0": {"status": "confirmed", "reason": "pair"}}', encoding="utf-8")
    html = rv._load_review_data(str(p), str(p))
    out = js.run_js(html, "rv", """
        select(APP, 0); pick(APP, 0); applySel(APP);
        return view(APP);
    """, tmp_path)
    a = _by_idx(out)
    assert any("TRANSFER" in c for c in a[0]["cells"])
    assert not _has_badge(a[0])


def test_engine_default_leaves_badges_alone_for_other_screens(tmp_path):
    """NEGATIVE: a screen that does not opt in (status_col='') keeps the old
    behaviour byte for byte -- the badge is NOT cleared."""
    spec = eng.ReviewSpec(
        app_id="zz", columns=[eng.Column("A", "A"), eng.Column("T", "T")],
        target_col="T", payload_var="_zz",
        picker_items=[eng.PickerItem("x", "x")],
    )
    rows = [{"A": "1", "T": "old", "_badges": {"T": {"text": "FLAG", "cls": "red"}}}]
    html = eng.build_html(spec, rows)
    out = js.run_js(html, "zz", """
        select(APP, 0); pick(APP, 0); applySel(APP);
        return view(APP);
    """, tmp_path)
    assert any("FLAG" in c for c in out[0]["cells"])
    assert "const STATUS_COL = \"\"" in html


def test_status_col_is_declared_only_by_the_review_tab():
    assert rv._spec([], "c", "g", "Deposit", "Withdrawal").status_col == "Confidence"
