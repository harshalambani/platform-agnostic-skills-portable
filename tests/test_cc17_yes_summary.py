"""
tests/test_cc17_yes_summary.py -- CC-17: the YES Bank summary printed in two
interleaved columns is read by position, and a figure set is accepted only when
previous + purchases - payments = total.

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

ROWS = [
    "05/03/2025 SAMPLE STORE DELHI RETAIL 2,400.00 Dr",
    "08/03/2025 EXAMPLE CAFE PUNE DINING 1,000.00 Dr",
    "12/03/2025 PAYMENT RECEIVED THANK YOU 2,650.00 Cr",
]


def _yes(prev="Rs. 1,250.00", purchases="Rs. 3,400.00 Dr", total="Rs. 2,000.00", payments="Rs. 2,650.00 Cr",
         rows=None):
    return [
        "Previous Balance :",
        prev,
        "Statement Period: Credit Limit:",
        "21/02/2025 To 20/03/2025 Rs. 5,00,000.00 Current Purchases / Cash Advance",
        "Available Credit Limit: & Other Charges :",
        f"Statement Date : 20/03/2025 Rs. 4,98,000.00 {purchases}".rstrip(),
        "Total Amount Due: Cash Limit: Points Earned : 123",
        f"{total} Rs. 50,000.00",
        "Payment & Credits Received :",
        f"Minimum Amount Due: Available Cash Limit: {payments}",
        "Rs. 100.00 Rs. 50,000.00",
        "Payment Due Date: 09/04/2025 | NO PYMT REQD YES ONLINE Other Mode",
        *(ROWS if rows is None else rows),
    ]


def _st(lines):
    return m.parse_statement(lines, "YES", "Sample", "y.pdf")


def test_two_column_summary_is_read_by_position_and_ties_out():
    st = _st(_yes())
    assert not st.parsed.missing, st.parsed.missing
    s = st.parsed.summary
    assert (s["prev"], s["purchases"], s["payments"], s["total"]) == (1250.0, 3400.0, 2650.0, 2000.0)
    assert len(st.parsed.rows) == 3
    assert m.tie_out(st).result == "PASS"


def test_purchases_without_dr_marker_is_read():
    # "Dr absent when the purchases figure is 0"
    rows = ["12/03/2025 PAYMENT RECEIVED THANK YOU 1,250.00 Cr"]
    st = _st(_yes(purchases="Rs. 0.00", total="Rs. 0.00", payments="Rs. 1,250.00 Cr", rows=rows))
    assert st.parsed.summary["purchases"] == 0.0
    assert m.tie_out(st).result == "PASS"


def test_previous_balance_credit_flag_is_honoured():
    # prev 500 Cr (-500) + 3400 - 2650 = 250
    st = _st(_yes(prev="Rs. 500.00 Cr", total="Rs. 250.00"))
    assert st.parsed.summary["prev"] == -500.0
    assert st.parsed.summary["total"] == 250.0


def test_wrapped_rows_do_not_break_row_parsing():
    rows = [
        "SAMPLE STORE WITH A VERY LONG PAYMENT & CREDIT NAME:",
        "05/03/2025 DELHI RETAIL 2,400.00 Dr",
        "Ref No: 123456789",
        "EXAMPLE CAFE WITH A LONG NAME",
        "08/03/2025 PUNE DINING 1,000.00 Dr",
        "Ref No :",
        "12/03/2025 PAYMENT RECEIVED THANK YOU 2,650.00 Cr",
    ]
    st = _st(_yes(rows=rows))
    assert not st.parsed.errors, st.parsed.errors
    assert len(st.parsed.rows) == 3
    assert st.parsed.summary["total"] == 2000.0
    assert m.tie_out(st).result == "PASS"


# --- negative ---------------------------------------------------------------

def test_figures_that_do_not_satisfy_the_equation_are_reported_missing_not_used():
    st = _st(_yes(total="Rs. 2,111.00"))
    assert st.parsed.summary == {}
    assert any("summary figures" in x for x in st.parsed.missing)
    t = m.tie_out(st)
    assert t.result != "PASS"


def test_a_figure_that_is_not_found_is_missing_never_zero():
    lines = [ln for ln in _yes() if not ln.startswith("Minimum Amount Due")]
    st = _st(lines)
    assert st.parsed.summary == {}
    assert any("payments" in x or "summary figures" in x for x in st.parsed.missing)
    assert m.tie_out(st).result != "PASS"


def test_rows_that_do_not_reach_the_total_fail():
    rows = ROWS[:2]          # the payment row is missing
    st = _st(_yes(rows=rows))
    assert st.parsed.summary          # figures are fine ...
    t = m.tie_out(st)
    assert t.result == "FAIL"         # ... the rows are not


def test_inline_layout_is_unchanged():
    lines = [
        "Previous Balance : Rs. 3,000.00",
        "Current Purchases / Cash Advance : Rs. 500.00",
        "Payment & Credits Received : Rs. 3,000.00",
        "Total Amount Due : Rs. 500.00",
        "01/09/2025 EXAMPLE SHOP FOOD 500.00 Dr",
        "02/09/2025 PAYMENT RECEIVED 3,000.00 Cr",
    ]
    st = _st(lines)
    assert st.parsed.summary["total"] == 500.0
    assert len(st.parsed.rows) == 2
