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
    acct, conf, basis, cands, tied = m.match_credit_account(name, cat, _accounts())
    assert acct == expected, f"{name}: got {acct} ({basis})"
    # NEGATIVE: a clear winner (strictly higher score than every other
    # candidate) must never be reported Ambiguous, and needs_review's
    # confidence check must see the same value it always has.
    assert conf != "Ambiguous"
    assert tied == []


def test_match_no_candidate_goes_suspense():
    """An interest deductor with no resembling account stays unmatched (Suspense)."""
    acct, conf, basis, cands, tied = m.match_credit_account("ZZ UNKNOWN ENTITY XQ", "A", _accounts())
    assert acct is None and conf == "Suspense"
    assert tied == []


# ---------------------------------------------------------------------------
# TDS-06 — tied candidates flag "Ambiguous" (first-wins matching is
# unchanged: the FIRST tied candidate in chart order is still posted).
# ---------------------------------------------------------------------------

def _tie_accounts():
    """Two Category-A income accounts that score EXACTLY the same against
    "ZENITH LIMITED" -- both share only the token ZENITH (LIMITED is a
    stopword, dropped from the deductor tokens; the extra token on the
    second leaf, GLOBAL, has no relationship to the deductor name and picks
    up neither a token nor a prefix hit, so it does not break the tie)."""
    return [
        m.Account("Income:Interest Income:Interest on Zenith Bank",
                  "Interest on Zenith Bank", "INCOME"),
        m.Account("Income:Interest Income:Interest on Zenith Global",
                  "Interest on Zenith Global", "INCOME"),
        m.Account("Expense:TDS on Interest", "TDS on Interest", "EXPENSE"),
        m.Account("Liabilities:Suspense", "Suspense", "LIABILITY"),
    ]


def _tie_accounts_below_threshold():
    """Two Category-A accounts that BOTH score 0 against the deductor (no
    shared tokens at all) -- a tie, but below the 1.5 confidence threshold,
    so it must stay Suspense, not become Ambiguous."""
    return [
        m.Account("Income:Interest Income:Interest on Foo Bank",
                  "Interest on Foo Bank", "INCOME"),
        m.Account("Income:Interest Income:Interest on Bar Bank",
                  "Interest on Bar Bank", "INCOME"),
    ]


def test_match_tied_candidates_flag_ambiguous_first_wins():
    accts = _tie_accounts()
    acct, conf, basis, cands, tied = m.match_credit_account("ZENITH LIMITED", "A", accts)
    assert conf == "Ambiguous"
    # First-wins: the account actually returned is the FIRST tied candidate
    # in chart order, never a different one because of the tie.
    assert acct == "Income:Interest Income:Interest on Zenith Bank"
    assert tied == [
        "Income:Interest Income:Interest on Zenith Bank",
        "Income:Interest Income:Interest on Zenith Global",
    ]
    # basis names each tied candidate and its own hits.
    assert "Zenith Bank" in basis and "Zenith Global" in basis


def test_match_tie_below_threshold_stays_suspense_not_ambiguous():
    """NEGATIVE: a tie that never clears the 1.5 confidence threshold is
    indistinguishable from any other unconfident result -- still Suspense."""
    acct, conf, basis, cands, tied = m.match_credit_account(
        "UNKNOWN ENTITY QQ", "A", _tie_accounts_below_threshold())
    assert acct is None
    assert conf == "Suspense"
    assert tied == []


def test_build_journals_ambiguous_row_not_moved_to_suspense_and_balances():
    """NEGATIVE: an Ambiguous row keeps its first-wins (real) credit account
    -- it must never be redirected to Suspense -- and its splits still
    balance exactly like any other Category A journal."""
    d = _deductor(1, "ZENITH LIMITED", "194A", 100000.0, 10000.0)
    j = m.build_journals([d], _tie_accounts())[0]
    assert j.credit_confidence == "Ambiguous"
    assert j.needs_review is True
    assert j.credit_account == "Income:Interest Income:Interest on Zenith Bank"
    assert j.credit_account != m.ACC_SUSPENSE
    assert j.tied_candidates == [
        "Income:Interest Income:Interest on Zenith Bank",
        "Income:Interest Income:Interest on Zenith Global",
    ]
    assert j.balanced


def test_build_journals_ambiguous_row_fails_on_pre_tds06_matcher():
    """This is the primary regression test: on the pre-fix matcher (strict
    'score > best_score'), the tie is silently resolved to the FIRST
    candidate with confidence High/Medium and needs_review stays False --
    the exact defect TDS-06 fixes. This assertion demonstrably FAILS against
    the code as it stood at ea227b7 (needs_review was False, confidence was
    "High", not "Ambiguous")."""
    d = _deductor(1, "ZENITH LIMITED", "194A", 100000.0, 10000.0)
    j = m.build_journals([d], _tie_accounts())[0]
    assert j.credit_confidence == "Ambiguous"
    assert j.needs_review is True


def test_build_15g_journals_tied_candidates_flagged_ambiguous_too():
    """The same tie in a Part II (15G/15H) deductor is flagged the same way
    -- build_15g_journals reuses match_credit_account's Category A pool."""
    d = _deductor(1, "ZENITH LIMITED", "194A", 100000.0, 0.0)
    j = m.build_15g_journals([d], _tie_accounts())[0]
    assert j.credit_confidence == "Ambiguous"
    assert j.needs_review is True
    assert j.credit_account == "Income:Interest Income:Interest on Zenith Bank"
    assert j.tied_candidates == [
        "Income:Interest Income:Interest on Zenith Bank",
        "Income:Interest Income:Interest on Zenith Global",
    ]
    assert j.balanced


def test_write_review_appends_tied_candidates_as_last_column():
    """The review CSV has the new last column; every existing column keeps
    its original position."""
    d = _deductor(1, "ZENITH LIMITED", "194A", 100000.0, 10000.0)
    journals = m.build_journals([d], _tie_accounts())
    out = Path(tempfile.gettempdir()) / "test_tds06_review.csv"
    m.write_review(journals, out, _tie_accounts())
    with out.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    assert header == ["Sr", "Deductor", "Section", "Category", "Credit Account",
                      "Confidence", "Account Exists", "Balanced", "Debit", "Credit",
                      "Needs Review", "Basis", "Tied Candidates"]
    data_row = rows[1]
    assert data_row[-1] == (
        "Income:Interest Income:Interest on Zenith Bank; "
        "Income:Interest Income:Interest on Zenith Global"
    )
    assert data_row[5] == "Ambiguous"       # Confidence position unchanged
    assert data_row[10] == "yes"            # Needs Review position unchanged


