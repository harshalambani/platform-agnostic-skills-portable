"""
tests/test_import_ready_standardization.py — guards for the standardized
GnuCash import-ready column schema.

Background: different banks were emitting the import-ready CSV with the
``Transfer Account`` column in different positions (HDFC after Account, BoB
last or absent), because the mapper only added it when the bank account
resolved and the Review-Mappings save appended new keys at the end. This suite
locks in the single-source-of-truth layout:

  * IMPORT_READY_FIELDS is the one ordering (Transfer Account right after
    Account); both the mapper write and the Review-save route through
    order_import_ready_headers.
  * The mapper ALWAYS emits Transfer Account (blank when the bank is
    unresolved) so the shape is invariant.
  * HDFC and ICICI source their canonical header from canonical_io rather than
    a duplicated literal.
  * The pipeline surfaces a visible warning when the bank account can't be
    found in the .gnucash book.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# _save_changes lazily imports from the `agents` package, so src/ must be on the
# path alongside the repo root and ui/.
for _p in (ROOT, ROOT / "src", ROOT / "ui"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.canonical_io import CANONICAL_FIELDS, IMPORT_READY_FIELDS  # noqa: E402


# ---------------------------------------------------------------------------
# Bank skills source their schema from the shared constant (no drift).
# ---------------------------------------------------------------------------

def test_hdfc_uses_shared_canonical_schema():
    from agents.skill_hdfc.agent import CANONICAL_COLS  # noqa: PLC0415
    assert CANONICAL_COLS == list(CANONICAL_FIELDS)


def test_icici_imports_shared_canonical_schema():
    import agents.skill_icici.agent as icici  # noqa: PLC0415
    # ICICI now writes its header from the shared constant, not a local literal.
    assert icici.CANONICAL_FIELDS == CANONICAL_FIELDS


# ---------------------------------------------------------------------------
# Mapper: Transfer Account is unconditional; ordering is delegated to the
# shared helper. (Source-level guards — a full mapper_run needs a real .gnucash
# book; the ordering behaviour itself is covered functionally in
# test_canonical_io.py and the review-save test below.)
# ---------------------------------------------------------------------------

def _mapper_source() -> str:
    return (ROOT / "src" / "agents" / "skill_gnucash_account_mapper" / "agent.py").read_text(
        encoding="utf-8"
    )


def test_mapper_always_emits_transfer_account():
    src = _mapper_source()
    # Transfer Account is assigned for every row, blank when unresolved.
    assert "row['Transfer Account'] = gnucash_bank_account or ''" in src
    # The old positional-reposition logic (which only ran when Transfer Account
    # was present) must be gone.
    assert "_REPOSITION" not in src


def test_mapper_write_uses_shared_ordering():
    src = _mapper_source()
    assert "order_import_ready_headers(mapped_rows[0].keys())" in src


# ---------------------------------------------------------------------------
# Pipeline surfaces a visible warning when the bank account isn't found.
# ---------------------------------------------------------------------------

def test_pipeline_warns_when_bank_account_not_found():
    src = (ROOT / "src" / "agents" / "skill_gnucash_pipeline" / "agent.py").read_text(
        encoding="utf-8"
    )
    assert "Couldn't find a '{bank}' bank account in the GnuCash book" in src
    # The warning must name what gets turned off so the reduced output is explained.
    assert "Transfer Account is left blank" in src


# ---------------------------------------------------------------------------
# Review-save reorders the re-exported CSV to the import-ready schema.
# ---------------------------------------------------------------------------

def _rows_transfer_account_last() -> list[dict]:
    """A payload row with Transfer Account in the WRONG place (last) — the bug."""
    return [{
        "Date": "2025-05-01",
        "Transaction ID": "",
        "Description": "COFFEE",
        "Account": "Expense:Food",
        "Deposit": "",
        "Withdrawal": "100.00",
        "Balance": "900.00",
        "Currency": "INR",
        "Confidence": "high",
        "MatchReason": "rule",
        "Transfer Account": "Assets:Cash and Bank:HDFC Bank - 1579",
    }]


def _save(tmp_path: Path, all_rows: list[dict]) -> Path:
    from ui.tabs import gnucash_review as gr_review  # noqa: PLC0415
    csv_p = tmp_path / "2026-01-01-TEST-GnuCash_import_ready.csv"
    csv_p.write_text("placeholder\n", encoding="utf-8")
    payload = {
        "gnucash_file": str(tmp_path / "nonexistent.gnucash"),
        "csv_path": str(csv_p),
        # Non-empty changes so _save_changes proceeds to the re-export.
        "changes": [{"description": "COFFEE", "account": "Expense:Food"}],
        "all_rows": all_rows,
    }
    gr_review._save_changes(json.dumps(payload))
    return csv_p


def test_review_save_reorders_transfer_account_after_account(tmp_path):
    csv_p = _save(tmp_path, _rows_transfer_account_last())
    with open(csv_p, encoding="utf-8") as f:
        header = next(csv.reader(f))
    # Re-saved layout matches the shared import-ready schema exactly, so
    # Transfer Account is back at position 5 (after Account), not last.
    assert header == list(IMPORT_READY_FIELDS)
    assert header.index("Transfer Account") == header.index("Account") + 1


def test_review_save_keeps_blank_transfer_account_column(tmp_path):
    """A blank Transfer Account value is still emitted in its canonical slot
    (the mapper's 'always emit, blank when unresolved' invariant survives a
    round-trip through the review save)."""
    rows = _rows_transfer_account_last()
    rows[0]["Transfer Account"] = ""  # unresolved-bank case
    csv_p = _save(tmp_path, rows)
    with open(csv_p, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        header = reader.fieldnames
        data = next(reader)
    assert header == list(IMPORT_READY_FIELDS)          # column present
    assert data["Transfer Account"] == ""               # value blank, not dropped
