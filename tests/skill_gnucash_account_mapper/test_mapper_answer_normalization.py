"""
tests/skill_gnucash_account_mapper/test_mapper_answer_normalization.py --
regression guard for MAP-03: the model's raw answer wasn't normalised before
validation/SKIP-checking.

Background (src/agents/skill_gnucash_account_mapper/agent.py):

  _validate_llm_answer only stripped three literal prefixes ("Account:",
  "->", "account:") and matched case-sensitively. Small/local models
  routinely wrap an account path in markdown, quote it, label it
  differently, add a trailing period, or space out the ':' -- all of which
  were previously rejected outright even though the answer was otherwise
  correct. The SKIP check (`answer.upper() == "SKIP"`) had the same
  problem: "`SKIP`", "SKIP.", '"skip"' etc. were never recognised as SKIP.

  Fixed with _normalize_llm_answer(), applied before both the SKIP check
  and _validate_llm_answer's matching, plus a case-insensitive matching
  tier in _validate_llm_answer that only activates when the case-sensitive
  tier found zero candidates (MAP-01's ambiguity policy applies there too).
  Normalisation strips wrapping only -- it must never turn a bare substring
  into a match.

All account names/descriptions below are synthetic, invented for this test
file.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402


ACCOUNT_SET = {
    "Expenses:Food and Dining",
    "Expenses:Travel:Airfare",
    "Income:Salary",
}


# ---------------------------------------------------------------------------
# Positive: each wrapper form still resolves
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wrapped", [
    '"Expenses:Food and Dining"',
    "'Expenses:Food and Dining'",
    "“Expenses:Food and Dining”",   # curly double quotes
    "`Expenses:Food and Dining`",             # single backtick
    "```Expenses:Food and Dining```",         # triple backtick
    "**Expenses:Food and Dining**",           # markdown bold
    "Expenses:Food and Dining.",              # trailing period
    "Root Account: Expenses:Food and Dining",
    "Answer: Expenses:Food and Dining",
    "Account: Expenses:Food and Dining",
    "-> Expenses:Food and Dining",
    "Expenses : Food and Dining",             # spaced colon
    "  Expenses:Food and Dining  ",           # surrounding whitespace
])
def test_wrapped_answer_resolves(wrapped):
    assert agent._validate_llm_answer(wrapped, ACCOUNT_SET) == "Expenses:Food and Dining"


def test_nested_wrapping_resolves():
    """Multiple layers of wrapping stacked together must all be peeled off."""
    wrapped = '**"Account: Expenses:Food and Dining"**.'
    assert agent._validate_llm_answer(wrapped, ACCOUNT_SET) == "Expenses:Food and Dining"


def test_different_case_resolves_when_unique():
    """A case-only difference resolves via the case-insensitive fallback
    tier, but ONLY when it uniquely identifies one account."""
    assert agent._validate_llm_answer("expenses:food and dining", ACCOUNT_SET) == "Expenses:Food and Dining"
    assert agent._validate_llm_answer("EXPENSES:FOOD AND DINING", ACCOUNT_SET) == "Expenses:Food and Dining"


# ---------------------------------------------------------------------------
# Negative: case-only duplicates are ambiguous
# ---------------------------------------------------------------------------

def test_case_only_duplicate_accounts_return_none():
    account_set = {
        "Expenses:Food and Dining",
        "Expenses:food and dining",
    }
    assert agent._validate_llm_answer("expenses:food and dining", account_set) is None


# ---------------------------------------------------------------------------
# Negative: normalisation must never create a bare-substring match
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("wrapped", [
    '"ining"',
    "'ining'",
    "`ining`",
    "**ining**",
    '"od and Dining"',
    "'od and Dining'",
    "`od and Dining`",
])
def test_normalisation_never_creates_bare_substring_match(wrapped):
    assert agent._validate_llm_answer(wrapped, ACCOUNT_SET) is None


# ---------------------------------------------------------------------------
# Negative: a nonexistent Root-Account-prefixed path returns None
# ---------------------------------------------------------------------------

def test_root_account_prefixed_nonexistent_path_returns_none():
    assert agent._validate_llm_answer(
        "Root Account: Expenses:Totally Made Up Account", ACCOUNT_SET
    ) is None


# ---------------------------------------------------------------------------
# _normalize_llm_answer -- idempotence and SKIP-form coverage
# ---------------------------------------------------------------------------

def test_normalize_is_idempotent():
    wrapped = '**"Account: Expenses:Food and Dining"**.'
    once = agent._normalize_llm_answer(wrapped)
    twice = agent._normalize_llm_answer(once)
    assert once == twice == "Expenses:Food and Dining"


@pytest.mark.parametrize("skip_form", [
    "SKIP", "skip", "Skip",
    "`SKIP`", "```SKIP```", "**SKIP**",
    '"SKIP"', "'SKIP'",
    "SKIP.", " SKIP ",
])
def test_skip_variants_all_normalize_to_skip(skip_form):
    assert agent._normalize_llm_answer(skip_form).upper() == "SKIP"


@pytest.mark.parametrize("skip_form", [
    "SKIP", "`SKIP`", "SKIP.", '"skip"', "**skip**",
])
def test_skip_variants_never_reach_validate_llm_answer_as_an_account(skip_form):
    """Mirrors the SKIP short-circuit in llm_fallback_mapping: once
    normalised, every one of these forms must be caught by the
    `answer.upper() == "SKIP"` check, never passed into
    _validate_llm_answer at all (which -- with a permissive account set --
    would otherwise be the observable symptom of a SKIP leaking through)."""
    normalized = agent._normalize_llm_answer(skip_form)
    assert normalized.upper() == "SKIP"
    # Defence in depth: even if it were (incorrectly) passed through, it
    # must never resolve to a real account.
    assert agent._validate_llm_answer(normalized, ACCOUNT_SET) is None