def test_review_flag_ambiguous_is_never_review():
    """LLM-fallback pin: AGENT.md tells the agent to call apply_overrides
    only for rows flagged REVIEW. The CLI flag text for an Ambiguous row
    must never contain the literal "REVIEW" (case-sensitive) that a
    text-reading LLM keys its fallback decision on."""
    assert m._review_flag(True, "Ambiguous") == "  <-- AMBIGUOUS (tag manually in Review tab)"
    assert "REVIEW" not in m._review_flag(True, "Ambiguous")
    assert m._review_flag(True, "Suspense") == "  <-- REVIEW"
    assert m._review_flag(False, "High") == ""


def test_agent_md_excludes_ambiguous_from_llm_fallback():
    """Pin: the agent prompt must explicitly tell the LLM never to call
    apply_overrides on an Ambiguous deductor -- otherwise a small
    tool-calling model, seeing a NEEDS-REVIEW row with a plausible-looking
    tied candidate, would guess exactly the account the user is supposed to
    confirm by hand."""
    agent_md = (ROOT / "src" / "agents" / "skill_26as_journal" / "AGENT.md").read_text(
        encoding="utf-8")
    normalized = " ".join(agent_md.split())
    assert "AMBIGUOUS" in agent_md
    assert "never call `apply_overrides` for an AMBIGUOUS deductor" in normalized


# ---------------------------------------------------------------------------
# Placeholder / hidden accounts are never valid credit candidates (the bug:
# 'Income:Interest Income' is a GnuCash placeholder and cannot be posted to).
# ---------------------------------------------------------------------------

def test_placeholder_parent_excluded_from_candidates():
    accts = [
        m.Account("Income:Interest Income", "Interest Income", "INCOME", special=True),
        m.Account("Income:Interest Income:Interest on HDFC - FD",
                  "Interest on HDFC - FD", "INCOME"),
    ]
    paths = [c.path for c in m._candidates_for("A", accts)]
    assert "Income:Interest Income" not in paths          # placeholder header
    assert "Income:Interest Income:Interest on HDFC - FD" in paths


def test_placeholder_only_interest_account_routes_to_suspense():
    """If the ONLY category-A account is the placeholder parent, the deductor
    must go to Suspense — never post directly to the placeholder."""
    accts = [m.Account("Income:Interest Income", "Interest Income", "INCOME",
                       special=True)]
    acct, conf, basis, cands, tied = m.match_credit_account("HDFC BANK LIMITED", "A", accts)
    assert acct is None and conf == "Suspense"
    assert cands == []
    assert tied == []


def test_hidden_account_never_matched():
    """A hidden (retired) account must not be offered even if the name matches."""
    accts = [
        m.Account("Income:Interest Income:Interest on Old Bank",
                  "Interest on Old Bank", "INCOME", special=True),
        m.Account("Income:Interest Income:Interest on HDFC - FD",
                  "Interest on HDFC - FD", "INCOME"),
    ]
    acct, conf, basis, cands, tied = m.match_credit_account("OLD BANK", "A", accts)
    assert "Income:Interest Income:Interest on Old Bank" not in cands
    assert acct != "Income:Interest Income:Interest on Old Bank"


def test_special_account_not_taken_as_generic_fd():
    """A placeholder/hidden FD account must not be picked as the generic FD
    debit account either."""
    def acc(p, special=False):
        return m.Account(p, p.split(":")[-1], "INCOME", special=special)
    accts = [
        acc("Income:Interest Income:Interest on FD", special=True),   # placeholder
        acc("Income:Interest Income:Interest on Fixed Deposit"),      # real generic
    ]
    assert m.find_generic_fd_account(accts) == \
        "Income:Interest Income:Interest on Fixed Deposit"


def test_build_journals_places_placeholder_only_deductor_on_suspense():
    """End-to-end at the journal level: a deductor whose category has only a
    placeholder account lands its credit split on Suspense and is flagged."""
    accts = [
        m.Account("Income:Interest Income", "Interest Income", "INCOME", special=True),
        m.Account("Expense:TDS on Interest", "TDS on Interest", "EXPENSE"),
        m.Account("Liabilities:Suspense", "Suspense", "LIABILITY"),
    ]
    d = _deductor(1, "SOME OBSCURE PAYER", "194A", 10000.0, 1000.0)
    journals = m.build_journals([d], accts)
    j = journals[0]
    assert j.credit_account == "Liabilities:Suspense"
    assert j.needs_review is True
    assert j.balanced


def test_generic_interest_on_fd_never_a_credit_match():
    """The generic FD-interest account must never be returned as a credit match."""
    fd = m.find_generic_fd_account(_accounts())
    for name in ("BANK OF BARODA", "SOME RANDOM FD HOLDER"):
        acct, _c, _b, cands, _tied = m.match_credit_account(name, "A", _accounts(), fd)
        assert acct != fd
        assert fd not in cands


def test_find_generic_fd_account_variants():
    """The generic FD account is fuzzy-found regardless of its exact name, and a
    deductor-specific '... - FD' account is NOT mistaken for the generic one."""
    def acc(p):
        return m.Account(p, p.split(":")[-1], "INCOME")
    # Harshal-style
    assert m.find_generic_fd_account([acc("Income:Interest Income:Interest on FD"),
                                      acc("Income:Interest Income:Interest on BOB - FD")]) \
        == "Income:Interest Income:Interest on FD"
    # Bob-style ('Interest on Fixed Deposit') + specific accounts present
    fd = m.find_generic_fd_account([
        acc("Income:Interest Income:Interest on Fixed Deposit"),
        acc("Income:Interest Income:Interest on HDFC - FD"),
        acc("Income:Interest Income:Interest on ICICI Bank - FD"),
    ])
    assert fd == "Income:Interest Income:Interest on Fixed Deposit"
    # No FD account at all -> None (caller falls back to the canonical name)
    assert m.find_generic_fd_account([acc("Income:Interest Income:Interest on Bonds")]) is None


