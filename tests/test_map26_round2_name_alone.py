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


def _ev(desc, name_alone=False):
    return m._has_own_transfer_evidence(desc, VOCAB, set(OWNB), set(OWNB), None, name_alone)


def test_plain_name_is_not_evidence_when_name_alone_is_false():
    assert _ev("ACMEINSURER CLAIM OWNERZQ") is False
    assert _ev("NEFT OWNERZQ ACMEEMPLOYER") is False
    assert _ev("NEFT OWNERZQ ACMEEMPLOYER", name_alone=True) is True   # legacy callers only


@pytest.mark.parametrize("desc", [
    "XFER TO SELF",
    "ownerzq/xfer to se/HDFC",                        # truncated marker
    "UPI OWNERZQ9@YBL PAYMENT",                       # own handle
    "NEFT OWNERZQ HDFC",                              # owner token + bank word of an own account
    "AUTOSWEEP TO 9990001 OWNERZQ MIDDLEQ OWNERZQ",   # owner + sweep keyword
    "FD BOOKING OWNERZQ",
    "FD CLOSURE OWNERZQ",
    "NEFT TO 0941235678",                             # own account number
])
def test_legitimate_evidence_routes_still_count(desc):
    assert _ev(desc) is True, desc


@pytest.mark.parametrize("desc", [
    "FD BOOKING ACMEVENDOR",                          # keyword without the owner
    "AUTOSWEEP ACMEVENDOR",
    "ACMEINSURER CLAIM OWNERZQ",
    "ACMEVENDOR to ACMEOTHER",
])
def test_keyword_without_owner_or_name_alone_is_not_evidence(desc):
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


def test_autosweep_shape_without_the_owner_or_fd_number_does_not_land(tmp_path, monkeypatch):
    """NEGATIVE: the sweep word alone, no owner, no own number."""
    r = _row(tmp_path, monkeypatch, "AUTOSWEEP TO 9990001 ACMEVENDOR", _hist_fd())
    assert not r["Account"].endswith("ICICI FD - 5551234")


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
