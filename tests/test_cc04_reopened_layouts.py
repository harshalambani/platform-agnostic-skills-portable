"""CC-04 (reopened): synthetic fixtures for each layout that failed on a real statement run.
Axis (right-column text on the values line), HDFC (total line before the four-value line, total
rounded to the rupee), YES (two-column summary, period line), ICICI (sidebar text before the
date), SBM (credit balance printed as 0.00, the due line, the statement month only), HSBC
(leaflets titled 'Important Terms and Conditions'). Every figure and name is made up."""
from __future__ import annotations

from datetime import date

from tests.test_cc04_tieout_layouts import (AXIS_OK, HDFC_NEW_EMI, ICICI_FEB, ICICI_MAR, SBM_NEW,
                                            _parse, m)


# ---- Axis -------------------------------------------------------------------------------

def _axis_rc(text="Reward points summary"):
    lines = list(AXIS_OK)
    lines[1] += " " + text
    return lines


def test_axis_values_line_with_right_column_text_still_ties_out():
    st = _parse("Axis", "Flipkart", _axis_rc())
    assert st.parsed.summary["total"] == -3220.5 or st.parsed.summary["total"] == 3220.5
    assert m.tie_out(st).result == "PASS"


def test_axis_a_ninth_number_is_never_swallowed_as_right_column_text():            # NEGATIVE
    st = _parse("Axis", "Flipkart", _axis_rc("1,250"))
    s = st.parsed.summary
    # whatever path reads it, the trailing number is never taken as a summary value
    assert 1250.0 not in s.values() and s.get("total") in (None, 3220.5, -3220.5)


def test_axis_rows_that_do_not_reach_the_printed_total_are_never_pass():           # NEGATIVE
    lines = [ln for ln in _axis_rc() if "FUEL" not in ln]
    assert m.tie_out(_parse("Axis", "Flipkart", lines)).result == "FAIL"


def test_axis_has_no_rupee_tolerance():                                            # NEGATIVE
    lines = [ln.replace("3,250.50", "3,250.00") for ln in _axis_rc()]
    assert m.tie_out(_parse("Axis", "Flipkart", lines)).result == "FAIL"


# ---- HDFC -------------------------------------------------------------------------------

def _hdfc(total="C5,001.00"):
    return [
        "Billing Period 01 Dec, 2025 - 31 Dec, 2025",
        "PREVIOUS STATEMENT DUES PAYMENTS/ CREDITS PURCHASES/ FINANCE CHARGES TOTAL AMOUNT DUE",
        "RECEIVED (Current Billing Cycle)",
        f"_ {total}",
        "C1,000.00 C1,000.00 + C5,000.00 + C0.00 =",
        "03/12/2025| 14:22 UPI-EXAMPLESHOP + 219 C 3,000.00 l",
        "05/12/2025| 09:10 EXAMPLEBASKET C 2,000.00 l",
        "10/12/2025| 18:00 BPPY CC PAYMENT + C 1,000.00 l",
    ]


def test_hdfc_total_line_before_the_four_value_line_is_read_and_rounding_is_tolerated():
    st = _parse("HDFC", "TataNeu", _hdfc("C5,000.60"))
    assert st.parsed.summary["total"] == 5000.6 and st.parsed.summary["purchases"] == 5000.0
    t = m.tie_out(st)
    assert t.result == "PASS", (t.note, t.difference)


def test_hdfc_total_after_the_four_value_line_still_works():
    assert m.tie_out(_parse("HDFC", "TataNeu", HDFC_NEW_EMI)).result == "PASS"


def test_hdfc_a_two_rupee_gap_still_fails():                                       # NEGATIVE
    assert m.tie_out(_parse("HDFC", "TataNeu", _hdfc("C5,002.00"))).result == "FAIL"


def test_hdfc_rows_that_do_not_reach_the_total_are_never_pass():                   # NEGATIVE
    lines = [ln for ln in _hdfc("C5,000.00") if "EXAMPLEBASKET" not in ln]
    assert m.tie_out(_parse("HDFC", "TataNeu", lines)).result == "FAIL"


