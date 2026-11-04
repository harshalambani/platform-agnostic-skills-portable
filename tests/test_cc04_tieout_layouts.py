"""
tests/test_cc04_tieout_layouts.py -- CC-04: real-statement layouts that CC-02
could not tie out (non-statements, wrapped/interleaved summaries, signed
openings, glyph amounts, chained tie-out, duplicate detection, gap report, EMI
kinds, named tolerances).

Everything is synthetic. The line shapes follow the masked layouts the planning
session described; every figure and name below is made up. Statement text is
supplied by monkeypatching ``read_lines``.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import date, datetime
from pathlib import Path

import pytest
from openpyxl import load_workbook

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


def _parse(bank, card, lines, name="x.pdf"):
    return m.parse_statement(lines, bank, card, name)


def _folder(tmp_path, monkeypatch, files):
    for (folder, name) in files:
        d = tmp_path / "in" / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / name).write_bytes(b"%PDF-1.4 synthetic")
    table = {name: lines for (_f, name), lines in files.items()}
    monkeypatch.setattr(m, "read_lines", lambda p: table[Path(p).name])
    return tmp_path / "in"


def _run(tmp_path, monkeypatch, files, start=date(2025, 1, 1), end=date(2026, 12, 31), label="all"):
    src = _folder(tmp_path, monkeypatch, files)
    out = tmp_path / "out.xlsx"
    return m.run_extraction(src, out, start, end, label), out


# ---------------------------------------------------------------------------
# A. Non-statements
# ---------------------------------------------------------------------------

MITC = [
    "Example Bank Premier Credit Card Statement",
    "Most Important Terms and Conditions",
    "Particulars Charges",
    "Annual fee 500.00 per year",
    "Cashback 1.5% on eligible spends",
]
YEAR_END = [
    "Year End Statement & Summary",
    "Account Summary for the period from APRIL-25 to MARCH-26",
    "Total purchases 12,000.00",
]
AXIS_OK = [
    "01/04/2025 - 30/04/2025 15/05/2025 25/05/2025",
    "5,000.00 Dr 5,000.00 30.00 3,250.50 0.00 0.00 3,220.50 Dr",
    "05/04/2025 EXAMPLE MART MUMBAI DEPT STORES 1,500.50 Dr 30.00 Cr",
    "10/04/2025 BBPS PAYMENT RECEIVED - 123456 PAYMENT 5,000.00 Cr",
    "12/04/2025 EXAMPLE FUEL STATION FUEL 1,750.00 Dr",
    "15/04/2025 CASHBACK CREDIT CASHBACK 30.00 Cr",
]


def test_non_statements_are_skipped_by_title_and_never_counted_na_or_zero_rows(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, {
        ("HSBC-Premier", "mitc.pdf"): MITC,
        ("Unknown-Unknown", "yearend.pdf"): YEAR_END,
        ("Axis-Flipkart", "a.pdf"): AXIS_OK,
    })
    assert len(res.skipped) == 2
    assert "Skipped: not a statement" in res.message
    assert "mitc.pdf" in res.message and "yearend.pdf" in res.message
    # NEGATIVE: not a tie-out row (no N/A), not a "no transactions parsed" issue
    assert [t.card for t in res.ties] == ["Axis-Flipkart"]
    assert not any("mitc" in i or "yearend" in i for i in res.issues)
    assert not any("NOT AVAILABLE" in i for i in res.issues)
    assert "completed successfully" in res.message


def test_a_real_statement_that_mentions_the_terms_document_is_not_skipped():
    assert m.non_statement_reason(AXIS_OK + ["See the Most Important Terms and Conditions on our site"]) is None
    # a year-end style header on a document that does carry a statement period is not skipped either
    assert m.non_statement_reason(["Year End Statement & Summary"] + AXIS_OK) is None


def test_scanned_statement_is_reported_loudly_never_zero_rows_or_pass(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, {("Axis-Flipkart", "scan.pdf"): ["", "   "]})
    assert any("No text layer: cannot read" in i for i in res.issues)
    assert "completed successfully" not in res.message
    assert res.ties == [] and res.skipped == []


# ---------------------------------------------------------------------------
# B. Axis IndianOil (old layout)
# ---------------------------------------------------------------------------

AXIS_OLD_CR = [
    "01/04/2025 - 30/04/2025 15/05/2025 25/05/2025",
    "Previous Balance - Payments - Credits + Purchase + Cash Advance + Other Charges",
    "=Total Payment Due",
    "",
    "500.00 Cr 0.00 0.00 1,500.00 0.00 0.00 1,000.00 Dr",
    "05/04/2025 EXAMPLE MART MUMBAI DEPT STORES *1,500.00 Dr",
]


def test_axis_old_layout_cr_opening_is_negative_and_star_glyph_is_ignored():
    st = _parse("Axis", "IndianOil", AXIS_OLD_CR)
    assert st.parsed.summary["prev"] == -500.0
    t = m.tie_out(st)
    assert t.result == "PASS", (t.note, t.difference)
    assert t.previous == -500.0          # NEGATIVE: a Cr opening is never added as a debit
    assert st.parsed.rows[0].amount == 1500.0


def test_axis_old_layout_opening_without_a_suffix_is_a_debit():
    lines = list(AXIS_OLD_CR)
    lines[4] = "500.00 0.00 0.00 1,500.00 0.00 0.00 2,000.00"
    st = _parse("Axis", "IndianOil", lines)
    assert st.parsed.summary["prev"] == 500.0
    assert m.tie_out(st).result == "PASS"


def test_a_cr_opening_misread_as_debit_would_fail_so_the_sign_matters():
    wrong = list(AXIS_OLD_CR)
    wrong[4] = "500.00 0.00 0.00 1,500.00 0.00 0.00 1,000.00 Dr"     # no Cr: opening +500
    assert m.tie_out(_parse("Axis", "IndianOil", wrong)).result == "FAIL"


# ---------------------------------------------------------------------------
# C. YES (interleaved labels and values)
# ---------------------------------------------------------------------------

YES_INTERLEAVED = [
    "Statement Period: 01/09/2025 To 30/09/2025",
    "Previous Balance :",
    "Current Purchases / Cash Advance & Other Charges :",
    "Payment & Credits Received :",
    "Total Amount Due:",
    "Rs. 300.00 Dr",
    "Rs. 1,500.00",
    "Rs. 300.00 Cr",
    "Rs. 1,500.00",
    "05/09/2025 EXAMPLE MARKET BANGALORE 1,500.00 Dr",
    "15/09/2025 PAYMENT RECEIVED BBPS - Ref No: 123456789 300.00 Cr",
]


def test_yes_interleaved_summary_is_paired_in_order_and_ties_out():
    st = _parse("YES", "Reserv", YES_INTERLEAVED)
    s = st.parsed.summary
    assert (s["prev"], s["purchases"], s["payments"], s["total"]) == (300.0, 1500.0, 300.0, 1500.0)
    assert len(st.parsed.rows) == 2          # low counts are genuine: nothing is invented
    assert m.tie_out(st).result == "PASS"


def test_yes_interleaved_with_a_wrong_value_fails_not_passes():
    bad = [ln.replace("Rs. 300.00 Cr", "Rs. 900.00 Cr") for ln in YES_INTERLEAVED]
    assert m.tie_out(_parse("YES", "Reserv", bad)).result == "FAIL"


# ---------------------------------------------------------------------------
# D. HDFC old (labels over three lines, negative opening)
# ---------------------------------------------------------------------------

HDFC_OLD_SPLIT = [
    "Statement Date:30/06/2025",
    "Account Summary",
    "Opening Payment/ Purchase/ Finance",
    "Total Dues",
    "Balance Credits Debits Charges",
    "-500.00 100.00 1,200.00 0.00 600.00",
    "05/06/2025 12:30:45 EXAMPLE FOODS BANGALORE (Ref# 12345) 1,200.00",
    "10/06/2025 EXAMPLE REFUND STORE (Ref# 12346) 100.00 Cr",
]


def test_hdfc_old_split_labels_and_negative_opening():
    st = _parse("HDFC", "Regalia", HDFC_OLD_SPLIT)
    assert st.parsed.summary["prev"] == -500.0
    assert len(st.parsed.rows) == 2
    assert {r.direction for r in st.parsed.rows} == {"Dr", "Cr"}
    t = m.tie_out(st)
    assert t.result == "PASS", (t.note, t.difference)


def test_hdfc_old_five_values_without_an_opening_label_are_not_a_summary():
    lines = [ln for ln in HDFC_OLD_SPLIT if "Opening" not in ln]
    assert "prev" not in _parse("HDFC", "Regalia", lines).parsed.summary


# ---------------------------------------------------------------------------
# E. HDFC new (glyph amounts, EMI marker)
# ---------------------------------------------------------------------------

HDFC_NEW_EMI = [
    "Billing Period 01 Dec, 2025 - 31 Dec, 2025",
    "PREVIOUS STATEMENT DUES PAYMENTS/ CREDITS PURCHASES/ FINANCE CHARGES TOTAL AMOUNT DUE",
    "RECEIVED (Current Billing Cycle)",
    "C1,000.00 C1,000.00 + C5,000.00 + C0.00 =",
    "_ C5,000.00",
    "03/12/2025| 14:22 EMI UPI-EXAMPLESHOP + 219 C 3,000.00 l",
    "05/12/2025| 09:10 EMI EXAMPLEBASKET C 2,000.00 l",
    "10/12/2025| 18:00 BPPY CC PAYMENT + C 1,000.00 l",
]


def test_emi_marker_prefix_is_stripped_and_never_makes_a_row_emi():
    st = _parse("HDFC", "TataNeu", HDFC_NEW_EMI)
    spends = [r for r in st.parsed.rows if r.direction == "Dr"]
    assert [r.kind for r in spends] == ["spend", "spend"]            # NEGATIVE: not emi_*
    assert all(not r.description.upper().startswith("EMI") for r in spends)
    assert {r.description for r in spends} == {"UPI-EXAMPLESHOP", "EXAMPLEBASKET"}
    assert m.tie_out(st).result == "PASS"


def test_explicit_emi_wording_after_the_marker_is_kept_and_classified():
    st = _parse("HDFC", "TataNeu", [
        "Billing Period 01 Dec, 2025 - 31 Dec, 2025",
        "05/12/2025| 09:10 EMI PRINCIPAL EXAMPLEBASKET C 2,000.00 l"])
    assert st.parsed.rows[0].kind == "emi_principal"


# ---------------------------------------------------------------------------
# F. ICICI (backtick rupee, own-line total, reward points)
# ---------------------------------------------------------------------------

ICICI_FEB = [
    "Statement period : February 5, 2025 to March 4, 2025",
    "Credit Limit (including cash) Available Credit Available Cash Limit",
    "`1,00,000.00 `90,000.00 `20,000.00 `15,000.00",
    "Total Amount due",
    "`3,000.00 = + + -",
    "10/02/2025 12345678900 EXAMPLE SHOP IN 30 3,000.00",
]
ICICI_MAR = [
    "Statement period : March 5, 2025 to April 4, 2025",
    "Credit Limit (including cash) Available Credit Available Cash Limit",
    "`1,00,000.00 `90,000.00 `20,000.00 `15,000.00",
    "Total Amount due",
    "`3,500.00 = + + -",
    "05/03/2025 12345678901 EXAMPLE SHOP IN 25 2,500.00",
    "10/03/2025 12345678902 EXAMPLE STORE -99 1,000.00",
    "15/03/2025 12345678903 BBPS Payment received 0 3,000.00 CR",
]


def test_icici_credit_limit_line_is_never_read_as_the_summary():
    st = _parse("ICICI", "Amazon", ICICI_MAR)
    assert "prev" not in st.parsed.summary                         # NEGATIVE
    assert st.parsed.summary.get("total") == 3500.0
    assert 1_00_000.0 not in st.parsed.summary.values()
    assert m.tie_out(st).result.startswith("NOT AVAILABLE")        # no chain, no opening


def test_icici_reward_points_are_never_part_of_the_amount():
    st = _parse("ICICI", "Amazon", ICICI_MAR)
    assert [r.amount for r in st.parsed.rows] == [2500.0, 1000.0, 3000.0]   # NEGATIVE: not 25, 99, 0
    assert [r.direction for r in st.parsed.rows] == ["Dr", "Dr", "Cr"]
    assert not st.parsed.errors


def test_chained_tie_out_passes_with_the_prior_statement(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, {("ICICI-Amazon", "feb.pdf"): ICICI_FEB,
                                          ("ICICI-Amazon", "mar.pdf"): ICICI_MAR})
    by = {t.source: t for t in res.ties}
    assert by["mar.pdf"].result == "PASS (chained)"
    assert by["mar.pdf"].previous == 3000.0 and by["mar.pdf"].computed == 3500.0
    # the first statement has no prior: not available, never PASS on its total alone
    assert by["feb.pdf"].result == "NOT AVAILABLE (no prior statement)"


def test_chained_pass_never_happens_without_a_prior_statement(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, {("ICICI-Amazon", "mar.pdf"): ICICI_MAR})
    assert res.ties[0].result == "NOT AVAILABLE (no prior statement)"
    assert "completed successfully" not in res.message


def test_chained_prior_must_be_the_same_card(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, {("ICICI-Sapphiro", "feb.pdf"): ICICI_FEB,
                                          ("ICICI-Amazon", "mar.pdf"): ICICI_MAR})
    by = {t.source: t for t in res.ties}
    assert by["mar.pdf"].result == "NOT AVAILABLE (no prior statement)"


def test_chained_prior_may_sit_outside_the_range_but_is_not_reported(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, {("ICICI-Amazon", "feb.pdf"): ICICI_FEB,
                                          ("ICICI-Amazon", "mar.pdf"): ICICI_MAR},
                  start=date(2025, 3, 5), end=date(2025, 4, 30))
    assert [t.source for t in res.ties] == ["mar.pdf"]
    assert res.ties[0].result == "PASS (chained)"


def test_chained_wrong_rows_fail_and_a_prior_that_is_not_adjacent_is_not_used(tmp_path, monkeypatch):
    broken = [ln for ln in ICICI_MAR if "EXAMPLE STORE" not in ln]
    res, _ = _run(tmp_path, monkeypatch, {("ICICI-Amazon", "feb.pdf"): ICICI_FEB,
                                          ("ICICI-Amazon", "mar.pdf"): broken})
    assert {t.source: t.result for t in res.ties}["mar.pdf"] == "FAIL"
    late = [ln.replace("March 5, 2025 to April 4, 2025", "April 20, 2025 to May 19, 2025") for ln in ICICI_MAR]
    res2, _ = _run(tmp_path / "b", monkeypatch, {("ICICI-Amazon", "feb.pdf"): ICICI_FEB,
                                                 ("ICICI-Amazon", "mar.pdf"): late})
    assert {t.source: t.result for t in res2.ties}["mar.pdf"] == "NOT AVAILABLE (no prior statement)"


def test_icici_components_that_do_not_add_up_are_not_trusted():
    lines = list(ICICI_MAR[:4]) + ["Previous Balance Purchases / Charges Cash Advances Payments / Credits",
                                  "`8,000.00 `3,850.20 `0.00 `8,000.00"] + ICICI_MAR[4:]
    st = _parse("ICICI", "Amazon", lines)
    assert "prev" not in st.parsed.summary            # 8000 + 3850.20 - 8000 != 3500
    assert any("do not add to the total" in x for x in st.parsed.missing)


# ---------------------------------------------------------------------------
# G/H. SBM
# ---------------------------------------------------------------------------

SBM_NEW = [
    "Statement Date : 9-Jan-2026 Statement Period : 9-Dec-2025 to 8-Jan-2026",
    "Opening Finance Total Amount",
    "Purchase/Debits Payment/Credits",
    "Balance Charges Due",
    "1,000.00 5,000.00 1,500.00 100.00 4,600.00",
    "10-Dec-2025 EXAMPLE SHOP 5,000.00 -",
    "20-Dec-2025 Repayment - 1,500.00",
    "28-Dec-2025 FINANCE CHARGES 100.00 -",
]


def test_sbm_period_and_roles_are_proven_by_arithmetic():
    st = _parse("SBM", "Global", SBM_NEW)
    assert st.period == (date(2025, 12, 9), date(2026, 1, 8))
    s = st.parsed.summary
    assert (s["prev"], s["purchases"], s["payments"], s["finance"], s["total"]) == \
        (1000.0, 5000.0, 1500.0, 100.0, 4600.0)
    assert m.tie_out(st).result == "PASS"


def test_sbm_roles_come_from_the_arithmetic_not_the_position():
    # same figures printed in a different middle order: the roles are found, not assumed
    # purchases and finance charges both add, so the arithmetic alone cannot tell them apart:
    # without rows to decide it, nothing is chosen
    assert m.resolve_sbm_roles(1000.0, (5000.0, 100.0, 1500.0), 4600.0, []) is None
    # a printed order that no assignment can explain is refused
    assert m.resolve_sbm_roles(1000.0, (5000.0, 100.0, 1500.0), 9999.0, []) is None
    st = _parse("SBM", "Global", [ln.replace("4,600.00", "9,999.00") for ln in SBM_NEW])
    assert m.tie_out(st).result == "NOT AVAILABLE"


def test_sbm_ambiguous_roles_are_settled_by_the_rows_or_refused():
    # purchases == payments: both assignments tie; the credit rows decide
    rows = _parse("SBM", "Global", [
        "Statement Date : 30-Sep-2025",
        "10-Sep-2025 EXAMPLE SHOP 700.00 -", "20-Sep-2025 Repayment - 500.00"]).parsed.rows
    got = m.resolve_sbm_roles(0.0, (700.0, 500.0, 0.0), 200.0, rows)
    assert got == (700.0, 500.0, 0.0)


def test_duplicate_statement_is_reported_as_duplicate_and_never_doubles_rows(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, {("SBM-Global", "a.pdf"): SBM_NEW,
                                          ("SBM-Global", "b.pdf"): SBM_NEW})
    assert len(res.rows) == 3                         # NEGATIVE: not 6
    assert len(res.ties) == 1
    assert len(res.duplicates) == 1 and "Duplicate of a.pdf" in res.duplicates[0]


def test_same_period_with_different_rows_is_not_silently_dropped(tmp_path, monkeypatch):
    other = [ln.replace("5,000.00 -", "4,000.00 -") if "SHOP" in ln else ln for ln in SBM_NEW]
    res, _ = _run(tmp_path, monkeypatch, {("SBM-Global", "a.pdf"): SBM_NEW,
                                          ("SBM-Global", "b.pdf"): other})
    assert res.duplicates == []
    assert any("different rows" in i for i in res.issues)
    assert "completed successfully" not in res.message


# ---------------------------------------------------------------------------
# I. Gap report
# ---------------------------------------------------------------------------

def test_gap_report_names_the_difference_and_a_likely_missed_row(tmp_path, monkeypatch):
    lines = [ln for ln in AXIS_OK if "FUEL" not in ln] + ["12/04/2025 EXAMPLE FUEL STATION FUEL 1,750.00 Xx"]
    res, out = _run(tmp_path, monkeypatch, {("Axis-Flipkart", "a.pdf"): lines})
    t = res.ties[0]
    assert t.result == "FAIL"
    assert "difference" in t.gap and "1,750.00" in t.gap
    assert "likely missed row" in t.gap
    assert "1 date-led line(s) not parsed" in t.gap
    assert "Gap report" in res.message
    wb = load_workbook(out)
    header = [c.value for c in wb["Tie-out"][1]]
    assert "Gap report" in header
    assert "likely missed row" in str(list(wb["Tie-out"].iter_rows(min_row=2, values_only=True))[0][header.index("Gap report")])


def test_gap_report_names_a_missing_component():
    st = _parse("ICICI", "Amazon", ICICI_MAR)
    t = m.tie_out(st)
    t.gap = m.gap_report(st, t)
    assert "missing" in t.gap and "previous balance" in t.gap


def test_a_passing_statement_has_no_gap_report(tmp_path, monkeypatch):
    res, _ = _run(tmp_path, monkeypatch, {("Axis-Flipkart", "a.pdf"): AXIS_OK})
    assert res.ties[0].gap == "" and "Gap report" not in res.message


# ---------------------------------------------------------------------------
# J. EMI kinds
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,direction,kind", [
    ("EMI PRINCIPAL 1/6 EXAMPLE PHONE", "Dr", "emi_principal"),
    ("EMI INTEREST 1/6", "Dr", "emi_interest"),
    ("EMI PROCESSING FEE", "Dr", "emi_processing_fee"),
    ("GST ON EMI PROCESSING FEE", "Dr", "gst_on_emi"),
    ("IGST ON EMI INTEREST", "Dr", "gst_on_emi"),
    ("CONVERTED TO EMI EXAMPLE PHONE", "Cr", "emi_conversion"),
    ("EMI CONVERSION EXAMPLE PHONE", "Cr", "emi_conversion"),
    ("NO COST EMI EXAMPLE PHONE", "Dr", "emi_unclassified"),
    ("EMI SOMETHING ELSE", "Dr", "emi_unclassified"),
])
def test_emi_kinds_explicit_wording_only_everything_else_is_unclassified(desc, direction, kind):
    assert m.classify(desc, direction) == kind


def test_an_emi_row_is_never_a_spend_a_fee_or_a_refund():
    for desc, d in [("NO COST EMI EXAMPLE PHONE", "Dr"), ("EMI PROCESSING FEE", "Dr"),
                    ("CONVERTED TO EMI X", "Cr"), ("EMI INTEREST", "Dr")]:
        assert m.classify(desc, d) not in ("spend", "fee", "refund")


def test_emi_rows_are_listed_loudly_and_make_the_run_incomplete(tmp_path, monkeypatch):
    lines = [
        "Billing Period 01 Dec, 2025 - 31 Dec, 2025",
        "PREVIOUS STATEMENT DUES PAYMENTS/ CREDITS PURCHASES/ FINANCE CHARGES TOTAL AMOUNT DUE",
        "C0.00 C0.00 + C3,000.00 + C0.00 =",
        "_ C3,000.00",
        "03/12/2025| 14:22 EMI PRINCIPAL EXAMPLE PHONE C 3,000.00 l",
    ]
    res, _ = _run(tmp_path, monkeypatch, {("HDFC-TataNeu", "a.pdf"): lines})
    assert "EMI-LIKE ROWS (1)" in res.message
    assert "completed successfully" not in res.message
    assert len(res.emi_rows) == 1 and res.emi_rows[0].kind == "emi_principal"


# ---------------------------------------------------------------------------
# K. Tolerances are named constants; regression
# ---------------------------------------------------------------------------

def test_tolerances_are_module_level_named_constants():
    assert m.TOL_STRICT == 0.01 and m.TOL_LOOSE == 1.0 and m.CHAIN_MAX_GAP_DAYS == 3


def test_axis_flipkart_layout_that_tied_out_before_still_passes():
    st = _parse("Axis", "Flipkart", AXIS_OK)
    t = m.tie_out(st)
    assert t.result == "PASS" and len(st.parsed.rows) == 4


# ---------------------------------------------------------------------------
# EMI interest stays its own kind (user refinement)
# ---------------------------------------------------------------------------

def test_emi_interest_is_never_a_fee_nor_merged_with_processing_fee_or_gst():
    kinds = {m.classify("EMI INTEREST 2/6", "Dr"), m.classify("EMI PROCESSING FEE", "Dr"),
             m.classify("GST ON EMI PROCESSING FEE", "Dr")}
    assert kinds == {"emi_interest", "emi_processing_fee", "gst_on_emi"}
    assert m.classify("EMI INTEREST 2/6", "Dr") != "fee"


def test_emi_interest_is_never_summed_into_the_fee_block_total(tmp_path, monkeypatch):
    lines = [
        "Billing Period 01 Dec, 2025 - 31 Dec, 2025",
        "PREVIOUS STATEMENT DUES PAYMENTS/ CREDITS PURCHASES/ FINANCE CHARGES TOTAL AMOUNT DUE",
        "C0.00 C0.00 + C1,250.00 + C0.00 =",
        "_ C1,250.00",
        "03/12/2025| 14:22 EMI INTEREST 2/6 C 250.00 l",
        "05/12/2025| 09:10 LATE PAYMENT FEE C 1,000.00 l",
    ]
    res, _ = _run(tmp_path, monkeypatch, {("HDFC-TataNeu", "a.pdf"): lines})
    assert [f.row.kind for f in res.fees] == ["fee"]
    assert sum(f.row.amount for f in res.fees) == 1000.0       # NEGATIVE: not 1,250
    assert "1,250.00" not in m.fee_block(res.fees)
    assert [r.kind for r in res.emi_rows] == ["emi_interest"]


# ---------------------------------------------------------------------------
# HSBC boilerplate: loan box and the worked interest example
# ---------------------------------------------------------------------------

HSBC_BOILER = [
    "16 DEC 2025 To 15 JAN 2026",
    "OPENING BALANCE 1,000.00",
    "20DEC EXAMPLE SHOP MUMBAI IN 500.00",
    "15JAN NET OUTSTANDING BALANCE 1,500.00",
    "TOTAL LOAN OUTSTANDING 0.00",
    "TOTAL BALANCE TRANSFER OUTSTANDING 0.00",
    "TOTAL CASH OUTSTANDING 0.00",
    "Example: if you opt for an EMI plan and the Outstanding due in the 3rd statement 10,000.00",
    "Interest on 10,000 for 30 days at the monthly rate is charged on the balance",
]


def test_hsbc_boilerplate_is_never_a_row_or_an_emi():
    st = _parse("HSBC", "Premier", HSBC_BOILER)
    assert [r.description for r in st.parsed.rows] == ["EXAMPLE SHOP MUMBAI IN"]
    assert not [r for r in st.parsed.rows if r.kind in m.EMI_KINDS]
    assert not st.parsed.errors
    assert m.tie_out(st).result == "PASS"
