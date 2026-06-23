"""
tests/test_26as_journal.py — Tests for the 26AS -> GnuCash TDS journal builder.

Uses synthetic deductors + a synthetic account tree (no PII, no external
files) so the matcher, split math, balancing, overrides and CSV output are
all exercised in CI. An optional end-to-end test runs only if a fixture
workbook + .gnucash are dropped under tests/fixtures/.

Run with:
    cd src && python -m pytest ../tests/test_26as_journal.py -v
"""
from __future__ import annotations

import csv
import datetime as dt
import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_26as_journal" / "scripts" / "build_tds_journals.py"


def _load():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("build_tds_journals", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # needed for dataclass annotation resolution
    spec.loader.exec_module(mod)
    return mod


m = _load()


def _accounts():
    """Synthetic account tree mirroring the real MyFinances2425 structure."""
    inc = [
        "Income:Interest Income:Interest on FD",
        "Income:Interest Income:Interest on BOB - FD",
        "Income:Interest Income:Interest on ICICI Bank - FD",
        "Income:Interest Income:Interest on Chola Bond",
        "Income:Interest Income:Interest on EPF Taxable",
        "Income:Interest Income:Interest from HSBC Bank",
        "Income:Dividend - MF",
        "Income:Dividend - Shares",
        "Income:Dividend - Shares:Dividend - DRL",
        "Income:Dividend - Shares:Dividend - Ramco Cements",
        "Income:Dividend - Shares:Dividend - JB Chemicals",
        "Income:xBusiness Income:Remuneration from Partnership",
    ]
    accts = [m.Account(path=p, leaf=p.split(":")[-1], type="INCOME") for p in inc]
    accts += [
        m.Account("Expense:TDS on Interest", "TDS on Interest", "EXPENSE"),
        m.Account("Expense:TDS on Dividend", "TDS on Dividend", "EXPENSE"),
        m.Account("Liabilities:Suspense", "Suspense", "LIABILITY"),
    ]
    return accts


def _deductor(sr, name, section, amt, tax):
    return m.Deductor(sr=sr, name=name, sections=(section,),
                      amount_paid=amt, tax_deducted=tax, tds_deposited=tax)


# ---------------------------------------------------------------------------
# Categorisation
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("section,cat", [
    ("194A", "A"), ("193", "A"), ("194", "B"), ("194T", "C"),
])
def test_categorize(section, cat):
    assert m.categorize((section,))[0] == cat


def test_categorize_unknown():
    assert m.categorize(("194I",))[0] is None  # unhandled section


# ---------------------------------------------------------------------------
# Matching — the 8 sample deductors land on the right account
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("name,cat,expected", [
    ("BANK OF BARODA", "A", "Income:Interest Income:Interest on BOB - FD"),
    ("ICICI BANK LIMITED", "A", "Income:Interest Income:Interest on ICICI Bank - FD"),
    ("CHOLAMANDALAM INVESTMENT AND FINANCE COMPANY LIMITED", "A",
     "Income:Interest Income:Interest on Chola Bond"),
    ("OFFICE OF REGIONAL PROVIDENT FUND COMMISSIONER BANDRA EAST", "A",
     "Income:Interest Income:Interest on EPF Taxable"),
    ("DR REDDY'S LABORATORIES LTD.", "B", "Income:Dividend - Shares:Dividend - DRL"),
    ("THE RAMCO CEMENTS LIMITED", "B", "Income:Dividend - Shares:Dividend - Ramco Cements"),
    ("J.B.CHEMICALS & PHARMACEUTICALS LTD.", "B", "Income:Dividend - Shares:Dividend - JB Chemicals"),
    ("ACME CONSULTING LLP", "C", "Income:xBusiness Income:Remuneration from Partnership"),
])
def test_match_sample_deductors(name, cat, expected):
    acct, conf, basis, cands = m.match_credit_account(name, cat, _accounts())
    assert acct == expected, f"{name}: got {acct} ({basis})"


def test_match_no_candidate_goes_suspense():
    """An interest deductor with no resembling account stays unmatched (Suspense)."""
    acct, conf, basis, cands = m.match_credit_account("ZZ UNKNOWN ENTITY XQ", "A", _accounts())
    assert acct is None and conf == "Suspense"


def test_generic_interest_on_fd_never_a_credit_match():
    """The fixed generic 'Interest on FD' must never be returned as a credit match."""
    for name in ("BANK OF BARODA", "SOME RANDOM FD HOLDER"):
        acct, *_ = m.match_credit_account(name, "A", _accounts())
        assert acct != m.ACC_INTEREST_ON_FD


# ---------------------------------------------------------------------------
# Split construction + balancing
# ---------------------------------------------------------------------------

def test_category_a_three_splits():
    d = _deductor(4, "BANK OF BARODA", "194A", 250237, 25024)
    j = m.build_journals([d], _accounts())[0]
    assert j.category == "A" and len(j.splits) == 3
    accts = {s.account: (s.debit, s.credit) for s in j.splits}
    assert accts[m.ACC_TDS_INTEREST] == (25024, 0)
    assert accts[m.ACC_INTEREST_ON_FD] == (round(250237 - 25024, 2), 0)
    assert accts["Income:Interest Income:Interest on BOB - FD"] == (0, 250237)
    assert j.balanced


def test_category_b_two_splits():
    d = _deductor(2, "DR REDDY'S LABORATORIES LTD.", "194", 208000, 20800)
    j = m.build_journals([d], _accounts())[0]
    assert j.category == "B" and len(j.splits) == 2
    accts = {s.account: (s.debit, s.credit) for s in j.splits}
    assert accts[m.ACC_TDS_DIVIDEND] == (20800, 0)
    assert accts["Income:Dividend - Shares:Dividend - DRL"] == (0, 20800)
    assert j.balanced


def test_category_c_emits_partnership_tds_as_is():
    d = _deductor(8, "ACME CONSULTING LLP", "194T", 3656276, 365628)
    j = m.build_journals([d], _accounts())[0]
    assert j.category == "C" and len(j.splits) == 2
    accts = {s.account: (s.debit, s.credit) for s in j.splits}
    assert accts[m.ACC_TDS_PARTNERSHIP] == (365628, 0)
    assert j.balanced


def test_all_sample_journals_balanced():
    deds = [
        _deductor(1, "CHOLAMANDALAM INVESTMENT AND FINANCE COMPANY LIMITED", "193", 45750, 4575),
        _deductor(2, "DR REDDY'S LABORATORIES LTD.", "194", 208000, 20800),
        _deductor(4, "BANK OF BARODA", "194A", 250237, 25024),
        _deductor(8, "ACME CONSULTING LLP", "194T", 3656276, 365628),
    ]
    for j in m.build_journals(deds, _accounts()):
        assert j.balanced, j.deductor


# ---------------------------------------------------------------------------
# Overrides + unknown section
# ---------------------------------------------------------------------------

def test_override_wins():
    d = _deductor(7, "OFFICE OF REGIONAL PROVIDENT FUND COMMISSIONER BANDRA EAST", "194A", 19380, 1938)
    j = m.build_journals([d], _accounts(), overrides={7: "Liabilities:Suspense"})[0]
    assert j.credit_account == "Liabilities:Suspense"
    assert j.credit_confidence == "Override" and not j.needs_review
    assert j.balanced


def test_unknown_section_flags_and_uses_suspense():
    d = _deductor(9, "MYSTERY CO", "194I", 1000, 100)
    j = m.build_journals([d], _accounts())[0]
    assert j.needs_review and j.credit_account == "Liabilities:Suspense"
    assert j.balanced


# ---------------------------------------------------------------------------
# Date + CSV output
# ---------------------------------------------------------------------------

def test_journal_date_is_march_31_current_year():
    assert m.journal_date() == f"{dt.date.today().year}-03-31"


def test_csv_roundtrips_and_balances():
    deds = [
        _deductor(4, "BANK OF BARODA", "194A", 250237, 25024),
        _deductor(2, "DR REDDY'S LABORATORIES LTD.", "194", 208000, 20800),
    ]
    journals = m.build_journals(deds, _accounts())
    out = Path(tempfile.gettempdir()) / "test_tds_journals.csv"
    m.write_csv(journals, out, "2025-26")

    # Re-parse: splits group by Transaction ID (repeated on every row); each
    # transaction balances when its signed Amount splits sum to zero. Debits
    # are positive, credits negative (GnuCash "Amount" convention).
    txns = {}
    with out.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            tid = row["Transaction ID"]
            assert tid.strip(), "every split row must carry the Transaction ID"
            assert tid.startswith("2526-"), f"Transaction ID needs FY prefix: {tid}"
            assert row["Date"].strip(), "every split row must carry the Date"
            assert row["Amount"].strip(), "every split row must carry an Amount"
            txns.setdefault(tid, 0.0)
            txns[tid] += float(row["Amount"])
    assert len(txns) == 2
    for tid, total in txns.items():
        assert abs(total) < 0.01, f"{tid} does not sum to zero: {total}"


# ---------------------------------------------------------------------------
# Optional end-to-end (only if fixtures provided — no PII committed)
# ---------------------------------------------------------------------------

_FX_XLSX = Path(__file__).resolve().parent / "fixtures" / "sample_26as.xlsx"
_FX_GNC = Path(__file__).resolve().parent / "fixtures" / "sample.gnucash"


@pytest.mark.skipif(not (_FX_XLSX.exists() and _FX_GNC.exists()),
                    reason="No fixtures at tests/fixtures/sample_26as.xlsx + sample.gnucash")
def test_end_to_end():
    out = Path(tempfile.gettempdir()) / "test_tds_e2e.csv"
    stats = m.run(_FX_XLSX, _FX_GNC, out)
    assert out.exists()
    assert stats["balanced_all"]
    assert stats["deductors"] >= 1
