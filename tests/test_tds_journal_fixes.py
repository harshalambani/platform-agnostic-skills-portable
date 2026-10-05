"""
TDS bundle (TDS-01..08) -- 26AS journal builder.

TDS-01 pins the behaviour that is correct today (it passes on the code before
any TDS fix), so the fixes below cannot quietly regress it. Each later section
carries its own negative tests.

Synthetic accounts and deductors only.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_26as_journal" / "scripts" / "build_tds_journals.py"


def _load():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("build_tds_journals", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load()

BOB = "Income:Interest Income:Interest on BOB - FD"
ICICI = "Income:Interest Income:Interest on ICICI Bank - FD"
GENERIC_FD = "Income:Interest Income:Interest on FD"


def acc(path, typ="INCOME", blocked=False):
    return m.Account(path=path, leaf=path.split(":")[-1], type=typ,
                     special=blocked, blocked=blocked)


def chart(*extra):
    base = [acc(BOB), acc(ICICI), acc(GENERIC_FD),
            acc("Expense:TDS on Interest", "EXPENSE"),
            acc("Expense:TDS on Dividend", "EXPENSE"),
            acc("Liabilities:Suspense", "LIABILITY")]
    return base + list(extra)


def ded(sr, name, section, paid, tax):
    return m.Deductor(sr=sr, name=name, sections=(section,), amount_paid=paid,
                      tax_deducted=tax, tds_deposited=tax)


# ---- TDS-01: pinned, currently correct --------------------------------------------------

def test_tds01_b2a_bank_of_baroda_matches_the_bob_account():
    acct, conf, _b, _c, tied = m.match_credit_account("BANK OF BARODA", "A", chart(), GENERIC_FD)
    assert acct == BOB and tied == []


def test_tds01_b2b_a_bank_with_its_own_account_does_not_land_on_bob():
    acct, _conf, _b, _c, _t = m.match_credit_account("ICICI BANK LIMITED", "A", chart(), GENERIC_FD)
    assert acct == ICICI
    assert acct != BOB


def test_tds01_b3a_an_override_changes_only_its_own_row():
    ds = [ded(1, "BANK OF BARODA", "194A", 1000.0, 100.0),
          ded(2, "ICICI BANK LIMITED", "194A", 2000.0, 200.0)]
    js = m.build_journals(ds, chart(), overrides={2: "Liabilities:Suspense"})
    assert js[0].credit_account == BOB and js[0].credit_confidence != "Override"
    assert js[1].credit_account == "Liabilities:Suspense" and js[1].credit_confidence == "Override"


def test_tds01_b3c_an_override_always_stays_for_review_and_balanced():
    j = m.build_journals([ded(1, "BANK OF BARODA", "194A", 1000.0, 100.0)], chart(),
                         overrides={1: ICICI})[0]
    assert j.needs_review and j.balanced
    assert j.credit_basis == "Model pick - confirm"


def test_tds01_b4a_the_generic_fd_account_is_found_and_used_as_the_second_debit():
    assert m.find_generic_fd_account(chart()) == GENERIC_FD
    j = m.build_journals([ded(1, "BANK OF BARODA", "194A", 1000.0, 100.0)], chart())[0]
    assert {s.account for s in j.splits if s.debit} == {"Expense:TDS on Interest", GENERIC_FD}
    assert j.balanced


def test_tds01_b4d_a_bank_specific_fd_account_is_never_the_generic_one():
    only_specific = [acc(BOB), acc(ICICI)]
    assert m.find_generic_fd_account(only_specific) is None
    assert m.find_generic_fd_account(chart()) not in (BOB, ICICI)


# ---- TDS-02: BANK alone never ties a payer to BoB ---------------------------------------

import pytest  # noqa: E402


@pytest.mark.parametrize("payer", ["HDFC BANK LIMITED", "ICICI BANK", "STATE BANK OF INDIA",
                                   "SOME OTHER BANK LTD", "AXIS BANK"])
def test_tds02_other_banks_do_not_resolve_to_bob(payer):
    two = [acc(BOB), acc("Income:Interest Income:Interest on Chola Bond")]
    acct, conf, _b, _c, _t = m.match_credit_account(payer, "A", two, "")
    assert acct != BOB                    # NEGATIVE: the word BANK is not a match
    assert acct is None and conf == "Suspense"


def test_tds02_bank_of_baroda_still_resolves_to_bob():
    acct, _c, _b, _cs, _t = m.match_credit_account(
        "BANK OF BARODA", "A", [acc(BOB), acc(ICICI)], "")
    assert acct == BOB


def test_tds02_the_alias_table_does_not_carry_the_bare_word_bank():
    assert "BANK" not in m.ALIASES["BOB"]


# ---- TDS-03: the generic FD account is recognised under its real names ------------------

@pytest.mark.parametrize("leaf", ["Interest on Fixed Deposits", "Interest on FDs",
                                  "Term Deposit", "Deposits", "Interest on Term Deposits",
                                  "Interest on Fixed Deposit", "Interest on FD", "FD Interest"])
def test_tds03_fd_account_name_variants_are_recognised(leaf):
    path = "Income:Interest Income:" + leaf
    got = m.find_generic_fd_account([acc(path), acc(BOB)])
    assert got == path


@pytest.mark.parametrize("leaf", ["Interest on Savings", "Interest from Savings Account",
                                  "Interest on Bonds", "Interest on Recurring Deposit",
                                  "Interest on Income Tax Refund"])
def test_tds03_a_non_fd_interest_account_is_not_picked(leaf):
    path = "Income:Interest Income:" + leaf
    assert m.find_generic_fd_account([acc(path), acc(BOB)]) is None   # NEGATIVE


def test_tds03_a_bank_specific_fd_account_is_still_not_generic():
    assert m.find_generic_fd_account([acc("Income:Interest Income:Interest on HDFC Fixed Deposits")]) is None


def test_tds03_a_blocked_fd_account_is_still_not_picked():
    assert m.find_generic_fd_account(
        [acc("Income:Interest Income:Interest on Fixed Deposits", blocked=True)]) is None
