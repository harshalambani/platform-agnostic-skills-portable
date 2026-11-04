"""
IMP-10 -- the Bank account dropdown / automatic pick / chosen_account use
BANK-type accounts only (the mapper's structural rule). An FD or a fund named
after the bank is typically ASSET and is never offered or accepted while a
BANK-typed account exists. ASSET-typed accounts are a fallback only when the
bank has no BANK-type account, and then a visible warning is attached.

Also pins the side effect of typing an FD as ASSET: the mapper still maps
sweep rows to the FD from history.
All data synthetic; masked numbers; tmp_path only.
"""
from __future__ import annotations

import csv
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402
from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402

CAB = "Assets:Current Assets:Cash and Bank:"
P_SAV = CAB + "ICICI Bank - 013065XXXX-001"
P_FD = CAB + "ICICI FD - 013065XXXX-FD1"
P_MF = CAB + "ICICI Pru Growth Fund - 094XXXX7777"


def _acc(aid, name, atype, flags=()):
    return fx.account_xml(aid, name, atype, "cab", flags)


def _book(tmp_path, extra, txns=()):
    return fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(extra), list(txns))


SAV = _acc("isav", "ICICI Bank - 013065XXXX-001", "BANK")
FD_ASSET = _acc("ifd", "ICICI FD - 013065XXXX-FD1", "ASSET")
FD_BANK = _acc("ifd", "ICICI FD - 013065XXXX-FD1", "BANK")
MF_ASSET = _acc("imf", "ICICI Pru Growth Fund - 094XXXX7777", "ASSET")


def test_dropdown_lists_only_bank_type_accounts(tmp_path):
    """NEGATIVE (a): ASSET-typed FD and MF named after the bank are not listed."""
    book = _book(tmp_path, [SAV, FD_ASSET, MF_ASSET])
    assert pipe.postable_bank_accounts(book, "ICICI") == [P_SAV]


def test_asset_typed_fd_and_mf_are_refused_as_chosen_account(tmp_path):
    """NEGATIVE (a): picking the FD or the MF is refused, with a clear message."""
    book = _book(tmp_path, [SAV, FD_ASSET, MF_ASSET])
    for bad in (P_FD, P_MF):
        res = pipe._get_gnucash_account_balance(book, "ICICI", chosen_account=bad)
        assert res["found"] is False and res["refused"] is True
        assert "not a BANK-type account" in res["match_warning"]
        assert P_SAV in res["match_warning"]


def test_bank_typed_savings_is_still_found_and_chosen(tmp_path):
    """(b) positive: auto pick and explicit pick both resolve to the savings account."""
    book = _book(tmp_path, [SAV, FD_ASSET, MF_ASSET])
    auto = pipe._get_gnucash_account_balance(book, "ICICI")
    assert auto["found"] and auto["account_name"].endswith("ICICI Bank - 013065XXXX-001")
    assert not auto["ambiguous"]
    chosen = pipe._get_gnucash_account_balance(book, "ICICI", chosen_account=P_SAV)
    assert chosen["found"] and not chosen["refused"]


def test_bank_typed_fd_is_still_a_candidate_as_before(tmp_path):
    """A BANK-typed FD stays listed (type is the only rule; the user re-types it)."""
    book = _book(tmp_path, [SAV, FD_BANK])
    assert sorted(pipe.postable_bank_accounts(book, "ICICI")) == sorted([P_SAV, P_FD])


def test_only_asset_typed_account_works_via_fallback_with_warning(tmp_path):
    """(c): no BANK-type match -> ASSET fallback, and a visible warning."""
    book = _book(tmp_path, [FD_ASSET])
    assert pipe.postable_bank_accounts(book, "ICICI") == [P_FD]
    res = pipe._get_gnucash_account_balance(book, "ICICI")
    assert res["found"] and res["account_name"].endswith("FD1")
    assert "No BANK-type account" in (res["match_warning"] or "")
    ok = pipe._get_gnucash_account_balance(book, "ICICI", chosen_account=P_FD)
    assert ok["found"] and "No BANK-type account" in (ok["match_warning"] or "")