def test_only_hdfc_gets_the_rupee_tolerance():                                     # NEGATIVE
    assert _parse("HDFC", "TataNeu", _hdfc()).parsed.tol_loose is True
    assert _parse("Axis", "Flipkart", _axis_rc()).parsed.tol_loose is False
    assert _parse("ICICI", "Amazon", ICICI_MAR).parsed.tol_loose is False


# ---- YES Reserv -------------------------------------------------------------------------

YES_TWO_COL = [
    "01/09/2025 To 30/09/2025 Rs. 1,500.00",
    "Previous Balance : Payment & Credits Received :",
    "Rs. 300.00 Dr Rs. 300.00 Cr",
    "Current Purchases / Cash Advance & Other Charges : Total Amount Due:",
    "Rs. 1,500.00 Rs. 1,500.00",
    "05/09/2025 EXAMPLE MARKET BANGALORE 1,500.00 Dr",
    "15/09/2025 PAYMENT RECEIVED BBPS - Ref No: 123456789 300.00 Cr",
]


def test_yes_two_column_summary_is_paired_and_the_period_line_is_not_an_unparsed_row():
    st = _parse("YES", "Reserv", YES_TWO_COL)
    s = st.parsed.summary
    assert (s["payments"], s["purchases"], s["total"]) == (300.0, 1500.0, 1500.0)
    assert st.parsed.errors == []
    assert len(st.parsed.rows) == 2
    assert m.tie_out(st).result == "PASS"


def test_yes_a_real_row_that_fails_to_parse_is_still_reported():                   # NEGATIVE
    st = _parse("YES", "Reserv", YES_TWO_COL + ["20/09/2025 EXAMPLE SHOP 250.00"])
    assert any("unparsed line" in e for e in st.parsed.errors)


def test_yes_wrong_value_in_the_two_column_summary_fails():                        # NEGATIVE
    lines = [ln.replace("Rs. 300.00 Dr Rs. 300.00 Cr", "Rs. 300.00 Dr Rs. 900.00 Cr") for ln in YES_TWO_COL]
    assert m.tie_out(_parse("YES", "Reserv", lines)).result != "PASS"


# ---- ICICI Amazon -----------------------------------------------------------------------

def _icici_sidebar(prefix="Offer: extra rewards this month"):
    lines = list(ICICI_MAR)
    lines[5] = f"{prefix} {lines[5]}"
    return lines


def test_icici_sidebar_text_before_the_date_does_not_drop_the_row():
    st = _parse("ICICI", "Amazon", _icici_sidebar())
    assert [r.amount for r in st.parsed.rows] == [2500.0, 1000.0, 3000.0]
    assert not st.parsed.errors


def test_icici_a_line_with_a_date_but_no_serial_is_not_a_row():                    # NEGATIVE
    lines = list(ICICI_MAR)
    lines[5] = "Offer ends 05/03/2025 EXAMPLE SHOP IN 25 2,500.00"
    st = _parse("ICICI", "Amazon", lines)
    assert [r.amount for r in st.parsed.rows] == [1000.0, 3000.0]


def test_icici_a_short_number_after_the_date_is_not_a_serial():                    # NEGATIVE
    lines = list(ICICI_MAR)
    lines[5] = "Offer ends 05/03/2025 1234 EXAMPLE SHOP 2,500.00"
    assert len(_parse("ICICI", "Amazon", lines).parsed.rows) == 2


# ---- SBM --------------------------------------------------------------------------------

SBM_CREDIT = [
    "Statement Date : 9-Jan-2026 Statement Period : 9-Dec-2025 to 8-Jan-2026",
    "Opening Finance Total Amount",
    "Purchase/Debits Payment/Credits",
    "Balance Charges Due",
    "200.00 1,000.00 1,500.00 0.00 0.00",
    "10-Dec-2025 EXAMPLE SHOP 1,000.00 -",
    "20-Dec-2025 Repayment - 1,500.00",
]


