"""
HSBC live-run fixes -- MAP-26 (e), MAP-28, MAP-29, MAP-30.

Every fixture is synthetic (no real names, amounts or account numbers).
Each fix carries NEGATIVE tests: the wrong behaviour must NOT occur.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
from agents.gnucash_accounts import TargetGuard  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402


def _apply(tmp_path, rules, rows, **kw):
    """Run map_accounts over (date, desc, deposit, withdrawal) rows."""
    ypath = tmp_path / "rules.yaml"
    ypath.write_text(yaml.safe_dump(rules), encoding="utf-8")
    cpath = tmp_path / "in.csv"
    with open(cpath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Description", "Deposit", "Withdrawal"])
        w.writerows(rows)
    out = tmp_path / "mapped.csv"
    m.map_accounts(str(cpath), str(ypath), str(out), str(tmp_path / "rep.txt"), **kw)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _rule(pattern, account, conf="high", last="2025-01-10", reason="rule", freq=5):
    return {"patterns": [pattern], "account": account, "confidence": conf,
            "frequency": freq, "last_date": last, "reason": reason}


# ---------------------------------------------------------------- MAP-28(c)

def test_hdfc_rule_never_fires_on_hsbc_statement(tmp_path):
    rules = {"HDFC": [_rule("zzcafe", "Expenses:Dining")],
             "HSBC": [_rule("yyshop", "Expenses:Groc")]}
    rows = [("2025-06-01", "ZZCAFE BILL", "", "100"), ("2025-06-02", "YYSHOP BILL", "", "50")]
    got = _apply(tmp_path, rules, rows, bank_key="HSBC")
    assert got[0]["Account"] == "" and got[0]["Confidence"] == "none"   # NEGATIVE
    assert got[1]["Account"] == "Expenses:Groc"


def test_global_rules_apply_to_every_bank(tmp_path):
    rules = {"HDFC": [], "_global": [_rule("zzcafe", "Expenses:Dining")]}
    for bank in ("HSBC", "HDFC", "ICICI"):
        got = _apply(tmp_path, rules, [("2025-06-01", "ZZCAFE BILL", "", "100")], bank_key=bank)
        assert got[0]["Account"] == "Expenses:Dining"


def test_no_bank_key_keeps_every_section(tmp_path):
    rules = {"HDFC": [_rule("zzcafe", "Expenses:Dining")]}
    got = _apply(tmp_path, rules, [("2025-06-01", "ZZCAFE BILL", "", "100")])
    assert got[0]["Account"] == "Expenses:Dining"


def test_overrides_are_not_bank_scoped(tmp_path):
    ov = [{"pattern": "zzcafe", "account": "Expenses:Dining", "reason": "mine"}]
    got = _apply(tmp_path, {"HDFC": []}, [("2025-06-01", "ZZCAFE BILL", "", "100")],
                 overrides=ov, bank_key="HSBC")
    assert got[0]["Confidence"] == "override"


# ---------------------------------------------------------------- MAP-28(a)

def test_old_rule_never_shows_high_or_medium(tmp_path):
    for conf in ("high", "medium"):
        rules = {"HSBC": [_rule("zzcafe", "Expenses:Dining", conf=conf, last="2012-03-04")]}
        got = _apply(tmp_path, rules, [("2025-06-01", "ZZCAFE BILL", "", "100")], bank_key="HSBC")
        assert got[0]["Confidence"] == "low"
        assert "old rule, last seen 2012" in got[0]["MatchReason"]


def test_recent_rule_keeps_its_confidence(tmp_path):
    rules = {"HSBC": [_rule("zzcafe", "Expenses:Dining", conf="high", last="2020-03-04")]}
    got = _apply(tmp_path, rules, [("2025-06-01", "ZZCAFE BILL", "", "100")], bank_key="HSBC")
    assert got[0]["Confidence"] == "high"
    assert "old rule" not in got[0]["MatchReason"]


def test_rule_with_no_last_date_is_left_alone(tmp_path):
    r = _rule("zzcafe", "Expenses:Dining", conf="high")
    r.pop("last_date")
    got = _apply(tmp_path, {"HSBC": [r]}, [("2025-06-01", "ZZCAFE BILL", "", "100")], bank_key="HSBC")
    assert got[0]["Confidence"] == "high"


# ---------------------------------------------------------------- MAP-28(b)

def _cr(*pats):
    return m.CompiledRules([{"patterns": list(pats), "account": "A:Lic", "confidence": "low"}])


def test_lic_does_not_match_inside_a_longer_word():
    cr = _cr("LIC")
    assert m.match_rule("EPILICIOUS RESTAURANT", cr)[0] is None        # NEGATIVE
    assert m.match_rule("PUBLICATION DUES", cr)[0] is None             # NEGATIVE


def test_lic_matches_as_a_whole_word():
    cr = _cr("LIC")
    assert m.match_rule("LIC PREMIUM 123", cr)[0] == "A:Lic"
    assert m.match_rule("UPI/LIC/1234", cr)[0] == "A:Lic"


def test_multi_token_literal_matches_as_a_phrase_on_word_boundaries():
    cr = _cr("life ins")
    assert m.match_rule("PAID LIFE   INS CO", cr)[0] == "A:Lic"
    assert m.match_rule("WILDLIFE INSTITUTE", cr)[0] is None           # NEGATIVE


def test_real_regex_patterns_are_left_alone():
    cr = _cr(r"swiggy.*order")
    assert m.match_rule("SWIGGYX ORDER 9", cr)[0] == "A:Lic"


# ------------------------------------------------- MAP-28: dormant highlight

def test_old_lic_style_account_still_highlighted_dormant_not_hidden(tmp_path):
    """The 'looks dormant' marker is a highlight on an account that is NOT hidden."""
    book = fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts([
        fx.account_xml("lic", "LIC Policy", "EXPENSE", "exp")]), [
        fx.txn_xml("LIC OLD", "2021-04-10", [(fx.HDFC1, -1000), ("lic", 1000)]),
        fx.txn_xml("LIC REFUND", "2021-05-10", [(fx.HDFC1, 1000), ("lic", -1000)])])
    guard = TargetGuard.from_book(book)
    rows = [{"Date": "2025-06-01", "Description": "LIC PREMIUM", "Account": "Expenses:LIC Policy",
             "Confidence": "high", "MatchReason": "r"}]
    m._apply_target_guard(rows, guard)
    assert rows[0]["Account"] == "Expenses:LIC Policy"                  # NEGATIVE: not blanked
    assert m._DORMANT_MARKER in rows[0]["MatchReason"]


# ---------------------------------------------------------------- MAP-26(e)

def test_owner_name_only_credit_never_lands_on_rent_from_a_relative_rule():
    rules = [{"patterns": ["relativea relativeb"], "account": "Income:Rent", "confidence": "medium"}]
    # the relative's two tokens also appear inside the owner's full name
    cr = m.CompiledRules(rules, owner_tokens={"relativea", "relativeb"})
    assert m.match_rule("CREDIT RELATIVEA RELATIVEB", cr)[0] is None   # NEGATIVE


def test_relative_named_alone_still_matches_the_relatives_rule():
    rules = [{"patterns": ["relativeb"], "account": "Income:Rent", "confidence": "medium"}]
    cr = m.CompiledRules(rules, owner_tokens={"ownerzq"})
    assert m.match_rule("CREDIT RELATIVEB MARCH", cr)[0] == "Income:Rent"


def test_real_rent_credit_with_a_tenant_token_still_maps_to_rent():
    rules = [{"patterns": ["relativea relativeb"], "account": "Income:Rent", "confidence": "medium"},
             {"patterns": ["flat7"], "account": "Income:Rent", "confidence": "medium"}]
    cr = m.CompiledRules(rules, owner_tokens={"relativea", "relativeb"})
    assert m.match_rule("RENT FLAT7 RELATIVEA RELATIVEB", cr)[0] == "Income:Rent"


def test_without_owner_tokens_behaviour_is_unchanged():
    rules = [{"patterns": ["relativea relativeb"], "account": "Income:Rent", "confidence": "medium"}]
    assert m.match_rule("CREDIT RELATIVEA RELATIVEB", m.CompiledRules(rules))[0] == "Income:Rent"


def test_history_pass_abstains_when_only_owner_tokens_drive_a_non_own_target():
    pairs = [{"description": f"NEFT RELATIVEA RELATIVEB RENT {i}", "account": "Income:Rent",
              "frequency": 1} for i in range(8)]
    model = m._build_history_token_model(pairs)
    legacy = m._history_token_match("RELATIVEA RELATIVEB", model)
    assert legacy and legacy["account"] == "Income:Rent"
    got = m._history_token_match("RELATIVEA RELATIVEB", model,
                                 owner_tokens={"relativea", "relativeb"})
    assert got is None                                                  # NEGATIVE
    got = m._history_token_match("NEFT RENT RELATIVEA", model,
                                 owner_tokens={"relativea", "relativeb"})
    assert got and got["account"] == "Income:Rent"


# ---------------------------------------------------------------- MAP-30

CB = "DEBIT CARD CASH BACK | FOR ELIGIBLE SPENDS IN | MONTH OF FEB 2025"


def test_cashback_credit_goes_to_drawings_never_cash_or_income(tmp_path):
    rules = {"HSBC": [_rule("cash", "Assets:Cash", conf="high"),
                      _rule("back", "Income:Misc", conf="high")]}
    got = _apply(tmp_path, rules, [("2025-02-28", CB, "25", "")], bank_key="HSBC",
                 cashback_account="Equity:Drawings")
    assert got[0]["Account"] == "Equity:Drawings"
    assert got[0]["Confidence"] == "medium"
    assert got[0]["MatchReason"] == "card cash back = reduction in spend"
    assert got[0]["Account"] not in ("Assets:Cash", "Income:Misc")      # NEGATIVE


def test_cashback_without_a_drawings_account_goes_to_review(tmp_path):
    rules = {"HSBC": [_rule("cash", "Assets:Cash", conf="high")]}
    got = _apply(tmp_path, rules, [("2025-02-28", CB, "25", "")], bank_key="HSBC")
    assert got[0]["Account"] == "" and got[0]["Confidence"] == "none"   # NEGATIVE: not Cash
    assert "cash back" in got[0]["MatchReason"].lower()


def test_atm_cash_withdrawal_debit_still_lands_on_cash(tmp_path):
    rules = {"HSBC": [_rule("atm cash wdl", "Assets:Cash"), _rule("cash withdrawal", "Assets:Cash")]}
    got = _apply(tmp_path, rules, [("2025-02-03", "ATM CASH WDL 123", "", "500"),
                                   ("2025-02-04", "CASH WITHDRAWAL SELF", "", "500")],
                 bank_key="HSBC", cashback_account="Equity:Drawings")
    assert [g["Account"] for g in got] == ["Assets:Cash", "Assets:Cash"]


def test_debit_mentioning_cash_back_is_not_cash_back(tmp_path):
    got = _apply(tmp_path, {"HSBC": []}, [("2025-02-03", "POS PURCHASE WITH CASH BACK", "", "500")],
                 bank_key="HSBC", cashback_account="Equity:Drawings")
    assert got[0]["Account"] != "Equity:Drawings"                       # NEGATIVE
    assert m._is_card_cashback_credit("CASHBACK", {"Deposit": "", "Withdrawal": "9"}) is False
    assert m._is_card_cashback_credit("CASHBACK", {"Deposit": "9", "Withdrawal": ""}) is True


def test_find_drawings_account():
    assert m.find_drawings_account(["Root Account:Equity:Drawings", "Expenses:Dining"]) == "Equity:Drawings"
    assert m.find_drawings_account(["Expenses:Dining"]) is None
    assert m.find_drawings_account(["Equity:Drawings", "Equity:Partner:Drawings"]) is None  # ambiguous


# ---------------------------------------------------------------- MAP-29

OWN1 = "Assets:Cash:HDFC Bank - 094XXXX1234"
OWN2 = "Assets:Cash:HDFC Bank - 094XXXX5678"
SRC = "Assets:Cash:HSBC Bank - 013065XXXX-001"
OWN_NAME = "ownerzq"


def _hdfc_model():
    pairs = [{"description": f"NEFT {OWN_NAME} SAVINGS SWEEP QQ{i}", "account": OWN1, "frequency": 1}
             for i in range(8)]
    pairs += [{"description": f"SHOP{i} GROCERY PURCHASE", "account": "Expenses:Groc", "frequency": 1}
              for i in range(24)]
    return m._build_history_token_model(pairs), {OWN1, OWN2, SRC}


def _match(desc):
    model, own = _hdfc_model()
    targets = m._own_target_accounts(own, own)
    vocab = {OWN_NAME, "savings", "sweep"}

    def ev(d):
        return m._has_own_transfer_evidence(d, vocab, targets)
    return m._history_token_match(desc, model, own_bank_accounts=own, source_account=SRC,
                                  own_evidence=ev, own_targets=targets)


def test_numbers_extracted():
    assert m._narration_account_numbers("X | HDFC0001234/50100098765 | REF9") == ["50100098765"]
    assert m._narration_account_numbers("TO XXXX1234 NEFT") == ["1234"]
    assert m._narration_account_numbers("NEFT SAVINGS SWEEP REF 99887766") == []


def test_non_own_hdfc_account_number_never_routes_to_own_hdfc():
    got = _match(f"NEFT {OWN_NAME} SAVINGS SWEEP HDFC0001234/50100098765")
    assert got is None or got.get("account") not in (OWN1, OWN2)        # NEGATIVE


def test_non_own_masked_number_never_routes_to_own_hdfc():
    got = _match(f"NEFT {OWN_NAME} SAVINGS SWEEP HDFC0001234 XXXX9999")
    assert got is None or got.get("account") not in (OWN1, OWN2)        # NEGATIVE


def test_own_masked_number_still_routes():
    got = _match(f"NEFT {OWN_NAME} SAVINGS SWEEP HDFC0001234 XXXX1234")
    assert got and got["account"] == OWN1


def test_number_of_a_different_own_account_reroutes_there():
    got = _match(f"NEFT {OWN_NAME} SAVINGS SWEEP HDFC0001234 XXXX5678")
    assert got and got["account"] == OWN2


def test_no_account_number_behaves_as_today():
    got = _match(f"NEFT {OWN_NAME} SAVINGS SWEEP HDFC0001234")
    assert got and got["account"] == OWN1
