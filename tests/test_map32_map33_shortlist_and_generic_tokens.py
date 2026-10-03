"""
MAP-32 -- the AI shortlist top-up is chosen by account TYPE.
MAP-33 -- generic words are not history evidence.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402

_WELF = "Income:Biz Income:Business Expenses:Staff Welfare"
_TELE = "Expense:Telephone"
_INT = "Income:Interest"
_BANK = "Assets:Current Assets:Cash and Bank:Synth Bank - 013065XXXX-001"
_CASH = "Assets:Current Assets:Cash in Hand"
_FUND = "Assets:Investments:Synth Fund"
_TYPES = {_WELF: "EXPENSE", _TELE: "EXPENSE", _INT: "INCOME", _BANK: "BANK",
          _CASH: "CASH", _FUND: "MUTUAL"}
_ALL = set(_TYPES)


def _sl(desc, dep, wd, **kw):
    return m._build_llm_shortlist(desc, dep, wd, {}, _ALL, 20, types=_TYPES, **kw)


def test_money_out_offers_expense_types_including_one_under_income_path():
    out = _sl("UNKNOWN MERCHANT", 0, 50)
    assert _WELF in out and _TELE in out and _FUND in out


def test_money_out_never_offers_income_type():
    assert _INT not in _sl("UNKNOWN MERCHANT", 0, 50)      # NEGATIVE
    assert _INT in _sl("UNKNOWN MERCHANT", 50, 0)
    assert _TELE not in _sl("UNKNOWN MERCHANT", 50, 0)     # NEGATIVE


def test_own_bank_and_cash_only_for_self_transfer_narration():
    plain = _sl("UNKNOWN MERCHANT", 0, 50)
    assert _BANK not in plain and _CASH not in plain       # NEGATIVE
    selfx = _sl("XFER TO SELF 1234", 0, 50)
    assert _BANK in selfx and _CASH in selfx
    # an explicit evidence callback wins over the marker
    assert _BANK in _sl("UNKNOWN MERCHANT", 0, 50, own_evidence=lambda d: True)
    assert _BANK not in _sl("XFER TO SELF 1234", 0, 50, own_evidence=lambda d: False)


def test_hidden_and_placeholder_accounts_never_reach_the_top_up(tmp_path):
    import gnc_book_fixture as fx
    from agents.gnucash_accounts import load_accounts, postable_accounts
    book = fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(), [])
    accts = load_accounts(book)
    m._set_account_types(accts)
    try:
        usable = {a.path for a in postable_accounts(accts)}
        out = m._build_llm_shortlist("UNKNOWN MERCHANT", 0, 50, {}, usable, 50)
        assert out, "expected some postable expense accounts"
        assert "Expenses:Household" not in out             # placeholder
        assert "Expenses:Old:Dining" not in out            # under a hidden parent
        assert "Expenses" not in out
    finally:
        m._ACCOUNT_TYPES.clear()


def _model():
    return m._build_history_token_model([
        {"description": "PAYTM USING WALLET TRAVEL", "account": "Expenses:Misc", "frequency": 3},
        {"description": "ACME STATIONERS PVT LTD", "account": "Expenses:Office", "frequency": 3},
        {"description": "RAHUL AMIT", "account": "Expenses:Gifts", "frequency": 3},
    ])


def test_only_generic_tokens_is_no_history_match():
    assert m._history_token_match("PAYTM USING TILL DEC", _model()) is None
    assert m._history_token_match("RAHUL", _model()) is None
    assert m._history_token_match("PVT LTD LIMITED", _model()) is None
    assert m._history_bayes_score({"paytm", "using"}, _model()) is None


def test_real_payee_token_still_matches():
    r = m._history_token_match("ACME STATIONERS PVT LTD DEC", _model())
    assert r is not None and r["account"] == "Expenses:Office"


def test_generic_constant_is_small_and_lowercase():
    assert {"paytm", "using", "dec", "pvt", "ltd", "travel"} <= m.HISTORY_GENERIC_TOKENS
    assert all(t == t.lower() for t in m.HISTORY_GENERIC_TOKENS)
