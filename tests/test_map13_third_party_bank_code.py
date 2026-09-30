"""
MAP-13 -- a third party's bank code must not route a row to YOUR account.

The IFSC literal-code fallback (_history_token_match) used to fire on any
narration whose ordinary words looked self-transfer-shaped, so "NEFT <vendor>
HSBC0..." landed on the owner's HSBC account. It now needs own-transfer
evidence derived from THIS book (own-history vocabulary, the 'xfer to self'
marker, or an own account number). Without it the route abstains, and no later
pass (smart, weak, AI) may put the row on that own account either.

Synthetic book (gnc_book_fixture). No real names or numbers.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402
from scripted_llm import ScriptedLLM, choose  # noqa: E402

OWN_NAME = "ownerzq"        # synthetic stand-in for the owner's name in narrations


def _t(desc, target, n=1):
    return [fx.txn_xml(desc, "2025-06-10", [(fx.HDFC1, -10000), (target, 10000)])
            for _ in range(n)]


def _accounts():
    # one HSBC account (drop HSBC2) so the bank code has a single own target
    return [a for a in fx.standard_accounts() if "013065XXXX-002" not in a]


def _history():
    h = []
    # the owner's own transfers are split across two own accounts, so the
    # ordinary history score cannot pick one and the IFSC fallback is what decides
    for i in range(4):
        h += _t(f"NEFT {OWN_NAME} SAVINGS SWEEP QQ{i}", fx.HSBC1)
        h += _t(f"NEFT {OWN_NAME} SAVINGS SWEEP QR{i}", fx.HDFC4)
    h += _t("NEFT VENDORONE INVOICE", "groc")
    h += _t("NEFT VENDORTWO INVOICE", "groc")
    for i in range(24):      # ordinary spending: keeps the owner's name a rare token
        h += _t(f"SHOP{i} GROCERY PURCHASE", "groc")
    return h


def _run(tmp_path, desc, txns=None, config_path=None, name="r"):
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    book = fx.write_book(d / "b.gnucash", _accounts(), txns if txns is not None else _history())
    csv_in = fx.canonical_csv(d / "in.csv", [("2025-08-01", desc, "", "100.00")])
    out = d / "out.csv"
    m.run(book, csv_in, str(out), config_path=config_path, bank_name="HDFC",
          gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[0]


def test_owner_named_deposit_with_own_bank_code_still_routes(tmp_path, monkeypatch):
    """(a) POSITIVE: the owner's history-learned name plus the own bank's IFSC."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, f"NEFT CR {OWN_NAME} HSBC0NEW456")
    assert r["Account"] == fx.P_HSBC1
    assert r["Confidence"] == "history"
    assert "Bank-code match" in r["MatchReason"]     # via the gated fallback itself


def test_xfer_to_self_with_own_bank_code_still_routes(tmp_path, monkeypatch):
    """(b) POSITIVE: the explicit self marker plus an IFSC."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, "XFER TO SELF NEFT HSBC0NEW456")
    assert r["Account"] == fx.P_HSBC1
    assert r["Confidence"] == "history"
    assert "Bank-code match" in r["MatchReason"]


def test_third_party_with_own_bank_code_is_not_routed_by_any_pass(tmp_path, monkeypatch):
    """NEGATIVE: a vendor paid via a bank where the owner ALSO holds an account,
    no owner vocabulary and no self marker -> not booked to the own account."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, "NEFT ACMEVENDOR PAYMENT HSBC0NEW456")
    assert r["Account"] != fx.P_HSBC1
    assert "HSBC" not in r["Account"]
    assert r["Confidence"] != "history"


@pytest.mark.parametrize("desc", [
    "NEFT HSBC INSURANCE PREMIUM HSBC0NEW456",     # names the bank as a word too
    "IMPS HSBC0NEW456 ACMEVENDOR",                 # different channel word
    "NEFT OWNERZQ2 HSBC0NEW456",                   # a near-miss of the owner's name
])
def test_other_third_party_shapes_are_not_booked_to_the_own_account(tmp_path, monkeypatch, desc):
    """NEGATIVE: smart/weak/history passes do not book these to the own HSBC account."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, desc)
    assert "HSBC" not in r["Account"]
    assert r["Confidence"] in ("suspense", "none")


def test_the_same_third_party_row_is_not_rescued_by_the_ai_pass(tmp_path, monkeypatch):
    """NEGATIVE: the AI wanting the own HSBC account for it is withheld too."""
    h = ScriptedLLM(monkeypatch, tmp_path)
    try:
        h.say("ACMEVENDOR", choose(fx.P_HSBC1))
        r = _run(tmp_path, "NEFT ACMEVENDOR PAYMENT HSBC0NEW456", config_path=h.config_path)
        assert r["Account"] != fx.P_HSBC1
        assert r["Confidence"] == "suspense"
    finally:
        h.close()


def test_the_matcher_abstains_without_evidence_and_routes_with_it():
    pairs = [{"description": d, "account": a, "frequency": 1} for d, a in [
        (f"NEFT {OWN_NAME} SAVINGS SWEEP QQ{i}", "Assets:Cash:HSBC Bank - 0004002")
        for i in range(8)]] + [
        {"description": "NEFT VENDORONE INVOICE", "account": "Expenses:Groc", "frequency": 1},
        {"description": "NEFT VENDORTWO INVOICE", "account": "Expenses:Groc", "frequency": 1}] + [
        {"description": f"SHOP{i} GROCERY PURCHASE", "account": "Expenses:Groc", "frequency": 1}
        for i in range(24)]
    own = {"Assets:Cash:HSBC Bank - 0004002"}
    model = m._build_history_token_model(pairs)
    targets = m._own_target_accounts(own, own)
    vocab = m._build_own_transfer_vocab(pairs, targets)
    assert OWN_NAME in vocab and "neft" not in vocab

    def ev(d):
        return m._has_own_transfer_evidence(d, vocab, targets)

    third = "NEFT ACMEVENDOR PAYMENT HSBC0NEW456"
    assert m._history_token_match(third, model, own_bank_accounts=own) is not None  # legacy: ungated
    assert m._history_token_match(third, model, own_bank_accounts=own, own_evidence=ev) is None
    mine = f"NEFT CR {OWN_NAME} HSBC0NEW456"
    got = m._history_token_match(mine, model, own_bank_accounts=own, own_evidence=ev)
    assert got and got["account"] == "Assets:Cash:HSBC Bank - 0004002"
