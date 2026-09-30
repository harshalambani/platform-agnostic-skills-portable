"""
MAP-22 -- one own-transfer evidence gate on EVERY pass that can land a row on
the owner's own account (own bank accounts and own FDs).

Closes the three gaps left open by MAP-13/MAP-21:
  1. the weak prefix-match pass (shared 10+ char prefix with past own rows);
  2. the Bayesian history pass reaching an own account through the row's
     `ifsc:<bank>` token alone;
  3. Step 4.9 (_ifsc_contradiction) treated "the guessed account itself carries
     the code" as correct, though a third party at the owner's bank carries it too.

Each gap has a NEGATIVE test (a third-party row with no evidence must not land
on an own bank / own FD) and a POSITIVE test (a genuine self-transfer -- self
marker, own vocabulary, own account number -- still lands).

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
import test_map13_third_party_bank_code as t13  # noqa: E402
from scripted_llm import ScriptedLLM, choose  # noqa: E402

OWN_NAME = "ownerzq"


def _run(tmp_path, desc, txns, name="r", config_path=None):
    return t13._run(tmp_path, desc, txns=txns, config_path=config_path, name=name)


def _ifsc_history():
    """The owner's transfers at the other bank ALL carry that bank's code and go
    to the one own account there: the Bayes score reaches it via ifsc:<bank>."""
    h = []
    for i in range(6):
        h += t13._t(f"NEFT {OWN_NAME} SWEEP HSBC0XYZ999 Q{i}", fx.HSBC1)
    h += t13._t("NEFT VENDORONE INVOICE", "groc")
    for i in range(24):
        h += t13._t(f"SHOP{i} GROCERY PURCHASE", "groc")
    return h


def _prefix_history():
    """One own-bank row and ordinary rows sharing a long narration prefix."""
    h = []
    for i in range(4):
        h += t13._t(f"NEFT {OWN_NAME} SELF ACCT Q{i}", fx.HDFC2)
    h += t13._t("MISC ADJUSTMENT ENTRY 1234567", fx.HDFC2)
    for i in range(24):
        h += t13._t(f"SHOP{i} GROCERY PURCHASE", "groc")
    return h


# ---- gap 2: Bayes ifsc token --------------------------------------------------

def test_bayes_ifsc_token_does_not_route_a_third_party_to_the_own_account(tmp_path, monkeypatch):
    """NEGATIVE: every earlier row with this bank code went to the own account,
    but a vendor paid at that bank is not a self-transfer."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, "NEFT ACMEVENDOR PAYMENT HSBC0NEW456", _ifsc_history())
    assert r["Account"] != fx.P_HSBC1
    assert "HSBC" not in r["Account"]
    assert r["Confidence"] != "history"


@pytest.mark.parametrize("desc", [
    f"NEFT CR {OWN_NAME} HSBC0NEW456",             # own vocabulary
    "XFER TO SELF NEFT HSBC0NEW456",               # self marker
])
def test_genuine_self_transfer_at_that_bank_still_routes(tmp_path, monkeypatch, desc):
    """POSITIVE: vocab, marker and own account number each still land."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, desc, _ifsc_history())
    assert r["Account"] == fx.P_HSBC1
    assert r["Confidence"] == "history"


def test_gate_helper_unit():
    """_gate_own_target: non-own accounts pass; own accounts need marker, vocab
    or an own account number run."""
    own = {"Assets:Cash:HSBC Bank - 013065001"}
    vocab = {"ownerzq"}
    g = m._gate_own_target
    assert g("ANYTHING", "Expenses:Groceries", vocab, own) is True
    assert g("ANYTHING", "", vocab, own) is True
    assert g("NEFT ACMEVENDOR", "Assets:Cash:HSBC Bank - 013065001", vocab, own) is False
    assert g("NEFT OWNERZQ", "Root Account:Assets:Cash:HSBC Bank - 013065001", vocab, own) is True
    assert g("XFER TO SELF", "Assets:Cash:HSBC Bank - 013065001", vocab, own) is True
    assert g("NEFT TO 013065001", "Assets:Cash:HSBC Bank - 013065001", vocab, own) is True


def test_bayes_reroute_drops_only_the_ifsc_token(monkeypatch):
    """Unit: no evidence -> ifsc dropped -> re-score; an ordinary-token route to
    a NON-own account still stands, an own-target route is refused."""
    model = m._build_history_token_model([
        {"description": "NEFT ACMEVENDOR INVOICE HSBC0AAA111", "account": "Expenses:Groceries",
         "frequency": 3},
        {"description": "NEFT OTHERSELF HSBC0AAA111", "account": "Assets:Cash:HSBC Bank - 1",
         "frequency": 3},
    ])
    own = {"Assets:Cash:HSBC Bank - 1"}
    r = m._history_token_match("NEFT ACMEVENDOR INVOICE HSBC0NEW456", model,
                               own_bank_accounts=own, own_evidence=lambda d: False,
                               own_targets=own)
    assert r is None or r["account"] != "Assets:Cash:HSBC Bank - 1"
    # legacy ungated call (no evidence callable) is unchanged
    m._history_token_match("NEFT OTHERSELF HSBC0NEW456", model, own_bank_accounts=own)


# ---- gap 1: weak prefix pass --------------------------------------------------

def test_weak_prefix_pass_does_not_book_a_third_party_to_an_own_bank(tmp_path, monkeypatch):
    """NEGATIVE: shares a 10+ char prefix with a past own-account row only."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, "MISC ADJUSTMENT ENTRY 7654321", _prefix_history())
    assert r["Account"] != fx.P_HDFC2
    assert r["Confidence"] in ("suspense", "none")


