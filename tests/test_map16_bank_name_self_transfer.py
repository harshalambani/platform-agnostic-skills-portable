"""
MAP-16 -- "xfer to self <bank>" with no IFSC. The bank word is taken from the
owner's own postable account names; a tie between two accounts at one bank is
sent to Review, never guessed; a hidden account is never the route; a
third-party row that merely names the bank is not routed.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402

HSBC3_HIDDEN = fx.account_xml("hsbc3", "HSBC Bank - 013065XXXX-003 (old)", "BANK", "cab", ["hidden"])
P_HSBC3 = "Assets:Current Assets:Cash and Bank:HSBC Bank - 013065XXXX-003 (old)"


def _t(desc, target):
    return fx.txn_xml(desc, "2025-06-10", [(fx.HDFC1, -10000), (target, 10000)])


def _run(tmp_path, monkeypatch, desc, accounts, txns):
    book = fx.write_book(tmp_path / "b.gnucash", accounts, txns)
    monkeypatch.chdir(tmp_path)
    csv_in = fx.canonical_csv(tmp_path / "in.csv", [("2025-08-01", desc, "", "100.00")])
    out = tmp_path / "out.csv"
    m.run(book, csv_in, str(out), config_path=None, bank_name="HDFC",
          gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[0]


def _accounts(drop=(), extra=()):
    return [a for a in fx.standard_accounts(extra) if not any(d in a for d in drop)]


def _hist(*targets):
    return [_t("MISC PAYMENT QQ" + str(i), t) for i, t in enumerate(targets)]


def test_single_postable_hsbc_account_is_routed(tmp_path, monkeypatch):
    accs = _accounts(drop=["013065XXXX-002"])
    r = _run(tmp_path, monkeypatch, "XFER TO SELF HSBC", accs, _hist(fx.HSBC1, "groc"))
    assert r["Account"] == fx.P_HSBC1
    assert r["Confidence"] == "history"


def test_tie_between_two_hsbc_accounts_is_not_auto_resolved(tmp_path, monkeypatch):
    """NEGATIVE: two HSBC accounts -> neither is picked; the row is a Review row."""
    r = _run(tmp_path, monkeypatch, "XFER TO SELF HSBC", _accounts(),
             _hist(fx.HSBC1, fx.HSBC2, "groc"))
    assert "HSBC" not in r["Account"]
    assert r["Confidence"] == "suspense"
    assert "2 accounts" in r["MatchReason"] and "review" in r["MatchReason"].lower()


def test_third_party_row_naming_the_bank_is_not_routed(tmp_path, monkeypatch):
    """NEGATIVE: no 'xfer to self' marker -> the bank word alone routes nothing."""
    accs = _accounts(drop=["013065XXXX-002"])
    r = _run(tmp_path, monkeypatch, "NEFT TO HSBC INSURANCE PREMIUM", accs,
             _hist(fx.HSBC1, "groc"))
    assert r["Account"] != fx.P_HSBC1
    assert r["Confidence"] != "history"


def test_hidden_hsbc_account_is_never_the_route(tmp_path, monkeypatch):
    """NEGATIVE: only a hidden HSBC account exists -> no route to it."""
    accs = _accounts(drop=["013065XXXX-001", "013065XXXX-002"], extra=[HSBC3_HIDDEN])
    r = _run(tmp_path, monkeypatch, "XFER TO SELF HSBC", accs, _hist("hsbc3", "groc"))
    assert r["Account"] != P_HSBC3 and "(old)" not in r["Account"]
    assert r["Confidence"] == "suspense"


def test_hidden_account_does_not_create_a_tie_with_a_postable_one(tmp_path, monkeypatch):
    """The postable HSBC account is still routed; the hidden twin is ignored."""
    accs = _accounts(drop=["013065XXXX-002"], extra=[HSBC3_HIDDEN])
    r = _run(tmp_path, monkeypatch, "XFER TO SELF HSBC", accs,
             _hist(fx.HSBC1, "hsbc3", "groc"))
    assert r["Account"] == fx.P_HSBC1


def test_a_word_shared_by_every_own_account_never_routes():
    """NEGATIVE: 'bank' is in every own account name, so it identifies none."""
    own = {fx.P_HSBC1, fx.P_HDFC2}
    assert m._bank_name_self_transfer("XFER TO SELF BANK", {"xfer", "self", "bank"}, own, None) is None


def test_source_account_is_never_its_own_route():
    own = {fx.P_HSBC1}
    assert m._bank_name_self_transfer(
        "XFER TO SELF HSBC", {"hsbc"}, own, "Root Account:" + fx.P_HSBC1) is None


def test_unit_tie_shape_and_single_shape():
    own = {fx.P_HSBC1, fx.P_HSBC2, fx.P_HDFC2}
    tie = m._bank_name_self_transfer("Xfer to self hsbc", {"hsbc"}, own, None)
    assert tie["account"] == "" and sorted(tie["tie"]) == sorted([fx.P_HSBC1, fx.P_HSBC2])
    one = m._bank_name_self_transfer("Xfer to self hsbc", {"hsbc"}, {fx.P_HSBC1, fx.P_HDFC2}, None)
    assert one["account"] == fx.P_HSBC1 and one["confidence"] == "history"
