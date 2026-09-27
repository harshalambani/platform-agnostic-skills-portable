"""
tests/skill_gnucash_account_mapper/test_mapper_llm_shortlist_map12.py -- MAP-12.

The LLM fallback must never accept free text as an account: a live run
showed the small model return past narrations as accounts, glue narration
onto a real account name, add a trailing colon, and give real-but-absurd
answers. MAP-12 replaces free-text answers with a numbered shortlist (built
from MAP-11's own history evidence, topped up with plausible-direction
accounts) that the model must answer with a bare list number, or 0/SKIP.
A non-numeric reply is still checked, but ONLY against the row's own
shortlist -- never the full account universe -- so it is never fuzzy and
never free text.

This also covers MAP-12's direction-check flag: a withdrawal landing in an
Income account, or a deposit landing in an Expenses account, is visibly
flagged (never changed).

All descriptions/accounts below are synthetic.

This file FAILS entirely on origin/main: _parse_shortlist_answer,
_build_llm_shortlist, and _direction_mismatch do not exist there (MAP-12 is
new code), so every test below raises AttributeError pre-fix.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402
from conftest import make_historical_mappings  # noqa: E402


SHORTLIST = ["Expenses:Food and Dining", "Expenses:Travel"]


# ---------------------------------------------------------------------------
# _parse_shortlist_answer: strict parsing, unit level
# ---------------------------------------------------------------------------

def test_functions_exist_and_are_callable():
    # Fails on origin/main: none of this exists there pre-MAP-12.
    assert callable(agent._parse_shortlist_answer)
    assert callable(agent._build_llm_shortlist)
    assert callable(agent._direction_mismatch)


def test_valid_number_in_range_matches():
    status, acct = agent._parse_shortlist_answer("2", SHORTLIST)
    assert status == "matched"
    assert acct == "Expenses:Travel"


def test_number_out_of_range_is_invalid_never_guessed():
    status, acct = agent._parse_shortlist_answer("5", SHORTLIST)
    assert status == "invalid"
    assert acct is None


def test_free_text_account_name_outside_shortlist_never_accepted():
    # A real-looking account name the row was never offered must be
    # rejected exactly like free text -- never fuzzy-matched in.
    status, acct = agent._parse_shortlist_answer("Expenses:Utilities:Electricity", SHORTLIST)
    assert status == "invalid"
    assert acct is None


def test_pasted_narration_never_accepted_as_account():
    status, acct = agent._parse_shortlist_answer(
        "AUTOSWEEP TO 444444444444 JANE Q SAMPLE", SHORTLIST
    )
    assert status == "invalid"
    assert acct is None


def test_trailing_colon_glued_answer_never_accepted():
    # "1:" is neither a bare number (regex requires ^[0-9]+$) nor an exact/
    # tail match against the shortlist -- must be rejected, not coerced to 1.
    status, acct = agent._parse_shortlist_answer("1:", SHORTLIST)
    assert status == "invalid"
    assert acct is None


def test_narration_glued_onto_real_account_never_accepted():
    status, acct = agent._parse_shortlist_answer(
        "Expenses:Food and Dining: SWIGGY FOOD ORDER", SHORTLIST
    )
    assert status == "invalid"
    assert acct is None


def test_zero_is_skip_not_invalid():
    status, acct = agent._parse_shortlist_answer("0", SHORTLIST)
    assert status == "skip"
    assert acct is None


def test_skip_word_is_skip():
    status, acct = agent._parse_shortlist_answer("SKIP", SHORTLIST)
    assert status == "skip"
    assert acct is None


def test_empty_reply_is_invalid():
    status, acct = agent._parse_shortlist_answer("", SHORTLIST)
    assert status == "invalid"
    assert acct is None


# ---------------------------------------------------------------------------
# End-to-end through llm_fallback_mapping + ScriptedLLM: 0/SKIP, retry ->
# suspense, and the direction-check flag never mutating the account.
# ---------------------------------------------------------------------------

def test_zero_skip_leaves_row_unmatched_end_to_end(scripted_llm):
    scripted_llm.queue("0")
    rows = [{"row": 1, "description": "VENDOR REFUND CREDIT", "deposit": "0", "withdrawal": "500.00"}]
    historical_mappings = make_historical_mappings({
        "Income:Refunds": ["VENDOR REFUND CREDIT"],
    }, frequency=5)

    result = agent.llm_fallback_mapping(
        unmatched_rows=rows,
        account_tree=[],
        example_mappings=[],
        config_path="fake_config.yaml",
        historical_mappings=historical_mappings,
    )

    assert result.get(1, {}).get("account", "") == ""
    assert result.get(1, {}).get("reason") == "LLM: skip"


def test_invalid_answer_after_one_retry_leaves_row_unmatched(scripted_llm):
    # Both replies are free text outside the shortlist -- one retry max,
    # then the row is left unmatched entirely (never guessed into suspense
    # via a fuzzy match).
    scripted_llm.queue(
        "AUTOSWEEP TO 444444444444 JANE Q SAMPLE",
        "AUTOSWEEP TO 444444444444 JANE Q SAMPLE",
    )
    rows = [{"row": 1, "description": "VENDOR REFUND CREDIT", "deposit": "0", "withdrawal": "500.00"}]
    historical_mappings = make_historical_mappings({
        "Income:Refunds": ["VENDOR REFUND CREDIT"],
    }, frequency=5)

    result = agent.llm_fallback_mapping(
        unmatched_rows=rows,
        account_tree=[],
        example_mappings=[],
        config_path="fake_config.yaml",
        historical_mappings=historical_mappings,
    )

    assert 1 not in result
    # warm-up ping + first (invalid) answer + one retry (still invalid).
    assert scripted_llm.call_count == 2


def test_withdrawal_mapped_to_income_flagged_account_unchanged(scripted_llm):
    scripted_llm.queue("1")
    rows = [{"row": 1, "description": "VENDOR REFUND CREDIT", "deposit": "0", "withdrawal": "500.00"}]
    historical_mappings = make_historical_mappings({
        "Income:Refunds": ["VENDOR REFUND CREDIT"],
    }, frequency=5)

    result = agent.llm_fallback_mapping(
        unmatched_rows=rows,
        account_tree=[],
        example_mappings=[],
        config_path="fake_config.yaml",
        historical_mappings=historical_mappings,
    )

    # Flagged, but the account itself is exactly what the shortlist offered
    # -- the direction check must never change or suppress it.
    assert result[1]["account"] == "Income:Refunds"
    assert "LLM: matched" in result[1]["reason"]
    assert agent._DIRECTION_FLAG_MARKER in result[1]["reason"]


def test_deposit_mapped_to_income_not_flagged(scripted_llm):
    scripted_llm.queue("1")
    rows = [{"row": 1, "description": "VENDOR REFUND CREDIT", "deposit": "500.00", "withdrawal": "0"}]
    historical_mappings = make_historical_mappings({
        "Income:Refunds": ["VENDOR REFUND CREDIT"],
    }, frequency=5)

    result = agent.llm_fallback_mapping(
        unmatched_rows=rows,
        account_tree=[],
        example_mappings=[],
        config_path="fake_config.yaml",
        historical_mappings=historical_mappings,
    )

    # A deposit landing in Income is the ordinary case (e.g. a refund
    # credited to the bank) -- never flagged.
    assert result[1]["account"] == "Income:Refunds"
    assert result[1]["reason"] == "LLM: matched"
    assert agent._DIRECTION_FLAG_MARKER not in result[1]["reason"]


def test_direction_mismatch_never_changes_the_account_unit_level():
    # Direct unit check of the flag-only contract: calling it must never
    # mutate or reject anything -- it only returns a bool.
    assert agent._direction_mismatch("Income:Refunds", 0.0, 500.0) is True
    assert agent._direction_mismatch("Income:Refunds", 500.0, 0.0) is False
    assert agent._direction_mismatch("Expenses:Food and Dining", 500.0, 0.0) is True
    assert agent._direction_mismatch("Expenses:Food and Dining", 0.0, 500.0) is False
    assert agent._direction_mismatch("Assets:Cash", 500.0, 0.0) is False
    assert agent._direction_mismatch("Assets:Cash", 0.0, 500.0) is False
