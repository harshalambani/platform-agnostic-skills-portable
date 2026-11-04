"""
tests/test_cc06_hsbc_emi.py -- CC-06: HSBC EMI (loan conversion) statements.

Everything is synthetic: invented merchants, round invented amounts. The line
shapes follow the layout described in planning; the Loan Summary Table row
shape is an assumption of this fixture.
"""
from __future__ import annotations

import copy
import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_cc_transactions" / "scripts" / "create_cc_transaction_list.py"
for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_cc_spend_booking import journal as J      # noqa: E402


def _load():
    spec = importlib.util.spec_from_file_location("create_cc_transaction_list", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load()

ACME = "ACME TELEVISIONS PRIVATE LIMITED"


# (a) conversion month
STMT_A = [
    "16 JUN 2026 To 15 JUL 2026",
    "OPENING BALANCE 10,000.00",
    "17JUN BBPS PAYMENT RECEIVED 10,000.00 CR",
    "18JUN ACME TELEVISIONS PV MUMBAI MAH CC1234 60,000.00",
    "20JUN ACME TELEVISIONS PRIVATE LIMITED CC1234 60,000.00 CR",
    "20JUN INSTALLMENT PROCESSING FEECC1234 500.00",
    "15JUL ACME TELEVISIONS PRIVATE LIMITED CC1234 20,000.00 CR",
    "1ST OF 3 INSTALLMENTS PRINCIPAL",
    "15JUL ACME TELEVISIONS PRIVATE LIMITED CC1234 20,000.00",
    "1ST OF 3 INSTALLMENTS PRINCIPAL",
    "15JUL ACME TELEVISIONS PRIVATE LIMITED CC1234 500.00 CR",
    "1ST OF 3 INSTALLMENTS INTEREST",
    "15JUL ACME TELEVISIONS PRIVATE LIMITED CC1234 500.00",
    "1ST OF 3 INSTALLMENTS INTEREST",
    "15JUL CGST ASSESSMENT @9.00% 90.00",
    "15JUL SGST ASSESSMENT @9.00% 90.00",
    "15JUL NET OUTSTANDING BALANCE 62,180.00",
    "TOTAL PURCHASE OUTSTANDING 21,180.00",
    "TOTAL LOAN OUTSTANDING 41,000.00",
    "Loan Summary Table",
    "ACME TELEVISIONS PRIVATE LIMITED 20 JUN 2026 60,000.00 1,500.00 20,500.00 1/3",
]

# (b) mid-loan
STMT_B = [
    "16 JUL 2026 To 15 AUG 2026",
    "OPENING BALANCE 62,180.00",
    "20JUL BBPS PAYMENT RECEIVED 21,180.00 CR",
    "15AUG ACME TELEVISIONS PRIVATE LIMITED CC1234 20,000.00 CR",
    "2ND OF 3 INSTALLMENTS PRINCIPAL",
    "15AUG ACME TELEVISIONS PRIVATE LIMITED CC1234 20,000.00",
    "2ND OF 3 INSTALLMENTS PRINCIPAL",
    "15AUG ACME TELEVISIONS PRIVATE LIMITED CC1234 500.00 CR",
    "2ND OF 3 INSTALLMENTS INTEREST",
    "15AUG ACME TELEVISIONS PRIVATE LIMITED CC1234 500.00",
    "2ND OF 3 INSTALLMENTS INTEREST",
    "15AUG CGST ASSESSMENT @9.00% 45.00",
    "15AUG SGST ASSESSMENT @9.00% 45.00",
    "15AUG NET OUTSTANDING BALANCE 41,090.00",
    "TOTAL PURCHASE OUTSTANDING 20,590.00",
    "TOTAL LOAN OUTSTANDING 20,500.00",
    "Loan Summary Table",
    "ACME TELEVISIONS PRIVATE LIMITED 20 JUN 2026 60,000.00 1,500.00 20,500.00 2/3",
]

# (c) a second conversion while the first loan runs its last instalment
STMT_C = [
    "16 AUG 2026 To 15 SEP 2026",
    "OPENING BALANCE 41,090.00",
    "20AUG BBPS PAYMENT RECEIVED 20,590.00 CR",
    "01SEP TRAVELCO PV DELHI DL CC1234 30,000.00",
    "02SEP TRAVELCO PRIVATE LIMITED CC1234 30,000.00 CR",
    "02SEP INSTALLMENT PROCESSING FEECC1234 300.00",
    "15SEP ACME TELEVISIONS PRIVATE LIMITED CC1234 20,000.00 CR",
    "3RD OF 3 INSTALLMENTS PRINCIPAL",
    "15SEP ACME TELEVISIONS PRIVATE LIMITED CC1234 20,000.00",
    "3RD OF 3 INSTALLMENTS PRINCIPAL",
    "15SEP ACME TELEVISIONS PRIVATE LIMITED CC1234 500.00 CR",
    "3RD OF 3 INSTALLMENTS INTEREST",
    "15SEP ACME TELEVISIONS PRIVATE LIMITED CC1234 500.00",
    "3RD OF 3 INSTALLMENTS INTEREST",
    "15SEP TRAVELCO PRIVATE LIMITED CC1234 10,000.00 CR",
    "1ST OF 3 INSTALLMENTS PRINCIPAL",
    "15SEP TRAVELCO PRIVATE LIMITED CC1234 10,000.00",
    "1ST OF 3 INSTALLMENTS PRINCIPAL",
    "15SEP TRAVELCO PRIVATE LIMITED CC1234 300.00 CR",
    "1ST OF 3 INSTALLMENTS INTEREST",
    "15SEP TRAVELCO PRIVATE LIMITED CC1234 300.00",
    "1ST OF 3 INSTALLMENTS INTEREST",
    "15SEP CGST ASSESSMENT @9.00% 99.00",
    "15SEP SGST ASSESSMENT @9.00% 99.00",
    "15SEP NET OUTSTANDING BALANCE 51,898.00",
    "TOTAL PURCHASE OUTSTANDING 31,298.00",
    "TOTAL LOAN OUTSTANDING 20,600.00",
    "Loan Summary Table",
    "ACME TELEVISIONS PRIVATE LIMITED 20 JUN 2026 60,000.00 1,500.00 20,500.00 3/3",
    "TRAVELCO PRIVATE LIMITED 02 SEP 2026 30,000.00 900.00 10,300.00 1/3",
]


def parse(lines):
    return m.parse_statement(list(lines), "HSBC", "Premier", "x.pdf")


@pytest.fixture(params=["A", "B", "C"])
def stmt(request):
    return parse({"A": STMT_A, "B": STMT_B, "C": STMT_C}[request.param])


# ---------------------------------------------------------------------------
# parsing and tie-out
# ---------------------------------------------------------------------------

def test_all_three_statements_tie_out(stmt):
    assert not stmt.parsed.errors
    t = m.tie_out(stmt)
    assert t.result == "PASS", (t.result, t.note if hasattr(t, "note") else t)


@pytest.mark.parametrize("name,lines", [("A", STMT_A), ("C", STMT_C)])
def test_conversion_months_fail_without_the_loan_reader(name, lines):
    st = parse(lines)
    st.parsed.loans = []
    assert m.tie_out(st).result != "PASS"


def test_mid_loan_month_passes_and_adds_no_earlier_loan():
    st = parse(STMT_B)
    assert m.loans_booked_in_period(st.parsed, st.period) == []
    st.parsed.loans = []
    assert m.tie_out(st).result == "PASS"      # no new loan: the reader changes nothing


def test_net_must_equal_purchase_plus_loan_outstanding():
    bad = [ln.replace("TOTAL LOAN OUTSTANDING 41,000.00", "TOTAL LOAN OUTSTANDING 40,000.00") for ln in STMT_A]
    assert m.tie_out(parse(bad)).result != "PASS"


def test_conversion_credit_is_never_a_refund():
    st = parse(STMT_A)
    conv = [r for r in st.parsed.rows if r.kind == "emi_conversion"]
    assert len(conv) == 1 and conv[0].amount == 60000 and conv[0].direction == "Cr"
    # the only refund-like Cr rows would be kind "refund": there are none
    assert not [r for r in st.parsed.rows if r.kind == "refund"]


def test_credit_equal_to_principal_but_other_merchant_is_not_a_conversion():
    lines = [ln.replace("20JUN ACME TELEVISIONS PRIVATE LIMITED CC1234 60,000.00 CR",
                        "20JUN OTHER SHOP CC1234 60,000.00 CR") for ln in STMT_A]
    st = parse(lines)
    assert not [r for r in st.parsed.rows if r.kind == "emi_conversion"]
    assert [r for r in st.parsed.rows if r.kind == "refund" and r.amount == 60000]


def test_processing_fee_with_glued_card_reference_is_recognised():
    st = parse(STMT_A)
    fee = [r for r in st.parsed.rows if r.kind == "emi_processing_fee"]
    assert len(fee) == 1 and fee[0].amount == 500


def test_instalment_text_is_attached_to_the_row_above():
    st = parse(STMT_A)
    inst = [r for r in st.parsed.rows if r.instalment]
    assert [r.kind for r in inst] == ["emi_principal", "emi_principal", "emi_interest", "emi_interest"]
    assert [r.direction for r in inst] == ["Cr", "Dr", "Cr", "Dr"]


# ---------------------------------------------------------------------------
# booking
# ---------------------------------------------------------------------------

def _journals(lines, pay=date(2026, 9, 20)):
    st = parse(lines)
    return J.journals_for_settlement(st, pay, m.FEE_REVERSAL_RX, 100000)


def test_instalment_credits_are_never_booked():
    js, emi_rows, issues = _journals(STMT_A)
    nums = {j.row_id for j in js}
    for r in emi_rows:
        assert id(r) not in nums
    assert not [j for j in js if j.side == "D"]            # no deposit-side journal at all
    assert not issues
    kinds = sorted(j.kind for j in js)
    # principal spend, interest, fee, two GST rows
    assert kinds.count(J.K_EMI_INT) == 1 and kinds.count(J.K_FEE) == 3 and kinds.count(J.K_SPEND) == 2


def test_principal_is_a_mapped_spend_and_interest_is_not():
    js, _e, _i = _journals(STMT_A)
    prin = [j for j in js if j.emi and j.kind == J.K_SPEND]
    assert len(prin) == 1 and prin[0].amount == 20000 and prin[0].needs_mapping
    assert "1St Of 3 Installments Principal" in prin[0].description
    assert prin[0].raw_description.startswith("ACME TELEVISIONS PRIVATE LIMITED")
    intr = [j for j in js if j.kind == J.K_EMI_INT]
    assert len(intr) == 1 and intr[0].amount == 500 and not intr[0].needs_mapping
    assert prin[0].emi_validated and intr[0].emi_validated
    assert not [j for j in js if j.kind == J.K_EMI_INT and j.needs_mapping]
    assert not [j for j in js if j.emi and j.kind == J.K_SPEND and j.kind == J.K_EMI_INT]


def test_original_purchase_is_withheld_and_principal_booked_once():
    all_js, all_emi = [], []
    for lines in (STMT_A, STMT_B, STMT_C):
        js, emi_rows, _i = _journals(lines)
        all_js += js
        all_emi += emi_rows
    J.withhold_emi_originals(all_js, all_emi)
    withheld = [j for j in all_js if j.status == J.ST_EMI]
    assert sorted(j.amount for j in withheld) == [30000, 60000]
    ready = [j for j in all_js if j.status == J.ST_READY]
    old = [j for j in ready if j.emi and j.kind == J.K_SPEND and j.raw_description.startswith("ACME")]
    assert sum(j.amount for j in old) == 60000                 # the loan principal, not twice it
    # no ready journal books the converted purchase itself
    assert not [j for j in ready if j.kind == J.K_SPEND and not j.emi and j.amount in (60000, 30000)]


def test_interest_summary_ignores_the_credit_legs():
    st = parse(STMT_A)
    _js, emi_rows, _i = J.journals_for_settlement(st, date(2026, 9, 20), m.FEE_REVERSAL_RX, 100000)
    s = J.emi_summary(st.parsed.rows and [r for r in st.parsed.rows if r.kind.startswith("emi")])
    assert list(s["interest"].values()) == [500.00]


def test_unusable_interest_account_lists_interest_and_books_no_spend_twice():
    st = parse(STMT_A)
    js, emi_rows, _i = J.journals_for_settlement(st, date(2026, 9, 20), m.FEE_REVERSAL_RX, 100000,
                                                 book_interest=False)
    assert not [j for j in js if j.kind == J.K_EMI_INT]
    assert [r for r in emi_rows if r.kind == "emi_interest" and r.direction == "Dr"]


# ---------------------------------------------------------------------------
# addendum: unrecognised EMI wording is loud, never spend or refund
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("desc,dirn", [
    ("ACME STORE 2ND OF 6 INSTALLMENTS", "Dr"),
    ("ACME STORE 2ND OF 6 INSTALLMENTS", "Cr"),
    ("LOAN CONVERSION ACME STORE", "Cr"),
    ("LOAN CONVERSION ACME STORE", "Dr"),
    ("ACME STORE 3 OF 12", "Dr"),
    ("ACME STORE EMI", "Dr"),
    ("ACME STORE INSTALMENT 4", "Cr"),
])
def test_unrecognised_emi_wording_is_never_spend_or_refund(desc, dirn):
    assert m.classify(desc, dirn) == "emi_unclassified"


@pytest.mark.parametrize("desc", ["BALLOONS AND MORE", "SEMINAR HALL", "LOANER CARS LTD", "REMIT SERVICES",
                                  "ACME STORE MUMBAI"])
def test_ordinary_merchants_still_book_as_spend(desc):
    assert m.classify(desc, "Dr") == "spend"


def test_unclassified_emi_row_is_not_journalled():
    from types import SimpleNamespace
    st = parse(["16 JUN 2026 To 15 JUL 2026", "OPENING BALANCE 0.00",
                "20JUN ACME STORE 2ND OF 6 INSTALLMENTS 1,000.00",
                "15JUL NET OUTSTANDING BALANCE 1,000.00"])
    js, emi_rows, _i = J.journals_for_settlement(st, date(2026, 9, 20), m.FEE_REVERSAL_RX, 100000)
    assert not js and len(emi_rows) == 1 and emi_rows[0].kind == "emi_unclassified"
    assert SimpleNamespace  # keep import used


def test_non_emi_hsbc_statement_is_unchanged():
    st = parse(["16 DEC 2025 To 15 JAN 2026", "OPENING BALANCE 1,000.00",
                "20DEC EXAMPLE SHOP MUMBAI IN 500.00",
                "15JAN NET OUTSTANDING BALANCE 1,500.00",
                "TOTAL PURCHASE OUTSTANDING 1,500.00", "TOTAL LOAN OUTSTANDING 0.00"])
    assert [r.kind for r in st.parsed.rows] == ["spend"]
    assert st.parsed.loans == []
    assert m.tie_out(st).result == "PASS"


def test_reports_name_the_patterns():
    # the exact patterns chosen are part of the contract
    assert m.EMI_LIKE_RX.pattern.startswith(r"\bINSTAL+MENTS?\b")
    assert m.HSBC_FEE_RX.pattern.endswith("FEE")
    assert copy.copy(m.HSBC_INSTALMENT_RX).pattern
