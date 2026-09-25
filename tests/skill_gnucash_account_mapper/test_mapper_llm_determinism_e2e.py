"""
tests/skill_gnucash_account_mapper/test_mapper_llm_determinism_e2e.py --
end-to-end coverage for MAP-01/02/03/04 through the real
llm_fallback_mapping() entry point, driven by the MAP-06 ScriptedLLM
harness (see conftest.py's `scripted_llm` fixture) so no real network call
is ever made.

Confirms the fixed pieces work together as llm_fallback_mapping actually
uses them: an ambiguous first answer (MAP-01) triggers the existing focused
retry path, and a subsequent unique answer from that retry is accepted and
recorded as a match -- but an answer that stays ambiguous through the retry
is never written as a match ("LLM: matched"), regardless of how plausible
it looks.

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
from conftest import make_historical_mappings  # noqa: E402


def _ambiguous_historical_mappings():
    return make_historical_mappings({
        "Expenses:Food and Dining": ["SWIGGY FOOD ORDER"],
        "Expenses:Travel:Food and Dining": ["AIRPORT SWIGGY FOOD ORDER"],
    }, frequency=3)


def test_ambiguous_first_answer_triggers_focused_retry_and_unique_answer_used(scripted_llm):
    scripted_llm.queue(
        "Food and Dining",              # first answer: ambiguous tail -> None
        "Expenses:Food and Dining",     # retry answer: exact match -> resolves
    )
    rows = [{"row": 1, "description": "SWIGGY FOOD ORDER", "deposit": "250.00", "withdrawal": "0"}]

    result = agent.llm_fallback_mapping(
        unmatched_rows=rows,
        account_tree=[],
        example_mappings=[],
        config_path="fake_config.yaml",
        historical_mappings=_ambiguous_historical_mappings(),
    )

    assert result[1]["account"] == "Expenses:Food and Dining"
    assert result[1]["reason"] == "LLM: matched"
    # warm-up ping + first (ambiguous) answer + retry (unique) answer.
    assert scripted_llm.call_count == 2


def test_ambiguous_answer_never_written_as_matched_even_after_retry(scripted_llm):
    """Both the first answer AND the retry answer are the ambiguous tail --
    matched_acct must stay None throughout, and the row must never be
    recorded with reason 'LLM: matched'."""
    scripted_llm.queue(
        "Food and Dining",   # first answer: ambiguous -> None
        "Food and Dining",   # retry answer: still ambiguous -> None
    )
    rows = [{"row": 1, "description": "SWIGGY FOOD ORDER", "deposit": "250.00", "withdrawal": "0"}]

    result = agent.llm_fallback_mapping(
        unmatched_rows=rows,
        account_tree=[],
        example_mappings=[],
        config_path="fake_config.yaml",
        historical_mappings=_ambiguous_historical_mappings(),
    )

    assert all(v.get("reason") != "LLM: matched" for v in result.values())
    assert result.get(1, {}).get("account", "") == ""


def test_wrapped_and_unique_answer_resolves_end_to_end(scripted_llm):
    """MAP-03 through the real pipeline: a markdown/quote-wrapped, uniquely
    resolving answer is accepted on the first pass, no retry needed."""
    scripted_llm.queue("**Expenses:Food and Dining**")
    rows = [{"row": 1, "description": "SWIGGY FOOD ORDER", "deposit": "250.00", "withdrawal": "0"}]
    historical_mappings = make_historical_mappings({
        "Expenses:Food and Dining": ["SWIGGY FOOD ORDER"],
    }, frequency=3)

    result = agent.llm_fallback_mapping(
        unmatched_rows=rows,
        account_tree=[],
        example_mappings=[],
        config_path="fake_config.yaml",
        historical_mappings=historical_mappings,
    )

    assert result[1]["account"] == "Expenses:Food and Dining"
    assert result[1]["reason"] == "LLM: matched"
    # warm-up ping + first answer only -- no retry needed.
    assert scripted_llm.call_count == 1
