"""
tests/test_cc18_axis_redemptions.py -- CC-18: an Axis summary that leaves reward
redemption credits out of Credits is explained, but only when the redemptions
account for the mismatch exactly.

Everything is synthetic: made-up merchants and amounts.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_cc_transactions" / "scripts" / "create_cc_transaction_list.py"
for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)


def _load_module():
    spec = importlib.util.spec_from_file_location("create_cc_transaction_list", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_module()


def _axis(summary, redemptions=("SMS BASED REDEMPTION/ABC 200.00 Cr",)):
    return [
        "01/04/2025 - 30/04/2025 15/05/2025 25/05/2025",
        summary,
        "05/04/2025 EXAMPLE MART MUMBAI DEPT STORES 1,000.00 Dr",
        "10/04/2025 BBPS PAYMENT RECEIVED - 123456 PAYMENT 2,000.00 Cr",
        "15/04/2025 CASHBACK CREDIT CASHBACK 30.00 Cr",
        *[f"20/04/2025 {x}" for x in redemptions],
    ]


# credits figure 30.00 leaves the 200.00 redemption out; the true balance is 770.00
LEAVES_OUT = "2,000.00 Dr 2,000.00 30.00 1,000.00 0.00 0.00 770.00 Dr"
INCLUDES = "2,000.00 Dr 2,000.00 230.00 1,000.00 0.00 0.00 770.00 Dr"


def _tie(lines):
    st = m.parse_statement(lines, "Axis", "Sample", "a.pdf")
    return st, m.tie_out(st)


def test_summary_that_leaves_redemptions_out_passes_with_the_note():
    st, t = _tie(_axis(LEAVES_OUT))
    assert len(st.parsed.rows) == 4
    assert t.result == "PASS", t.note
    assert "the bank's summary leaves 1 reward redemption credit(s) out of Credits" in t.note
    assert "do not add up" not in t.note


def test_several_redemptions_are_summed():
    red = ("SMS BASED REDEMPTION/ABC 120.00 Cr", "SMS BASED REDEMPTION/DEF 80.00 Cr")
    st, t = _tie(_axis(LEAVES_OUT, red))
    assert t.result == "PASS", t.note
    assert "leaves 2 reward redemption credit(s)" in t.note


def test_summary_that_already_includes_redemptions_passes_without_the_note():
    _, t = _tie(_axis(INCLUDES))
    assert t.result == "PASS", t.note
    assert "redemption" not in t.note


def test_any_other_mismatch_still_fails():
    # no redemption rows: the summary purchases figure is simply wrong
    lines = _axis("2,000.00 Dr 2,000.00 30.00 1,050.00 0.00 0.00 970.00 Dr", redemptions=())
    _, t = _tie(lines)
    assert t.result == "FAIL"
    assert "do not add up" in t.note


def test_a_mismatch_only_partly_explained_by_redemptions_still_fails():
    # 250.00 gap in the summary, redemptions explain only 200.00
    _, t = _tie(_axis("2,000.00 Dr 2,000.00 30.00 1,050.00 0.00 0.00 770.00 Dr"))
    assert t.result == "FAIL"
    assert "redemption" not in t.note
    assert "do not add up" in t.note


def test_redemption_wording_on_a_debit_row_is_not_an_explanation():
    lines = _axis("2,000.00 Dr 2,000.00 30.00 1,000.00 0.00 0.00 770.00 Dr",
                  redemptions=("SMS BASED REDEMPTION/ABC 200.00 Dr",))
    _, t = _tie(lines)
    assert t.result == "FAIL"


def test_redemptions_do_not_excuse_rows_that_miss_the_total():
    # the summary is explained by the redemption, but the rows do not reach the printed total
    lines = _axis("2,000.00 Dr 2,000.00 30.00 1,000.00 0.00 0.00 760.00 Dr")
    _, t = _tie(lines)
    assert t.result == "FAIL"