def test_category_a_debit_uses_fuzzy_found_fd_account():
    """Category A's second debit posts to the chart's actual FD account, even
    when it isn't literally named 'Interest on FD'."""
    def acc(p, t="INCOME"):
        return m.Account(p, p.split(":")[-1], t)
    accts = [
        acc("Income:Interest Income:Interest on Fixed Deposit"),
        acc("Income:Interest Income:Interest on HDFC - FD"),
        acc("Expense:TDS on Interest", "EXPENSE"),
        acc("Liabilities:Suspense", "LIABILITY"),
    ]
    d = _deductor(2, "HDFC BANK LIMITED", "193", 18023.06, 0.0)
    j = m.build_journals([d], accts)[0]
    debit_accts = {s.account for s in j.splits if s.debit}
    assert "Income:Interest Income:Interest on Fixed Deposit" in debit_accts
    assert j.credit_account == "Income:Interest Income:Interest on HDFC - FD"
    assert j.balanced


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
# A1 (194T double-booking fix): when partner_comp_configured is True, this
# skill's own Category C journals must be left OUT of the importable CSV
# (partner_comp_recon's jv_emitter already books that TDS month-by-month) --
# but still fully present in the Journal objects (for the review CSV/tab),
# with a clear basis instead of a needs-review flag. When False (today's
# default), Category C is unchanged except for a new double-booking warning
# appended to credit_basis.
#
# Proven to fail on b3a9df6 (pre-fix): partner_comp_configured did not exist
# as a build_journals()/build_csv_rows() parameter at all, so Category C rows
# were always included in the CSV -- test_partner_comp_configured_excludes_
# category_c_from_csv's "no Category C row present" assertion fails on
# b3a9df6 (TypeError: build_journals() got an unexpected keyword argument
# 'partner_comp_configured').
# ---------------------------------------------------------------------------

def test_partner_comp_configured_excludes_category_c_from_csv():
    d = _deductor(8, "ACME CONSULTING LLP", "194T", 3656276, 365628)
    journals = m.build_journals([d], _accounts(), partner_comp_configured=True)
    j = journals[0]
    assert j.category == "C"
    assert j.excluded_from_journal is True
    assert j.needs_review is False
    assert "Partner Comp journal" in j.credit_basis
    rows = m.build_csv_rows(journals, "2025-26")
    assert rows == []  # excluded entirely -- not merely re-labelled


def test_partner_comp_not_configured_keeps_category_c_with_warning():
    d = _deductor(8, "ACME CONSULTING LLP", "194T", 3656276, 365628)
    journals = m.build_journals([d], _accounts(), partner_comp_configured=False)
    j = journals[0]
    assert j.excluded_from_journal is False
    assert "WARNING" in j.credit_basis and "double-book" in j.credit_basis
    rows = m.build_csv_rows(journals, "2025-26")
    assert len(rows) == 2  # both splits still emitted, exactly as before


def test_partner_comp_configured_other_categories_unchanged_byte_for_byte():
    """A/B categories in the SAME batch as an excluded Category C must come
    out identical whether partner_comp_configured is True or False -- the
    exclusion must not perturb any other deductor's rows."""
    deds = [
        _deductor(1, "BANK OF BARODA", "194A", 250237, 25024),
        _deductor(2, "DR REDDY'S LABORATORIES LTD.", "194", 208000, 20800),
        _deductor(8, "ACME CONSULTING LLP", "194T", 3656276, 365628),
    ]
    rows_off = m.build_csv_rows(
        m.build_journals(deds, _accounts(), partner_comp_configured=False), "2025-26")
    rows_on = m.build_csv_rows(
        m.build_journals(deds, _accounts(), partner_comp_configured=True), "2025-26")
    non_c_off = [r for r in rows_off if "Sec 194T" not in r["Description"]]
    non_c_on = [r for r in rows_on if "Sec 194T" not in r["Description"]]
    assert non_c_off == non_c_on
    assert len(rows_on) == len(rows_off) - 2  # exactly the 194T splits dropped


def test_end_to_end_194t_tds_counted_exactly_once_across_both_journals():
    """Cross-skill assertion: the s.194T TDS debit total, summed across BOTH
    partner_comp_recon's monthly journal CSV (jv_emitter) and this skill's
    26AS journal CSV (build_tds_journals, partner_comp_configured=True), must
    equal the 26AS-reported TDS figure exactly ONCE -- not twice (the double-
    booking this fix exists to close) and not zero (the exclusion must not
    silently drop the TDS from every ledger)."""
    from agents.skill_partner_comp_recon.jv_emitter import build_journals as pc_build_journals
    from agents.skill_partner_comp_recon.mapper import build_input_data
    from agents.skill_partner_comp_recon.engine import build_report

    pc_accounts = {
        "bank": "Assets:Bank:Current Account",
        "tds_expense": "Expenses:Tax:TDS 194T",
        "interest_on_capital": "Income:PGBP:Interest on Capital",
        "current_account": "Assets:Firm Current Account",
        "capital_contribution": "Assets:Firm Capital Account",
        "medical_expense": "Expenses:Medical",
        "remuneration_income": "Income:PGBP:Remuneration",
        "share_of_profit_income": "Income:PGBP:Share of Profit",
    }
    advice = {
        "month": "2025-04",
        "source_name": "synthetic_l1_classA_2025-04.pdf",
        "total_paid": 480000.0,
        "remuneration": 200000.0,
        "share_of_profit_gross": 300000.0,
        "additional_share_of_profit": 0.0,
        "tds": -20000.0,
    }
    data = build_input_data(
        financial_year="2025-26", advice_records=[advice],
        accounts=pc_accounts, firm_name="Synthetic Test LLP",
    )
    report = build_report(data)
    pc_journals = pc_build_journals(report, pc_accounts)
    pc_tds_total = sum(
        s.debit for j in pc_journals for s in j.splits
        if s.account == pc_accounts["tds_expense"]
    )
    assert pc_tds_total == pytest.approx(20000.0, abs=0.01)

    # The SAME s.194T TDS (20000.0) as it would appear in the 26AS workbook
    # for this firm's deductor -- this skill must exclude it from its CSV.
    d = _deductor(1, "SYNTHETIC TEST LLP", "194T", 220000, 20000.0)
    journals = m.build_journals([d], _accounts(), partner_comp_configured=True)
    rows = m.build_csv_rows(journals, "2025-26")
    journal_194t_total = sum(
        float(r["Amount"]) for r in rows if float(r["Amount"]) > 0
    )
    assert journal_194t_total == 0.0  # nothing from THIS skill's CSV

    combined_total = pc_tds_total + journal_194t_total
    assert combined_total == pytest.approx(20000.0, abs=0.01)  # counted once


# ---------------------------------------------------------------------------
# Category G -- 15G/15H (Part II)
#
# The 15G/15H interest is already booked in a generic FD-interest bucket --
# this journal is a RECLASSIFICATION (Dr generic FD interest, Cr the specific
# NBFC account), not new income. With tax_deducted == 0 (the statutory case)
# that's a 2-split; if tax was ever withheld anyway it degenerates to the
# identical 3-split Category A uses.
# ---------------------------------------------------------------------------

