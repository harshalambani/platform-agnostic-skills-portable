"""
MAP-26 round 2 -- the owner's plain NAME alone is not own-transfer evidence on
ANY pass (history/Bayes, smart, weak prefix, AI), not only the AI gate.

Legitimate own-evidence routes that must keep working: the self marker (full or
field-truncated), an own VPA/handle, the owner token with a bank word naming an
own account, an own account-number digit run, and the owner token with a sweep /
FD / closure keyword (the real "AUTOSWEEP TO <fd no> <OWNER ...>" shape).

Synthetic and masked: no real names or numbers.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402
import test_map26_own_bank_word as t26  # noqa: E402
import test_map21_fd_outside_bank_folder as t21  # noqa: E402

OWN = t26.OWN
INSURER_DEP = f"ACMEINSURER CLAIM {OWN.upper()}"
EMPLOYER = f"NEFT {OWN.upper()} ACMEEMPLOYER"


def _hist_bank():
    """History where the owner's name rode on transfers to own HDFC2."""
    return t26._history(narr="{o} FUNDS MOVE", target=fx.HDFC2)


def _hist_fd():
    """History with the real sweep shape landing on the own FD, plus bank rows."""
    h = t26._history(narr="{o} FUNDS MOVE", target=fx.HDFC2)
    for i in range(4):
        h.append(t26._t(f"AUTOSWEEP TO 5551234 {OWN} MIDDLEQ {OWN} R{i}", "fd"))
    return h


def _acc_fd():
    return t26._accounts(extra=t21.INV)


def _row(tmp_path, monkeypatch, desc, hist, accounts=None, **k):
    return t26._one(tmp_path, monkeypatch, desc,
                    accounts if accounts is not None else _acc_fd(), hist, **k)


def _off_own(r):
    a = r["Account"]
    return not (a.endswith("094XXXX5678") or "HSBC Bank" in a or "FD - 5551234" in a
                or "HDFC Bank" in a)


# ---- history pass: name-only must not land ------------------------------------------

@pytest.mark.parametrize("desc", [INSURER_DEP, EMPLOYER])
def test_history_pass_name_only_deposit_does_not_land_on_an_own_bank(tmp_path, monkeypatch, desc):
    r = _row(tmp_path, monkeypatch, desc, _hist_bank())
    assert _off_own(r), r["Account"]
    assert r["Confidence"] != "history"


@pytest.mark.parametrize("desc", [INSURER_DEP, EMPLOYER])
def test_history_pass_name_only_deposit_does_not_land_on_the_own_fd(tmp_path, monkeypatch, desc):
    hist = _hist_fd() + [t26._t(f"FD BOOKING {OWN} Z{i}", "fd") for i in range(3)]
    r = _row(tmp_path, monkeypatch, desc, hist)
    assert _off_own(r), r["Account"]
    assert r["Confidence"] != "history"


def test_to_owner_still_reverts(tmp_path, monkeypatch):
    r = _row(tmp_path, monkeypatch, "FTTransferP2B/ACMEINSURER to OWNERZQ", _hist_bank())
    assert _off_own(r)
    assert r["Confidence"] == "suspense"


# ---- smart pass ------------------------------------------------------------------------

def _force_smart(monkeypatch, acct):
    monkeypatch.setattr(
        m, "smart_pattern_match",
        lambda desc, accts, w, d: {"account": "Root Account:" + acct,
                                   "reason": "forced guess", "confidence": "smart"})


def test_smart_pass_name_only_guess_onto_an_own_bank_is_refused(tmp_path, monkeypatch):
    _force_smart(monkeypatch, fx.P_HDFC2)
    r = _row(tmp_path, monkeypatch, INSURER_DEP, t26._history())
    assert r["Account"] != fx.P_HDFC2
    assert r["Confidence"] == "suspense"


def test_smart_pass_guess_onto_an_own_bank_with_a_marker_still_lands(tmp_path, monkeypatch):
    _force_smart(monkeypatch, fx.P_HDFC2)
    r = _row(tmp_path, monkeypatch, f"XFER TO SELF {OWN.upper()} ZZ9", t26._history())
    assert r["Account"] == fx.P_HDFC2


# ---- evidence unit ---------------------------------------------------------------------

VOCAB = {"ownerzq", "ownerzq9@ybl"}
OWNB = {"Assets:Cash:HDFC Bank - 0941235678", "Assets:Cash:HSBC Bank - 0131234567"}


FDT = "Assets:Investments:Fixed Deposits:ICICI FD - 5551234"   # an FD target
BANKT = "Assets:Cash:HDFC Bank - 0941235678"                    # an own bank target


