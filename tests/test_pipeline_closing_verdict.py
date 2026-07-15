"""
tests/test_pipeline_closing_verdict.py -- unit tests for
skill_gnucash_pipeline.agent.final_closing_balance_verdict (Defect 1 fix).

Previously the pipeline's final check compared the statement-derived
canonical CSV's closing balance against itself (via a sidecar written by the
same extraction pass) -- circular, and blind to a real opening-balance gap
between the statement and the GnuCash book. This verifies the fixed verdict:
post-import GnuCash balance vs the statement's own closing balance, with any
unresolved opening gap always winning over a coincidentally-matching total.

Covers the exact reported scenario: book pre-import 4381.17, statement opens
4383.17 (an unexplained pounds-2 gap the pipeline itself flags), 4 new
transactions net +115.00, statement closes at 4498.17. Post-import book is
4381.17 + 115.00 = 4496.17 -- which does NOT match the statement's 4498.17
(diff exactly the unreconciled gap) -- so the verdict must be AMBER/RED, never
a false green.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_pipeline.agent import final_closing_balance_verdict  # noqa: E402


def _rows(*pairs):
    """pairs of (deposit, withdrawal) -> canonical-shaped row dicts."""
    return [{"Deposit": str(d), "Withdrawal": str(w)} for d, w in pairs]


def test_unresolved_opening_gap_always_wins_never_false_green():
    """The exact reported BoB scenario: dedup found 0 overlaps, so the ₹2
    opening gap must survive into the final verdict as AMBER, not evaporate."""
    recon = {"account_found": True, "gnucash_balance": 4381.17}
    final_rows = _rows((50, 0), (0, 20), (100, 0), (0, 15))  # net +115.00
    verdict = final_closing_balance_verdict(
        recon, final_rows, stmt_closing=4498.17, unresolved_opening_gap=2.00
    )
    assert "unreconciled 2.00" in verdict
    assert "⚠" in verdict
    assert "VERIFIED" not in verdict


def test_clean_tie_is_verified():
    recon = {"account_found": True, "gnucash_balance": 1000.00}
    final_rows = _rows((200, 0), (0, 50))  # net +150.00
    verdict = final_closing_balance_verdict(
        recon, final_rows, stmt_closing=1150.00, unresolved_opening_gap=None
    )
    assert "VERIFIED (independent)" in verdict


def test_gap_explained_by_dedup_is_verified():
    """If dedup resolved the opening gap (unresolved_opening_gap is None,
    since the caller clears it once dedup accounts for the overlap), the
    post-import math should be checked normally."""
    recon = {"account_found": True, "gnucash_balance": 500.00}
    final_rows = _rows((25, 0),)  # net +25.00
    verdict = final_closing_balance_verdict(
        recon, final_rows, stmt_closing=525.00, unresolved_opening_gap=None
    )
    assert "VERIFIED (independent)" in verdict


def test_genuine_mismatch_flagged_even_without_known_gap():
    recon = {"account_found": True, "gnucash_balance": 500.00}
    final_rows = _rows((25, 0),)  # net +25.00 -> post-import 525.00
    verdict = final_closing_balance_verdict(
        recon, final_rows, stmt_closing=600.00, unresolved_opening_gap=None
    )
    assert "CLOSING BALANCE MISMATCH" in verdict
    assert "❌" in verdict


def test_account_not_found_short_circuits():
    recon = {"account_found": False, "gnucash_balance": 0.0}
    verdict = final_closing_balance_verdict(recon, [], stmt_closing=100.0, unresolved_opening_gap=None)
    assert "account not found" in verdict


def test_no_independent_closing_balance_is_not_a_false_ok():
    recon = {"account_found": True, "gnucash_balance": 500.00}
    verdict = final_closing_balance_verdict(recon, _rows((10, 0)), stmt_closing=None, unresolved_opening_gap=None)
    assert "No independent statement closing balance" in verdict
    assert "VERIFIED" not in verdict
