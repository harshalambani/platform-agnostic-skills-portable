"""
tests/skill_gnucash_account_mapper/test_mapper_retry_tokenizer.py --
regression guard for MAP-04: the retry tokeniser dropped two-letter tokens.

Background (src/agents/skill_gnucash_account_mapper/agent.py):

  _retry_with_focused_prompt tokenised with `[A-Z]{3,}` while
  _score_account_relevance used `[A-Z]{2,}`. A transaction description like
  "TO PF" has no 3-letter token at all ("TO" and "PF" are both 2 letters),
  so the retry's keyword-overlap scoring found nothing, `top` was empty, and
  the function returned None WITHOUT EVER CALLING THE MODEL -- even though
  a perfectly good historical match (e.g. "... PF ...") existed.

  Fixed by giving both call sites one shared tokeniser (_extract_tokens:
  letters, 2+) via the shared ranking helper (_rank_account_groups, MAP-02),
  so the retry only skips the model call when there is genuinely zero
  keyword overlap -- not merely because every shared token happened to be
  short.

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


def test_extract_tokens_keeps_two_letter_tokens():
    assert agent._extract_tokens("TO PF") == {"TO", "PF"}


def test_two_letter_overlap_makes_a_retry_model_call(scripted_llm):
    """'TO PF' has no 3-letter token at all; on the unfixed [A-Z]{3,}
    tokeniser this scenario returns None with zero calls. With the shared
    2+ tokeniser it must find the historical 'PF' overlap and call the
    model exactly once."""
    scripted_llm.queue("Expenses:Provident Fund Contribution")
    historical_mappings = [
        {"account": "Expenses:Provident Fund Contribution",
         "description": "SALARY DEDUCTION TO PF", "frequency": 3},
    ]

    reply = agent._retry_with_focused_prompt(
        "TO PF", "", historical_mappings,
        "ollama", "http://fake-ollama.invalid:11434", "fake-scripted-model",
    )

    assert reply == "Expenses:Provident Fund Contribution"
    assert scripted_llm.call_count == 1


def test_zero_overlap_description_returns_none_with_zero_calls(scripted_llm):
    historical_mappings = [
        {"account": "Expenses:Utilities",
         "description": "ELECTRICITY BOARD PAYMENT", "frequency": 5},
    ]

    reply = agent._retry_with_focused_prompt(
        "COMPLETELY UNRELATED TOKENS", "", historical_mappings,
        "ollama", "http://fake-ollama.invalid:11434", "fake-scripted-model",
    )

    assert reply is None
    assert scripted_llm.call_count == 0
