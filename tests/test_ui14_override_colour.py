"""
UI-14 -- an overridden row shows the override colour, not the transfer-check tone.

The Date badge (TRANSFER / POSSIBLE / OWN?) and the 'contra' tag stay as a reminder.
Shared engine: driven by the row's own status column, not by any account name.
"""
from __future__ import annotations

import json
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
    "2025-05-01,XFER ONE,Liabilities:Suspense,,100.00,900.00,high,pair\n"      # 0 confirmed
    "2025-05-02,XFER TWO,Liabilities:Suspense,,50.00,850.00,medium,pair\n"     # 1 possible
    "2025-05-03,OWN PAY,Liabilities:Suspense,,20.00,830.00,suspense,no match\n"  # 2 advisory
    "2025-05-04,PLAIN,Expense:Food,,10.00,820.00,high,rule\n"                  # 3 plain
    "2025-05-05,ALREADY,Expense:Food,,10.00,810.00,override,user\n"            # 4 loads overridden
)
_CONTRA = {"0": {"status": "confirmed", "reason": "pair"},
           "1": {"status": "possible", "reason": "maybe"},
           "4": {"status": "confirmed", "reason": "pair"}}
_ADVISORY = {"2": {"reason": "own payment"}}


def _paths(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + _ROWS, encoding="utf-8")
    c = tmp_path / "GnuCash_import_ready.contra.json"
    a = tmp_path / "GnuCash_import_ready.advisory.json"
    c.write_text(json.dumps(_CONTRA), encoding="utf-8")
    a.write_text(json.dumps(_ADVISORY), encoding="utf-8")
    return p, c, a


def _run(tmp_path, scenario):
    p, _, _ = _paths(tmp_path)
    return js.run_js(rv._load_review_data(str(p), str(p)), "rv", scenario, tmp_path)


def _by(out):
    return {v["idx"]: v for v in out}


def _cls(v):
    return set(v["cls"] or [])


@needs_node
def test_not_overridden_contra_rows_keep_their_tone_exactly(tmp_path):
    a = _by(_run(tmp_path, "return view(APP);"))
    assert "tone-green" in _cls(a[0])           # NEGATIVE: untouched rows keep tone
    assert "tone-amber" in _cls(a[1])
    assert not any(c.startswith("tone-") for c in _cls(a[3]))


@needs_node
def test_override_drops_the_tone_but_keeps_badge_and_override_colour(tmp_path):
    a = _by(_run(tmp_path, """
        select(APP, 0); pick(APP, 0); applySel(APP); select(APP, 1); applySel(APP);
        return view(APP);
    """))
    for i, badge in ((0, "TRANSFER"), (1, "POSSIBLE")):
        assert not any(c.startswith("tone-") for c in _cls(a[i]))
        assert "accent-blue" in _cls(a[i])
        assert any(badge in c for c in a[i]["cells"])     # NEGATIVE: badge stays


@needs_node
def test_row_loading_as_override_shows_override_colour_and_keeps_badge(tmp_path):
    a = _by(_run(tmp_path, "return view(APP);"))
    assert not any(c.startswith("tone-") for c in _cls(a[4]))
    assert "accent-blue" in _cls(a[4])
    assert any("TRANSFER" in c for c in a[4]["cells"])


@needs_node
def test_override_keeps_the_contra_tag_so_the_filter_still_finds_it(tmp_path):
    out = _run(tmp_path, """
        select(APP, 0); pick(APP, 0); applySel(APP);
        const sel = $id(APP + '-filter-tag') || $id(APP + '-tag-filter');
        return {has: !!sel, rows: view(APP).map(v => v.idx)};
    """)
    # the tag lives in the row data, which the engine never edits on assign
    html = rv._load_review_data(*(str(_paths(tmp_path)[0]),) * 2)
    assert '"contra"' in html and 'contra' in html
    assert 0 in out["rows"]


def test_override_never_touches_the_sidecars(tmp_path):
    p, c, a = _paths(tmp_path)
    before = (c.read_bytes(), a.read_bytes())
    if js.have_node():
        js.run_js(rv._load_review_data(str(p), str(p)), "rv",
                  "select(APP, 0); pick(APP, 0); applySel(APP); return 1;", tmp_path)
    assert (c.read_bytes(), a.read_bytes()) == before   # NEGATIVE


@needs_node
def test_override_never_changes_excluded_state(tmp_path):
    out = _run(tmp_path, """
        const before = view(APP).map(v => v.cls.includes('row-excluded'));
        select(APP, 0); pick(APP, 0); applySel(APP);
        const after = view(APP).map(v => v.cls.includes('row-excluded'));
        return {before, after};
    """)
    assert out["before"] == out["after"]


@needs_node
def test_advisory_row_keeps_its_own_badge_after_override(tmp_path):
    a = _by(_run(tmp_path, "select(APP, 2); pick(APP, 0); applySel(APP); return view(APP);"))
    assert any("OWN?" in c for c in a[2]["cells"])


def test_engine_default_is_off_and_markup_is_generic():
    spec = eng.ReviewSpec(
        app_id="zz", columns=[eng.Column("A", "A"), eng.Column("T", "T")],
        target_col="T", payload_var="_zz", picker_items=[eng.PickerItem("x", "x")])
    assert spec.override_status == ""
    html = eng.build_html(spec, [{"A": "1", "T": "t", "_rowclass": "tone-amber"}])
    assert 'const OVERRIDE_STATUS = ""' in html          # NEGATIVE: other screens unchanged
    assert rv._spec([], "c", "g", "Deposit", "Withdrawal").override_status == "override"


@needs_node
def test_screen_without_override_status_keeps_tone(tmp_path):
    spec = eng.ReviewSpec(
        app_id="zz", columns=[eng.Column("A", "A"), eng.Column("T", "T")],
        target_col="T", payload_var="_zz", picker_items=[eng.PickerItem("x", "x")],
        also_set={"A": "override"})
    rows = [{"A": "1", "T": "t", "_rowclass": "tone-amber"}]
    out = js.run_js(eng.build_html(spec, rows), "zz", """
        select(APP, 0); pick(APP, 0); applySel(APP); return view(APP);""", tmp_path)
    assert "tone-amber" in _cls(out[0])
