"""
tests/skill_gnucash_account_mapper/test_mapper_period_terminated_accounts.py --
regression guard for a defect introduced by MAP-03 (PR #267, commit 79505fb):
_normalize_llm_answer peeled a trailing "." unconditionally, and
_validate_llm_answer normalised BEFORE the Tier 0 exact-match check.

GnuCash account names can legitimately end with a period -- the common case
is a company name, e.g. a dividend account "Income:Dividend:Acme Industries
Ltd.". On 574f384 (pre-MAP-03) an exact answer "Income:Dividend:Acme
Industries Ltd." resolved to itself. On 79505fb it became
"...Acme Industries Ltd" (period stripped before Tier 0 ever ran): Tier 0
missed, the tail tiers missed too (no account ends in a bare, unpunctuated
"Ltd"), and the result was None. Worse -- if both "X Ltd" and "X Ltd." exist
as accounts, the answer would resolve to the WRONG one.

Fix: _normalize_llm_answer(raw, strip_trailing_period) takes an explicit
flag. _validate_llm_answer now runs two passes: Pass 1 with the period
preserved (so a genuinely period-terminated account name is seen intact by
Tier 0/1/2); Pass 2, only if Pass 1 found nothing AND the two normalised
forms differ, with the period stripped (so an ordinary sentence-final
period, e.g. "Expenses:Food and Dining.", is still peeled). Pass 1 being
ambiguous (more than one candidate at any tier) ends the whole call as
None immediately -- Pass 2 never gets a chance to "rescue" an ambiguous
Pass 1 into a specific answer.

All account names/descriptions below are synthetic, invented for this test
file.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402


# ---------------------------------------------------------------------------
# Negative: an exact, genuinely period-terminated account name must resolve
# to itself, not to a period-stripped rewrite of itself.
# ---------------------------------------------------------------------------

def test_period_terminated_account_exact_match_not_rewritten():
    account_set = {"Income:Dividend:Acme Industries Ltd."}
    result = agent._validate_llm_answer(
        "Income:Dividend:Acme Industries Ltd.", account_set
    )
    assert result == "Income:Dividend:Acme Industries Ltd."
    assert result != "Income:Dividend:Acme Industries Ltd"  # the bug's rewrite


# ---------------------------------------------------------------------------
# Negative: dotted and undotted sibling accounts must each resolve to
# themselves, never to each other and never to None.
# ---------------------------------------------------------------------------

def test_dotted_and_undotted_sibling_accounts_resolve_correctly():
    account_set = {
        "Income:Dividend:Acme Ltd",
        "Income:Dividend:Acme Ltd.",
    }

    dotted = agent._validate_llm_answer("Income:Dividend:Acme Ltd.", account_set)
    undotted = agent._validate_llm_answer("Income:Dividend:Acme Ltd", account_set)

    assert dotted == "Income:Dividend:Acme Ltd."
    assert undotted == "Income:Dividend:Acme Ltd"
    assert dotted is not None
    assert undotted is not None
    assert dotted != undotted


# ---------------------------------------------------------------------------
# Negative: a backticked/quoted period-terminated answer still resolves to
# the dotted account -- wrapper-stripping must not consume the period too.
# ---------------------------------------------------------------------------

def test_backticked_period_terminated_account_resolves():
    account_set = {"Income:Dividend:Acme Industries Ltd."}
    assert agent._validate_llm_answer(
        "`Income:Dividend:Acme Industries Ltd.`", account_set
    ) == "Income:Dividend:Acme Industries Ltd."


def test_quoted_period_terminated_account_resolves():
    account_set = {"Income:Dividend:Acme Industries Ltd."}
    assert agent._validate_llm_answer(
        '"Income:Dividend:Acme Industries Ltd."', account_set
    ) == "Income:Dividend:Acme Industries Ltd."


# ---------------------------------------------------------------------------
# Positive: the ordinary sentence-final period case (no real period-bearing
# account involved) must still be peeled -- Pass 2 exists for exactly this.
# ---------------------------------------------------------------------------

def test_sentence_final_period_still_stripped():
    account_set = {"Expenses:Food and Dining"}
    assert agent._validate_llm_answer(
        "Expenses:Food and Dining.", account_set
    ) == "Expenses:Food and Dining"


def test_bold_wrapped_trailing_period_still_resolves():
    account_set = {"Expenses:Food and Dining"}
    assert agent._validate_llm_answer(
        "**Expenses:Food and Dining**.", account_set
    ) == "Expenses:Food and Dining"


# ---------------------------------------------------------------------------
# Negative: Pass 2 must never rescue an ambiguous Pass 1. Constructed so
# that Pass 1 (period preserved) is genuinely ambiguous (two accounts both
# legitimately end in ":X Ltd."), while Pass 2 (period stripped), if it
# were mistakenly tried, WOULD uniquely resolve to a third, unrelated
# account ending in the bare "X Ltd" (no period) -- proving the None result
# is Pass 1's ambiguity verdict standing, not a missed opportunity.
# ---------------------------------------------------------------------------

def test_pass_two_never_rescues_an_ambiguous_pass_one():
    account_set = {
        "Expenses:Alpha:X Ltd.",
        "Expenses:Beta:X Ltd.",
        "Expenses:Gamma:X Ltd",
    }
    # Sanity check: if Pass 1's period-preserved ambiguity were bypassed,
    # the period-stripped form alone WOULD uniquely resolve to Gamma.
    assert agent._validate_llm_answer("X Ltd", account_set) == "Expenses:Gamma:X Ltd"

    # The actual (period-terminated) answer must return None: Pass 1 is
    # ambiguous between Alpha and Beta, and that verdict must stand.
    assert agent._validate_llm_answer("X Ltd.", account_set) is None
