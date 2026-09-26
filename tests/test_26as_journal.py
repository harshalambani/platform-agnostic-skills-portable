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


def _deductor(sr, name, section, amt, tax, tan=""):
    return m.Deductor(sr=sr, name=name, sections=(section,),
                      amount_paid=amt, tax_deducted=tax, tds_deposited=tax,
                      tan=tan)


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
    """The review CSV has "Tied Candidates" then "TAN" as its last two
    columns (TDS-11 added TAN after Tied Candidates); every earlier column
    keeps its original position."""
    d = _deductor(1, "ZENITH LIMITED", "194A", 100000.0, 10000.0, tan="AAAA00000A")
    journals = m.build_journals([d], _tie_accounts())
    out = Path(tempfile.gettempdir()) / "test_tds06_review.csv"
    m.write_review(journals, out, _tie_accounts())
    with out.open(newline="", encoding="utf-8") as f:
        rows = list(csv.reader(f))
    header = rows[0]
    assert header == ["Sr", "Deductor", "Section", "Category", "Credit Account",
                      "Confidence", "Account Exists", "Balanced", "Debit", "Credit",
                      "Needs Review", "Basis", "Tied Candidates", "TAN"]
    data_row = rows[1]
    assert data_row[-1] == "AAAA00000A"
    assert data_row[-2] == (
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
# TDS-... zero-amount rows must never reach either CSV (item 3.5): a split
# whose signed Amount rounds to 0.00 never changes whether the transaction
# balances, and importing a Rs 0.00 row is pure line noise some GnuCash
# importer builds even warn/reject on.
# ---------------------------------------------------------------------------

def test_build_csv_rows_drops_zero_amount_tds_split():
    """Category A with tax_deducted == 0 must not emit a Rs 0.00 TDS-account
    row -- the two nonzero legs (Dr generic FD interest, Cr the specific
    NBFC account) still fully represent and balance the transaction."""
    d = _deductor(4, "BANK OF BARODA", "194A", 50000, 0)
    j = m.build_journals([d], _accounts())[0]
    assert j.category == "A" and len(j.splits) == 3  # Split objects unchanged
    rows = m.build_csv_rows([j], "2025-26")
    amounts = [float(r["Amount"]) for r in rows]
    assert 0.0 not in amounts, f"a zero-amount row leaked into the CSV rows: {rows}"
    assert len(rows) == 2, "only the two nonzero splits should reach the CSV"
    assert abs(sum(amounts)) < 0.01, "remaining rows must still balance to zero"


def test_build_csv_rows_drops_whole_transaction_when_all_splits_are_zero():
    """A TCS row with tax == 0 has both splits at 0.00 -- the whole
    (no-op) transaction must vanish from the CSV, not appear as two Rs 0.00
    rows under a live Transaction ID."""
    c = _collector(1, "X TOURS", "206CQ", 100, 0)
    j = m.build_tcs_journals([c], _tcs_accounts())[0]
    rows = m.build_csv_rows([j], "2025-26")
    assert rows == []


def test_write_csv_zero_amount_row_fails_on_pre_fix_build_csv_rows():
    """Negative-test anchor: proves the zero-amount split really did reach
    write_csv()'s output before the fix (guards against a future change
    that reintroduces zero rows by writing straight from j.splits again
    instead of through the filtered build_csv_rows())."""
    d = _deductor(4, "BANK OF BARODA", "194A", 50000, 0)
    j = m.build_journals([d], _accounts())[0]
    out = Path(tempfile.gettempdir()) / "test_zero_amount_row.csv"
    m.write_csv([j], out, "2025-26")
    with out.open(newline="", encoding="utf-8") as f:
        amounts = [float(row["Amount"]) for row in csv.DictReader(f)]
    assert 0.0 not in amounts, f"a zero-amount row leaked into write_csv() output: {amounts}"


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
    # FL1.2: an accepted LLM override must still need human confirmation --
    # it is a model pick, not a verified match, until the Review tab clears
    # it. This is a deliberate behavior change from the pre-fix "not
    # j.needs_review" (see test_override_stays_needs_review_fl1_2 below for
    # the proof this changed).
    assert j.credit_confidence == "Override" and j.needs_review
    assert j.credit_basis == "Model pick - confirm"
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


def _collector(sr, name, section, amt, tax, tan=""):
    return m.Deductor(sr=sr, name=name, sections=(section,),
                      amount_paid=amt, tax_deducted=tax, tds_deposited=tax,
                      tan=tan)


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
    # FL1.2: same as test_override_wins -- a TCS override is still a model
    # pick, so it must keep needs_review True until a human confirms it.
    assert j.credit_confidence == "Override" and j.needs_review
    assert j.credit_basis == "Model pick - confirm"


# ---------------------------------------------------------------------------
# FL1.2 -- an accepted LLM/model override must never skip human review.
# Before this fix, all three builders forced needs_review=False the moment
# an override was accepted, so a model pick on a Suspense row -- or on an
# Ambiguous row with a tied candidate the gate (_gate_ambiguous_overrides in
# tools.py) let through -- silently became "reviewed" with no human ever
# looking at it. Only a human save from the Review tab
# (ui/tabs/tds_journal_review.py's _apply_changes) may now clear the flag.
#
# Proven to fail pre-fix (tip bbbe369, rebased onto 2245b3b): each assertion
# below on `j.needs_review is True` for an override fails with
# `assert False is True`, since the pre-fix code set needs_review = False
# (build_journals/build_15g_journals explicitly; build_tcs_journals via the
# Journal dataclass's needs_review=False default, since it never touched
# the field on the override branch at all).
# ---------------------------------------------------------------------------

def test_override_on_suspense_row_stays_needs_review_fl1_2():
    """A deductor that would otherwise land on Suspense (unmatched name) but
    gets an accepted LLM override must still be flagged needs_review -- the
    override is a model pick, not a human-verified match."""
    d = _deductor(1, "SOME OBSCURE PAYER", "194A", 10000.0, 1000.0)
    accts = [
        m.Account("Income:Interest Income", "Interest Income", "INCOME", special=True),
        m.Account("Expense:TDS on Interest", "TDS on Interest", "EXPENSE"),
        m.Account("Liabilities:Suspense", "Suspense", "LIABILITY"),
    ]
    # Without an override this deductor goes to Suspense (see
    # test_build_journals_places_placeholder_only_deductor_on_suspense).
    j = m.build_journals([d], accts, overrides={1: "Liabilities:Suspense"})[0]
    assert j.credit_confidence == "Override"
    assert j.needs_review is True
    assert j.credit_basis == "Model pick - confirm"


def test_override_on_ambiguous_tied_candidate_stays_needs_review_fl1_2():
    """An Ambiguous row with a tied candidate, accepted as an override (the
    only kind _gate_ambiguous_overrides lets through), must still be flagged
    needs_review -- it is a confirmed-as-plausible model pick, not a
    human-confirmed one."""
    d = _deductor(1, "ZENITH LIMITED", "194A", 100000.0, 10000.0)
    accts = _tie_accounts()
    tied_candidate = "Income:Interest Income:Interest on Zenith Global"
    j = m.build_journals([d], accts, overrides={1: tied_candidate})[0]
    assert j.credit_account == tied_candidate
    assert j.credit_confidence == "Override"
    assert j.needs_review is True
    assert j.credit_basis == "Model pick - confirm"


def test_15g_override_stays_needs_review_fl1_2():
    """Same FL1.2 guarantee for the Part II (15G/15H) builder."""
    d = _deductor(1, "ZENITH LIMITED", "194A", 100000.0, 0.0)
    j = m.build_15g_journals([d], _tie_accounts(),
                             overrides={1: "Income:Interest Income:Interest on Zenith Bank"})[0]
    assert j.credit_confidence == "Override"
    assert j.needs_review is True
    assert j.credit_basis == "Model pick - confirm"


# ---------------------------------------------------------------------------
# TDS-11 -- persisted human confirmations ("learnings") for the 26AS Review
# tab, mirroring skill_gnucash_account_mapper.persistent_rules in spirit:
# only a genuinely human Review-tab save may create a learning (see FL1.2 --
# an accepted LLM override must NOT become a learning); a stored learning is
# applied automatically on a later run (confidence "Learned", needs_review
# cleared, since it represents a PRIOR human confirmation, not a fresh
# guess); an explicit per-run override still wins over a stored learning;
# learnings are keyed by TAN (falling back to normalised name) and
# namespaced by domain (DOMAIN_INCOME for Categories A/B/C/G, DOMAIN_TCS for
# Category T) so the same TAN acting as both a deductor and a collector
# can't cross-contaminate; and a learning can never bypass the s.194T
# partner-comp exclusion in Category C, since it feeds the same
# credit_acc/credit_confidence/credit_basis/needs_review variables the
# deterministic matcher and override paths already feed, with the exclusion
# logic running unconditionally afterwards.
#
# Proven to fail pre-fix (tip 3b346f6, before TDS-11): build_journals() /
# build_15g_journals() / build_tcs_journals() had no `learnings` parameter
# at all, so every test below that passes `learnings=...` fails with
# `TypeError: build_journals() got an unexpected keyword argument
# 'learnings'` (or the m.tds_learnings module simply doesn't exist yet,
# `AttributeError: module 'build_tds_journals' has no attribute
# 'tds_learnings'`).
# ---------------------------------------------------------------------------

def test_learning_applied_when_no_override_income_domain():
    """A stored learning (prior human confirmation) is applied when this run
    has no explicit override for the same deductor -- confidence "Learned",
    needs_review cleared."""
    d = _deductor(1, "SOME OBSCURE PAYER", "194A", 10000.0, 1000.0, tan="AAAA00000A")
    accts = [
        m.Account("Income:Interest Income", "Interest Income", "INCOME", special=True),
        m.Account("Expense:TDS on Interest", "TDS on Interest", "EXPENSE"),
        m.Account("Liabilities:Suspense", "Suspense", "LIABILITY"),
    ]
    learn_key = m.tds_learnings.deductor_key(
        m.tds_learnings.DOMAIN_INCOME, "AAAA00000A", "SOME OBSCURE PAYER")
    j = m.build_journals([d], accts,
                         learnings={learn_key: "Income:Interest Income"})[0]
    assert j.credit_account == "Income:Interest Income"
    assert j.credit_confidence == "Learned"
    assert j.needs_review is False


def test_override_wins_over_learning():
    """An explicit per-run override beats a matching stored learning -- this
    run's deliberate choice outranks a prior confirmation."""
    d = _deductor(1, "ZENITH LIMITED", "194A", 100000.0, 10000.0, tan="AAAA00000A")
    accts = _tie_accounts()
    learn_key = m.tds_learnings.deductor_key(
        m.tds_learnings.DOMAIN_INCOME, "AAAA00000A", "ZENITH LIMITED")
    j = m.build_journals(
        [d], accts,
        overrides={1: "Income:Interest Income:Interest on Zenith Global"},
        learnings={learn_key: "Income:Interest Income:Interest on Zenith Bank"},
    )[0]
    assert j.credit_account == "Income:Interest Income:Interest on Zenith Global"
    assert j.credit_confidence == "Override"
    # FL1.2: an override is still a model pick pending human confirmation on
    # this base -- it does not clear needs_review just for beating a learning.
    assert j.needs_review is True


def test_learning_keyed_by_tan_ignores_name_change():
    """Same TAN, different name text -- the learning still matches, proving
    the key is TAN-based, not name-based."""
    d = _deductor(1, "ZENITH LIMITED (RENAMED)", "194A", 100000.0, 10000.0,
                  tan="AAAA00000A")
    accts = _tie_accounts()
    learn_key = m.tds_learnings.deductor_key(
        m.tds_learnings.DOMAIN_INCOME, "AAAA00000A", "ZENITH LIMITED")
    j = m.build_journals(
        [d], accts,
        learnings={learn_key: "Income:Interest Income:Interest on Zenith Bank"},
    )[0]
    assert j.credit_account == "Income:Interest Income:Interest on Zenith Bank"
    assert j.credit_confidence == "Learned"
    assert j.needs_review is False


def test_learning_falls_back_to_name_when_no_tan():
    """A blank/invalid TAN falls back to a name-keyed learning."""
    d = _deductor(1, "SOME OBSCURE PAYER", "194A", 10000.0, 1000.0, tan="")
    accts = [
        m.Account("Income:Interest Income", "Interest Income", "INCOME", special=True),
        m.Account("Expense:TDS on Interest", "TDS on Interest", "EXPENSE"),
        m.Account("Liabilities:Suspense", "Suspense", "LIABILITY"),
    ]
    learn_key = m.tds_learnings.deductor_key(
        m.tds_learnings.DOMAIN_INCOME, "", "SOME OBSCURE PAYER")
    j = m.build_journals([d], accts,
                         learnings={learn_key: "Income:Interest Income"})[0]
    assert j.credit_account == "Income:Interest Income"
    assert j.credit_confidence == "Learned"
    assert j.needs_review is False


def test_category_c_learning_does_not_bypass_partner_comp_exclusion():
    """Category C with a learning hit AND partner_comp_configured=True still
    gets excluded_from_journal=True, needs_review False, and the exclusion
    basis -- the s.194T exclusion must win over the "Learned from..." text,
    exactly as it already wins over the deterministic matcher and override
    paths."""
    d = _deductor(1, "ZENITH LIMITED", "194T", 100000.0, 10000.0, tan="AAAA00000A")
    accts = _tie_accounts()
    learn_key = m.tds_learnings.deductor_key(
        m.tds_learnings.DOMAIN_INCOME, "AAAA00000A", "ZENITH LIMITED")
    j = m.build_journals(
        [d], accts,
        learnings={learn_key: "Income:Interest Income:Interest on Zenith Bank"},
        partner_comp_configured=True,
    )[0]
    assert j.excluded_from_journal is True
    assert j.needs_review is False
    assert "Learned from" not in j.credit_basis


def test_tcs_learning_uses_separate_domain_from_income():
    """A DOMAIN_INCOME-keyed learning for a given TAN must NOT be picked up
    by build_tcs_journals() for a collector with the same TAN -- proves
    domain isolation between the deductor (income) and collector (TCS)
    roles a single TAN can hold."""
    c = _collector(1, "X TOURS", "206CQ", 100, 10, tan="AAAA00000A")
    income_key = m.tds_learnings.deductor_key(
        m.tds_learnings.DOMAIN_INCOME, "AAAA00000A", "X TOURS")
    j = m.build_tcs_journals(
        [c], _tcs_accounts(),
        learnings={income_key: "Expense:TCS on Foreign Trip"},
    )[0]
    assert j.credit_confidence != "Learned"


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


# ---------------------------------------------------------------------------
# FJ1.4 -- code gate on LLM overrides of Ambiguous rows.
#
# Proven to fail on b3a9df6 (pre-fix): tl._gate_ambiguous_overrides did not
# exist at all on that build, so every test in this block fails with
# AttributeError on b3a9df6, and (at the run_apply level)
# test_run_apply_rejects_non_tied_ambiguous_override_before_subprocess would
# have called the real subprocess with the bad override instead of
# short-circuiting.
# ---------------------------------------------------------------------------

def _write_review_csv(path: Path, rows: list[dict]) -> None:
    fieldnames = ["Sr", "Deductor", "Section", "Category", "Credit Account",
                 "Confidence", "Account Exists", "Balanced", "Debit", "Credit",
                 "Needs Review", "Basis", "Tied Candidates"]
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def _ambiguous_review_row(sr="2", deductor="ACME BANK", tied=("Income:A", "Income:B")):
    return {"Sr": sr, "Deductor": deductor, "Section": "194A", "Category": "A",
            "Credit Account": "Income:A", "Confidence": "Ambiguous",
            "Needs Review": "no", "Basis": "tied on score",
            "Tied Candidates": "; ".join(tied)}


def test_gate_ambiguous_overrides_rejects_non_tied_account(tmp_path):
    out = tmp_path / "out.csv"
    _write_review_csv(out.with_name("out-review.csv"), [_ambiguous_review_row()])
    result = tl._gate_ambiguous_overrides({"2": "Income:NotTied"}, str(out))
    assert isinstance(result, str)
    assert "REJECTED" in result
    assert "Sr 2" in result and "ACME BANK" in result


def test_gate_ambiguous_overrides_accepts_tied_candidate(tmp_path):
    out = tmp_path / "out.csv"
    _write_review_csv(out.with_name("out-review.csv"), [_ambiguous_review_row()])
    result = tl._gate_ambiguous_overrides({"2": "Income:A"}, str(out))
    accepted, rejections = result
    assert accepted == {"2": "Income:A"}
    assert rejections == []


def test_gate_ambiguous_overrides_no_review_file_passthrough(tmp_path):
    out = tmp_path / "out.csv"  # no -review.csv sibling written
    result = tl._gate_ambiguous_overrides({"5": "Income:Anything"}, str(out))
    accepted, rejections = result
    assert accepted == {"5": "Income:Anything"}
    assert rejections == []


def test_gate_ambiguous_overrides_non_ambiguous_row_unaffected(tmp_path):
    out = tmp_path / "out.csv"
    row = _ambiguous_review_row(sr="7")
    row["Confidence"] = "Suspense"  # not Ambiguous -- gate must not apply
    _write_review_csv(out.with_name("out-review.csv"), [row])
    result = tl._gate_ambiguous_overrides({"7": "Income:Whatever:NotInTiedList"}, str(out))
    accepted, rejections = result
    assert accepted == {"7": "Income:Whatever:NotInTiedList"}
    assert rejections == []


def test_run_apply_rejects_non_tied_ambiguous_override_before_subprocess(tmp_path, monkeypatch):
    """Integration-level: run_apply must short-circuit BEFORE invoking the
    builder subprocess when every override is rejected, and must leave the
    existing output CSV byte-for-byte unchanged."""
    out = tmp_path / "out.csv"
    out.write_text("Date,Transaction ID\n2025-01-01,X\n", encoding="utf-8")
    before = out.read_bytes()
    _write_review_csv(out.with_name("out-review.csv"), [_ambiguous_review_row()])

    def _boom(args):
        raise AssertionError("subprocess must not run when all overrides are rejected")
    monkeypatch.setattr(tl, "_run_script", _boom)

    result = tl.run_apply("x.xlsx", "y.gnucash", str(out), {"2": "Income:NotTied"})
    assert "REJECTED" in result
    assert out.read_bytes() == before


def test_run_apply_accepts_tied_candidate_override_and_invokes_subprocess(tmp_path, monkeypatch):
    out = tmp_path / "out.csv"
    out.write_text("Date,Transaction ID\n2025-01-01,X\n", encoding="utf-8")
    _write_review_csv(out.with_name("out-review.csv"), [_ambiguous_review_row()])

    calls = []

    def _fake_run_script(args):
        calls.append(args)
        return "Done."

    monkeypatch.setattr(tl, "_run_script", _fake_run_script)
    monkeypatch.setattr(tl, "_verify", lambda p: "VERIFIED — 1 transactions, all balanced.")

    result = tl.run_apply("x.xlsx", "y.gnucash", str(out), {"2": "Income:A"})
    assert len(calls) == 1
    assert "VERIFIED" in result
    assert "REJECTED" not in result



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

    def _fake_make_tools(xlsx_path, gnucash_path, output_path,
                         partner_comp_configured=False, tds_expense_account=""):
        captured["partner_comp_configured"] = partner_comp_configured
        return []

    def _fake_final_summary(output_path, gnucash_path, xlsx_path="",
                            tds_expense_account=""):
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


# ---------------------------------------------------------------------------
# TDS-13: read-only s.194T reconciliation (26AS vs Partner Comp journal)
# ---------------------------------------------------------------------------

def _write_194t_workbook(path, rows, fy="2025-26"):
    """Write a Part I sheet with real per-TRANSACTION rows: columns 1/2/4/5/6/8
    as _party_sheet (above) does, PLUS column 9 (Transaction Date,
    "DD-Mon-YYYY") and column 14 (that transaction's own Tax Deducted) --
    which parse_194t_monthly reads directly and _party_sheet never populates
    (it only carries the FY sub-totals, not per-transaction dates). `rows` is
    a list of (sr, name, section, txn_date_str, txn_tax) tuples; columns
    4/5/6 are set to the same per-row tax figure since these tests never
    look at the FY sub-totals."""
    from openpyxl import Workbook
    wb = Workbook()
    wb.remove(wb.active)
    ws = wb.create_sheet(title="Part I")
    ws.cell(1, 1, "Part I - Details")
    ws.cell(2, 1, f"Assessee Name: X  |  PAN: AAAAA1111A  |  Financial Year: {fy}")
    ws.cell(3, 1, "Sr.No.")
    r = 4
    for sr, name, section, txn_date, txn_tax in rows:
        ws.cell(r, 1, sr)
        ws.cell(r, 2, name)
        ws.cell(r, 4, txn_tax)
        ws.cell(r, 5, txn_tax)
        ws.cell(r, 6, txn_tax)
        ws.cell(r, 8, section)
        ws.cell(r, 9, txn_date)
        ws.cell(r, 14, txn_tax)
        r += 1
    wb.save(path)
    return path


def test_parse_194t_monthly_sums_two_rows_same_month():
    """26AS can carry two rows in one month (e.g. an interest-on-capital row
    and a remuneration row) -- brief 1.2 -- these must sum per month."""
    p = Path(tempfile.gettempdir()) / "test_194t_two_rows.xlsx"
    _write_194t_workbook(p, [
        (1, "PARTNERSHIP FIRM", "194T", "12-Jun-2025", 500.0),
        (1, "PARTNERSHIP FIRM", "194T", "20-Jun-2025", 700.0),
    ])
    assert m.parse_194t_monthly(p) == {"2025-06": 1200.0}


def test_parse_194t_monthly_ignores_non_194t_sections():
    p = Path(tempfile.gettempdir()) / "test_194t_ignores_other.xlsx"
    _write_194t_workbook(p, [(1, "BANK OF BARODA", "194A", "12-Jun-2025", 100.0)])
    assert m.parse_194t_monthly(p) == {}


def _reco(p, months_book, fy="2025-26",
         account="Expense:xBusiness Expense:TDS on Partner Comp"):
    """reconcile_s194t against a stubbed monthly_debits_for_account -- this
    tests reconcile_s194t's classification logic in isolation, without
    hand-building a synthetic .gnucash XML fixture (monthly_debits_for_account
    itself is plain read-only XML parsing, exercised directly by its own
    docstring/adaptation from reconcile_intercompany.py; what TDS-13 needs
    pinned here is reconcile_s194t's OPEN/MATCH/PARTIAL/VARIANCE decision
    logic given a book-side result)."""
    orig = m.monthly_debits_for_account
    m.monthly_debits_for_account = lambda *a, **k: months_book
    try:
        return m.reconcile_s194t(p, Path("dummy.gnucash"), account, fy=fy)
    finally:
        m.monthly_debits_for_account = orig


def test_reconcile_s194t_nothing_posted_is_open_and_loud():
    """(a) Nothing posted is never MATCH, and it is loud."""
    p = Path(tempfile.gettempdir()) / "test_194t_reco_open.xlsx"
    _write_194t_workbook(p, [(1, "PARTNERSHIP FIRM", "194T", "12-Jun-2025", 1000.0)])
    reco = _reco(p, {})
    assert reco is not None
    assert reco.status == "OPEN"
    assert reco.status != "MATCH"
    assert reco.loud is True
    assert "partner recon" in reco.message


def test_reconcile_s194t_variance_over_re1_is_loud():
    """(b) A variance over Re 1 is loud."""
    p = Path(tempfile.gettempdir()) / "test_194t_reco_variance.xlsx"
    _write_194t_workbook(p, [(1, "PARTNERSHIP FIRM", "194T", "12-Jun-2025", 1000.0)])
    reco = _reco(p, {"2025-06": 998.0})  # diff 2.00, over the Re 1 tolerance
    assert reco.status == "VARIANCE"
    assert reco.loud is True
    assert "2025-06" in reco.message


def test_reconcile_s194t_within_re1_is_match_and_not_loud():
    """(b) Re 1 or less is not loud."""
    p = Path(tempfile.gettempdir()) / "test_194t_reco_within_tolerance.xlsx"
    _write_194t_workbook(p, [(1, "PARTNERSHIP FIRM", "194T", "12-Jun-2025", 1000.0)])
    reco = _reco(p, {"2025-06": 999.50})  # diff 0.50, at the Re 1 tolerance
    assert reco.status == "MATCH"
    assert reco.loud is False


def test_reconcile_s194t_two_26as_rows_reconcile_against_one_posting():
    """(d) Two 26AS rows in one month reconcile against one combined monthly
    posting -- the partner journal books a single monthly transaction, so the
    sum of 26AS's interest-on-capital + remuneration rows for that month must
    match it, not either row alone."""
    p = Path(tempfile.gettempdir()) / "test_194t_reco_two_rows.xlsx"
    _write_194t_workbook(p, [
        (1, "PARTNERSHIP FIRM", "194T", "05-Jun-2025", 500.0),
        (1, "PARTNERSHIP FIRM", "194T", "25-Jun-2025", 700.0),
    ])
    reco = _reco(p, {"2025-06": 1200.0})
    assert reco.months_26as == {"2025-06": 1200.0}
    assert reco.status == "MATCH"


def test_reconcile_s194t_partial_names_missing_months():
    p = Path(tempfile.gettempdir()) / "test_194t_reco_partial.xlsx"
    _write_194t_workbook(p, [
        (1, "PARTNERSHIP FIRM", "194T", "05-Jun-2025", 500.0),
        (1, "PARTNERSHIP FIRM", "194T", "05-Jul-2025", 600.0),
    ])
    reco = _reco(p, {"2025-06": 500.0})  # July never posted
    assert reco.status == "PARTIAL"
    assert reco.loud is True
    assert "2025-07" in reco.message


def test_reconcile_s194t_returns_none_when_26as_has_no_194t():
    """26AS with no s.194T amounts at all -- nothing to reconcile, and this
    must be distinguishable from OPEN (which means 194T IS present but
    nothing is posted for it yet)."""
    p = Path(tempfile.gettempdir()) / "test_194t_reco_none.xlsx"
    _write_194t_workbook(p, [(1, "BANK OF BARODA", "194A", "12-Jun-2025", 100.0)])
    assert _reco(p, {}) is None


def test_194t_never_reaches_either_csv_regardless_of_reco_state():
    """(c) No s.194T line reaches either importable CSV, in any reco state --
    the exclusion (excluded_from_journal, driven only by
    partner_comp_configured) is computed entirely independently of the
    reconciliation's OPEN/MATCH/PARTIAL/VARIANCE outcome (tools.final_summary
    computes the reco separately, after the CSV is already written), so the
    CSV content is identical no matter what the reco says."""
    d = _deductor(8, "ACME CONSULTING LLP", "194T", 3656276, 365628)
    journals = m.build_journals([d], _accounts(), partner_comp_configured=True)
    j = journals[0]
    assert j.excluded_from_journal is True
    rows = m.build_csv_rows(journals, "2025-26")
    assert rows == []  # never reaches the importable CSV...

    for fake_status in ("OPEN", "MATCH", "PARTIAL", "VARIANCE"):
        # ...no matter what a hypothetical reco result for this run would be.
        fake_reco = m.S194TReco(applicable=True, status=fake_status, message="x")
        assert m.build_csv_rows(journals, "2025-26") == rows
        del fake_reco  # constructed only to prove it plays no part above


def test_no_partner_comp_accounts_entity_shows_no_reco_item():
    """(e) An entity without partner_comp_accounts behaves as today (26AS
    books s.194T itself, with the existing double-booking warning) and shows
    NO reco item at all. tds_expense_account is "" for such an entity
    (agent.py only ever resolves it when partner_comp_configured is True --
    see _resolve_partner_comp_tds_expense_account's caller in agent.run()),
    and final_summary must never append a reco line when it is empty, even
    when the workbook DOES carry s.194T amounts."""
    d = _deductor(8, "ACME CONSULTING LLP", "194T", 3656276, 365628)
    journals = m.build_journals([d], _accounts())  # partner_comp_configured defaults False
    j = journals[0]
    assert j.excluded_from_journal is False  # booked as today, not excluded
    assert "WARNING" in j.credit_basis and "double-book" in j.credit_basis
    rows = m.build_csv_rows(journals, "2025-26")
    assert len(rows) == 2  # both splits still emitted, exactly as before

    out = Path(tempfile.gettempdir()) / "test_194t_no_partner_comp.csv"
    m.write_csv(journals, out, "2025-26")

    xlsx = Path(tempfile.gettempdir()) / "test_194t_no_partner_comp.xlsx"
    _write_194t_workbook(xlsx, [(1, "ACME CONSULTING LLP", "194T", "12-Jun-2025", 365628.0)])

    summary = tl.final_summary(str(out), "", str(xlsx), "")  # tds_expense_account=""
    assert "s.194T reconciliation" not in summary