def test_sbm_credit_balance_printed_as_zero_is_proven_by_the_arithmetic():
    st = _parse("SBM", "Global", SBM_CREDIT)
    assert st.parsed.summary["total"] == -300.0
    assert "credit balance of 300.00" in st.parsed.note
    t = m.tie_out(st)
    assert t.result == "PASS", (t.note, t.difference)


def test_sbm_zero_printed_when_the_figures_do_not_prove_a_credit_is_not_accepted():   # NEGATIVE
    lines = [ln.replace("200.00 1,000.00 1,500.00 0.00 0.00", "200.00 1,000.00 100.00 0.00 0.00")
             for ln in SBM_CREDIT]
    assert m.tie_out(_parse("SBM", "Global", lines)).result != "PASS"


def test_sbm_a_non_zero_mismatch_still_fails():                                    # NEGATIVE
    lines = [ln.replace("1,000.00 5,000.00 1,500.00 100.00 4,600.00", "1,000.00 5,000.00 1,500.00 100.00 4,650.00")
             for ln in SBM_NEW]
    assert m.tie_out(_parse("SBM", "Global", lines)).result != "PASS"


def test_sbm_credit_balance_with_rows_that_miss_the_figures_fails():               # NEGATIVE
    lines = [ln for ln in SBM_CREDIT if "EXAMPLE SHOP" not in ln]
    assert m.tie_out(_parse("SBM", "Global", lines)).result != "PASS"


def test_sbm_due_line_is_not_a_row_and_a_merchant_with_due_in_its_name_still_is():
    lines = SBM_NEW + ["25-Jan-2026 Minimum Amount Due 100.00 -"]
    st = _parse("SBM", "Global", lines)
    assert len(st.parsed.rows) == 3
    assert m.tie_out(st).result == "PASS"
    shop = _parse("SBM", "Global", SBM_NEW + ["11-Dec-2025 DUE DILIGENCE SERVICES 100.00 -"])
    assert any("DUE DILIGENCE" in r.description for r in shop.parsed.rows)           # NEGATIVE
    assert m.tie_out(shop).result != "PASS"


def test_older_sbm_prints_the_statement_month_only_and_the_period_is_that_month():
    lines = ["Statement Month : January-2026"] + SBM_NEW[1:]
    period, note = m.detect_period(lines)
    assert period == (date(2026, 1, 1), date(2026, 1, 31))
    assert "month" in note


def test_a_printed_sbm_period_beats_the_month_and_junk_months_are_ignored():       # NEGATIVE
    period, _ = m.detect_period(["Statement Month : January-2026"] + SBM_NEW)
    assert period == (date(2025, 12, 9), date(2026, 1, 8))
    assert m.detect_period(["Statement Month : Foo-2026"])[0] is None


# ---- HSBC leaflets ----------------------------------------------------------------------

LEAFLET = ["IMPORTANT TERMS AND CONDITIONS", "Interest, fees and charges apply as listed.",
           "Cash advance fee 2.5 per cent", "Late payment charge 500"]


def test_hsbc_important_terms_leaflet_is_a_non_statement_by_title():
    assert m.non_statement_reason(LEAFLET, "HSBC") == "Important Terms and Conditions"


def test_a_footer_mention_of_terms_and_conditions_does_not_make_a_statement_a_non_statement():  # NEGATIVE
    stmt = ["HSBC Premier statement"] + AXIS_OK + \
        ["Please read the Important Terms and Conditions on the back of this statement"]
    assert m.non_statement_reason(stmt, "HSBC") is None
    far = ["HSBC Premier statement", "a", "b", "c", "d", "e", "IMPORTANT TERMS AND CONDITIONS"]
    assert m.non_statement_reason(far, "HSBC") is None            # title not among the first lines
    sentence = ["Please read the Important Terms and Conditions on the back of this statement "
                "before using the card", "x", "y"]
    assert m.non_statement_reason(sentence, "HSBC") is None       # a sentence, not a title


def test_a_titled_document_with_transaction_rows_is_never_skipped():               # NEGATIVE
    assert m.non_statement_reason(["IMPORTANT TERMS AND CONDITIONS"] + AXIS_OK, "HSBC") is None
