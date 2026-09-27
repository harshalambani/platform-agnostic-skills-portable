"""
tests/skill_gnucash_account_mapper/test_map11_pipeline_negative_paths.py --
PR #278 follow-up (2026-09-27): negative-assertion tests for behaviours the
existing suite left uncovered or only weakly covered:

  1. Override-first order: a User override and a High-confidence rule both
     match one row -- the override wins, and the rule's account is never
     written (map_accounts() itself, real function, no re-derived logic).
  2. Med/Low rule preservation: a Medium/Low-confidence rule match is kept
     verbatim when the history matcher abstains (tie / thin support /
     single support) -- and, conversely, IS replaced once history clears
     all four MAP-11 thresholds. Both directions exercised through the same
     real Step-3.6 mechanics run() uses (agent._history_token_match /
     agent._build_history_token_model), mirrored here exactly as
     test_confidence_report_reflects_final_state.py already mirrors run()'s
     Step 4a/5 for the same reason: run()'s own Step 3.6 body is not itself
     an importable function.
  3. RTGS/UTR-prefix collision: RTGS rows that share a UTR-like reference
     prefix but go to different counterparties must never collapse onto one
     account via _historical_prefix_match or _history_token_match. This is
     also the regression test for a real defect found and fixed by this
     follow-up -- see the RED FLAG note above the relevant test below.
  4. Self-transfer / IFSC routing (_self_transfer_candidates,
     _literal_bank_code_match, _history_token_match, _ifsc_contradiction):
     correct-bank routing, no-bank-code-match, ambiguous-two-bank-code-match,
     no-bank-marker at all, a non-self-transfer narration that happens to
     contain an IFSC, and the real-book defect this follow-up fixes: an
     all-or-nothing shape test that never fires once a description's plain
     tokens (self-transfer marker words, the user's own name) also appear on
     non-asset accounts elsewhere in the book -- which is true of every real
     book. Fixtures here are REAL-SHAPED for that reason: the self-transfer
     marker words and the user's own name deliberately also lead to
     Income/Expenses/Liabilities accounts, mirroring the real-book
     measurement that found the defect (see RED FLAG note above
     _self_transfer_candidates in agent.py).

     ROUND 2 (2026-09-27 real-book re-measure): the round-1 fix above did
     NOT change the real-book result -- 15 HSBC-IFSC and 1 HDFC-IFSC row
     still went to SBM. Two further real-book-only defects, both now fixed
     and covered here:
       (a) own_bank_accounts was still gated by BANK_PATTERNS name matching
           (fixed in the extractor, see test_bank_split_classification.py's
           new test) -- SBM was silently never a member of own_bank_accounts,
           so _ifsc_contradiction could not recognise it as "one of the
           book's own accounts" and never fired;
       (b) the real book has MULTIPLE own accounts per bank (e.g. two HSBC
           BANK-type accounts -- a current account and an FD also of type
           BANK) so "hsbc" always hit 2 own accounts and
           _literal_bank_code_match abstained as ambiguous before ever
           reaching a real decision, letting the row fall through to the
           weak prefix matcher. Fixed with a deterministic historical-
           evidence tie-break (most self-transfer support wins; still-tied
           or zero evidence stays unmatched -- never a name-order pick).
     The fixture below is rebuilt to have the SAME shape as the real book:
     two BANK-type accounts per bank (ICICI/HDFC/HSBC), SBM/Kotak/Barclays as
     BANK-type accounts NOT in any name-pattern list, an FD of BANK type
     named after its bank, and self-transfer history concentrated 10-to-1 on
     ONE of the two HSBC accounts (mirroring the real book's own numbers).
  5. Direction: a self-transfer/sweep match is never assigned to an Income
     or Expense account.
  6. End-to-end: agent.run() itself, with a REAL-SHAPED history fixture,
     routes an HSBC-IFSC self-transfer row to HSBC and an HDFC-IFSC one to
     HDFC -- never to SBM (the bank that used to win by default via the
     weak prefix matcher once the old shape test always abstained).

All data (descriptions, account paths, IFSC-shaped codes) is synthetic --
no real names, account numbers, FD numbers, PANs, or IFSC codes.
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path
from typing import Dict, List

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_account_mapper import agent  # noqa: E402


def _mk(desc: str, account: str, frequency: int = 1) -> dict:
    return {"description": desc, "account": account, "frequency": frequency}


# Synthetic account paths reused across this file.
OWN_HSBC = "Assets:Current Assets:Cash and Bank:HSBC Bank - 0004002"
OWN_HSBC_BUSINESS = "Assets:Current Assets:Cash and Bank:HSBC Business - 0009001"
OWN_SBM = "Assets:Current Assets:Cash and Bank:SBM Bank - 0009999"
OWN_HDFC = "Assets:Current Assets:Cash and Bank:HDFC Bank - 0001579"
BOND_ACCOUNT = "Assets:Investments:Bonds:Sample Bond"
LOAN_ACCOUNT = "Liabilities:Loans:Sample Loan"
EXPENSE_VENDOR = "Expenses:Sample Vendor"
INCOME_SALARY = "Income:Salary"

# Real-book-shaped account structure (round 2, 2026-09-27): each of ICICI,
# HDFC, HSBC has TWO of its own BANK-type accounts (a current account and an
# FD, both type BANK -- a real book keeps some FDs as type BANK). SBM, Kotak,
# and Barclays are BANK-type accounts whose names match NO entry in
# BANK_PATTERNS at all -- the exact real-book shape that used to silently
# fall out of own_bank_accounts.
OWN_HSBC_FD = "Assets:Current Assets:Cash and Bank:HSBC Bank - FD"
OWN_HDFC_FD = "Assets:Current Assets:Cash and Bank:HDFC Bank - FD"
OWN_KOTAK = "Assets:Current Assets:Cash and Bank:Kotak Bank - 0002222"
OWN_KOTAK_FD = "Assets:Current Assets:Cash and Bank:Kotak Bank FD - 0002223"
OWN_BARCLAYS = "Assets:Current Assets:Cash and Bank:Barclays Bank - 0003333"


# ---------------------------------------------------------------------------
# 1. Override-first order: override wins, the rule's account is NEVER
#    written for that row (a structural skip, not an overwrite).
# ---------------------------------------------------------------------------

def test_override_wins_over_high_confidence_rule_and_rule_account_never_written(tmp_path):
    canonical_csv = tmp_path / "canonical.csv"
    with open(canonical_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Date", "Description", "Withdrawal", "Deposit"])
        writer.writeheader()
        writer.writerow({"Date": "01-04-2025", "Description": "SALARY CREDIT APR",
                          "Withdrawal": "", "Deposit": "50000.00"})

    mapping_yaml = tmp_path / "rules.yaml"
    import yaml
    mapping_yaml.write_text(yaml.dump({
        "BankX": [
            {"patterns": ["SALARY"], "account": INCOME_SALARY, "confidence": "high",
             "reason": "Salary credit", "frequency": 5},
        ]
    }), encoding="utf-8")

    override_account = "Assets:Current Assets:Cash and Bank:Own Sample Account"
    overrides = [{"patterns": ["SALARY"], "account": override_account, "added": "2026-01-01"}]

    mapped_csv = tmp_path / "mapped.csv"
    report_path = tmp_path / "mapped_confidence.txt"
    result = agent.map_accounts(
        str(canonical_csv), str(mapping_yaml), str(mapped_csv), str(report_path),
        overrides=overrides,
    )

    with open(mapped_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 1
    row = rows[0]

    # The override wins outright.
    assert row["Account"] == override_account
    assert row["Confidence"] == "override"

    # Negative assertion: the High rule's account is NEVER written anywhere
    # for this row -- not as the final Account, and not embedded in the
    # MatchReason either (which would indicate the rule ran and was merely
    # overwritten after the fact, rather than skipped as documented).
    assert row["Account"] != INCOME_SALARY
    assert INCOME_SALARY not in row["Account"]
    assert INCOME_SALARY not in row["MatchReason"]
    assert result["confidence_counts"].get("high", 0) == 0
    assert result["confidence_counts"].get("override", 0) == 1


# ---------------------------------------------------------------------------
# 2. Med/Low rule preservation vs. replacement by the real Step-3.6 history
#    pass. _apply_history_pass mirrors run()'s Step 3.6 body using the same
#    real functions (agent._build_history_token_model / _history_token_match
#    / _strip_root / _safe_float / _direction_mismatch) -- run()'s Step 3.6
#    is not itself a standalone function, so this is the same technique
#    test_confidence_report_reflects_final_state.py already uses for Step
#    4a/5, applied to Step 3.6 instead.
# ---------------------------------------------------------------------------

def _apply_history_pass(mapped_csv: Path, historical_pairs: List[Dict]) -> None:
    with open(mapped_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
        fieldnames = list(rows[0].keys())

    history_model = agent._build_history_token_model(historical_pairs)
    for row in rows:
        conf = row.get("Confidence") or "none"
        if conf in ("high", "override"):
            continue
        desc = row.get("Description") or ""
        match = agent._history_token_match(desc, history_model)
        if match and match.get("account"):
            row["Account"] = agent._strip_root(match["account"])
            row["Confidence"] = "history"
            reason = f"History: {match['reason']}"
            d_amt = agent._safe_float(row.get("Deposit", ""))
            w_amt = agent._safe_float(row.get("Withdrawal", ""))
            if agent._direction_mismatch(match["account"], d_amt, w_amt):
                reason += f" [{agent._DIRECTION_FLAG_MARKER}]"
            row["MatchReason"] = reason

    with open(mapped_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _write_single_row_mapped_csv(mapped_csv: Path, desc: str, account: str, confidence: str,
                                  withdrawal: str = "", deposit: str = "") -> None:
    fieldnames = ["Date", "Description", "Withdrawal", "Deposit", "Account", "Confidence", "MatchReason"]
    with open(mapped_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerow({
            "Date": "01-04-2025", "Description": desc,
            "Withdrawal": withdrawal, "Deposit": deposit,
            "Account": account, "Confidence": confidence,
            "MatchReason": f"Rule: {confidence} guess",
        })


def test_medium_rule_kept_when_history_matcher_abstains_on_a_tie(tmp_path):
    # Two accounts tie on the only shared token -- _history_token_match must
    # abstain (agent's own documented tie behaviour), so the row's existing
    # Medium-confidence rule match must survive completely untouched.
    mapped_csv = tmp_path / "mapped.csv"
    _write_single_row_mapped_csv(
        mapped_csv, "AMBIGUOUS SWEEP TXN", "Expenses:Misc", "medium", withdrawal="10.00",
    )
    historical_pairs = [
        _mk("ambiguous sweep alpha", BOND_ACCOUNT, frequency=20),
        _mk("ambiguous sweep beta", LOAN_ACCOUNT, frequency=20),
    ]
    _apply_history_pass(mapped_csv, historical_pairs)

    with open(mapped_csv, newline="", encoding="utf-8") as f:
        row = list(csv.DictReader(f))[0]

    # Negative assertions: nothing about the row moved.
    assert row["Account"] == "Expenses:Misc"
    assert row["Confidence"] == "medium"
    assert not row["MatchReason"].startswith("History:")
    assert row["Account"] != BOND_ACCOUNT
    assert row["Account"] != LOAN_ACCOUNT


def test_low_rule_kept_when_history_matcher_abstains_on_thin_single_support(tmp_path):
    # Only ONE historical transaction behind the would-be winning account --
    # fails HISTORY_MIN_SUPPORT_TXNS even though the token is otherwise
    # unique, so the Low-confidence rule match must be left untouched.
    mapped_csv = tmp_path / "mapped.csv"
    _write_single_row_mapped_csv(
        mapped_csv, "UNIQUEPHRASE PAYMENT", "Expenses:Misc", "low", withdrawal="10.00",
    )
    historical_pairs = [_mk("uniquephrase payment", EXPENSE_VENDOR, frequency=1)]
    _apply_history_pass(mapped_csv, historical_pairs)

    with open(mapped_csv, newline="", encoding="utf-8") as f:
        row = list(csv.DictReader(f))[0]

    assert row["Account"] == "Expenses:Misc"
    assert row["Confidence"] == "low"
    assert row["Account"] != EXPENSE_VENDOR


def test_low_rule_replaced_when_history_clears_all_thresholds(tmp_path):
    # Converse of the two tests above: strong, unambiguous, repeated history
    # evidence (support >= 2, single discriminating token, no spread) DOES
    # clear MAP-11's thresholds, so it is allowed to replace a weak rule
    # guess.
    mapped_csv = tmp_path / "mapped.csv"
    _write_single_row_mapped_csv(
        mapped_csv, "SPECIALPAYEE PAYMENT", "Expenses:WrongGuess", "low", withdrawal="10.00",
    )
    historical_pairs = [
        _mk("specialpayee invoice one", EXPENSE_VENDOR, frequency=5),
        _mk("specialpayee invoice two", EXPENSE_VENDOR, frequency=5),
    ]
    _apply_history_pass(mapped_csv, historical_pairs)

    with open(mapped_csv, newline="", encoding="utf-8") as f:
        row = list(csv.DictReader(f))[0]

    assert row["Account"] == EXPENSE_VENDOR
    assert row["Confidence"] == "history"
    assert row["Account"] != "Expenses:WrongGuess"


# ---------------------------------------------------------------------------
# 3. RTGS/UTR-prefix collision.
#
# RED FLAG (real defect found and fixed by this follow-up):
# _historical_prefix_match's exact-normalised-match / prefix-overlap branch
# tracked only a single running "best" candidate and never checked for a
# tie between the top two. Two RTGS narrations to DIFFERENT counterparties
# that share a UTR-like reference prefix (e.g.
# "RTGS/ABCDR12345678901/PARTYA" and "RTGS/ABCDR12345678902/PARTYB") both
# normalise to the identical prefix "RTGS/ABCDR" once the trailing
# reference-number run is stripped -- and the function would silently pick
# whichever historical account happened to be scored first (or, on a tie
# score+frequency, essentially arbitrarily), routing a brand-new
# counterparty's RTGS payment onto an unrelated bond or loan account with
# NO real evidence behind it. Confirmed by direct repro before this fix:
# swapping the two historical entries' list order flipped the returned
# account between the Bond and Loan accounts for the exact same live row.
# Fixed in agent._historical_prefix_match by scoring ALL candidates and
# refusing to guess when the top two tie on (score, frequency) and name
# different accounts -- mirroring the tie-detection the keyword-fallback
# branch already had. This test is the regression guard for that fix.
# ---------------------------------------------------------------------------

def test_rtgs_shared_prefix_never_collapses_onto_one_account_via_prefix_match():
    historical_pairs = [
        _mk("RTGS/ABCDR12345678901/PARTYA", BOND_ACCOUNT, frequency=5),
        _mk("RTGS/ABCDR12345678902/PARTYB", LOAN_ACCOUNT, frequency=5),
    ]
    new_counterparty_desc = "RTGS/ABCDR12345678999/PARTYC"

    match_forward = agent._historical_prefix_match(new_counterparty_desc, historical_pairs)
    match_reversed = agent._historical_prefix_match(new_counterparty_desc, list(reversed(historical_pairs)))

    # Never guess an unrelated bond/loan account for a brand-new
    # counterparty on nothing but a shared reference-number prefix.
    assert match_forward is None
    assert match_reversed is None

    # Independent of the defect above: the result must not depend on the
    # caller's list order (a match, if any were ever returned, must be
    # deterministic).
    assert match_forward == match_reversed


def test_rtgs_shared_prefix_never_collapses_via_history_token_matcher():
    # Same shared-prefix shape, this time through the Bayesian history
    # matcher: the two counterparties' descriptions share only generic
    # "RTGS" tokens (no discriminating token in common with an unseen third
    # counterparty), so the row must stay unmatched -- never assigned to
    # either PARTYA's or PARTYB's unrelated account.
    history = [
        _mk("RTGS ABCDR PARTYA", BOND_ACCOUNT, frequency=20),
        _mk("RTGS ABCDR PARTYB", LOAN_ACCOUNT, frequency=20),
    ]
    model = agent._build_history_token_model(history)
    match = agent._history_token_match("RTGS ABCDR PARTYC", model)
    assert match is None


def test_rtgs_same_account_duplicate_descriptions_are_not_falsely_flagged_ambiguous():
    # Guard against an over-broad fix: several historical rows for the SAME
    # account (different reference numbers, same counterparty) must still
    # match -- only a tie between two DIFFERENT accounts should abstain.
    historical_pairs = [
        _mk("RTGS/ABCDR12345678901/PARTYA", BOND_ACCOUNT, frequency=5),
        _mk("RTGS/ABCDR12345678903/PARTYA", BOND_ACCOUNT, frequency=3),
    ]
    match = agent._historical_prefix_match("RTGS/ABCDR12345678999/PARTYA", historical_pairs)
    assert match is not None
    assert match["account"] == BOND_ACCOUNT
    assert match["confidence"] == "weak"


# ---------------------------------------------------------------------------
# 4. Self-transfer / IFSC routing.
# ---------------------------------------------------------------------------

OWN_NAME = "maskedname"  # stands in for the book owner's own name in narrations

# Real-shaped fixture (round 2, mirrors the actual real-book re-measurement,
# names/codes masked): SBM self-transfers are FREQUENT (12) in this book's
# history and carry no code any own account's name spells out ("stcb", which
# no own account claims). HSBC self-transfer history is CONCENTRATED 10-to-1
# on just ONE of the book's two HSBC BANK-type accounts (the current
# account, never the FD) -- exactly the real book's own numbers. HDFC
# likewise concentrates on its current account, not its FD. The self-transfer
# marker words PLUS the user's own name also appear on ordinary
# Income/Expenses/Liabilities narrations elsewhere in the book -- exactly the
# shape that made the old all-or-nothing rule's `candidates` always None, and
# neither HSBC_FD nor HDFC_FD ever appears as a self-transfer target in
# history at all (zero evidence), which is what the round-2 tie-break must
# use to prefer the evidenced account over its same-bank sibling.
_REAL_SHAPED_SELF_TRANSFER_HISTORY = [
    _mk(f"xfer to self {OWN_NAME} stcb0abc999", OWN_SBM, frequency=12),
    _mk(f"xfer to self {OWN_NAME} hsbc transfer", OWN_HSBC, frequency=10),
    _mk(f"xfer to self {OWN_NAME} hdfc transfer", OWN_HDFC, frequency=2),
    _mk(f"salary payment to {OWN_NAME}", INCOME_SALARY, frequency=5),
    _mk(f"loan emi to {OWN_NAME}", LOAN_ACCOUNT, frequency=4),
    _mk(f"vendor payment to {OWN_NAME}", EXPENSE_VENDOR, frequency=3),
]


def _real_shaped_self_transfer_model():
    return agent._build_history_token_model(_REAL_SHAPED_SELF_TRANSFER_HISTORY)


# The FULL real-book-shaped own_bank_accounts set: two accounts per bank for
# ICICI/HDFC/HSBC (only HSBC/HDFC are exercised by history here, but the
# real book has an ICICI pair too -- omitted since no test below needs it),
# plus SBM/Kotak/Barclays as BANK-type accounts outside any name pattern.
_REAL_SHAPED_OWN_BANK_ACCOUNTS = {
    OWN_HSBC, OWN_HSBC_FD, OWN_HDFC, OWN_HDFC_FD,
    OWN_SBM, OWN_KOTAK, OWN_KOTAK_FD, OWN_BARCLAYS,
}


def test_self_transfer_routes_hsbc_ifsc_to_the_evidenced_hsbc_account_real_shaped():
    # The exact real-book defect, round 2: "hsbc" substring-matches BOTH
    # OWN_HSBC and OWN_HSBC_FD (two real BANK-type accounts), so the round-1
    # fix's ambiguity check alone would still abstain here and let the row
    # fall through to the weak prefix matcher -> SBM. The evidence-based tie
    # -break must prefer OWN_HSBC (10 historical self-transfers) over
    # OWN_HSBC_FD (zero) -- never SBM, never a name-order pick.
    model = _real_shaped_self_transfer_model()
    match = agent._history_token_match(
        f"Xfer to self {OWN_NAME} HSBC0NEW456",
        model,
        own_bank_accounts=_REAL_SHAPED_OWN_BANK_ACCOUNTS,
    )
    assert match is not None, "evidence tie-break must resolve the two-HSBC-account ambiguity"
    assert match["account"] == OWN_HSBC
    assert match["account"] != OWN_HSBC_FD
    assert match["account"] != OWN_SBM
    assert match["account"] != OWN_HDFC


def test_self_transfer_routes_hdfc_ifsc_to_the_evidenced_hdfc_account_real_shaped():
    model = _real_shaped_self_transfer_model()
    match = agent._history_token_match(
        f"Xfer to self {OWN_NAME} HDFC0NEW789",
        model,
        own_bank_accounts=_REAL_SHAPED_OWN_BANK_ACCOUNTS,
    )
    assert match is not None
    assert match["account"] == OWN_HDFC
    assert match["account"] != OWN_HDFC_FD
    assert match["account"] != OWN_SBM
    assert match["account"] != OWN_HSBC


def test_self_transfer_without_own_bank_accounts_kwarg_abstains():
    # If the caller doesn't wire in own_bank_accounts (e.g. an older call
    # site, or an extractor run that found none), the fallback must abstain
    # rather than silently reproduce the old always-None behaviour as a
    # false "unmatched is safe" signal -- this documents that own_bank_
    # accounts is REQUIRED for the fallback to ever engage.
    model = _real_shaped_self_transfer_model()
    match = agent._history_token_match(f"Xfer to self {OWN_NAME} HSBC0NEW456", model)
    assert match is None


def test_ifsc_code_tie_between_two_own_accounts_with_equal_evidence_stays_unmatched():
    # Round 2: when the historical-evidence tie-break itself is TIED (both
    # hit accounts have the SAME support), the row must still stay unmatched
    # -- never resolved by name order, and never rescued downstream either
    # (see the _ifsc_contradiction assertion below, mirroring how Step 4.9
    # would revert a later pass's guess in this exact shape).
    tied_a = "Assets:Current Assets:Cash and Bank:HSBC Bank - Alpha"
    tied_b = "Assets:Current Assets:Cash and Bank:HSBC Bank - Beta"
    own_bank_accounts = {tied_a, tied_b, OWN_SBM}
    history = [
        _mk(f"xfer to self {OWN_NAME} alpha route", tied_a, frequency=5),
        _mk(f"xfer to self {OWN_NAME} beta route", tied_b, frequency=5),
        _mk(f"xfer to self {OWN_NAME} stcb0abc999", OWN_SBM, frequency=12),
    ]
    model = agent._build_history_token_model(history)
    match = agent._history_token_match(
        f"Xfer to self {OWN_NAME} HSBC0NEW456",
        model,
        own_bank_accounts=own_bank_accounts,
    )
    assert match is None
    # Not rescued by prefix onto SBM afterwards either: since HSBC is a code
    # some own account visibly claims (both tied ones do), a later guess of
    # SBM would be flagged as a contradiction by Step 4.9's guard.
    assert agent._ifsc_contradiction(
        f"Xfer to self {OWN_NAME} HSBC0NEW456", OWN_SBM, own_bank_accounts,
    ) is True


def test_ifsc_code_matching_no_own_account_not_rescued_onto_another_bank():
    # A code that matches NO own account (e.g. a third-party bank the row's
    # counterparty happens to bank with) must stay unmatched -- and,
    # crucially, must never be "rescued" by silently landing on some OTHER
    # own bank account just because the shape test passed.
    model = _real_shaped_self_transfer_model()
    match = agent._history_token_match(
        f"Xfer to self {OWN_NAME} SBIN0NEW111",
        model,
        own_bank_accounts=_REAL_SHAPED_OWN_BANK_ACCOUNTS,
    )
    assert match is None


def test_ifsc_bank_code_matching_no_own_account_stays_unmatched():
    # A bank code that has never been seen at all, and does not appear as a
    # substring of any own-account leaf name -- must stay unmatched, never
    # guessed.
    codes = {"ifsc:sbin"}
    candidates = {OWN_HSBC, OWN_SBM, OWN_HDFC}
    assert agent._literal_bank_code_match(codes, candidates) is None


def test_ifsc_bank_code_matching_two_own_accounts_stays_unmatched():
    # A code that is ambiguous -- it substring-matches TWO own account leaf
    # names -- must stay unmatched rather than guess between them.
    candidates = {OWN_HSBC, OWN_HSBC_BUSINESS, OWN_SBM}
    match = agent._literal_bank_code_match({"ifsc:hsbc"}, candidates)
    assert match is None


def test_self_transfer_with_no_bank_marker_at_all_stays_unmatched():
    # "xfer to self" alone, with no IFSC token whatsoever: _history_token_
    # match requires an ifsc: token to even attempt the self-transfer
    # fallback (there is nothing else to disambiguate the tied plain-token
    # evidence), so this must abstain -- never guess one of the three own
    # accounts.
    model = _real_shaped_self_transfer_model()
    match = agent._history_token_match(
        f"xfer to self {OWN_NAME}",
        model,
        own_bank_accounts=_REAL_SHAPED_OWN_BANK_ACCOUNTS,
    )
    assert match is None


def test_non_self_transfer_narration_with_incidental_ifsc_not_routed_to_own_account():
    # A payment TO a vendor, whose plain tokens' evidence mass is
    # overwhelmingly non-asset (Expenses) even though a marker word ("self")
    # also happens to touch the three own accounts a little, must never be
    # treated as self-transfer-shaped just because its narration happens to
    # contain an IFSC-shaped code for a known own account. Exercises the
    # real statistical gate (own_bank_accounts supplied), not the "no kwarg"
    # short-circuit.
    plain_tokens = {"self", "vendor"}
    model = {
        "self": {OWN_HSBC: 1, OWN_SBM: 1, OWN_HDFC: 1},
        "vendor": {EXPENSE_VENDOR: 50},
    }
    candidates = agent._self_transfer_candidates(
        plain_tokens, model, own_bank_accounts=_REAL_SHAPED_OWN_BANK_ACCOUNTS
    )
    assert candidates is None


def test_self_transfer_candidates_uses_majority_of_evidence_mass_not_unanimity():
    # RED FLAG fix: the OLD rule rejected the whole description the instant
    # ANY led-to account was non-asset, which never held on real data (see
    # module docstring). The NEW rule tolerates a MINORITY of non-asset mass
    # and only rejects when non-asset mass is the majority.
    plain_tokens = {"mixed"}
    own_bank_accounts = {OWN_HSBC, OWN_SBM}

    # 70% asset mass -- clears HISTORY_SELF_TRANSFER_MIN_ASSET_FRACTION
    # (0.6) despite NOT being unanimous -- the old rule would have rejected
    # this outright.
    majority_asset_model = {"mixed": {OWN_HSBC: 7, EXPENSE_VENDOR: 3}}
    result = agent._self_transfer_candidates(
        plain_tokens, majority_asset_model, own_bank_accounts=own_bank_accounts
    )
    # Candidates are the FULL own_bank_accounts set, never just the
    # accounts "mixed" happened to reach (OWN_HSBC only, here) -- the point
    # of the fallback is to reach a bank/branch this exact code has never
    # been seen at before.
    assert result == own_bank_accounts

    # 30% asset mass -- does not clear the threshold, correctly rejected.
    minority_asset_model = {"mixed": {OWN_HSBC: 3, EXPENSE_VENDOR: 7}}
    assert agent._self_transfer_candidates(
        plain_tokens, minority_asset_model, own_bank_accounts=own_bank_accounts
    ) is None


# ---------------------------------------------------------------------------
# 5. Direction: self-transfer / sweep is never assigned to Income/Expenses.
# ---------------------------------------------------------------------------

def test_self_transfer_match_never_lands_on_income_or_expense_account():
    model = _real_shaped_self_transfer_model()
    match = agent._history_token_match(
        f"Xfer to self {OWN_NAME} HSBC0NEW456",
        model,
        own_bank_accounts=_REAL_SHAPED_OWN_BANK_ACCOUNTS,
    )
    assert match is not None
    top_level = agent._strip_root(match["account"]).split(":", 1)[0]
    assert top_level == "Assets"
    assert top_level not in ("Income", "Expenses")


def test_sweep_matches_never_land_on_income_or_expense_account():
    # Reuses the sweep shapes from test_history_token_matcher.py's own
    # fixtures (mapping only to an Assets FD account) to confirm the same
    # invariant holds for the sweep-narration matcher path, not just
    # self-transfer/IFSC.
    fd_account = "Assets:Current Assets:Fixed Deposits:ICICI Bank - FD"
    history = [
        _mk("111111111111: Rev Sweep From", fd_account, frequency=40),
        _mk("AUTOSWEEP TO 444444444444 JANE Q SAMPLE", fd_account, frequency=40),
    ]
    model = agent._build_history_token_model(history)
    for desc in ("999999999999: Rev Sweep From", "AUTOSWEEP TO 121212121212 JOHN Q SAMPLE"):
        match = agent._history_token_match(desc, model)
        assert match is not None
        top_level = agent._strip_root(match["account"]).split(":", 1)[0]
        assert top_level == "Assets"
        assert top_level not in ("Income", "Expenses")


def test_self_transfer_candidates_can_never_surface_an_income_or_expense_account():
    # Structural guarantee, independent of any specific description:
    # _self_transfer_candidates only ever returns exactly `own_bank_accounts`
    # (a set the caller itself controls) or None -- it can never hand back a
    # mixed or non-asset candidate pool, no matter what the model contains.
    own_bank_accounts = {OWN_HSBC}

    # Bare-majority (50/50) mass is BELOW the 0.6 threshold -- still
    # correctly rejected, not just because income/expense is "present" but
    # because the mass isn't clearly asset-majority.
    plain_tokens = {"self"}
    model_with_income = {"self": {OWN_HSBC: 5, INCOME_SALARY: 5}}
    assert agent._self_transfer_candidates(
        plain_tokens, model_with_income, own_bank_accounts=own_bank_accounts
    ) is None

    model_with_expense = {"self": {OWN_HSBC: 5, EXPENSE_VENDOR: 5}}
    assert agent._self_transfer_candidates(
        plain_tokens, model_with_expense, own_bank_accounts=own_bank_accounts
    ) is None

    # Even when the shape test DOES pass (majority asset mass despite income
    # also being present), the returned set is exactly own_bank_accounts --
    # INCOME_SALARY can never leak into it.
    model_majority_asset = {"self": {OWN_HSBC: 8, INCOME_SALARY: 2}}
    result = agent._self_transfer_candidates(
        plain_tokens, model_majority_asset, own_bank_accounts=own_bank_accounts
    )
    assert result == own_bank_accounts
    assert INCOME_SALARY not in result


# ---------------------------------------------------------------------------
# 7. _ifsc_contradiction (Step 4.9 guard): "HSBC IFSC -> SBM must be
#    impossible by any path, including prefix, keyword and LLM." Once Step
#    3.6's dedicated self-transfer/IFSC route has abstained on a row, no
#    LATER pass (smart pattern, weak prefix/keyword fallback, or the LLM) may
#    silently ship a guess landing it on a DIFFERENT own bank account whose
#    bank code contradicts the row's own IFSC. This is the hard downstream
#    gate for that -- unit-tested directly here, and exercised through the
#    real run() pipeline in section 8 below.
# ---------------------------------------------------------------------------

def test_ifsc_contradiction_flags_a_guess_onto_the_wrong_own_bank():
    # A row whose description carries an HSBC IFSC, but whose Account was
    # (hypothetically) resolved by some other pass to SBM -- exactly the
    # live defect shape -- must be flagged as a contradiction.
    assert agent._ifsc_contradiction(
        f"Xfer to self {OWN_NAME} HSBC0NEW456",
        OWN_SBM,
        _REAL_SHAPED_OWN_BANK_ACCOUNTS,
    ) is True


def test_ifsc_contradiction_does_not_flag_the_correct_own_bank():
    assert agent._ifsc_contradiction(
        f"Xfer to self {OWN_NAME} HSBC0NEW456",
        OWN_HSBC,
        _REAL_SHAPED_OWN_BANK_ACCOUNTS,
    ) is False


def test_ifsc_contradiction_ignores_accounts_outside_own_bank_accounts():
    # A row resolved to a non-bank account (Expenses, say) is not this
    # guard's concern at all -- it only ever polices own-bank-vs-own-bank
    # contradictions, never overrides an Income/Expenses resolution.
    assert agent._ifsc_contradiction(
        f"Xfer to self {OWN_NAME} HSBC0NEW456",
        EXPENSE_VENDOR,
        _REAL_SHAPED_OWN_BANK_ACCOUNTS,
    ) is False


def test_ifsc_contradiction_ignores_descriptions_with_no_ifsc():
    assert agent._ifsc_contradiction(
        f"xfer to self {OWN_NAME}",
        OWN_SBM,
        _REAL_SHAPED_OWN_BANK_ACCOUNTS,
    ) is False


def test_ifsc_contradiction_does_not_revert_an_stcb_row_no_own_account_spells_out():
    # Round 2, requirement #3: SBM's own account name is simply "SBM Bank -
    # 0009999" -- it never spells out "stcb" (the real book's SBM branch
    # code) anywhere in its own name, and NO other own account does either.
    # A code that no own account carries at all is not evidence the guess is
    # wrong -- it just means the code isn't literally in any account's name.
    # This must NOT be flagged as a contradiction, even though the guessed
    # account (SBM) doesn't itself carry "stcb".
    assert agent._ifsc_contradiction(
        f"Xfer to self {OWN_NAME} STCB0ABC999",
        OWN_SBM,
        _REAL_SHAPED_OWN_BANK_ACCOUNTS,
    ) is False


# ---------------------------------------------------------------------------
# 8. End-to-end: agent.run() itself, real-shaped fixture. Drives the REAL
#    pipeline (not a re-derivation of Step 3.6/4.9's logic) so a future
#    change that reintroduces the defect at the wiring level -- not just in
#    _self_transfer_candidates/_history_token_match themselves -- would be
#    caught here too.
# ---------------------------------------------------------------------------

def test_run_end_to_end_routes_hsbc_and_hdfc_self_transfers_never_to_sbm(tmp_path, monkeypatch):
    import csv as _csv
    import sys as _sys
    from pathlib import Path as _Path

    ROOT_ = _Path(__file__).resolve().parent.parent.parent
    SRC_ = ROOT_ / "src"
    AGENTS_ROOT_ = SRC_ / "agents"
    if str(AGENTS_ROOT_) not in _sys.path:
        _sys.path.insert(0, str(AGENTS_ROOT_))

    import skill_gnucash_xml_extractor.agent as xml_agent_mod
    import skill_gnucash_mapping_generator.agent as mapgen_mod
    import skill_gnucash_account_mapper.persistent_rules as persistent_rules_mod

    def fake_parse_gnucash_file(path, gnucash_bank_account=None):
        return {
            "mappings": {"BankX": list(_REAL_SHAPED_SELF_TRANSFER_HISTORY)},
            "own_bank_accounts": sorted(_REAL_SHAPED_OWN_BANK_ACCOUNTS),
        }

    def fake_generate_rules(extractor_output, min_freq=1):
        # No rules at all: the ONLY thing that can resolve the self-transfer
        # rows is the real Step 3.6 history pass (or, if that fails, Step 4
        # + the Step 4.9 guard) -- never a rule coincidentally matching.
        return {}

    def fake_merge_auto_rules(gnucash_file, rules_by_bank, config_path=None):
        return rules_by_bank

    def fake_load_overrides(gnucash_file, config_path=None):
        return []

    def fake_migrate_legacy_overrides(gnucash_file, config_path=None):
        return 0

    def fake_rules_path(gnucash_file, config_path=None):
        return tmp_path / "fake_persistent_rules.yaml"

    monkeypatch.setattr(xml_agent_mod, "parse_gnucash_file", fake_parse_gnucash_file)
    monkeypatch.setattr(mapgen_mod, "generate_rules", fake_generate_rules)
    monkeypatch.setattr(persistent_rules_mod, "merge_auto_rules", fake_merge_auto_rules)
    monkeypatch.setattr(persistent_rules_mod, "load_overrides", fake_load_overrides)
    monkeypatch.setattr(persistent_rules_mod, "migrate_legacy_overrides", fake_migrate_legacy_overrides)
    monkeypatch.setattr(persistent_rules_mod, "rules_path", fake_rules_path)

    hsbc_desc = f"Xfer to self {OWN_NAME} HSBC0NEW456"
    hdfc_desc = f"Xfer to self {OWN_NAME} HDFC0NEW789"
    canonical_rows = [
        {"Date": "01-04-2025", "Description": hsbc_desc, "Withdrawal": "5000.00", "Deposit": ""},
        {"Date": "02-04-2025", "Description": hdfc_desc, "Withdrawal": "3000.00", "Deposit": ""},
    ]
    canonical_csv = tmp_path / "canonical.csv"
    with open(canonical_csv, "w", newline="", encoding="utf-8") as f:
        writer = _csv.DictWriter(f, fieldnames=["Date", "Description", "Withdrawal", "Deposit"])
        writer.writeheader()
        writer.writerows(canonical_rows)

    output_path = tmp_path / "mapped.csv"

    from conftest import ScriptedLLM, _fake_resolve_llm_endpoint_config
    import urllib.request

    fake_llm = ScriptedLLM()
    fake_llm.queue("0")
    fake_llm.queue("0")

    def _blocked_urlopen(*a, **k):
        raise AssertionError("a real network call was attempted from a ScriptedLLM test")

    monkeypatch.setattr(agent, "_llm_chat", fake_llm)
    monkeypatch.setattr(agent, "_resolve_llm_endpoint_config", _fake_resolve_llm_endpoint_config)
    monkeypatch.setattr(agent, "_emit_mapper_progress", lambda msg: None)
    monkeypatch.setattr(urllib.request, "urlopen", _blocked_urlopen)

    agent.run(
        gnucash_file=str(tmp_path / "synthetic-nonexistent.gnucash"),
        canonical_csv=str(canonical_csv),
        output_path=str(output_path),
        config_path="fake-config.yaml",
        model_override=None,
        bank_name=None,
        gnucash_bank_account=None,
    )

    with open(output_path, newline="", encoding="utf-8") as f:
        mapped_rows = list(_csv.DictReader(f))

    by_desc = {row["Description"]: row for row in mapped_rows}
    hsbc_row = by_desc[hsbc_desc]
    hdfc_row = by_desc[hdfc_desc]

    assert hsbc_row["Account"] == agent._strip_root(OWN_HSBC)
    assert hsbc_row["Account"] != agent._strip_root(OWN_SBM)

    assert hdfc_row["Account"] == agent._strip_root(OWN_HDFC)
    assert hdfc_row["Account"] != agent._strip_root(OWN_SBM)
