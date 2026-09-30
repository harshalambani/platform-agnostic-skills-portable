"""
MAP-15 -- a 2-3 letter fragment, or a bank-name-only token, is not "history"
evidence. Distinctiveness is derived from the book (account share, own
bank-account names), never from a stop-word list.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402

DINING = "Expenses:Food:Dining"
GROC = "Expenses:Groceries"
FUEL = "Expenses:Fuel"
FEES = "Expenses:Bank Fees"
OWN1 = "Assets:Cash and Bank:HDFC Bank - 094XXXX1234"
OWN2 = "Assets:Cash and Bank:HDFC Bank - 094XXXX5678"


def _pairs(rows, freq=1):
    return [{"description": d, "account": a, "frequency": freq} for d, a in rows]


def test_tokenizer_drops_one_and_two_char_fragments_but_keeps_real_tokens():
    toks = m._tokenize_history("UPI/AB/7/payee@ybl/ZOMATO")
    assert "ab" not in toks and "7" not in toks
    assert "payee@ybl" in toks and "zomato" in toks


def test_single_shared_two_letter_fragment_never_yields_history():
    """NEGATIVE: rows that only share 'sb' must not be a history match."""
    hist = _pairs([("SB XQ1", DINING), ("SB XQ2", DINING),
                   ("SB XQ3", DINING)], freq=3)
    model = m._build_history_token_model(hist)
    assert m._history_token_match("SB ZZZ9", model) is None


def test_two_letter_fragment_cannot_be_the_discriminating_token():
    """NEGATIVE: even repeated many times, 'to' alone never matches."""
    hist = _pairs([("PAID TO", DINING)] * 6)
    model = m._build_history_token_model(hist)
    assert "to" not in model
    assert m._history_token_match("SENT TO", model) is None


def test_bank_name_only_token_never_yields_history():
    """NEGATIVE: 'hdfc' appears only in the bank's own account name -- naming
    the bank says nothing about which payee/account."""
    hist = _pairs([("HDFC CHARGES A1", FEES), ("HDFC CHARGES A2", FEES)] * 2)
    model = m._build_history_token_model(hist)
    own = {OWN1, OWN2}
    assert m._history_token_match("HDFC PAYMENT QQ", model, own_bank_accounts=own) is None


def test_bank_name_token_is_ignored_even_when_history_is_unanimous():
    hist = _pairs([("HDFC ZZQ", FEES)] * 4)
    model = m._build_history_token_model(hist)
    assert m._history_token_match("HDFC", model, own_bank_accounts={OWN1}) is None


def test_token_touching_most_of_the_book_is_not_evidence():
    """NEGATIVE: a token seen against most accounts identifies nothing."""
    hist = _pairs([("COMMON ALPHA", DINING), ("COMMON BRAVO", GROC),
                   ("COMMON CHARLIE", FUEL), ("COMMON DELTA", FEES),
                   ("COMMON ECHO", DINING)])
    model = m._build_history_token_model(hist)
    assert m._history_token_match("COMMON", model) is None


def test_real_multi_token_history_match_still_works():
    hist = _pairs([("SWIGGY ORDER 4471 BANGALORE", DINING),
                   ("SWIGGY ORDER 9921 BANGALORE", DINING)], freq=3)
    hist += _pairs([("BIGBASKET GROCERY", GROC)], freq=3)
    model = m._build_history_token_model(hist)
    res = m._history_token_match("SWIGGY ORDER 5555 BANGALORE", model,
                                 own_bank_accounts={OWN1})
    assert res and res["account"] == DINING and res["confidence"] == "history"


def test_own_bank_name_tokens_come_from_the_book_not_a_list():
    toks = m._own_bank_name_tokens({OWN1, "Assets:Cash and Bank:Foo Trust - 111"})
    assert {"hdfc", "bank", "foo", "trust"} <= toks
    assert not any(t.isdigit() for t in toks)
    assert m._own_bank_name_tokens(None) == set()
