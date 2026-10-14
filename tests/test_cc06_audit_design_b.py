"""
tests/test_cc06_audit_design_b.py -- CC-06 design B audit: gaps found against the
shipped HSBC EMI work. Synthetic fixtures only.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_cc06_hsbc_emi as base  # noqa: E402
from agents.skill_cc_spend_booking import journal as J

m = base.m

AXIS = [
    "01/04/2025 - 30/04/2025 15/05/2025 25/05/2025",
    "5,000.00 Dr 5,000.00 0.00 3,250.50 0.00 0.00 8,250.50 Dr",
    "05/04/2025 EXAMPLE MART MUMBAI DEPT STORES 1,500.50 Dr",
    "12/04/2025 ACME STORE 2ND OF 6 INSTALLMENTS 1,000.00 Dr",
    "13/04/2025 ACME STORE 3 OF 12 EMI 750.00 Cr",
]


@pytest.mark.parametrize("bank,card", [("Axis", "Flipkart"), ("ICICI", "Amazon"), ("SBI", "Cashback"),
                                       ("Yes", "Prosperity")])
def test_emi_wording_on_any_bank_is_classified_loud(bank, card):
    for desc, d in [("ACME STORE 2ND OF 6 INSTALLMENTS", "Dr"), ("ACME STORE 3 OF 12 EMI", "Cr"),
                    ("LOAN CONVERSION ACME", "Dr"), ("ACME STORE INSTALMENT", "Cr")]:
        assert m.classify(desc, d) == "emi_unclassified", (bank, desc, d)


def test_non_hsbc_emi_row_is_listed_never_booked_as_spend_or_refund():
    st = m.parse_statement(list(AXIS), "Axis", "Flipkart", "x.pdf")
    kinds = {(r.description, r.direction): r.kind for r in st.parsed.rows}
    assert kinds[("ACME STORE 2ND OF 6 INSTALLMENTS", "Dr")] == "emi_unclassified"
    assert kinds[("ACME STORE 3 OF 12 EMI", "Cr")] == "emi_unclassified"
    js, emi_rows, _i = J.journals_for_settlement(st, date(2025, 5, 20), m.FEE_REVERSAL_RX, 100000)
    assert sorted(r.amount for r in emi_rows) == [750, 1000]
    assert [j.amount for j in js] == [1500.5]                 # only the ordinary spend is booked
    assert not [j for j in js if j.side == "D"]               # the Cr is never a refund deposit
    assert not [r for r in st.parsed.rows if r.kind == "refund"]


def test_purchase_and_instalment_principal_are_never_both_booked():
    allj, alle = [], []
    for lines in (base.STMT_A, base.STMT_B, base.STMT_C):
        js, e, _ = base._journals(lines)
        allj += js
        alle += e
    J.withhold_emi_originals(allj, alle)
    ready = [j for j in allj if j.status == J.ST_READY and j.kind == J.K_SPEND]
    acme = [j for j in ready if "ACME" in j.raw_description]
    assert [j.amount for j in acme] == [20000, 20000, 20000]  # three principals, never the 60,000


def test_interest_is_never_booked_to_the_item_mapping():
    js, _e, _i = base._journals(base.STMT_A)
    intr = [j for j in js if j.kind == J.K_EMI_INT]
    assert len(intr) == 1 and not intr[0].needs_mapping       # no item account is ever asked for
    prin = [j for j in js if j.emi and j.kind == J.K_SPEND]
    assert all(j.amount != 500 for j in prin)


def test_conversion_credit_is_never_a_deposit_journal():
    js, emi_rows, _i = base._journals(base.STMT_A)
    assert not [j for j in js if j.side == "D"]
    assert [r for r in emi_rows if r.kind == "emi_conversion"]


def test_tie_out_fails_without_loan_reader_and_passes_with_it():
    for lines in (base.STMT_A, base.STMT_C):
        st = base.parse(lines)
        assert m.tie_out(st).result == "PASS"
        st.parsed.loans = []
        assert m.tie_out(st).result != "PASS"