def test_category_g_zero_tax_two_split_reclass():
    d = _deductor(1, "BANK OF BARODA", "194A", 50000, 0)
    j = m.build_15g_journals([d], _accounts())[0]
    assert j.category == "G" and len(j.splits) == 2
    accts = {s.account: (s.debit, s.credit) for s in j.splits}
    assert accts[m.ACC_INTEREST_ON_FD] == (50000, 0)
    assert accts["Income:Interest Income:Interest on BOB - FD"] == (0, 50000)
    assert j.balanced


def test_category_g_nonzero_tax_three_split_like_category_a():
    """Statutory tax on a 15G/15H deductor 'can't be' non-zero, but if the
    26AS ever shows one, it must post exactly like Category A's 3-split."""
    d = _deductor(1, "BANK OF BARODA", "194A", 250237, 25024)
    j = m.build_15g_journals([d], _accounts())[0]
    assert j.category == "G" and len(j.splits) == 3
    accts = {s.account: (s.debit, s.credit) for s in j.splits}
    assert accts[m.ACC_TDS_INTEREST] == (25024, 0)
    assert accts[m.ACC_INTEREST_ON_FD] == (round(250237 - 25024, 2), 0)
    assert accts["Income:Interest Income:Interest on BOB - FD"] == (0, 250237)
    assert j.balanced


def test_category_g_never_collides_with_category_a_tdsj_series():
    """A Part II 194A deductor must NOT become Category A / TDSJ -- category
    is decided by which Part the row came from, never by a section lookup."""
    part_i = _deductor(1, "BANK OF BARODA", "194A", 1000, 100)
    part_ii = _deductor(1, "BANK OF BARODA", "194A", 50000, 0)  # same Sr, same section
    journals = m.build_journals([part_i], _accounts())
    journals += m.build_15g_journals([part_ii], _accounts())
    assert [j.category for j in journals] == ["A", "G"]

    out = Path(tempfile.gettempdir()) / "test_category_g_ids.csv"
    m.write_csv(journals, out, "2025-26")
    txns = {}
    with out.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            txns.setdefault(row["Transaction ID"], 0.0)
            txns[row["Transaction ID"]] += float(row["Amount"])
    assert set(txns) == {"2526-TDSJ01", "2526-15GJ01"}, \
        "Category G must get its own 15GJ series, never fold into TDSJ"
    for tid, total in txns.items():
        assert abs(total) < 0.01, f"{tid} does not sum to zero: {total}"


def test_category_g_empty_part_ii_is_a_noop():
    """No 15G/15H deductors (Part II absent or empty) must not crash and must
    not emit any Category G journals."""
    assert m.build_15g_journals([], _accounts()) == []


def test_series_for_category_maps_g_to_15gj_and_rejects_unknown():
    assert m.series_for_category("G") == "15GJ"
    assert m.series_for_category("A") == "TDSJ"
    assert m.series_for_category("T") == "TCSJ"
    with pytest.raises(ValueError):
        m.series_for_category("Z")


# ---------------------------------------------------------------------------
# split_part_ii -- the Part-I-only sibling file that fixes the stale
# hand-filtered-copy bug (see build_tds_journals.py module docstring and
# split_part_ii's own docstring).
# ---------------------------------------------------------------------------

def test_part_ii_series_derived_not_hardcoded():
    # PART_II_SERIES must track CATEGORY_SERIES["G"], not restate "15GJ"
    # independently -- a rename of the series in one place must not desync
    # the splitter from the id the journal was actually built with.
    assert m.PART_II_SERIES == m.CATEGORY_SERIES["G"] == "15GJ"


def _row(txn_id, account, amount):
    return {"Date": "2026-03-31", "Transaction ID": txn_id, "Number": txn_id,
            "Description": "x", "Account": account, "Amount": f"{amount:.2f}",
            "Currency": "INR"}


def test_split_part_ii_partitions_mixed_journal_tcs_stays_with_part_i():
    rows = [
        # TDSJ01 -- Part I, 2 splits
        _row("2526-TDSJ01", "Expense:TDS on Interest", 100.0),
        _row("2526-TDSJ01", "Income:Interest:BOB", -100.0),
        # TCSJ01 -- Part VI, stays with Part I
        _row("2526-TCSJ01", "Expense:TCS on Foreign Trip", 50.0),
        _row("2526-TCSJ01", "Expense:Drawings", -50.0),
        # 15GJ01 -- Part II, dropped to its own side
        _row("2526-15GJ01", "Income:Interest:Generic FD", 200.0),
        _row("2526-15GJ01", "Income:Interest:Bajaj Finance", -200.0),
    ]
    part_i, part_ii, problems = m.split_part_ii(rows)
    assert problems == []
    assert {r["Transaction ID"] for r in part_i} == {"2526-TDSJ01", "2526-TCSJ01"}
    assert {r["Transaction ID"] for r in part_ii} == {"2526-15GJ01"}
    assert len(part_i) == 4 and len(part_ii) == 2


def test_split_part_ii_every_side_balances_per_transaction():
    rows = [
        _row("2526-TDSJ01", "Expense:TDS on Interest", 25024.0),
        _row("2526-TDSJ01", "Income:Interest:FD", 225213.0),
        _row("2526-TDSJ01", "Income:Interest:BOB", -250237.0),
        _row("2526-15GJ01", "Income:Interest:FD", 50000.0),
        _row("2526-15GJ01", "Income:Interest:Bajaj", -50000.0),
    ]
    part_i, part_ii, _problems = m.split_part_ii(rows)

    def _totals(side):
        totals: dict[str, float] = {}
        for r in side:
            totals[r["Transaction ID"]] = totals.get(r["Transaction ID"], 0.0) + float(r["Amount"])
        return totals

    for tid, total in {**_totals(part_i), **_totals(part_ii)}.items():
        assert abs(total) < 0.01, f"{tid} does not sum to zero: {total}"


def test_split_part_ii_drops_multi_split_15gj_transaction_whole():
    """A 3-split 15GJ transaction (nonzero tax -- Category G's 3-split path)
    must move to part_ii in its entirety, never half of it left in part_i."""
    rows = [
        _row("2526-15GJ01", "Expense:TDS on Interest", 1000.0),
        _row("2526-15GJ01", "Income:Interest:FD", 24000.0),
        _row("2526-15GJ01", "Income:Interest:Bajaj", -25000.0),
        _row("2526-TDSJ01", "Expense:TDS on Interest", 10.0),
        _row("2526-TDSJ01", "Income:Interest:BOB", -10.0),
    ]
    part_i, part_ii, _problems = m.split_part_ii(rows)
    assert len(part_ii) == 3
    assert all(r["Transaction ID"] == "2526-15GJ01" for r in part_ii)
    assert not any(r["Transaction ID"] == "2526-15GJ01" for r in part_i)


