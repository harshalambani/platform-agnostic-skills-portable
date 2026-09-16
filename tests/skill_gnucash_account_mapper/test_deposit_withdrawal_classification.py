"""
tests/skill_gnucash_account_mapper/test_deposit_withdrawal_classification.py --
regression guard for IMP-02: withdrawals mislabeled as deposits.

llm_fallback_mapping() (src/agents/skill_gnucash_account_mapper/agent.py)
builds a " [deposit]" / " [withdrawal]" hint that gets interpolated into the
LLM prompt for each unmatched row. The amount-text convention differs per
bank: HDFC leaves the empty side blank, but ICICI/BoB/HSBC write it as the
string '0' (or '0.0'). Because a non-empty string is truthy in Python, the
old code (`if row.get("deposit"): ... elif row.get("withdrawal"): ...`)
treated every ICICI/BoB/HSBC withdrawal row as a deposit, since its
'deposit' field held the truthy string '0'.

This drives llm_fallback_mapping() with _ollama_chat and _resolve_ollama_config
monkeypatched (no network, no real Ollama config), and inspects the actual
prompt text sent for each row to confirm the correct token is used --
regardless of whether the empty side is blank ('' , HDFC), the string '0'
(ICICI/BoB), or '0.0' (HSBC) -- and that a row where both sides are zero (or
unparseable) gets no token at all rather than a guess.
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


def _fake_resolve_ollama_config(config_path, model_override=None):
    return "http://fake-ollama:11434", "fake-model"


def _run_and_capture_prompts(monkeypatch, rows):
    """Drive llm_fallback_mapping() with the network fully stubbed out and
    return the list of non-warmup user prompts sent, in row order."""
    calls = []

    def fake_ollama_chat(base_url, model, system, user, timeout=None):
        calls.append(user)
        if user == "ping":
            return "OK"  # warm-up reply
        return "SKIP"  # main-prompt reply -- classification content is what we test

    monkeypatch.setattr(agent, "_resolve_ollama_config", _fake_resolve_ollama_config)
    monkeypatch.setattr(agent, "_ollama_chat", fake_ollama_chat)
    monkeypatch.setattr(agent, "_emit_mapper_progress", lambda msg: None)

    agent.llm_fallback_mapping(
        unmatched_rows=rows,
        account_tree=["Expenses:Misc", "Income:Other"],
        example_mappings=[],
        config_path="fake_config.yaml",
        historical_mappings=None,
    )

    # First call is always the warm-up ping; the rest are one per row, in order.
    assert calls[0] == "ping"
    return calls[1:]


def test_withdrawal_with_zero_string_deposit_is_not_labelled_deposit(monkeypatch):
    """ICICI/BoB/HSBC convention: the empty (deposit) side is the string '0',
    which is truthy -- must still classify as a withdrawal, never a deposit."""
    rows = [
        {"row": 1, "description": "ATM CASH WDL", "deposit": "0", "withdrawal": "750.00"},
    ]
    prompts = _run_and_capture_prompts(monkeypatch, rows)
    assert " [withdrawal]" in prompts[0]
    assert " [deposit]" not in prompts[0]


def test_withdrawal_with_zero_point_zero_string_deposit_is_not_labelled_deposit(monkeypatch):
    """HSBC-style '0.0' empty-side string -- same requirement."""
    rows = [
        {"row": 1, "description": "NEFT OUT", "deposit": "0.0", "withdrawal": "1200.50"},
    ]
    prompts = _run_and_capture_prompts(monkeypatch, rows)
    assert " [withdrawal]" in prompts[0]
    assert " [deposit]" not in prompts[0]


# ── Negative tests ────────────────────────────────────────────────────────
# Explicitly assert the WRONG label is absent, per bank convention -- not
# just that the right one is present. This is exactly the defect: on
# unfixed code, " [deposit]" appears even though the row is a withdrawal.

@pytest.mark.parametrize("zero_string", ["0", "0.0", "0.00"])
def test_withdrawal_never_labelled_deposit_across_zero_string_conventions(monkeypatch, zero_string):
    """ICICI/BoB write the empty side as '0', HSBC as '0.0'; guard '0.00' too
    in case a bank ever zero-pads to two decimals. None of these truthy-but-
    zero strings may produce ' [deposit]' for a withdrawal row."""
    rows = [
        {"row": 1, "description": "POS PURCHASE", "deposit": zero_string, "withdrawal": "999.00"},
    ]
    prompts = _run_and_capture_prompts(monkeypatch, rows)
    assert " [deposit]" not in prompts[0], (
        f"withdrawal row with deposit={zero_string!r} was wrongly labelled "
        f"[deposit]: {prompts[0]!r}"
    )
    assert " [withdrawal]" in prompts[0]


def test_hdfc_blank_empty_side_still_classifies_correctly(monkeypatch):
    """HDFC convention: the empty side is a blank string, not '0' -- must keep
    working exactly as before the fix."""
    rows = [
        {"row": 1, "description": "SALARY CREDIT", "deposit": "50000.00", "withdrawal": ""},
        {"row": 2, "description": "ATM WDL", "deposit": "", "withdrawal": "2000.00"},
    ]
    prompts = _run_and_capture_prompts(monkeypatch, rows)
    assert " [deposit]" in prompts[0] and " [withdrawal]" not in prompts[0]
    assert " [withdrawal]" in prompts[1] and " [deposit]" not in prompts[1]


def test_deposit_with_zero_string_withdrawal_is_labelled_deposit(monkeypatch):
    """The mirror case: a genuine deposit whose withdrawal side is the string
    '0' must still be classified as a deposit."""
    rows = [
        {"row": 1, "description": "IMPS CREDIT", "deposit": "3300.00", "withdrawal": "0"},
    ]
    prompts = _run_and_capture_prompts(monkeypatch, rows)
    assert " [deposit]" in prompts[0]
    assert " [withdrawal]" not in prompts[0]


def test_both_sides_zero_gets_no_amount_token_never_a_guess(monkeypatch):
    """Neither side has a genuine amount -- amt_info must stay empty rather
    than guess either label."""
    rows = [
        {"row": 1, "description": "BALANCE ENQUIRY", "deposit": "0", "withdrawal": "0.0"},
    ]
    prompts = _run_and_capture_prompts(monkeypatch, rows)
    assert " [deposit]" not in prompts[0]
    assert " [withdrawal]" not in prompts[0]


def test_unparseable_amounts_get_no_amount_token(monkeypatch):
    """Garbage / unparseable amount text on both sides must not be guessed
    either way."""
    rows = [
        {"row": 1, "description": "MISC ENTRY", "deposit": "N/A", "withdrawal": "-"},
    ]
    prompts = _run_and_capture_prompts(monkeypatch, rows)
    assert " [deposit]" not in prompts[0]
    assert " [withdrawal]" not in prompts[0]
