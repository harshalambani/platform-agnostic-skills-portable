"""tests/test_cc36_sbm_printed_total.py -- CC-36: an SBM statement prints Total Amount Due twice (Statement Summary box
and Calculation summary box). A printed 0.00 is a total; a wrapped header reads like a one-line one; the bank's
one-paisa rounding either way still ties. Synthetic names and amounts only."""
from __future__ import annotations

import pytest

from tests.test_cc04_tieout_layouts import _parse, m

ROWS = ["5-Feb-2026 EXAMPLE MART 100.00 -", "9-Feb-2026 PAYMENT RECEIVED - 100.00"]


def lay(header="one", opening="0.00", purch="100.00", pay="100.00", total="0.00", rows=ROWS, calc=True):
    L = ["Example Holder", "Your Credit Card Statement", "Statement Summary",
         "Statement Date : 1-Mar-2026 Statement Duration : 1-Feb-2026 to 28-Feb-2026",
         "Payment Due Date Total Amount Due Minimum Amount Due", f"16-Mar-2026 {total} 0.00",
         "Credit Limit Available Credit Limit Available Cash Limit", "50,000.00 50,000.00 0.00"]
    if calc:
        L.append("Calculation summary")
        if header == "one":
            L.append("Opening Balance Purchase/Debits Finance Charges Payment/Credit Total Amount Due")
        elif header == "two":
            L += ["Opening Purchase/Debits Finance Charges Payment/Credit Total Amount", "Balance Due"]
        else:
            L += ["Opening", "Balance", "Purchase/", "Debits", "Finance", "Charges", "Payment/", "Credit",
                  "Total Amount", "Due"]
        L.append(f"{opening} {purch} 0.00 {pay} {total}")
    return L + ["Transactions", "Date Transaction Description/Narration Purchase/Debit Payment/Credit"] + rows


def tie(**kw):
    st = _parse("SBM", "Niyo", lay(**kw))
    return st, m.tie_out(st)


@pytest.mark.parametrize("header", ["one", "two", "many"])
def test_a_zero_total_is_read_and_ties_for_every_header_wrap(header):
    st, t = tie(header=header)
    assert st.parsed.summary["total"] == 0.0 and t.printed == 0.0 and m.is_pass(t.result)
    assert len(st.parsed.rows) == 2                                             # no row read twice


def test_b_wrapped_reads_the_same_as_one_line():
    a, b = tie(header="one")[0].parsed, tie(header="many")[0].parsed
    assert a.summary == b.summary and [(r.amount, r.direction) for r in a.rows] == [(r.amount, r.direction) for r in b.rows]


def test_c_rows_one_paisa_in_credit_ties():
    st, t = tie(rows=["5-Feb-2026 EXAMPLE MART 99.99 -", "9-Feb-2026 PAYMENT RECEIVED - 100.00"])
    assert m.is_pass(t.result) and t.printed == 0.0


def test_c2_summary_itself_one_paisa_in_credit_ties_and_total_stays_zero():
    st, t = tie(purch="99.99", rows=["5-Feb-2026 EXAMPLE MART 99.99 -", "9-Feb-2026 PAYMENT RECEIVED - 100.00"])
    assert m.is_pass(t.result) and t.printed == 0.0 and st.parsed.summary["total"] == 0.0


def test_d_rows_one_paisa_owed_ties():
    st, t = tie(rows=["5-Feb-2026 EXAMPLE MART 100.01 -", "9-Feb-2026 PAYMENT RECEIVED - 100.00"])
    assert m.is_pass(t.result) and t.printed == 0.0


@pytest.mark.parametrize("spend", ["100.02", "99.98"])
def test_e_two_paise_either_way_stays_not_tied(spend):                          # NEGATIVE
    st, t = tie(rows=[f"5-Feb-2026 EXAMPLE MART {spend} -", "9-Feb-2026 PAYMENT RECEIVED - 100.00"])
    assert not m.is_pass(t.result)


def test_f_no_printed_total_at_all_stays_not_available():                       # NEGATIVE
    L = [x for x in lay(calc=False) if "Total Amount Due" not in x and not x.startswith("16-Mar")]
    st = _parse("SBM", "Niyo", L)
    assert "total" not in st.parsed.summary and not m.is_pass(m.tie_out(st).result)


def test_g_unreadable_calculation_box_falls_back_to_the_statement_summary_total():
    st, t = tie(calc=False)
    assert st.parsed.summary == {"total": 0.0} and t.printed == 0.0
    assert not m.is_pass(t.result)          # a total alone never passes without a prior statement (NEGATIVE)


def test_h_a_wrong_calculation_box_is_not_rescued_by_the_fallback():           # NEGATIVE
    st, t = tie(purch="100.00", pay="40.00", total="0.00")      # figures that cannot add up
    assert not m.is_pass(t.result)


def test_i_other_banks_are_unchanged():
    assert m.SBM_HEADER_LOOKBACK == 12
    # a non-SBM, non-summary line never becomes an SBM total
    st = _parse("HDFC", "Other", ["Example Bank Statement", "Nothing here"])
    assert not st.parsed.summary.get("total")