def test_split_part_ii_unparseable_id_kept_in_part_i_and_reported():
    rows = [
        _row("garbage-with-no-dash-removed", "Expense:X", 10.0),
    ]
    rows[0]["Transaction ID"] = "nodashhere"   # no '-' at all -- unparseable
    part_i, part_ii, problems = m.split_part_ii(rows)
    assert len(part_i) == 1 and part_ii == []
    assert problems and "nodashhere" in problems[0]


def test_split_part_ii_fy_prefix_containing_dash_still_parses():
    """fy_prefix may itself contain '-' -- the token must be parsed off the
    LAST '-', not the first, or a prefix like '25-26' would break the split."""
    rows = [
        _row("25-26-15GJ01", "Income:Interest:FD", 100.0),
        _row("25-26-15GJ01", "Income:Interest:Bajaj", -100.0),
        _row("25-26-TDSJ01", "Expense:TDS on Interest", 5.0),
        _row("25-26-TDSJ01", "Income:Interest:BOB", -5.0),
    ]
    part_i, part_ii, problems = m.split_part_ii(rows)
    assert problems == []
    assert {r["Transaction ID"] for r in part_ii} == {"25-26-15GJ01"}
    assert {r["Transaction ID"] for r in part_i} == {"25-26-TDSJ01"}


def test_split_part_ii_no_part_ii_rows_returns_empty_second_side():
    rows = [
        _row("2526-TDSJ01", "Expense:TDS on Interest", 10.0),
        _row("2526-TDSJ01", "Income:Interest:BOB", -10.0),
    ]
    part_i, part_ii, problems = m.split_part_ii(rows)
    assert part_ii == [] and problems == []
    assert len(part_i) == 2


def test_write_part_i_split_writes_file_only_when_part_ii_present(tmp_path):
    out_path = tmp_path / "run-tds-journals.csv"
    rows_with_g = [
        _row("2526-TDSJ01", "Expense:TDS on Interest", 10.0),
        _row("2526-TDSJ01", "Income:Interest:BOB", -10.0),
        _row("2526-15GJ01", "Income:Interest:FD", 100.0),
        _row("2526-15GJ01", "Income:Interest:Bajaj", -100.0),
    ]
    part_i_path, problems = m.write_part_i_split(rows_with_g, out_path)
    assert problems == []
    assert part_i_path is not None
    written = Path(part_i_path)
    assert written.name == "run-tds-journals-partI.csv"
    assert written.exists()
    with written.open(newline="", encoding="utf-8") as f:
        written_rows = list(csv.DictReader(f))
    assert {r["Transaction ID"] for r in written_rows} == {"2526-TDSJ01"}


def test_write_part_i_split_no_part_ii_writes_nothing_and_deletes_stale(tmp_path):
    out_path = tmp_path / "run-tds-journals.csv"
    stale = m.part_i_path_for(out_path)
    stale.write_text("stale content from a previous run\n", encoding="utf-8")
    assert stale.exists()

    rows_without_g = [
        _row("2526-TDSJ01", "Expense:TDS on Interest", 10.0),
        _row("2526-TDSJ01", "Income:Interest:BOB", -10.0),
    ]
    part_i_path, problems = m.write_part_i_split(rows_without_g, out_path)
    assert part_i_path is None and problems == []
    assert not stale.exists(), "a stale Part I file from a previous run must be deleted"


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
# Category T — TCS (Part VI)
#
# TCS is a tax credit like TDS: if it never reaches the books the return
# understates taxes paid and overstates the balance due. The journal moves the
# TAX only, out of the personal spending it was collected on.
# ---------------------------------------------------------------------------

def _tcs_accounts():
    return _accounts() + [
        m.Account("Expense:TCS on Foreign Trip", "TCS on Foreign Trip", "EXPENSE"),
        m.Account("Expense:Drawings", "Drawings", "EXPENSE"),
        m.Account("Assets:Current Assets:HDFC Bank", "HDFC Bank", "BANK"),
    ]


def _collector(sr, name, section, amt, tax):
    return m.Deductor(sr=sr, name=name, sections=(section,),
                      amount_paid=amt, tax_deducted=tax, tds_deposited=tax)


@pytest.mark.parametrize("section", ["206CQ", "206CR", "206CL", "206C"])
def test_tcs_sections_recognised(section):
    assert m.is_tcs_section((section,))


def test_non_tcs_section_not_recognised():
    assert not m.is_tcs_section(("194A",))
    assert not m.is_tcs_section(())


def test_tcs_journal_posts_tax_only_dr_tcs_cr_drawings():
    c = _collector(1, "THOMAS COOK INDIA LIMITED", "206CQ", 500000, 25000)
    j = m.build_tcs_journals([c], _tcs_accounts())[0]
    assert j.category == "T" and len(j.splits) == 2
    accts = {s.account: (s.debit, s.credit) for s in j.splits}
    # The 500000 spend is already in the books — only the 25000 tax moves.
    assert accts["Expense:TCS on Foreign Trip"] == (25000, 0)
    assert accts["Expense:Drawings"] == (0, 25000)
    assert j.balanced


def test_tcs_accounts_discovered_from_chart_not_hardcoded():
    """A book that names the account differently must still be found."""
    accts = _accounts() + [
        m.Account("Assets:TCS Receivable", "TCS Receivable", "ASSET"),
        m.Account("Equity:Drawings", "Drawings", "EQUITY"),
    ]
    j = m.build_tcs_journals([_collector(1, "X TOURS", "206CQ", 100, 10)], accts)[0]
    paths = {s.account for s in j.splits}
    assert paths == {"Assets:TCS Receivable", "Equity:Drawings"}


def test_tcs_falls_back_to_canonical_names_when_chart_has_neither():
    """Missing accounts must surface as names to CREATE, not vanish."""
    j = m.build_tcs_journals([_collector(1, "X TOURS", "206CQ", 100, 10)],
                             _accounts())[0]
    paths = {s.account for s in j.splits}
    assert paths == {m.ACC_TCS_DEFAULT, m.ACC_DRAWINGS}


def test_tcs_credit_account_is_configurable():
    """TCS paid across separately credits Bank, not Drawings."""
    bank = "Assets:Current Assets:HDFC Bank"
    j = m.build_tcs_journals([_collector(1, "X TOURS", "206CQ", 100, 10)],
                             _tcs_accounts(), credit_account=bank)[0]
    assert j.credit_account == bank
    assert {s.account for s in j.splits} == {"Expense:TCS on Foreign Trip", bank}
    assert j.balanced


