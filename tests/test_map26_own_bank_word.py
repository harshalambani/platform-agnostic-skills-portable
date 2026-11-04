"""
MAP-26 -- RED FLAG: an own transfer must never be booked to the WRONG own bank,
and an insurer credit "<INSURER> to <OWNER NAME>" must never reach an own bank.

Defects (real v3.14 run, here synthetic and masked):
  A. History (Bayes) strips the bank words, so owner-name tokens pulled
     "<owner>/<handle>-/xfer to se/HDFC BANK" onto SBM. ICICI also truncates the
     marker to "xfer to se".
  B. "FTTransferP2B/<INSURER> to <OWNER NAME>" was mapped by the AI to an own bank.

Rules: the bank word the narration names has the last say over WHICH own account;
the truncated marker counts only when the field ends there; the owner's plain name
alone is not own-transfer evidence for the AI pass.
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

OWN = "ownerzq"
HANDLE = "ownerzq9@ybl"
SBM1, SBM2 = "sbm1", "sbm2"
SBM_HIDDEN = "sbm_old"
P_SBM1 = "Assets:Current Assets:Cash and Bank:SBM Bank - 551XXXX001"
P_SBM2 = "Assets:Current Assets:Cash and Bank:SBM Bank - 551XXXX002"
P_SBM_HIDDEN = "Assets:Current Assets:Cash and Bank:SBM Bank - 551XXXX009 (old)"


def _sbm(two=False, hidden=False):
    out = [fx.account_xml(SBM1, "SBM Bank - 551XXXX001", "BANK", "cab")]
    if two:
        out.append(fx.account_xml(SBM2, "SBM Bank - 551XXXX002", "BANK", "cab"))
    if hidden:
        out.append(fx.account_xml(SBM_HIDDEN, "SBM Bank - 551XXXX009 (old)", "BANK", "cab",
                                  ["hidden"]))
    return out


def _accounts(drop=("094XXXX3456", "013065XXXX-002"), extra=()):
    return [a for a in fx.standard_accounts(list(extra)) if not any(d in a for d in drop)]


def _t(desc, target):
    return fx.txn_xml(desc, "2025-06-10", [(fx.HDFC1, -10000), (target, 10000)])


def _history(target=SBM1, narr="{o}/{h}-/xfer to se/SBM", n=6):
    h = [_t(narr.format(o=OWN, h=HANDLE) + f" Q{i}", target) for i in range(n)]
    # the extractor only knows an own account once it has been moved money
    h += [_t(f"MISC MOVEMENT {k}", k) for k in (fx.HDFC2, fx.HDFC4, fx.HSBC1, fx.HSBC2)]
    h += [_t("NEFT VENDORONE INVOICE", "groc")]
    h += [_t(f"SHOP{i} GROCERY PURCHASE", "groc") for i in range(24)]
    return h


def _run(tmp_path, monkeypatch, desc, accounts, txns, config_path=None, extra_rows=()):
    monkeypatch.chdir(tmp_path)
    book = fx.write_book(tmp_path / "b.gnucash", accounts, txns)
    rows = [("2025-08-01", desc, "", "100.00")] + list(extra_rows)
    csv_in = fx.canonical_csv(tmp_path / "in.csv", rows)
    out = tmp_path / "out.csv"
    m.run(book, csv_in, str(out), config_path=config_path, bank_name="HDFC",
          gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _one(*a, **k):
    return _run(*a, **k)[0]


# ---- (A) the bank word beats owner-name history ------------------------------------

@pytest.mark.parametrize("bank,expect", [("HDFC BANK", fx.P_HDFC2), ("HSBC", fx.P_HSBC1)])
def test_truncated_marker_names_the_bank_and_beats_sbm_history(tmp_path, monkeypatch, bank, expect):
    r = _one(tmp_path, monkeypatch, f"{OWN}/{HANDLE}-/xfer to se/{bank}",
             _accounts(extra=_sbm()), _history())
    assert r["Account"] == expect
    assert r["Account"] != P_SBM1                    # NEGATIVE: never the history favourite
    assert "bank-name match" in r["MatchReason"].lower()


def test_sbm_named_row_keeps_the_history_pick_of_two_sbm_accounts(tmp_path, monkeypatch):
    """No regression: the chosen account IS a carrier of the named bank."""
    hist = _history(target=SBM2)
    r = _one(tmp_path, monkeypatch, f"{OWN}/{HANDLE}-/xfer to se/SBM",
             _accounts(extra=_sbm(two=True)), hist)
    assert r["Account"] == P_SBM2
    assert r["Account"] != P_SBM1


def test_full_marker_with_two_accounts_at_the_bank_still_ties_to_review(tmp_path, monkeypatch):
    r = _one(tmp_path, monkeypatch, "XFER TO SELF HSBC",
             _accounts(drop=("094XXXX3456",), extra=_sbm()), _history())
    assert "HSBC" not in r["Account"] and "SBM" not in r["Account"]
    assert r["Confidence"] == "suspense"
    assert "2 accounts" in r["MatchReason"]


def test_rtgs_hsbc_with_owner_name_still_reaches_own_hsbc(tmp_path, monkeypatch):
    r = _one(tmp_path, monkeypatch, f"RTGS/REF123/HSBC/{OWN}",
             _accounts(extra=_sbm()), _history())
    assert r["Account"] == fx.P_HSBC1
    assert r["Account"] != P_SBM1


# ---- (B) the insurer credit ----------------------------------------------------------

INSURER = "FTTransferP2B/ACMEINSURER to OWNERZQ"


def test_ai_suggesting_own_hdfc_for_the_insurer_credit_is_gated(tmp_path, monkeypatch):
    h = ScriptedLLM(monkeypatch, tmp_path)
    try:
        h.say("ACMEINSURER", choose(fx.P_HDFC2))
        r = _one(tmp_path, monkeypatch, INSURER, _accounts(extra=_sbm()), _history(),
                 config_path=h.config_path)
        assert r["Account"] != fx.P_HDFC2
        assert r["Account"] not in (fx.P_HSBC1, P_SBM1)
        assert r["Confidence"] == "suspense"
    finally:
        h.close()


def test_insurer_credit_with_history_on_an_own_bank_never_ends_on_it(tmp_path, monkeypatch):
    """History that favours an own bank for the owner's name does not carry an
    insurer credit onto it either: no bank word, no marker, no handle."""
    hist = _history(narr="{o} FUNDS MOVE", target=fx.HDFC2)
    r = _one(tmp_path, monkeypatch, INSURER, _accounts(extra=_sbm()), hist)
    assert r["Account"] not in (fx.P_HDFC2, fx.P_HSBC1, P_SBM1)


# ---- negatives: no evidence, no route ------------------------------------------------

def test_payee_row_naming_hdfc_without_own_evidence_never_routes_to_own_hdfc(tmp_path, monkeypatch):
    r = _one(tmp_path, monkeypatch, "ACMEPAYEE/acme@okhdfc/UPI/HDFC BANK",
             _accounts(extra=_sbm()), _history())
    assert r["Account"] != fx.P_HDFC2
    assert r["Confidence"] != "history"


def test_bank_alone_never_routes(tmp_path, monkeypatch):
    r = _one(tmp_path, monkeypatch, "NEFT BANK CHARGES",
             _accounts(extra=_sbm()), _history())
    assert not any(a in r["Account"] for a in ("HDFC Bank", "HSBC Bank", "SBM Bank"))


def test_a_narration_naming_the_source_bank_never_routes_to_the_source(tmp_path, monkeypatch):
    r = _one(tmp_path, monkeypatch, f"{OWN}/{HANDLE}-/xfer to se/HDFC",
             _accounts(drop=("094XXXX3456", "094XXXX5678"), extra=_sbm()), _history())
    assert r["Account"] != fx.P_HDFC1                # the source itself
    assert r["Account"] != P_SBM1                    # nor the history favourite


# ---- the marker -----------------------------------------------------------------------

@pytest.mark.parametrize("text,ok", [
    ("xfer to se/HDFC BANK", True),
    ("xfer to se", True),
    ("trf to s/HDFC", True),
    ("transfer to sel/HDFC", True),
    ("transfer to sanjay/HDFC", False),              # NEGATIVE: a payee
    ("xfer to sebastian/x", False),
    ("xfer to self/HDFC", True),
])
def test_self_marker_truncation_needs_the_field_to_end(text, ok):
    assert bool(m._SELF_MARKER_RE.search(text.lower())) is ok


# ---- evidence rule (d) -----------------------------------------------------------------

def test_owner_name_alone_is_not_evidence_for_the_ai_but_a_handle_is():
    own = {"Assets:Cash:HDFC Bank - 1", "Assets:Cash:HSBC Bank - 2"}
    vocab = {OWN, HANDLE}
    f = lambda d, **k: m._has_own_transfer_evidence(d, vocab, own, own, "Assets:Cash:SRC", **k)
    assert f(f"ACMEINSURER to {OWN.upper()}", name_alone=False) is False   # NEGATIVE
    assert f(f"ACMEINSURER {HANDLE}", name_alone=False) is True            # own handle counts
    assert f(f"ACMEINSURER {OWN} HSBC", name_alone=False) is True          # name + own bank word
    assert f("anything xfer to se/HDFC", name_alone=False) is True         # truncated marker
    assert f("anything xfer to sanjay/x", name_alone=False) is False       # NEGATIVE


# ---- overrides, hidden, counts -----------------------------------------------------------

def test_override_row_is_never_touched(tmp_path, monkeypatch):
    """A user override sending a narration that names HDFC to SBM stays on SBM:
    the bank-name guard never touches 'override' rows."""
    monkeypatch.chdir(tmp_path)
    book = fx.write_book(tmp_path / "b.gnucash", _accounts(extra=_sbm()), _history())
    (tmp_path / "b_mapping_rules.yaml").write_text(
        "_overrides:\n"
        f"- pattern: 'xfer to se'\n  account: '{P_SBM1}'\n  added: '2025-01-01'\n",
        encoding="utf-8")
    narr = f"{OWN}/{HANDLE}-/xfer to se/HDFC BANK"
    csv_in = fx.canonical_csv(tmp_path / "in.csv", [("2025-08-01", narr, "", "100.00")])
    out = tmp_path / "out.csv"
    m.run(book, csv_in, str(out), config_path=None, bank_name="HDFC",
          gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        row = list(csv.DictReader(f))[0]
    assert row["Confidence"] == "override"            # the override really fired
    assert row["Account"] == P_SBM1                   # NEGATIVE: not rerouted to HDFC


def test_guard_helper_abstains_when_the_chosen_account_carries_the_word():
    own = {m._strip_root(P_SBM1), m._strip_root(fx.P_HDFC2)}
    assert m._own_bank_word_verdict("xfer to se/HDFC", P_SBM1, own, fx.P_HDFC1,
                                    lambda d: True)["account"] == m._strip_root(fx.P_HDFC2)
    assert m._own_bank_word_verdict("xfer to se/SBM", P_SBM1, own, fx.P_HDFC1,
                                    lambda d: True) is None
    assert m._own_bank_word_verdict("plain narration", P_SBM1, own, fx.P_HDFC1) is None


def test_hidden_account_is_never_a_reroute_target(tmp_path, monkeypatch):
    r = _one(tmp_path, monkeypatch, f"{OWN}/{HANDLE}-/xfer to se/SBM",
             _accounts(drop=("094XXXX3456", "013065XXXX-002"),
                       extra=_sbm(hidden=True)), _history(target=fx.HDFC2))
    assert "(old)" not in r["Account"] and r["Account"] != P_SBM_HIDDEN


def test_confidence_counts_stay_consistent_after_a_reroute(tmp_path, monkeypatch):
    rows = _run(tmp_path, monkeypatch, f"{OWN}/{HANDLE}-/xfer to se/HDFC BANK",
                _accounts(extra=_sbm()), _history(),
                extra_rows=[("2025-08-02", "SHOP9 GROCERY PURCHASE", "", "50.00")])
    assert len(rows) == 2
    assert rows[0]["Account"] == fx.P_HDFC2 and rows[0]["Confidence"] == "history"
    assert rows[1]["Account"].endswith("Groceries")
