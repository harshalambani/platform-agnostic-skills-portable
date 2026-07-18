"""
tests/skill_bob/test_bob_multi_statement.py -- P3b: BoB multi-file batches
must be consolidated by actual transaction date (via bank_common), not by
naive sorted(glob) + blind concat.

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

import bob_fixture_gen as fixture_gen  # noqa: E402
from agents.skill_bob.agent import BoBSkill  # noqa: E402

_JAN_TXNS = [
    ("15-01-2025", "NEFT SALARY CREDIT-SYNCO", "", "", "50,000.00", "1,50,000.00Cr"),
]
_FEB_TXNS = [
    ("10-02-2025", "UPI-GROCERY STORE-SYN", "", "2,000.00", "", "1,48,000.00Cr"),
]
_MAR_TXNS = [
    ("05-03-2025", "ACH D-BD-SYNTH MF-SIP0001", "", "5,000.00", "", "1,43,000.00Cr"),
]


def test_non_chronological_filenames_come_out_date_ordered(tmp_path):
    # Filename order (b, a, c) intentionally does NOT match date order
    # (Jan, Feb, Mar) -- naive sorted(glob) would misorder these.
    (tmp_path / "stmt_b_feb.pdf").write_bytes(
        fixture_gen.build_pdf_for(_FEB_TXNS, "01-02-2025", "28-02-2025"))
    (tmp_path / "stmt_a_jan.pdf").write_bytes(
        fixture_gen.build_pdf_for(_JAN_TXNS, "01-01-2025", "31-01-2025"))
    (tmp_path / "stmt_c_mar.pdf").write_bytes(
        fixture_gen.build_pdf_for(_MAR_TXNS, "01-03-2025", "31-03-2025"))

    result = BoBSkill().parse(tmp_path)

    assert [r["Date"] for r in result.rows] == ["2025-01-15", "2025-02-10", "2025-03-05"]


def test_gap_between_batch_statements_produces_warning(tmp_path):
    (tmp_path / "stmt_jan.pdf").write_bytes(
        fixture_gen.build_pdf_for(_JAN_TXNS, "01-01-2025", "31-01-2025"))
    (tmp_path / "stmt_mar.pdf").write_bytes(
        fixture_gen.build_pdf_for(_MAR_TXNS, "01-03-2025", "31-03-2025"))

    result = BoBSkill().parse(tmp_path)

    assert any("POSSIBLE MISSING STATEMENT" in w for w in result.warnings)


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

    result = BoBSkill().parse(tmp_path)

    assert any("OVERLAPPING/OUT-OF-ORDER" in w for w in result.warnings)


def test_single_file_batch_is_a_no_op_consolidation(tmp_path):
    """The dominant real-world case (one annual-statement PDF) must not pick
    up any consolidation warnings or reordering."""
    pdf_path = tmp_path / "syn_bob.pdf"
    pdf_path.write_bytes(fixture_gen.build_pdf())

    result = BoBSkill().parse(pdf_path)

    assert len(result.rows) == 5
    assert not any(
        "POSSIBLE MISSING STATEMENT" in w or "OVERLAPPING/OUT-OF-ORDER" in w
        for w in result.warnings
    )