def test_weak_prefix_pass_still_lands_with_own_evidence(tmp_path, monkeypatch):
    """POSITIVE: the same prefix, plus the owner's own vocabulary."""
    monkeypatch.chdir(tmp_path)
    r = _run(tmp_path, f"MISC ADJUSTMENT ENTRY 7654321 {OWN_NAME}", _prefix_history())
    assert r["Account"] == fx.P_HDFC2


def test_weak_prefix_refusal_falls_to_the_ai_and_the_ai_is_gated_too(tmp_path, monkeypatch):
    """NEGATIVE: a refused prefix guess goes on to the AI pass, whose own-account
    proposal is withheld by the MAP-14 gate (same helper) -> Suspense."""
    monkeypatch.chdir(tmp_path)
    h = ScriptedLLM(monkeypatch, tmp_path)
    try:
        h.say("MISC ADJUSTMENT", choose(fx.P_HDFC2))
        r = _run(tmp_path, "MISC ADJUSTMENT ENTRY 7654321", _prefix_history(),
                 config_path=h.config_path)
        assert r["Account"] != fx.P_HDFC2
        assert r["Confidence"] == "suspense"
    finally:
        h.close()


# ---- gap 3: Step 4.9 ----------------------------------------------------------

def test_ifsc_guard_reverts_a_guess_onto_the_account_that_carries_the_code():
    own = {"Assets:Cash:HSBC Bank - 013065XXXX-001"}
    d = "NEFT ACMEVENDOR PAYMENT HSBC0NEW456"
    acct = "Assets:Cash:HSBC Bank - 013065XXXX-001"
    assert m._ifsc_contradiction(d, acct, own, lambda _d: False) is True   # no evidence
    assert m._ifsc_contradiction(d, acct, own, lambda _d: True) is False   # own transfer
    assert m._ifsc_contradiction(d, acct, own) is False                    # legacy callers


def test_step_4_9_reverts_a_smart_guess_onto_the_code_carrying_own_account(tmp_path, monkeypatch):
    """NEGATIVE, end to end: a smart-pass guess onto the own HSBC account for a
    third party's payment is reverted to Suspense; the owner's own row stands."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        m, "smart_pattern_match",
        lambda desc, accts, w, d: {"account": "Root Account:" + fx.P_HSBC1,
                                   "reason": "forced guess", "confidence": "smart"})
    r = _run(tmp_path, "NEFT ACMEVENDOR PAYMENT HSBC0NEW456", t13._history(), name="a")
    assert r["Account"] != fx.P_HSBC1
    assert r["Confidence"] == "suspense"
    r2 = _run(tmp_path, f"NEFT {OWN_NAME} HSBC0NEW456 TOPUP", t13._history(), name="b")
    assert r2["Account"].endswith("HSBC Bank - 013065XXXX-001")


# ---- own FD (MAP-21 target) goes through the same gate -------------------------

def test_own_fd_target_is_gated_on_the_weak_pass_too(tmp_path):
    import test_map21_fd_outside_bank_folder as t21
    r = t21._run(tmp_path, None, "FD BOOKING ACMEVENDOR")
    assert r["Account"] != t21.P_FD
    # positive: an owner-named FD top-up still lands
    r2 = t21._run(tmp_path, None, f"FD BOOKING {OWN_NAME} Z9", name="p")
    assert r2["Account"] == t21.P_FD
