"""
tests/skill_icici/test_icici_run_multi_statement.py -- P3b follow-up: the
legacy standalone-UI-tab ``run()`` entry point must route multi-file batches
through the same ``bank_common.consolidate`` helper ``ICICISkill.parse()``
already uses (test_icici_multi_statement.py), not the old naive
sorted(glob) + blind concat. ``run()`` is reachable from the shipped UI
(ICICI's tab stages multi-file uploads into a temp directory), so this was
a live, silently mis-ordering bug on the UI path -- see
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

import icici_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_icici.agent import run as icici_run  # noqa: E402

_JAN_TXNS = [
    ("15,Jan,2025", "15,Jan,2025", "-", "NEFT-REF0000000001-SYN SALARY CREDIT", "", "50000.00", "150000.00"),
]
_FEB_TXNS = [
    ("10,Feb,2025", "10,Feb,2025", "-", "UPI/300000000002/NA/synshop//SYNabc0000ef", "2000.00", "", "148000.00"),
]
_MAR_TXNS = [
    ("05,Mar,2025", "05,Mar,2025", "-", "BIL/ONL/REF0000000003/SYN UTILITY BILL", "5000.00", "", "143000.00"),
]


def _read_dates(csv_path: Path) -> list[str]:
    with open(csv_path, "r", encoding="utf-8") as f:
        return [row["Date"] for row in csv.DictReader(f)]


def test_non_chronological_filenames_come_out_date_ordered(tmp_path):
    # Filename order (b, a, c) intentionally does NOT match date order
    # (Jan, Feb, Mar) -- naive sorted(glob) would misorder these.
    (tmp_path / "stmt_b_feb.xls").write_bytes(
        fixture_gen.build_xls_for(_FEB_TXNS, "01,Feb,2025", "28,Feb,2025"))
    (tmp_path / "stmt_a_jan.xls").write_bytes(
        fixture_gen.build_xls_for(_JAN_TXNS, "01,Jan,2025", "31,Jan,2025"))
    (tmp_path / "stmt_c_mar.xls").write_bytes(
        fixture_gen.build_xls_for(_MAR_TXNS, "01,Mar,2025", "31,Mar,2025"))

    out_csv = tmp_path / "out.csv"
    icici_run(str(tmp_path), str(out_csv))

    assert _read_dates(out_csv) == ["2025-01-15", "2025-02-10", "2025-03-05"]


def test_gap_between_batch_statements_produces_warning(tmp_path):
    (tmp_path / "stmt_jan.xls").write_bytes(
        fixture_gen.build_xls_for(_JAN_TXNS, "01,Jan,2025", "31,Jan,2025"))
    (tmp_path / "stmt_mar.xls").write_bytes(
        fixture_gen.build_xls_for(_MAR_TXNS, "01,Mar,2025", "31,Mar,2025"))

    out_csv = tmp_path / "out.csv"
    summary = icici_run(str(tmp_path), str(out_csv))

    assert "POSSIBLE MISSING STATEMENT" in summary


def test_overlapping_statement_periods_produce_warning(tmp_path):
    # Group period is derived from actual transaction dates within it, so
    # the "full month" statement must span both ends for the mid-month
    # statement's single date to fall inside its range.
    full_month_txns = [
        ("01,Jan,2025", "01,Jan,2025", "-", "NEFT-REF0000000001-SYN SALARY CREDIT", "", "50000.00", "150000.00"),
        ("31,Jan,2025", "31,Jan,2025", "-", "UPI/300000000004/NA/synrefund//SYNdef0001gh", "", "500.00", "150500.00"),
    ]
    overlap_txns = [
        ("15,Jan,2025", "15,Jan,2025", "-", "UPI/300000000002/NA/synshop//SYNabc0000ef", "2000.00", "", "148500.00"),
    ]
    (tmp_path / "stmt_jan_full.xls").write_bytes(
        fixture_gen.build_xls_for(full_month_txns, "01,Jan,2025", "31,Jan,2025"))
    (tmp_path / "stmt_jan_overlap.xls").write_bytes(
        fixture_gen.build_xls_for(overlap_txns, "15,Jan,2025", "15,Jan,2025"))

    out_csv = tmp_path / "out.csv"
    summary = icici_run(str(tmp_path), str(out_csv))

    assert "OVERLAPPING/OUT-OF-ORDER" in summary


def test_single_file_batch_run_is_a_no_op_consolidation(tmp_path):
    """A directory containing exactly one XLS must NOT enter the
    len(csv_parts) > 1 consolidate() path at all (single-file runs write
    straight to output_path), so it stays byte-identical to the plain
    single-file run() and produces no continuity warnings."""
    direct_xls = tmp_path / "syn_icici.xls"
    direct_xls.write_bytes(fixture_gen.build_xls())
    direct_out = tmp_path / "direct.csv"
    direct_summary = icici_run(str(direct_xls), str(direct_out))

    batch_dir = tmp_path / "batch"
    batch_dir.mkdir()
    (batch_dir / "syn_icici.xls").write_bytes(fixture_gen.build_xls())
    batch_out = tmp_path / "batch.csv"
    batch_summary = icici_run(str(batch_dir), str(batch_out))

    assert direct_out.read_text(encoding="utf-8") == batch_out.read_text(encoding="utf-8")
    assert "Continuity warnings" not in batch_summary
    assert "Continuity warnings" not in direct_summary