def test_tcs_override_wins():
    j = m.build_tcs_journals([_collector(3, "X TOURS", "206CQ", 100, 10)],
                             _tcs_accounts(),
                             overrides={3: "Liabilities:Suspense"})[0]
    assert j.credit_account == "Liabilities:Suspense"
    assert j.credit_confidence == "Override" and not j.needs_review


def test_non_206c_section_in_part_vi_goes_suspense():
    """A section we can't justify as TCS must not silently claim a tax credit."""
    j = m.build_tcs_journals([_collector(1, "ODD CO", "194A", 100, 10)],
                             _tcs_accounts())[0]
    assert j.needs_review and j.credit_account == "Liabilities:Suspense"
    assert j.balanced


def test_tcs_never_uses_the_income_matcher():
    """match_credit_account searches INCOME subtrees; a collection at source
    has no income leg, so no TCS split may land on one."""
    j = m.build_tcs_journals([_collector(1, "BANK OF BARODA", "206CQ", 100, 10)],
                             _tcs_accounts())[0]
    income = {a.path for a in _tcs_accounts() if a.type == "INCOME"}
    assert not ({s.account for s in j.splits} & income)


def _party_sheet(ws, title, rows):
    """Write a Convert-shaped Part I / Part VI sheet: title band, meta strip,
    header row, then one row per transaction with the party's header totals
    repeated in cols 2/4/5/6 and the section in col 8."""
    ws.cell(1, 1, f"{title} - Details")
    ws.cell(2, 1, "Assessee Name: X  |  PAN: AAAAA1111A  |  Financial Year: 2025-26")
    ws.cell(3, 1, "Sr.No.")
    r = 4
    for sr, name, section, amt, tax in rows:
        ws.cell(r, 1, sr)
        ws.cell(r, 2, name)
        ws.cell(r, 4, amt)
        ws.cell(r, 5, tax)
        ws.cell(r, 6, tax)
        ws.cell(r, 8, section)
        r += 1


def _make_workbook(path, parts):
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    for title, rows in parts.items():
        _party_sheet(wb.create_sheet(title=title), title, rows)
    wb.save(path)
    return path


def test_parse_parts_reads_both_tds_and_tcs():
    p = Path(tempfile.gettempdir()) / "test_26as_both.xlsx"
    _make_workbook(p, {
        "Part I": [(1, "BANK OF BARODA", "194A", 1000.0, 100.0)],
        "Part VI": [(1, "THOMAS COOK INDIA LIMITED", "206CQ", 500000.0, 25000.0)],
    })
    deductors, g_deductors, collectors, fy = m.parse_parts(p)
    assert fy == "2025-26"
    assert [d.name for d in deductors] == ["BANK OF BARODA"]
    assert g_deductors == []
    assert [c.name for c in collectors] == ["THOMAS COOK INDIA LIMITED"]
    assert collectors[0].tax_deducted == 25000.0
    assert collectors[0].sections == ("206CQ",)


def test_parse_parts_accepts_tcs_only_workbook():
    """A 26AS with TCS but no TDS is valid and must not be rejected."""
    p = Path(tempfile.gettempdir()) / "test_26as_tcs_only.xlsx"
    _make_workbook(p, {"Part VI": [(1, "X TOURS", "206CQ", 100.0, 10.0)]})
    deductors, g_deductors, collectors, fy = m.parse_parts(p)
    assert deductors == [] and g_deductors == [] and len(collectors) == 1
    assert fy == "2025-26"


def test_parse_parts_reads_part_ii_15g_15h():
    """A 26AS with only 15G/15H (Part II) deductors is valid — no TDS/TCS
    required — and Part II shares columns 1/2/4/5/6/8 with Part I/VI."""
    p = Path(tempfile.gettempdir()) / "test_26as_part_ii_only.xlsx"
    _make_workbook(p, {
        "Part II": [(1, "BAJAJ FINANCE LIMITED", "194A", 50000.0, 0.0)],
    })
    deductors, g_deductors, collectors, fy = m.parse_parts(p)
    assert deductors == [] and collectors == []
    assert [d.name for d in g_deductors] == ["BAJAJ FINANCE LIMITED"]
    assert g_deductors[0].amount_paid == 50000.0
    assert g_deductors[0].tax_deducted == 0.0
    assert fy == "2025-26"


def test_parse_parts_rejects_workbook_with_neither_part():
    p = Path(tempfile.gettempdir()) / "test_26as_neither.xlsx"
    _make_workbook(p, {"Part VII": []})
    with pytest.raises(ValueError, match="Part I"):
        m.parse_parts(p)


def test_tcs_and_tds_transaction_ids_never_collide():
    """Part I Sr.1 and Part VI Sr.1 are different parties — a shared ID would
    make GnuCash fuse their splits into one unbalanced transaction."""
    journals = m.build_journals([_deductor(1, "BANK OF BARODA", "194A", 1000, 100)],
                                _tcs_accounts())
    journals += m.build_tcs_journals([_collector(1, "X TOURS", "206CQ", 500, 50)],
                                     _tcs_accounts())
    out = Path(tempfile.gettempdir()) / "test_tcs_ids.csv"
    m.write_csv(journals, out, "2025-26")

    txns = {}
    with out.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            txns.setdefault(row["Transaction ID"], 0.0)
            txns[row["Transaction ID"]] += float(row["Amount"])
    assert set(txns) == {"2526-TDSJ01", "2526-TCSJ01"}
    for tid, total in txns.items():
        assert abs(total) < 0.01, f"{tid} does not sum to zero: {total}"


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


# ---------------------------------------------------------------------------
# apply_overrides graceful degradation
#
# A weak tool-calling model sometimes invokes apply_overrides with no arguments.
# When `overrides` is a REQUIRED tool parameter, strict endpoints (e.g. Groq)
# reject the whole request with HTTP 400 `tool_use_failed` before the tool body
# ever runs. These tests pin the fix: `overrides` is optional in the tool schema,
# and a None / empty payload normalizes to a harmless no-op.
# ---------------------------------------------------------------------------

TOOLS = SRC / "agents" / "skill_26as_journal" / "tools.py"


def _load_tools():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("t26as_tools", TOOLS)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


tl = _load_tools()


@pytest.mark.parametrize("value", [None, "", "   ", {}])
def test_normalize_overrides_empty_is_noop(value):
    assert tl._normalize_overrides(value) == {}


def test_normalize_overrides_dict_passthrough_stringifies_keys():
    assert tl._normalize_overrides({2: "Income:Interest Income:Interest on FD"}) == {
        "2": "Income:Interest Income:Interest on FD"}


