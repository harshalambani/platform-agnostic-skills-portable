"""
MAP-14 -- the AI fallback must not book a third-party payment to the owner's
OWN bank / FD account. An own account needs own-transfer evidence (the
"xfer to self" marker, an own-history VPA/name token, or one of the own
accounts' numbers in the narration). Otherwise the row goes to Suspense.

Also: the reason must never read "LLM: LLM: ...".

Synthetic book (see gnc_book_fixture): several own accounts at one bank, a
hidden one, and an FD account beside them.
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

from agents.skill_gnucash_account_mapper import agent as mapper  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402

FD = "Assets:Current Assets:Cash and Bank:FD - 5551234"
FD_ACC = fx.account_xml("fd1", "FD - 5551234", "ASSET", "cab")


def _hist(desc, target_id, n, bank=fx.HDFC1):
    return [fx.txn_xml(desc, "2025-06-10", [(bank, -10000), (target_id, 10000)])
            for _ in range(n)]


def _run(tmp_path, monkeypatch, txns, desc, llm_account, extra=()):
    book = fx.write_book(tmp_path / "b.gnucash",
                         fx.standard_accounts([FD_ACC, *extra]), txns)
    monkeypatch.chdir(tmp_path)
    (tmp_path / "settings").mkdir(exist_ok=True)
    cfg = str(tmp_path / "settings" / "config.yaml")

    def fake_llm(unmatched_rows, **kw):
        return {r["row"]: {"account": llm_account, "reason": "LLM: fake"}
                for r in unmatched_rows}
    monkeypatch.setattr(mapper, "llm_fallback_mapping", fake_llm)
    csv_in = fx.canonical_csv(tmp_path / "in.csv", [("2025-08-01", desc, "", "100.00")])
    out = tmp_path / "out.csv"
    mapper.run(book, csv_in, str(out), config_path=cfg, bank_name="HDFC",
               gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[0]


BASE = _hist("SOMETHING ELSE", "groc", 1)


@pytest.mark.parametrize("own", [fx.P_HDFC2, fx.P_HSBC1, FD])
def test_third_party_row_never_lands_on_an_own_account_via_llm(tmp_path, monkeypatch, own):
    """NEGATIVE: a third-party UPI payment the LLM wants on an own bank or FD
    account goes to Suspense instead."""
    r = _run(tmp_path, monkeypatch, BASE, "UPI/PAYMENT XQZJ MERCHANT/4471", "Root Account:" + own)
    assert r["Confidence"] == "suspense"
    assert r["Account"] not in (own, "Root Account:" + own)
    assert "Suspense" in r["Account"]
    assert "your own account" in r["MatchReason"]


def test_xfer_to_self_marker_still_allows_an_own_account(tmp_path, monkeypatch):
    """The genuine own transfer still works (the guard is not a blanket ban)."""
    r = _run(tmp_path, monkeypatch, BASE, "XFER TO SELF QZ", "Root Account:" + fx.P_HDFC2)
    assert r["Confidence"] == "llm"
    assert r["Account"] == fx.P_HDFC2


def test_own_account_number_in_narration_is_evidence(tmp_path, monkeypatch):
    r = _run(tmp_path, monkeypatch, BASE, "NEFT TO A/C 094XXXX5678 QZ", "Root Account:" + fx.P_HDFC2)
    assert r["Confidence"] == "llm" and r["Account"] == fx.P_HDFC2


def test_year_like_digits_are_not_own_account_evidence(tmp_path, monkeypatch):
    """NEGATIVE: '2025' is not an account number, even if an own account name carried it."""
    yr = fx.account_xml("y", "HDFC Bank - 094XXXX2025", "BANK", "cab")
    r = _run(tmp_path, monkeypatch, BASE, "FY 2025 PAYMENT XQZJ", "Root Account:" + fx.P_HDFC2,
             extra=[yr])
    assert r["Confidence"] == "suspense"


def test_reason_never_carries_a_doubled_llm_prefix(tmp_path, monkeypatch):
    """NEGATIVE: 'LLM: LLM:' must not appear."""
    r = _run(tmp_path, monkeypatch, BASE, "UNSEEN MERCHANT XYZ", "Root Account:Expenses:Food:Dining")
    assert r["Confidence"] == "llm"
    assert "LLM: LLM:" not in r["MatchReason"]
    assert r["MatchReason"].startswith("LLM:")


def test_non_own_account_needs_no_evidence(tmp_path, monkeypatch):
    """NEGATIVE: the gate does not touch ordinary expense targets."""
    r = _run(tmp_path, monkeypatch, BASE, "UPI/PAYMENT XQZJ", "Root Account:Expenses:Food:Dining")
    assert r["Confidence"] == "llm" and r["Account"] == "Expenses:Food:Dining"


def test_llm_reason_prefixes_exactly_once():
    assert mapper._llm_reason("LLM: matched") == "LLM: matched"
    assert mapper._llm_reason("llm: skip") == "llm: skip"
    assert mapper._llm_reason("close fit") == "LLM: close fit"


# -- vocabulary derived from the book ---------------------------------------

def _pairs(rows):
    return [{"description": d, "account": a, "frequency": 1} for d, a in rows]


OWN = {fx.P_HDFC2, FD}


def test_vocab_learns_owner_vpa_from_own_account_history():
    pairs = _pairs([("UPI/ownerqz@ybl/SELF", fx.P_HDFC2),
                    ("UPI/ownerqz@ybl/FD TOPUP", FD),
                    ("UPI/SHOP1", "Expenses:Food:Dining")])
    v = mapper._build_own_transfer_vocab(pairs, OWN)
    assert "ownerqz@ybl" in v
    assert mapper._has_own_transfer_evidence("UPI/ownerqz@ybl/X", v, OWN)


def test_vocab_ignores_a_token_shared_with_third_party_rows():
    """NEGATIVE: a token seen on own AND third-party rows is not the owner's."""
    pairs = _pairs([("UPI/SHARED/1", fx.P_HDFC2), ("UPI/SHARED/2", FD),
                    ("UPI/SHARED/3", "Expenses:Food:Dining")])
    v = mapper._build_own_transfer_vocab(pairs, OWN)
    assert "shared" not in v
    assert not mapper._has_own_transfer_evidence("UPI/SHARED/9", v, OWN)