def _ev(desc, name_alone=False, target=None):
    return m._has_own_transfer_evidence(desc, VOCAB, set(OWNB), set(OWNB), None, name_alone,
                                        target=target)


def test_plain_name_is_not_evidence_when_name_alone_is_false():
    assert _ev("ACMEINSURER CLAIM OWNERZQ") is False
    assert _ev("NEFT OWNERZQ ACMEEMPLOYER") is False
    assert _ev("NEFT OWNERZQ ACMEEMPLOYER", name_alone=True) is True   # legacy callers only


@pytest.mark.parametrize("desc", [
    "XFER TO SELF",
    "ownerzq/xfer to se/HDFC",                        # truncated marker
    "UPI OWNERZQ9@YBL PAYMENT",                       # own handle
    "NEFT OWNERZQ HDFC",                              # owner token + bank word of an own account
    "NEFT TO 0941235678",                             # own account number
])
def test_legitimate_evidence_routes_still_count(desc):
    assert _ev(desc) is True, desc


@pytest.mark.parametrize("desc", [
    "AUTOSWEEP TO 9990001 OWNERZQ MIDDLEQ OWNERZQ",   # owner + sweep keyword
    "FD BOOKING OWNERZQ",
    "FD CLOSURE OWNERZQ",
])
def test_owner_plus_fd_keyword_counts_for_an_fd_target_only(desc):
    assert _ev(desc, target=FDT) is True, desc
    # round 3: the FD keyword says nothing about an own savings account
    assert _ev(desc, target=BANKT) is False, desc
    assert _ev(desc) is False, desc


@pytest.mark.parametrize("desc", [
    "FD BOOKING ACMEVENDOR",                          # weak keyword without the owner
    "ACMEINSURER CLAIM OWNERZQ",
    "ACMEVENDOR to ACMEOTHER",
])
@pytest.mark.parametrize("target", [None, FDT, BANKT])
def test_keyword_without_owner_or_name_alone_is_not_evidence(desc, target):
    assert _ev(desc, target=target) is False, (desc, target)


@pytest.mark.parametrize("desc", [
    "AUTOSWEEP ACMEVENDOR", "123456789012: Rev Sweep From", "123456789012: Closure Proceeds",
    "PREMAT CLOSURE 123",
])
def test_strong_fd_keyword_alone_is_evidence_for_an_fd_target_only(desc):
    assert _ev(desc, target=FDT) is True, desc
    assert _ev(desc, target=BANKT) is False, desc      # NEGATIVE: not for own savings
    assert _ev(desc, target="Assets:Cash and Bank:Cash") is False, desc
    assert _ev(desc) is False, desc


@pytest.mark.parametrize("desc", [
    "CASH WDL/T0012/ACMETOWN/01-08", "CAM/123456/CASH WDL/ACMETOWN", "ATM WDL 0012", "NWD-123"])
def test_cash_withdrawal_keyword_alone_is_evidence_for_the_cash_account_only(desc):
    cash = "Assets:Current Assets:Cash and Bank:Cash"
    assert _ev(desc, target=cash) is True, desc
    assert _ev(desc, target=BANKT) is False, desc      # NEGATIVE
    assert _ev(desc, target=FDT) is False, desc        # NEGATIVE
    assert _ev(desc) is False, desc


# ---- the real shape: AUTOSWEEP TO <fd no> <OWNER> <MIDDLE> <OWNER> ---------------------

SWEEP = f"AUTOSWEEP TO 5551234 {OWN.upper()} MIDDLEQ {OWN.upper()}"


def test_autosweep_shape_stays_on_the_own_fd_from_history(tmp_path, monkeypatch):
    r = _row(tmp_path, monkeypatch, SWEEP, _hist_fd())
    assert r["Account"].endswith("ICICI FD - 5551234")
    assert r["Confidence"] == "history"


def test_autosweep_with_a_different_fd_number_still_lands_via_owner_plus_keyword(
        tmp_path, monkeypatch):
    r = _row(tmp_path, monkeypatch,
             f"AUTOSWEEP TO 9990001 {OWN.upper()} MIDDLEQ {OWN.upper()}", _hist_fd())
    assert r["Account"].endswith("ICICI FD - 5551234")


def test_autosweep_shape_without_the_owner_now_lands_on_the_fd(tmp_path, monkeypatch):
    """Round 3 FLIPPED (was a negative): a sweep keyword alone IS evidence for an FD
    target, and "AUTOSWEEP TO <n> <VENDOR>" really is an own sweep into the FD."""
    r = _row(tmp_path, monkeypatch, "AUTOSWEEP TO 9990001 ACMEVENDOR", _hist_fd())
    assert r["Account"].endswith("ICICI FD - 5551234")


# ---- adversarial AI: still nothing on an own bank --------------------------------------