def test_normalize_overrides_drops_falsy_values():
    assert tl._normalize_overrides({"2": "Income:X", "3": "", "4": None}) == {"2": "Income:X"}


def test_normalize_overrides_parses_json_string():
    assert tl._normalize_overrides('{"2": "Income:X"}') == {"2": "Income:X"}


def test_normalize_overrides_extracts_number_from_label_keys():
    # A tool-calling model often echoes the display label ("Sr 7") instead of
    # the bare number. Normalize to the digits so the subprocess int() succeeds.
    assert tl._normalize_overrides({"Sr 7": "Income:X"}) == {"7": "Income:X"}
    assert tl._normalize_overrides({"sr7": "Income:X", " 2 ": "Income:Y"}) == {
        "7": "Income:X", "2": "Income:Y"}


def test_normalize_overrides_drops_keys_without_a_number():
    # No number in the key -> can't resolve to a deductor Sr, so drop it rather
    # than pass a key that would crash int() in the builder subprocess.
    assert tl._normalize_overrides({"total": "Income:X"}) == {}


def test_normalize_overrides_bad_string_returns_error():
    out = tl._normalize_overrides("not an object")
    assert isinstance(out, str) and out.startswith("ERROR")


def test_run_apply_none_is_noop_and_touches_nothing():
    # Dummy, nonexistent paths: run_apply must short-circuit on the empty
    # override BEFORE it ever reads the workbook or rebuilds the CSV.
    missing = str(Path(tempfile.gettempdir()) / "does-not-exist-26as.csv")
    out = tl.run_apply(missing, missing, missing, None)
    assert out == "No overrides supplied; the existing CSV is unchanged and valid."
    assert not Path(missing).exists()


# ---------------------------------------------------------------------------
# A1 wiring: run_build/run_apply must thread partner_comp_configured through
# to the build_tds_journals.py subprocess as the --partner-comp-configured
# CLI flag (see that script's `main()` / _PARTNER_COMP_FLAG). agent.py's
# run() already resolves partner_comp_configured and passes it into both
# tool closures unconditionally -- if these two functions didn't accept the
# kwarg, EVERY real call to build_journals()/apply_overrides() would raise
# TypeError, not just entities with partner_comp_accounts configured.
#
# Proven to fail on b3a9df6 (pre-fix): run_build/run_apply had no
# partner_comp_configured parameter at all, so both calls below raised
# `TypeError: ...got an unexpected keyword argument 'partner_comp_configured'`.
# ---------------------------------------------------------------------------

def test_run_build_appends_partner_comp_flag_when_configured(monkeypatch):
    captured = {}

    def _fake_run_script(args):
        captured["args"] = args
        return "Done."

    monkeypatch.setattr(tl, "_run_script", _fake_run_script)
    tl.run_build("x.xlsx", "y.gnucash", "z.csv", partner_comp_configured=True)
    assert captured["args"] == ["x.xlsx", "y.gnucash", "z.csv",
                                "--partner-comp-configured"]


def test_run_build_omits_partner_comp_flag_when_not_configured(monkeypatch):
    captured = {}

    def _fake_run_script(args):
        captured["args"] = args
        return "Done."

    monkeypatch.setattr(tl, "_run_script", _fake_run_script)
    tl.run_build("x.xlsx", "y.gnucash", "z.csv")
    assert captured["args"] == ["x.xlsx", "y.gnucash", "z.csv"]
    tl.run_build("x.xlsx", "y.gnucash", "z.csv", partner_comp_configured=False)
    assert captured["args"] == ["x.xlsx", "y.gnucash", "z.csv"]


def test_run_apply_appends_partner_comp_flag_when_configured(monkeypatch, tmp_path):
    captured = {}

    def _fake_run_script(args):
        captured["args"] = args
        return "Done."

    monkeypatch.setattr(tl, "_run_script", _fake_run_script)
    out = str(tmp_path / "out.csv")
    tl.run_apply("x.xlsx", "y.gnucash", out, {"2": "Income:X"},
                 partner_comp_configured=True)
    assert captured["args"][:3] == ["x.xlsx", "y.gnucash", out]
    assert captured["args"][-1] == "--partner-comp-configured"


def test_run_apply_omits_partner_comp_flag_when_not_configured(monkeypatch, tmp_path):
    captured = {}

    def _fake_run_script(args):
        captured["args"] = args
        return "Done."

    monkeypatch.setattr(tl, "_run_script", _fake_run_script)
    out = str(tmp_path / "out.csv")
    tl.run_apply("x.xlsx", "y.gnucash", out, {"2": "Income:X"})
    assert "--partner-comp-configured" not in captured["args"]


def test_apply_overrides_tool_param_is_optional():
    """The real tool schema must NOT list `overrides` as required — that is what
    prevents strict endpoints from 400-ing an argument-less call."""
    from agents.skill_26as_journal.agent import _make_tools

    tools = {t.name: t for t in _make_tools("x.xlsx", "y.gnucash", "z.csv")}
    schema = tools["apply_overrides"].args_schema.model_json_schema()
    assert "overrides" in schema.get("properties", {}), "param must still exist"
    assert "overrides" not in schema.get("required", []), \
        "overrides must be optional so a no-arg tool call is accepted, not 400'd"


# ---------------------------------------------------------------------------
# FL1.1 -- `entity` is now a required input (skill.yaml). A blank entity, or
# one not found in entities.yaml, must fail loud in agent.run() BEFORE any
# CSV is written -- never fall back to silently journalling Category C. An
# entity that IS found but has no partner_comp_accounts configured must
# proceed exactly as before (double-booking warning kept only for that case).
# ---------------------------------------------------------------------------

import yaml as _yaml  # noqa: E402


def _write_entities_yaml_26as(tmp_path, *, key="syn-firm", partner_comp_accounts=None):
    fields = {"name": "Synthetic Firm", "pan": "AAAAA0000A", "status": "Firm"}
    if partner_comp_accounts is not None:
        fields["partner_comp_accounts"] = partner_comp_accounts
    path = tmp_path / "entities.yaml"
    path.write_text(_yaml.safe_dump({key: fields}), encoding="utf-8")
    return path


def test_run_refuses_blank_entity_and_writes_no_csv(tmp_path, monkeypatch):
    from agents.skill_26as_journal import agent as AG

    entities_path = _write_entities_yaml_26as(tmp_path)
    out = tmp_path / "out.csv"

    def _boom(*a, **k):
        raise AssertionError("build_agent must not be called when entity is blank")

    monkeypatch.setattr(AG, "build_agent", _boom)

    result = AG.run(
        xlsx_path="x.xlsx",
        gnucash_path="y.gnucash",
        output_path=str(out),
        entity="",
        entities_path=str(entities_path),
    )
    assert result.startswith("ERROR")
    assert "entity" in result.lower()
    assert not out.exists(), "no CSV may be written when entity resolution fails"


