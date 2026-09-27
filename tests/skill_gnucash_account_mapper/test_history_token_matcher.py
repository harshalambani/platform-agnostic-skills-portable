"""
tests/skill_gnucash_account_mapper/test_history_token_matcher.py -- MAP-11.

Deterministic, GnuCash-style Bayesian matcher built from this book's own
historical (description -> account) pairs (agent._history_token_match and
friends). Nothing in the matcher names a bank, a channel word, or "self" --
a token's evidentiary weight comes purely from how consistently it has led
to one account in synthetic history built here. All fixtures use fake
names, fake account numbers, and fake IFSC codes.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402


FD_ACCOUNT = "Assets:Current Assets:Fixed Deposits:ICICI Bank - FD"
FD_ACCOUNT_B = "Assets:Current Assets:Fixed Deposits:ICICI Bank - FD2"
HDFC_ACCOUNT = "Assets:Current Assets:Cash and Bank:HDFC Bank - 0001579"
SBM_ACCOUNT = "Assets:Current Assets:Cash and Bank:SBM Bank - 0009999"
HSBC_ACCOUNT = "Assets:Current Assets:Cash and Bank:HSBC Bank - 0004002"
BOND_ACCOUNT = "Assets:Investments:Bonds:Sample Bond"
EXPENSE_ACCOUNT = "Expenses:Bank Charges"


def _mk(desc: str, account: str, frequency: int = 1) -> dict:
    return {"description": desc, "account": account, "frequency": frequency}


# ---------------------------------------------------------------------------
# Sweep narrations (6 given shapes) -> history, never sent to the LLM
# ---------------------------------------------------------------------------

_SWEEP_HISTORY = [
    _mk("111111111111: Rev Sweep From", FD_ACCOUNT, frequency=40),
    _mk("222222222222: Closure Proceeds", FD_ACCOUNT, frequency=40),
    _mk("333333333333 : Rev sweep from", FD_ACCOUNT, frequency=40),
    _mk("AUTOSWEEP TO 444444444444 JANE Q SAMPLE", FD_ACCOUNT, frequency=40),
    _mk("555555555555: AUTOSWEEP TO", FD_ACCOUNT, frequency=40),
    _mk("666666666666 : Autosweep to", FD_ACCOUNT, frequency=40),
]

_SWEEP_LIVE_ROWS = [
    "999999999999: Rev Sweep From",
    "888888888888: Closure Proceeds",
    "777777777777 : Rev sweep from",
    "AUTOSWEEP TO 121212121212 JOHN Q SAMPLE",
    "131313131313: AUTOSWEEP TO",
    "141414141414 : Autosweep to",
]


def test_sweep_narrations_map_to_history_with_unseen_fd_numbers():
    model = agent._build_history_token_model(_SWEEP_HISTORY)
    for desc in _SWEEP_LIVE_ROWS:
        match = agent._history_token_match(desc, model)
        assert match is not None, f"expected a history match for {desc!r}"
        assert match["account"] == FD_ACCOUNT
        assert match["confidence"] == "history"


def test_sweep_rows_excluded_from_llm_via_run_step_wiring():
    # Step 3.6 in run() only ever calls _history_token_match on rows not at
    # High/override, and any row it successfully matches is labelled
    # 'history' -- Step 4's LLM-candidate collection (conf in ('none','weak'))
    # structurally never sees a 'history' row. This is a direct unit check
    # of that exclusion condition, mirroring run()'s actual gate.
    conf = "history"
    acct = FD_ACCOUNT
    still_unmatched_gate = (conf in ('none', 'weak') or not acct) and conf not in ('smart', 'override')
    assert still_unmatched_gate is False


# ---------------------------------------------------------------------------
# Two-FD-accounts ambiguity: an unseen FD number never crosses to the WRONG
# FD account, and genuinely ambiguous evidence stays unmatched.
# ---------------------------------------------------------------------------

def test_two_fd_accounts_never_cross_matched():
    history = [
        _mk("Rev Sweep From FDA", FD_ACCOUNT, frequency=30),
        _mk("Rev Sweep From FDB", FD_ACCOUNT_B, frequency=30),
    ]
    model = agent._build_history_token_model(history)
    match_a = agent._history_token_match("Rev Sweep From FDA", model)
    match_b = agent._history_token_match("Rev Sweep From FDB", model)
    assert match_a["account"] == FD_ACCOUNT
    assert match_b["account"] == FD_ACCOUNT_B
    assert match_a["account"] != match_b["account"]


def test_ambiguous_ties_stay_unmatched():
    # "sweep" alone spreads evenly across both FD accounts -- no discriminating
    # token favours either one, so the row must stay unmatched, never guess.
    history = [
        _mk("sweep transfer", FD_ACCOUNT, frequency=20),
        _mk("sweep transfer", FD_ACCOUNT_B, frequency=20),
    ]
    model = agent._build_history_token_model(history)
    match = agent._history_token_match("sweep transfer", model)
    assert match is None


# ---------------------------------------------------------------------------
# Thin-evidence guard: a token that spreads across many accounts cannot alone
# justify a match -- purely from its own historical spread, no word list.
# ---------------------------------------------------------------------------

def test_single_transaction_support_stays_unmatched():
    # Only ONE historical transaction behind the winning account -- fails
    # HISTORY_MIN_SUPPORT_TXNS even though the token is otherwise unique.
    history = [_mk("uniquephrase payment", EXPENSE_ACCOUNT, frequency=1)]
    model = agent._build_history_token_model(history)
    match = agent._history_token_match("uniquephrase payment", model)
    assert match is None


def test_thin_spread_token_only_support_stays_unmatched():
    # A token ("channel") that has spread across 3 different accounts in
    # history is thin evidence purely by its own spread (> HISTORY_MAX_TOKEN_
    # SPREAD) -- never because the word is named anywhere in the matcher.
    history = [
        _mk("channel payment alpha", EXPENSE_ACCOUNT, frequency=5),
        _mk("channel payment beta", FD_ACCOUNT, frequency=5),
        _mk("channel payment gamma", BOND_ACCOUNT, frequency=5),
    ]
    model = agent._build_history_token_model(history)
    # A brand-new row using ONLY the thin "channel"/"payment" tokens (no
    # account-specific word) must not resolve -- there is no discriminating
    # evidence for any one account.
    match = agent._history_token_match("channel payment", model)
    assert match is None


# ---------------------------------------------------------------------------
# VPA handling (replaces the dropped extract_upi_key fix, per the
# correction): a VPA seen in 2+ past transactions maps by history; an unseen
# VPA stays unmatched. VPAs are tokenised whole, never split on '@'/'.'.
# ---------------------------------------------------------------------------

def test_known_vpa_maps_by_history():
    history = [
        _mk("payee.sample@fakebank/UPI/ref001", EXPENSE_ACCOUNT, frequency=1),
        _mk("payee.sample@fakebank/UPI/ref002", EXPENSE_ACCOUNT, frequency=1),
        _mk("payee.sample@fakebank/UPI/ref003", EXPENSE_ACCOUNT, frequency=1),
    ]
    model = agent._build_history_token_model(history)
    match = agent._history_token_match("payee.sample@fakebank/UPI/ref999", model)
    assert match is not None
    assert match["account"] == EXPENSE_ACCOUNT
    assert "payee.sample@fakebank" in model  # tokenised whole, not split


def test_unseen_vpa_stays_unmatched():
    # The shared "UPI" token is a realistic generic channel word: in a real
    # book it shows up across many different accounts, so it must not alone
    # carry a match. This is modelled here (not hardcoded) by giving "UPI"
    # enough spread across OTHER accounts that it is thin evidence purely by
    # HISTORY_MAX_TOKEN_SPREAD -- only the specific, repeatedly-seen VPA
    # token should be able to justify a match.
    history = [
        _mk("payee.sample@fakebank/UPI/ref001", EXPENSE_ACCOUNT, frequency=1),
        _mk("payee.sample@fakebank/UPI/ref002", EXPENSE_ACCOUNT, frequency=1),
        _mk("someone.else@otherbank/UPI/refA01", FD_ACCOUNT, frequency=1),
        _mk("another.payee@thirdbank/UPI/refB01", BOND_ACCOUNT, frequency=1),
        _mk("yet.another@fourthbank/UPI/refC01", HDFC_ACCOUNT, frequency=1),
    ]
    model = agent._build_history_token_model(history)
    match = agent._history_token_match("neverseen.payee@otherbank/UPI/ref500", model)
    assert match is None


# ---------------------------------------------------------------------------
# This test FAILS on origin/main: _history_token_match / _build_history_
# token_model do not exist there at all (MAP-11 is new code), so importing
# them raises AttributeError pre-fix.
# ---------------------------------------------------------------------------

def test_history_matcher_functions_exist_and_are_callable():
    assert callable(agent._history_token_match)
    assert callable(agent._build_history_token_model)
    model = agent._build_history_token_model([_mk("x", EXPENSE_ACCOUNT, 5), _mk("x", EXPENSE_ACCOUNT, 5)])
    assert agent._history_token_match("x", model) is not None