def test_vocab_ignores_a_channel_word_present_in_most_rows():
    """NEGATIVE: 'upi' on nearly every row is a channel, not the owner."""
    rows = [(f"UPI/M{i}", "Expenses:Food:Dining") for i in range(10)]
    rows += [("UPI/SELFA", fx.P_HDFC2), ("UPI/SELFB", FD)]
    v = mapper._build_own_transfer_vocab(_pairs(rows), OWN)
    assert "upi" not in v


def test_vocab_ignores_one_off_short_and_numeric_tokens():
    """NEGATIVE: single-occurrence, 2-letter, numeric and IFSC tokens never count."""
    pairs = _pairs([("AB 123456 HDFC0001234 ONCE", fx.P_HDFC2),
                    ("AB 123456 HDFC0001234", FD)])
    v = mapper._build_own_transfer_vocab(pairs, OWN)
    assert not ({"ab", "123456", "hdfc0001234"} & v)


def test_no_evidence_without_history_or_marker():
    assert not mapper._has_own_transfer_evidence("UPI/SOME SHOP", set(), OWN)
    assert mapper._has_own_transfer_evidence("Xfer to self", set(), OWN)


def test_own_targets_include_fd_beside_bank_but_not_elsewhere():
    all_accts = [fx.P_HDFC2, FD, "Assets:Investments:Mutual Fund X", "Expenses:Food:Dining"]
    got = mapper._own_target_accounts({fx.P_HDFC2}, all_accts)
    assert FD in got and fx.P_HDFC2 in got
    assert "Assets:Investments:Mutual Fund X" not in got
