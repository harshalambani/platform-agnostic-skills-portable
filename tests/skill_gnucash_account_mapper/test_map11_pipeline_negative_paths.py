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
     _literal_bank_code_match, _history_token_match): correct-bank routing,
     no-bank-code-match, ambiguous-two-bank-code-match, no-bank-marker at
     all, and a non-self-transfer narration that happens to contain an IFSC.
  5. Direction: a self-transfer/sweep match is never assigned to an Income
     or Expense account.

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

def _own_asset_history_model():
    # Three own accounts, each reached via generic self-transfer plain
    # tokens ("xfer", "to", "self") with EQUAL frequency (so plain-token
    # evidence ties across all three and the Bayesian pass abstains on its
    # own, forcing the self-transfer/IFSC fallback to decide), plus a
    # distinct synthetic IFSC-shaped code per account.
    history = [
        _mk("xfer to self hsbc0abc123", OWN_HSBC, frequency=5),
        _mk("xfer to self stcb0xyz567", OWN_SBM, frequency=5),
        _mk("xfer to self hdfc0def901", OWN_HDFC, frequency=5),
    ]
    return agent._build_history_token_model(history)


def test_self_transfer_routes_to_correct_own_account_never_others_hsbc():
    model = _own_asset_history_model()
    match = agent._history_token_match("xfer to self hsbc0new456", model)
    assert match is not None
    assert match["account"] == OWN_HSBC
    assert match["account"] != OWN_SBM
    assert match["account"] != OWN_HDFC


def test_self_transfer_routes_to_correct_own_account_never_others_sbm():
    model = _own_asset_history_model()
    match = agent._history_token_match("xfer to self stcb0new987", model)
    assert match is not None
    assert match["account"] == OWN_SBM
    assert match["account"] != OWN_HSBC
    assert match["account"] != OWN_HDFC


def test_self_transfer_routes_to_correct_own_account_never_others_hdfc():
    model = _own_asset_history_model()
    match = agent._history_token_match("xfer to self hdfc0new112", model)
    assert match is not None
    assert match["account"] == OWN_HDFC
    assert match["account"] != OWN_HSBC
    assert match["account"] != OWN_SBM


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
    # evidence), so this must abstain -- never guess one of the three tied
    # own accounts.
    model = _own_asset_history_model()
    match = agent._history_token_match("xfer to self", model)
    assert match is None


def test_non_self_transfer_narration_with_incidental_ifsc_not_routed_to_own_account():
    # A payment TO a vendor, whose plain tokens have only ever led to a
    # non-asset (Expenses) account in history, must never be treated as
    # self-transfer-shaped just because its narration happens to contain an
    # IFSC-shaped code for a known own account.
    plain_tokens = {"self", "vendor"}
    model = {
        "self": {OWN_HSBC: 5, OWN_SBM: 5, OWN_HDFC: 5},
        "vendor": {EXPENSE_VENDOR: 5},
    }
    candidates = agent._self_transfer_candidates(plain_tokens, model)
    assert candidates is None


def test_self_transfer_candidates_requires_every_led_to_account_be_an_asset():
    # Direct unit check of the guard itself: if the union of accounts a
    # description's plain tokens have ever led to includes even one
    # non-asset account, the whole description is rejected as
    # self-transfer-shaped.
    plain_tokens = {"mixed"}
    model = {"mixed": {OWN_HSBC: 3, EXPENSE_VENDOR: 3}}
    assert agent._self_transfer_candidates(plain_tokens, model) is None

    all_asset_model = {"mixed": {OWN_HSBC: 3, OWN_SBM: 3}}
    result = agent._self_transfer_candidates(plain_tokens, all_asset_model)
    assert result == {OWN_HSBC, OWN_SBM}


# ---------------------------------------------------------------------------
# 5. Direction: self-transfer / sweep is never assigned to Income/Expenses.
# ---------------------------------------------------------------------------

def test_self_transfer_match_never_lands_on_income_or_expense_account():
    model = _own_asset_history_model()
    match = agent._history_token_match("xfer to self hsbc0new456", model)
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
    # Structural guarantee, independent of any specific description: even if
    # a plain token's historical accounts include Income or Expenses
    # entries alongside asset ones, _self_transfer_candidates rejects the
    # whole set rather than ever returning a mixed or non-asset candidate
    # pool that a downstream match could land on.
    plain_tokens = {"self"}
    model_with_income = {"self": {OWN_HSBC: 5, INCOME_SALARY: 5}}
    assert agent._self_transfer_candidates(plain_tokens, model_with_income) is None

    model_with_expense = {"self": {OWN_HSBC: 5, EXPENSE_VENDOR: 5}}
    assert agent._self_transfer_candidates(plain_tokens, model_with_expense) is None
