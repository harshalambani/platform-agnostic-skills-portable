"""
UI-05 -- "Conf" -> "Match type", with a coloured left edge per match-type band
and a legend. CSV column stays "Confidence"; TRANSFER tints unchanged; the
IMP-09 violet DORMANT? badge stays distinct.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))

import _review_js as js  # noqa: E402
from ui.tabs import gnucash_review as rv  # noqa: E402

_HEADER = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"
KNOWN = ("override", "high", "history", "medium", "llm", "smart", "weak", "low",
         "suspense", "none")


def _pres(conf, contra=None, reason=""):
    row = {"Confidence": conf, "MatchReason": reason}
    rv._row_presentation(row, contra)
    return row


def test_header_label_is_match_type_but_key_stays_confidence():
    spec = rv._spec([], "c", "g", "Deposit", "Withdrawal")
    col = next(c for c in spec.columns if c.key == "Confidence")
    assert col.label == "Match type"
    assert not any(c.label == "Conf" for c in spec.columns)


def test_saved_csv_header_is_still_confidence(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + "2025-05-01,X,Expense:Food,,1.00,9.00,suspense,r\n", encoding="utf-8")
    row = {"Date": "2025-05-01", "Description": "X", "Account": "Expense:Food",
           "Deposit": "", "Withdrawal": "1.00", "Balance": "9.00",
           "Confidence": "override", "MatchReason": "User override (review)"}
    payload = {"context": {"csv_path": str(p), "gnucash_file": str(p)},
               "changes": [], "all_rows": [row]}
    rv._save_changes(json.dumps(payload))
    with open(p, newline="", encoding="utf-8") as f:
        header = next(csv.reader(f))
    assert "Confidence" in header and "Match type" not in header


@pytest.mark.parametrize("conf,colour", [
    ("override", "blue"), ("high", "green"), ("history", "green"),
    ("medium", "amber"), ("llm", "amber"), ("smart", "amber"),
    ("weak", "orange"), ("low", "orange"),
    ("suspense", "red"), ("none", "red"),
])
def test_each_match_type_gets_its_band(conf, colour):
    assert _pres(conf)["_band"] == f"accent-{colour}"


def test_every_match_type_maps_to_exactly_one_colour():
    """NEGATIVE: no type is in two bands, and each known type is in one."""
    seen = {}
    for colour, _label, types in rv.MATCH_BANDS:
        for t in types:
            assert t not in seen, f"{t} is in both {seen[t]} and {colour}"
            seen[t] = colour
    assert set(KNOWN) <= set(seen)
    for t in KNOWN:
        cls = [c for c in _pres(t)["_band"].split() if c.startswith("accent-")]
        assert len(cls) == 1


@pytest.mark.parametrize("conf", ["", "banana", "HIGHISH", "unknown"])
def test_unknown_match_type_gets_no_colour_not_red(conf):
    """NEGATIVE: an unrecognised value is uncoloured; red is reserved for
    suspense/unmatched."""
    row = _pres(conf)
    assert row["_band"] == "" and "accent" not in row["_rowclass"]
    assert rv.match_band(conf) is None


def test_match_type_is_case_insensitive():
    assert rv.match_band(" High ") == "green"


def test_transfer_tints_are_unchanged():
    conf = _pres("high", {"status": "confirmed", "reason": "r"})
    poss = _pres("high", {"status": "possible", "reason": "r"})
    assert "tone-green" in conf["_rowclass"].split()
    assert "tone-amber" in poss["_rowclass"].split()
    assert conf["_badges"]["Date"] == {"text": "TRANSFER", "cls": "green"}
    assert poss["_badges"]["Date"] == {"text": "POSSIBLE", "cls": "amber"}
    # and a non-contra row carries no tone
    assert not any(c.startswith("tone-") for c in _pres("high")["_rowclass"].split())


def test_dormant_violet_badge_is_distinct_from_every_band():
    """NEGATIVE: violet is not a band colour, and the DORMANT? badge is intact."""
    assert "violet" not in [c for c, _l, _t in rv.MATCH_BANDS]
    row = _pres("high", reason="target looks dormant (no postings for 400 days)")
    assert row["_badges"]["Account"]["cls"] == "violet"
    assert row["_badges"]["Account"]["text"] == "DORMANT?"


def test_legend_lists_every_band_and_renders(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + "2025-05-01,X,Expense:Food,,1.00,9.00,high,r\n", encoding="utf-8")
    html = rv._load_review_data(str(p), str(p))
    for colour, label, _t in rv.MATCH_BANDS:
        assert f'sw {colour}"' in html and label in html
    assert "Match type" in html


@pytest.mark.skipif(not js.have_node(), reason="node not installed")
def test_band_follows_the_row_when_it_is_reassigned(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER
                 + "2025-05-01,X,Liabilities:Suspense,,1.00,9.00,suspense,r\n"
                 + "2025-05-02,Y,Liabilities:Suspense,,1.00,8.00,suspense,r\n",
                 encoding="utf-8")
    html = rv._load_review_data(str(p), str(p))
    out = js.run_js(html, "rv", """
        const before = view(APP);
        select(APP, 0); pick(APP, 0); applySel(APP);
        return {before, after: view(APP)};
    """, tmp_path)
    b = {r["idx"]: r for r in out["before"]}
    a = {r["idx"]: r for r in out["after"]}
    assert "accent-red" in b[0]["cls"]
    assert "accent-blue" in a[0]["cls"] and "accent-red" not in a[0]["cls"]
    assert "accent-red" in a[1]["cls"], "untouched row keeps its band"
