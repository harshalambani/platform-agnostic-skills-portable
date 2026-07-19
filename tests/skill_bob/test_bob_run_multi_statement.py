"""
tests/skill_bob/test_bob_run_multi_statement.py -- P3b follow-up: the legacy
standalone-UI-tab ``run()`` entry point must route multi-file batches through
the same ``bank_common.consolidate`` helper ``BoBSkill.parse()`` already
uses (test_bob_multi_statement.py), not the old naive sorted(glob) + blind
concat. ``run()`` is reachable from the shipped UI (BoB's tab stages
multi-file uploads into a temp directory), so this was a live, silently
mis-ordering bug on the UI path -- see
2026-07-19-bank-P3b-followup-run-path-prompt.md.

Covers, all offline / synthetic-only:
  - Non-chronologically-sorting filenames: run()'s output CSV comes out
    date-ordered, not filename-ordered.
  - A period gap between statements is surfaced in run()'s returned summary.
  - Overlapping statement periods are surfaced in run()'s returned summary.
  - A single-file batch (via the directory/consolidate code path, not the
    single-file fast path) is byte-identical to the plain single-file run().
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import bob_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_bob.agent import run as bob_run  # noqa: E402

_JAN_TXNS = [
    ("15-01-2025", "NEFT SALARY CREDIT-SYNCO", "", "", "50,000.00", "1,50,000.00Cr"),
]
_FEB_TXNS = [
    ("10-02-2025", "UPI-GROCERY STORE-SYN", "", "2,000.00", "", "1,48,000.00Cr"),
]
_MAR_TXNS = [
    ("05-03-2025", "ACH D-BD-SYNTH MF-SIP0001", "", "5,000.00", "", "1,43,000.00Cr"),
]


def _read_dates(csv_path: Path) -> list[str]:
    with open(csv_path, "r", encoding="utf-8") as f:
        return [row["DATE"] for row in csv.DictReader(f)]


def test_non_chronological_filenames_come_out_date_ordered(tmp_path):
    # Filename order (b, a, c) intentionally does NOT match date order
    # (Jan, Feb, Mar) -- naive sorted(glob) would misorder these.
    (tmp_path / "stmt_b_feb.pdf").write_bytes(
        fixture_gen.build_pdf_for(_FEB_TXNS, "01-02-2025", "28-02-2025"))
    (tmp_path / "stmt_a_jan.pdf").write_bytes(
        fixture_gen.build_pdf_for(_JAN_TXNS, "01-01-2025", "31-01-2025"))
    (tmp_path / "stmt_c_mar.pdf").write_bytes(
        fixture_gen.build_pdf_for(_MAR_TXNS, "01-03-2025", "31-03-2025"))

    out_csv = tmp_path / "out.csv"
    bob_run(str(tmp_path), str(out_csv))

    assert _read_dates(out_csv) == ["15-01-2025", "10-02-2025", "05-03-2025"]


def test_gap_between_batch_statements_produces_warning(tmp_path):
    (tmp_path / "stmt_jan.pdf").write_bytes(
        fixture_gen.build_pdf_for(_JAN_TXNS, "01-01-2025", "31-01-2025"))
    (tmp_path / "stmt_mar.pdf").write_bytes(
        fixture_gen.build_pdf_for(_MAR_TXNS, "01-03-2025", "31-03-2025"))

    out_csv = tmp_path / "out.csv"
    summary = bob_run(str(tmp_path), str(out_csv))

    assert "POSSIBLE MISSING STATEMENT" in summary


def test_overlapping_statement_periods_produce_warning(tmp_path):
    # Group period is derived from actual transaction dates within it, so
    # the "full month" statement must span both ends for the mid-month
    # statement's single date to fall inside its range.
    full_month_txns = [
        ("01-01-2025", "NEFT SALARY CREDIT-SYNCO", "", "", "50,000.00", "1,50,000.00Cr"),
        ("31-01-2025", "UPI-REFUND ORDER-SYNSHOP", "", "", "500.00", "1,50,500.00Cr"),
    ]
    overlap_txns = [
        ("15-01-2025", "UPI-GROCERY STORE-SYN", "", "2,000.00", "", "1,48,500.00Cr"),
    ]
    (tmp_path / "stmt_jan_full.pdf").write_bytes(
        fixture_gen.build_pdf_for(full_month_txns, "01-01-2025", "31-01-2025"))
    (tmp_path / "stmt_jan_overlap.pdf").write_bytes(
        fixture_gen.build_pdf_for(overlap_txns, "15-01-2025", "15-01-2025"))

    out_csv = tmp_path / "out.csv"
    summary = bob_run(str(tmp_path), str(out_csv))

    assert "OVERLAPPING/OUT-OF-ORDER" in summary


def test_single_file_batch_run_is_a_no_op_consolidation(tmp_path):
    """A directory containing exactly one PDF must go through the new
    per-batch consolidate() path as a single-group no-op, producing output
    byte-identical to the plain single-file fast path (_run_single) and no
    continuity warnings in the summary."""
    direct_pdf = tmp_path / "syn_bob.pdf"
    direct_pdf.write_bytes(fixture_gen.build_pdf())
    direct_out = tmp_path / "direct.csv"
    direct_summary = bob_run(str(direct_pdf), str(direct_out))

    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    (batch_dir / "syn_bob.pdf").write_bytes(fixture_gen.build_pdf())
    batch_out = tmp_path / "batch.csv"
    batch_summary = bob_run(str(batch_dir), str(batch_out))

    assert direct_out.read_text(encoding="utf-8") == batch_out.read_text(encoding="utf-8")
    assert "Continuity warnings" not in batch_summary
    assert "Continuity warnings" not in direct_summary
