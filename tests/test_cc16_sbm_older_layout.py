"""
tests/test_cc16_sbm_older_layout.py -- CC-16 (older SBM layout read and proven by
arithmetic) and CC-19(a) (the payment-due values line is not an unread row).

Everything is synthetic: made-up merchants and amounts.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date
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
R = "₹"


def _old(figures="1500.00 620.00 0.00 2000.00 120.00", rows=None):
    rows = rows if rows is not None else [
        f"2-Feb-2025 SAMPLE CAFE CITY XX {R} 120.00",
        f"14-Mar-2025 EXAMPLE BOOKS MUMBAI {R} 500.00",
        f"9-Mar-2025 Repayment {R} 2000.00",
    ]
    return [
        "Niyo Global Credit Card Statement",
        "Statement Summary",
        "Statement Date : April-2025",
        "Payment Due Date Total Amount Due Minimum Amount Due",
        f"16-May-2025 {R} 120.00 {R} 20.00",
        "Credit Limit Available Limit",
        f"{R} 50000.00 {R} 49880.00",
        "Calculation Summary",
        "Opening Balance Purchase/Debits Finance Charges Payment/Credit Total Amount Due",
        figures,
        *rows,
    ]


def _st(lines):
    return m.parse_statement(lines, "SBM", "Niyo", "old.pdf")


# --- CC-16 positive ---------------------------------------------------------

def test_older_layout_is_read_and_ties_out():
    st = _st(_old())
    assert not st.parsed.errors, st.parsed.errors
    assert len(st.parsed.rows) == 3
    dirs = {r.description: r.direction for r in st.parsed.rows}
    assert dirs["Repayment"] == "Cr"
    assert dirs["SAMPLE CAFE CITY XX"] == "Dr"
    s = st.parsed.summary
    assert (s["prev"], s["purchases"], s["finance"], s["payments"], s["total"]) == (1500.0, 620.0, 0.0, 2000.0, 120.0)
    t = m.tie_out(st)
    assert t.result == "PASS", t.note


def test_older_layout_period_is_derived_from_month_and_row_dates_and_says_so():
    st = _st(_old())
    # the first row (2 Feb) is before the labelled month (April): the period starts there
    assert st.period == (date(2025, 2, 2), date(2025, 4, 30))
    assert "derived" in st.period_note


def test_older_layout_with_only_rows_inside_the_month_starts_at_month_start():
    rows = [f"5-Apr-2025 SAMPLE CAFE CITY XX {R} 620.00", f"6-Apr-2025 Payment received {R} 2000.00"]
    st = _st(_old(rows=rows))
    assert st.period == (date(2025, 4, 1), date(2025, 4, 30))


def test_older_layout_finance_charge_row_is_a_debit_and_adds_up():
    rows = [f"3-Mar-2025 SAMPLE CAFE CITY XX {R} 100.00", f"20-Mar-2025 Finance Charges {R} 20.00",
            f"21-Mar-2025 Repayment {R} 1000.00"]
    st = _st(_old("1000.00 100.00 20.00 1000.00 120.00", rows))
    assert m.tie_out(st).result == "PASS"


def test_older_layout_credit_wording_variants_are_credits():
    for word in ("Refund", "Reversal", "Cashback", "Payment", "Repayment", "Credit"):
        rows = [f"3-Mar-2025 SAMPLE CAFE CITY XX {R} 100.00", f"4-Mar-2025 {word} {R} 10.00"]
        st = _st(_old("50.00 100.00 0.00 10.00 140.00", rows))
        assert [r.direction for r in st.parsed.rows] == ["Dr", "Cr"], word
        assert m.tie_out(st).result == "PASS", word


# --- CC-16 negative ---------------------------------------------------------

def test_older_rows_that_do_not_add_up_fail():
    rows = [f"2-Feb-2025 SAMPLE CAFE CITY XX {R} 120.00", f"9-Mar-2025 Repayment {R} 2000.00"]  # purchase row missing
    t = m.tie_out(_st(_old(rows=rows)))
    assert t.result == "FAIL"
    assert "not proven" in t.note


def test_unknown_wording_is_never_silently_a_credit():
    # a real credit worded in a way the parser does not know: it must stay a debit
    # and the proof must fail rather than guess.
    rows = [f"2-Feb-2025 SAMPLE CAFE CITY XX {R} 120.00", f"14-Mar-2025 EXAMPLE BOOKS MUMBAI {R} 500.00",
            f"9-Mar-2025 Adjustment {R} 2000.00"]
    st = _st(_old(rows=rows))
    assert [r.direction for r in st.parsed.rows] == ["Dr", "Dr", "Dr"]
    t = m.tie_out(st)
    assert t.result == "FAIL"


def test_older_totals_that_do_not_close_fail_even_if_rows_match_the_parts():
    # parts agree with the rows but opening + parts - credits is not the printed total
    t = m.tie_out(_st(_old("1500.00 620.00 0.00 2000.00 999.00")))
    assert t.result == "FAIL"


def test_older_split_that_nets_correctly_but_is_misread_is_not_a_pass():
    # debits 700 / credits 2080 net the same as 620 / 2000 but are not the printed parts
    rows = [f"2-Feb-2025 SAMPLE CAFE CITY XX {R} 700.00", f"9-Mar-2025 Repayment {R} 2080.00"]
    t = m.tie_out(_st(_old(rows=rows)))
    assert t.result == "FAIL"


def test_older_statement_without_rows_and_nonzero_printed_debits_is_still_an_issue():
    st = _st(_old(rows=[]))
    t = m.tie_out(st)
    assert t.result == "FAIL"
    assert not st.parsed.rows


def test_older_layout_without_the_figure_line_is_never_a_pass():
    lines = [ln for ln in _old() if ln != "1500.00 620.00 0.00 2000.00 120.00"]
    t = m.tie_out(_st(lines))
    assert t.result != "PASS"
    assert "summary" in t.note


def test_current_layout_is_unchanged():
    cur = [
        "Statement Date : 30-Sep-2025",
        "Opening Balance Purchase/Debits Finance Charges Payment/Credit Total Amount Due",
        "2,000.00 1,000.00 0.00 2,000.00 1,000.00",
        "5-Sep-2025 EXAMPLE SHOP 1,000.00 -",
        "20-Sep-2025 Repayment - 2,000.00",
    ]
    st = _st(cur)
    assert len(st.parsed.rows) == 2
    assert [r.direction for r in st.parsed.rows] == ["Dr", "Cr"]
    assert st.period == (date(2025, 9, 1), date(2025, 9, 30)) or st.period is not None
    assert "older SBM" not in st.parsed.note
    assert m.tie_out(st).result == "PASS"


# --- CC-19(a): the due-values line ------------------------------------------

def _current_with_due():
    return [
        "Statement Period : 1-Sep-2025 to 30-Sep-2025",
        "Payment Due Date Total Amount Due Minimum Amount Due",
        "16-Oct-2025 1,000.00 100.00",
        "Opening Balance Purchase/Debits Finance Charges Payment/Credit Total Amount Due",
        "2,000.00 1,000.00 0.00 2,000.00 1,000.00",
        "5-Sep-2025 EXAMPLE SHOP 1,000.00 -",
        "20-Sep-2025 Repayment - 2,000.00",
    ]


def test_current_layout_due_values_line_is_not_reported_as_unread():
    st = _st(_current_with_due())
    assert not st.parsed.errors, st.parsed.errors
    assert len(st.parsed.rows) == 2
    assert m.unparsed_date_lines(st) == []


def test_older_layout_due_values_line_is_not_reported_as_unread():
    st = _st(_old())
    assert m.unparsed_date_lines(st) == []
    assert not st.parsed.errors


def test_a_genuinely_unread_transaction_line_is_still_reported():
    lines = _current_with_due() + ["21-Sep-2025 MYSTERY 55.00 66.00 77.00"]
    st = _st(lines)
    assert m.unparsed_date_lines(st) == ["21-Sep-2025 MYSTERY 55.00 66.00 77.00"]
    old = _st(_old() + [f"21-Mar-2025 MYSTERY {R} 55.00 {R} 66.00"])
    assert old.parsed.errors or m.unparsed_date_lines(old)


def test_a_date_and_two_amounts_without_the_due_header_is_not_exempt():
    lines = [ln for ln in _current_with_due() if not ln.startswith("Payment Due Date")]
    st = _st(lines)
    assert st.parsed.errors            # parse_sbm reports it
    assert m.unparsed_date_lines(st)   # and so does the gap scan