def test_ai_always_picking_own_hdfc_for_a_name_only_row_is_withheld(tmp_path, monkeypatch):
    from scripted_llm import ScriptedLLM, choose
    h = ScriptedLLM(monkeypatch, tmp_path)
    try:
        h.say("ACMEINSURER", choose(fx.P_HDFC2))
        r = _row(tmp_path, monkeypatch, INSURER_DEP, _hist_bank(), config_path=h.config_path)
        assert r["Account"] != fx.P_HDFC2
        assert r["Confidence"] == "suspense"
    finally:
        h.close()


# ---- round 3: sweep / closure / cash-withdrawal rows with no owner token -----------------
# Real shapes: "<FD digits>: Rev Sweep From" learned onto a digit-free FD, and
# "CASH WDL/<terminal>/<city>/<date>" learned onto the own Cash account.

FD_FREE = "Assets:Current Assets:Cash and Bank:ICICI FD"
CASH = "Assets:Current Assets:Cash and Bank:Cash"
P_HDFC2 = fx.P_HDFC2
_FDFREE_ACCTS = [
    fx.account_xml("fdn", "ICICI FD", "ASSET", "cab"),     # no digits in the name
    fx.account_xml("cashacc", "Cash", "ASSET", "cab"),
]


def _hist3(sweeps=("{n}: Rev Sweep From", "{n}: Closure Proceeds"),
           cash=("CASH WDL/T00{i}/ACMETOWN/0{i}-08",), target_sweep="fdn",
           target_cash="cashacc"):
    h = t26._history(narr="{o} FUNDS MOVE", target=fx.HDFC2)
    for i in range(1, 6):
        for tmpl in sweeps:
            h.append(t26._t(tmpl.format(n=900000000000 + i), target_sweep))
        for tmpl in cash:
            h.append(t26._t(tmpl.format(i=i), target_cash))
    # ordinary words also ride on non-own rows, so they are NOT learned as owner tokens
    for i in range(2):
        h.append(t26._t(f"REV PAYMENT FROM SHOP{i} PROCEEDS", "groc"))
    return h


def _row3(tmp_path, monkeypatch, desc, hist=None):
    return t26._one(tmp_path, monkeypatch, desc, t26._accounts(extra=_FDFREE_ACCTS),
                    hist if hist is not None else _hist3())


@pytest.mark.parametrize("desc", ["912345678901: Rev Sweep From", "912345678901: Closure Proceeds"])
def test_rev_sweep_and_closure_proceeds_land_on_the_digit_free_fd(tmp_path, monkeypatch, desc):
    r = _row3(tmp_path, monkeypatch, desc)
    assert r["Account"].endswith("Cash and Bank:ICICI FD"), r["Account"]
    assert r["Confidence"] == "history"


@pytest.mark.parametrize("desc", [
    "CASH WDL/T0099/ACMETOWN/09-08", "CAM/123456/CASH WDL/ACMETOWN/09-08"])
def test_cash_withdrawal_lands_on_the_cash_account(tmp_path, monkeypatch, desc):
    r = _row3(tmp_path, monkeypatch, desc)
    assert r["Account"].endswith("Cash and Bank:Cash"), r["Account"]


def test_rev_sweep_does_not_land_on_an_own_savings_account(tmp_path, monkeypatch):
    """NEGATIVE: history that learned the sweep onto a SAVINGS account is still refused."""
    hist = _hist3(target_sweep=fx.HDFC2)
    r = _row3(tmp_path, monkeypatch, "912345678901: Rev Sweep From", hist)
    assert r["Account"] != P_HDFC2
    assert _off_own(r), r["Account"]


def test_cash_withdrawal_does_not_land_on_an_own_bank_or_fd(tmp_path, monkeypatch):
    """NEGATIVE: history that learned CASH WDL onto a bank / an FD is refused."""
    for tgt in (fx.HDFC2, "fdn"):
        r = _row3(tmp_path, monkeypatch, "CASH WDL/T0099/ACMETOWN/09-08",
                  _hist3(target_cash=tgt))
        assert "HDFC Bank" not in r["Account"] and "ICICI FD" not in r["Account"], (tgt, r["Account"])


def test_neft_owner_fd_does_not_land_on_an_own_savings_account(tmp_path, monkeypatch):
    """NEGATIVE: owner token + FD keyword is evidence for an FD only, never for savings."""
    hist = t26._history(narr="{o} FUNDS MOVE", target=fx.HDFC2)
    hist += [t26._t(f"NEFT {OWN.upper()} FD Z{i}", fx.HDFC2) for i in range(4)]
    r = _row3(tmp_path, monkeypatch, f"NEFT {OWN.upper()} FD", hist)
    assert r["Account"] != P_HDFC2
