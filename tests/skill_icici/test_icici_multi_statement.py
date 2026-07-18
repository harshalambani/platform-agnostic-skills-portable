"""
tests/skill_icici/test_icici_multi_statement.py -- P3b: ICICI multi-file
batches must be consolidated by actual transaction date (via bank_common),
not by naive sorted(glob) + blind concat.

Covers, all offline / synthetic-only:
  - Non-chronologically-sorting filenames: rows come out date-ordered.
  - A period gap between statements is reported as a warning.
  - Overlapping statement periods are reported as a warning.
  - A single-file batch (the dominant real-world annual-statement case)
    stays a no-op consolidation with no warnings.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import icici_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_icici.agent import ICICISkill  # noqa: E402

_JAN_TXNS = [
    ("15,Jan,2025", "15,Jan,2025", "-", "NEFT-REF0000000001-SYN SALARY CREDIT", "", "50000.00", "150000.00"),
]
_FEB_TXNS = [
    ("10,Feb,2025", "10,Feb,2025", "-", "UPI/300000000002/NA/synshop//SYNabc0000ef", "2000.00", "", "148000.00"),
]
_MAR_TXNS = [
    ("05,Mar,2025", "05,Mar,2025", "-", "BIL/ONL/REF0000000003/SYN UTILITY BILL", "5000.00", "", "143000.00"),
]


def test_non_chronological_filenames_come_out_date_ordered(tmp_path):
    # Filename order (b, a, c) intentionally does NOT match date order
    # (Jan, Feb, Mar) -- naive sorted(glob) would misorder these.
    (tmp_path / "stmt_b_feb.xls").write_bytes(
        fixture_gen.build_xls_for(_FEB_TXNS, "01,Feb,2025", "28,Feb,2025"))
    (tmp_path / "stmt_a_jan.xls").write_bytes(
        fixture_gen.build_xls_for(_JAN_TXNS, "01,Jan,2025", "31,Jan,2025"))
    (tmp_path / "stmt_c_mar.xls").write_bytes(
        fixture_gen.build_xls_for(_MAR_TXNS, "01,Mar,2025", "31,Mar,2025"))

    result = ICICISkill().parse(tmp_path)

    assert [r["Date"] for r in result.rows] == ["2025-01-15", "2025-02-10", "2025-03-05"]


def test_gap_between_batch_statements_produces_warning(tmp_path):
    (tmp_path / "stmt_jan.xls").write_bytes(
        fixture_gen.build_xls_for(_JAN_TXNS, "01,Jan,2025", "31,Jan,2025"))
    (tmp_path / "stmt_mar.xls").write_bytes(
        fixture_gen.build_xls_for(_MAR_TXNS, "01,Mar,2025", "31,Mar,2025"))

    result = ICICISkill().parse(tmp_path)

    assert any("POSSIBLE MISSING STATEMENT" in w for w in result.warnings)


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

    result = ICICISkill().parse(tmp_path)

    assert any("OVERLAPPING/OUT-OF-ORDER" in w for w in result.warnings)


def test_single_file_batch_is_a_no_op_consolidation(tmp_path):
    """The dominant real-world case (one annual-statement .xls) must not
    pick up any consolidation warnings or reordering."""
    xls_path = tmp_path / "syn_icici.xls"
    xls_path.write_bytes(fixture_gen.build_xls())

    result = ICICISkill().parse(xls_path)

    assert len(result.rows) == 5
    assert not any(
        "POSSIBLE MISSING STATEMENT" in w or "OVERLAPPING/OUT-OF-ORDER" in w
        for w in result.warnings
    )
