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


# ---- MAP-33 follow-up: generic tokens only gate, they never lower a score ----

def _mixed_model():
    return m._build_history_token_model([
        {"description": "ZETA TOURS LIMITED", "account": "Expenses:Travel Fares", "frequency": 4},
        {"description": "OMEGA FOODS PRIVATE", "account": "Expenses:Meals", "frequency": 4},
        {"description": "OTHER SHOP LIMITED", "account": "Expenses:Misc", "frequency": 1},
    ])


def test_mixed_generic_and_real_token_keeps_unstripped_score():
    model = _mixed_model()
    toks = {"zeta", "tours", "limited"}
    got = m._history_bayes_score(toks, model)
    assert got is not None and got["account"] == "Expenses:Travel Fares"
    # the score is what main computes: generic 'limited' is still counted
    raw_all = m._history_bayes_raw(toks, model)
    raw_real = m._history_bayes_raw({"zeta", "tours"}, model)
    assert raw_all is not None and raw_real is not None
    assert raw_all[0][0][1] == "Expenses:Travel Fares"
    assert raw_all[0][0][0] != raw_real[0][0][0]     # 'limited' still contributes


def test_mixed_foods_private_still_matches():
    got = m._history_bayes_score({"omega", "foods", "private"}, _mixed_model())
    assert got is not None and got["account"] == "Expenses:Meals"


def test_all_generic_shared_tokens_are_no_match_even_with_unknown_extras():
    model = _model()
    for toks in ({"pvt", "ltd"}, {"month", "dec"}, {"paytm", "using"},
                 {"pvt", "ltd", "neverseenword"}):
        assert m._history_bayes_score(toks, model) is None
        assert m._history_bayes_raw(toks, model) is None


def test_first_name_only_is_still_no_match():
    assert m._history_bayes_score({"rahul"}, _model()) is None
    assert m._history_bayes_score({"rahul", "amit"}, _model()) is None


# ---- MAP-33 (per-account gate): an account supported only by generic tokens
# ---- is not a candidate, however many OTHER real tokens the narration has ----

def _acct_model():
    return m._build_history_token_model([
        {"description": "PVT LTD HOLDINGS", "account": "Equity:Drawings", "frequency": 5},
        {"description": "INDIA POST FEE", "account": "Expenses:Charges", "frequency": 5},
        {"description": "OMEGA FOODS PRIVATE", "account": "Expenses:Meals", "frequency": 5},
        {"description": "SMITH AND SONS", "account": "Expenses:Repairs", "frequency": 5},
        {"description": "ASHA KUMAR", "account": "Expenses:Gifts", "frequency": 5},
        {"description": "INTEREST PAID TILL MAR", "account": "Income:Interest", "frequency": 5},
        {"description": "ZED STORES LIMITED", "account": "Expenses:Shopping", "frequency": 5},
    ])


def _accounts_in(toks, model):
    raw = m._history_bayes_raw(toks, model)
    return None if raw is None else {a for _s, a in raw[0]}


def test_mixed_narration_generic_only_account_is_not_a_candidate():
    """Real token 'india' is known for Charges; Drawings is linked ONLY by
    pvt/ltd. Drawings must not be a candidate (and must not win)."""
    model = _acct_model()
    toks = {"india", "pvt", "ltd"}
    got = _accounts_in(toks, model)
    assert got is not None and "Equity:Drawings" not in got
    r = m._history_bayes_score(toks, model)
    assert r is None or r["account"] != "Equity:Drawings"


def test_travel_co_pvt_only_links_to_winning_account_is_no_match():
    model = _acct_model()
    toks = {"travco", "india", "pvt", "l"}
    r = m._history_bayes_score({"travco", "pvt", "ltd"}, model)
    assert r is None
    assert _accounts_in({"travco", "pvt", "ltd"}, model) is None
    assert "Equity:Drawings" not in (_accounts_in(toks, model) or set())


def test_surname_and_sons_is_no_match():
    assert m._history_bayes_score({"jones", "and", "sons"}, _acct_model()) is None
    assert _accounts_in({"jones", "and", "sons"}, _acct_model()) is None


def test_name_kumar_is_no_match():
    assert m._history_bayes_score({"ravi", "kumar"}, _acct_model()) is None


def test_interest_paid_till_mar_is_not_a_history_match():
    model = _acct_model()
    assert "Income:Interest" not in (_accounts_in({"till", "mar", "unseenword"}, model) or set())
    assert m._history_bayes_score({"till", "mar"}, model) is None


def test_brand_online_limited_still_matches_on_brand():
    model = m._build_history_token_model([
        {"description": "ZED STORES LIMITED", "account": "Expenses:Shopping", "frequency": 5},
        {"description": "OTHER THING", "account": "Expenses:Misc", "frequency": 5},
    ])
    r = m._history_bayes_score({"zed", "stores", "online", "limited"}, model)
    assert r is not None and r["account"] == "Expenses:Shopping"


def test_reverse_real_token_account_keeps_mains_exact_score():
    """An account with a real supporting token is scored exactly as main:
    prod(p)/(prod(p)+prod(1-p)) over EVERY contributing token, generic included."""
    import math
    model = m._build_history_token_model([
        {"description": "ZETA TOURS LIMITED", "account": "Expenses:Travel Fares", "frequency": 4},
        {"description": "OTHER SHOP LIMITED", "account": "Expenses:Misc", "frequency": 1},
    ])
    toks = {"zeta", "tours", "limited"}
    scored = m._history_bayes_raw(toks, model)[0]
    top = dict((a, s) for s, a in scored)["Expenses:Travel Fares"]
    lp = ln = 0.0
    for t in toks:
        b = model[t]
        p = b["Expenses:Travel Fares"] / sum(b.values())
        p = min(max(p, 1e-6), 1 - 1e-6)
        lp += math.log(p)
        ln += math.log(1 - p)
    assert abs(top - 1.0 / (1.0 + math.exp(ln - lp))) < 1e-12