def test_run_refuses_unknown_entity_and_writes_no_csv(tmp_path, monkeypatch):
    from agents.skill_26as_journal import agent as AG

    entities_path = _write_entities_yaml_26as(tmp_path, key="syn-firm")
    out = tmp_path / "out.csv"

    def _boom(*a, **k):
        raise AssertionError("build_agent must not be called when entity is unknown")

    monkeypatch.setattr(AG, "build_agent", _boom)

    result = AG.run(
        xlsx_path="x.xlsx",
        gnucash_path="y.gnucash",
        output_path=str(out),
        entity="does-not-exist",
        entities_path=str(entities_path),
    )
    assert result.startswith("ERROR")
    assert "does-not-exist" in result
    assert not out.exists(), "no CSV may be written when entity resolution fails"


def test_run_proceeds_when_entity_found_without_partner_comp(tmp_path, monkeypatch):
    """An entity that IS found but has no partner_comp_accounts configured must
    proceed exactly as today (no fail-loud refusal) -- only the resolved
    `partner_comp_configured` bool changes, and it must be False here."""
    from agents.skill_26as_journal import agent as AG

    entities_path = _write_entities_yaml_26as(tmp_path, key="syn-firm")
    captured = {}

    class _FakeAgent:
        def invoke(self, _messages):
            return {"messages": [type("M", (), {"content": "ok"})()]}

    def _fake_build_agent(tools, prompt, config_path, model_override):
        captured["tools"] = tools
        return _FakeAgent()

    def _fake_make_tools(xlsx_path, gnucash_path, output_path, partner_comp_configured=False):
        captured["partner_comp_configured"] = partner_comp_configured
        return []

    def _fake_final_summary(output_path, gnucash_path):
        return "Done."

    monkeypatch.setattr(AG, "build_agent", _fake_build_agent)
    monkeypatch.setattr(AG, "_make_tools", _fake_make_tools)
    monkeypatch.setattr(AG.T, "final_summary", _fake_final_summary)

    result = AG.run(
        xlsx_path="x.xlsx",
        gnucash_path="y.gnucash",
        output_path=str(tmp_path / "out.csv"),
        entity="syn-firm",
        entities_path=str(entities_path),
    )
    assert not result.startswith("ERROR")
    assert captured["partner_comp_configured"] is False


def test_skill_yaml_entity_input_is_required():
    import yaml as _y

    skill_yaml = SRC / "agents" / "skill_26as_journal" / "skill.yaml"
    data = _y.safe_load(skill_yaml.read_text(encoding="utf-8"))
    inputs = data["inputs"]
    names = [i["name"] for i in inputs]
    entity_input = next(i for i in inputs if i["name"] == "entity")
    assert entity_input["required"] is True, "entity must be required per FL1.1"
    assert "optional" not in entity_input["label"].lower(), \
        "label must drop the stale (optional) wording now that entity is required"
    # entity must stay out of first place -- it is a "consumed" input (used
    # in run_args), and ui/tabs/_generic.py names the output after whichever
    # consumed input is FIRST in this list. xlsx_path must still lead.
    assert names[0] == "xlsx_path", \
        "entity must not become the first input -- would break output-file naming"


# ---------------------------------------------------------------------------
# FL1.1 amendment (PR #269 hand-back ruling): entities.yaml itself being
# missing, unreadable, or unparsable must ALSO fail loud -- the earlier cut
# of this fix left that case as a silent fallback to
# partner_comp_configured=False, which is exactly the silent fallback the
# double-booking ruling was meant to close: if the file cannot be read, we
# cannot know whether Category C (s.194T) would double-book TDS that
# skill_partner_comp_recon's monthly journal already booked. The ONLY path
# that may still journal Category C is an entity that IS found in
# entities.yaml and has no partner_comp_accounts configured.
# ---------------------------------------------------------------------------

def test_run_refuses_missing_entities_yaml_and_writes_no_csv(tmp_path, monkeypatch):
    from agents.skill_26as_journal import agent as AG

    missing_path = tmp_path / "does-not-exist-entities.yaml"
    out = tmp_path / "out.csv"

    def _boom(*a, **k):
        raise AssertionError("build_agent must not be called when entities.yaml is missing")

    monkeypatch.setattr(AG, "build_agent", _boom)

    result = AG.run(
        xlsx_path="x.xlsx",
        gnucash_path="y.gnucash",
        output_path=str(out),
        entity="syn-firm",
        entities_path=str(missing_path),
    )
    assert result.startswith("ERROR")
    assert str(missing_path) in result or "entities.yaml" in result.lower()
    assert not out.exists(), "no CSV may be written when entities.yaml cannot be read"


def test_run_refuses_unparsable_entities_yaml_and_writes_no_csv(tmp_path, monkeypatch):
    from agents.skill_26as_journal import agent as AG

    entities_path = tmp_path / "entities.yaml"
    # Deliberately malformed YAML (unbalanced flow mapping) -- must raise a
    # YAML parse error inside configs.load_entities(), not resolve to False.
    entities_path.write_text("syn-firm: {name: Synthetic Firm, pan: [unterminated\n",
                              encoding="utf-8")
    out = tmp_path / "out.csv"

    def _boom(*a, **k):
        raise AssertionError("build_agent must not be called when entities.yaml doesn't parse")

    monkeypatch.setattr(AG, "build_agent", _boom)

    result = AG.run(
        xlsx_path="x.xlsx",
        gnucash_path="y.gnucash",
        output_path=str(out),
        entity="syn-firm",
        entities_path=str(entities_path),
    )
    assert result.startswith("ERROR")
    assert not out.exists(), "no CSV may be written when entities.yaml cannot be parsed"


def test_resolve_partner_comp_configured_raises_not_returns_false_on_bad_entities_yaml(tmp_path):
    """Direct unit-level proof (not just through run()): the earlier cut of
    this fix had `except Exception: return False` here -- this pins the
    fixed contract, a raised EntityResolutionError, so a future change
    cannot silently reintroduce that fallback."""
    from agents.skill_26as_journal.agent import EntityResolutionError, _resolve_partner_comp_configured

    missing_path = tmp_path / "nope.yaml"
    try:
        _resolve_partner_comp_configured("syn-firm", str(missing_path))
        assert False, "must raise EntityResolutionError, not return False"
    except EntityResolutionError:
        pass