def test_no_fallback_warning_when_a_bank_type_account_exists(tmp_path):
    """NEGATIVE: the fallback warning is not shown on the normal path."""
    book = _book(tmp_path, [SAV, FD_ASSET])
    res = pipe._get_gnucash_account_balance(book, "ICICI")
    assert res["found"] and not res["match_warning"]


def test_hidden_bank_typed_account_is_never_listed(tmp_path):
    """NEGATIVE (d): IMP-09 guard kept. A hidden BANK account is never a candidate."""
    hidden = _acc("ihid", "ICICI Bank - 013065XXXX-009 (old)", "BANK", ["hidden"])
    book = _book(tmp_path, [SAV, hidden])
    assert pipe.postable_bank_accounts(book, "ICICI") == [P_SAV]
    res = pipe._get_gnucash_account_balance(
        book, "ICICI", chosen_account=CAB + "ICICI Bank - 013065XXXX-009 (old)")
    assert res["refused"] is True


def test_uses_the_extractors_structural_rule_not_a_second_one():
    import agents.skill_gnucash_xml_extractor.agent as ext
    assert pipe._is_structural_bank_account is ext._is_structural_bank_account


# ---- side effect: the FD left own_bank_accounts when typed ASSET ------------

def _sweep_book(tmp_path, fd_account):
    txns = [fx.txn_xml("AUTO SWEEP TRF TO FD 0130XX", "2025-%02d-10" % (i + 1),
                       [("isav", -500000), ("ifd", 500000)]) for i in range(8)]
    txns.append(fx.txn_xml("CAFE ALPHA ORDER", "2025-06-11", [("isav", -10000), ("groc", 10000)]))
    return _book(tmp_path, [SAV, fd_account], txns)


def _map(tmp_path, monkeypatch, desc, fd_account):
    book = _sweep_book(tmp_path, fd_account)
    monkeypatch.chdir(tmp_path)
    csv_in = fx.canonical_csv(tmp_path / "in.csv", [("2025-09-01", desc, "", "5000.00")])
    out = tmp_path / "out.csv"
    m.run(book, csv_in, str(out), config_path=None, bank_name="ICICI",
          gnucash_bank_account=P_SAV)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[0]


def test_sweeps_still_map_to_an_asset_typed_fd_from_history(tmp_path, monkeypatch):
    """The FD is ASSET-typed (outside own_bank_accounts): new sweep rows still
    map to it from history, not Suspense."""
    r = _map(tmp_path, monkeypatch, "AUTO SWEEP TRF TO FD 0130XX", FD_ASSET)
    assert r["Account"] == P_FD and r["Confidence"] == "history"


def test_sweep_route_is_the_same_for_asset_and_bank_typed_fd(tmp_path, monkeypatch):
    a = _map(tmp_path / "a" if (tmp_path / "a").mkdir() is None else tmp_path,
             monkeypatch, "AUTO SWEEP TRF TO FD 0130XX", FD_ASSET)
    b = _map(tmp_path / "b" if (tmp_path / "b").mkdir() is None else tmp_path,
             monkeypatch, "AUTO SWEEP TRF TO FD 0130XX", FD_BANK)
    assert (a["Account"], a["Confidence"], a["MatchReason"]) == \
           (b["Account"], b["Confidence"], b["MatchReason"])


def test_asset_typed_fd_does_not_send_sweeps_to_suspense(tmp_path, monkeypatch):
    """NEGATIVE: the wrong behaviour (Suspense / another account) does not occur."""
    r = _map(tmp_path, monkeypatch, "AUTO SWEEP TRF TO FD 0130XX", FD_ASSET)
    assert r["Confidence"] != "suspense" and r["Account"] != P_SAV


def test_self_transfer_wording_route_is_the_same_for_both_fd_types(tmp_path, monkeypatch):
    """Finding: even a marker-style 'XFER TO SELF ICICI' row lands on the FD the
    same way whether the FD is BANK- or ASSET-typed (weak keyword pass)."""
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _map(tmp_path / "a", monkeypatch, "XFER TO SELF ICICI", FD_ASSET)
    b = _map(tmp_path / "b", monkeypatch, "XFER TO SELF ICICI", FD_BANK)
    assert (a["Account"], a["Confidence"]) == (b["Account"], b["Confidence"])
    assert a["Account"] == P_FD
