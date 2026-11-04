"""
tests/test_bank_common_consolidate.py — unit tests for
agents/bank_common/consolidate.py (P3b): the shared multi-statement
ordering/continuity helper, lifted verbatim (in behavior) from HSBC's
original inline logic and now also used by BoB and ICICI.

Run with:
    cd src && python -m pytest ../tests/test_bank_common_consolidate.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.bank_common.consolidate import (  # noqa: E402
    StatementGroup,
    check_continuity,
    consolidate,
)


def _group(name, rows, start, end):
    return StatementGroup(name=name, rows=rows, period_start=start, period_end=end)


# ---------------------------------------------------------------------------
# consolidate(): ordering by transaction date, not filename
# ---------------------------------------------------------------------------

def test_consolidate_orders_by_date_not_filename():
    march = _group("stmt_z_march", [{"row": "march"}], "2025-03-01", "2025-03-31")
    january = _group("stmt_a_january", [{"row": "january"}], "2025-01-01", "2025-01-31")
    february = _group("stmt_m_february", [{"row": "february"}], "2025-02-01", "2025-02-28")

    result = consolidate([march, january, february])

    assert result.rows == [{"row": "january"}, {"row": "february"}, {"row": "march"}]
    assert result.warnings == []


def test_consolidate_single_group_is_a_no_op():
    """A single-file 'batch' (e.g. a full-year statement) must pass through
    unchanged — this is what keeps the dominant single-statement workflow
    byte-identical after routing through the shared helper."""
    only = _group("annual_statement", [{"row": 1}, {"row": 2}], "2025-04-01", "2026-03-31")
    result = consolidate([only])
    assert result.rows == [{"row": 1}, {"row": 2}]
    assert result.warnings == []


def test_consolidate_undated_statements_sort_last_in_natural_order():
    dated = _group("stmt_jan", [{"row": "jan"}], "2025-01-01", "2025-01-31")
    undated_2 = _group("stmt_unreadable2", [{"row": "u2"}], None, None)
    undated_10 = _group("stmt_unreadable10", [{"row": "u10"}], None, None)

    result = consolidate([undated_10, dated, undated_2])

    assert result.rows == [{"row": "jan"}, {"row": "u2"}, {"row": "u10"}]


def test_consolidate_concatenates_rows_in_group_order():
    g1 = _group("a", [{"n": 1}, {"n": 2}], "2025-01-01", "2025-01-31")
    g2 = _group("b", [{"n": 3}], "2025-02-01", "2025-02-28")
    result = consolidate([g1, g2])
    assert result.rows == [{"n": 1}, {"n": 2}, {"n": 3}]


# ---------------------------------------------------------------------------
# check_continuity(): gap / overlap / undated warnings
# ---------------------------------------------------------------------------

def test_check_continuity_no_warnings_for_back_to_back_statements():
    groups = [
        _group("stmt_apr", [], "2025-04-01", "2025-04-30"),
        _group("stmt_may", [], "2025-05-01", "2025-05-31"),
    ]
    assert check_continuity(groups) == []


def test_check_continuity_flags_missing_statement_gap():
    groups = [
        _group("stmt_apr", [], "2025-04-01", "2025-04-30"),
        _group("stmt_jun", [], "2025-06-01", "2025-06-30"),  # May missing
    ]
    warnings = check_continuity(groups)
    assert len(warnings) == 1
    assert "POSSIBLE MISSING STATEMENT" in warnings[0]
    assert "stmt_apr" in warnings[0] and "stmt_jun" in warnings[0]


def test_check_continuity_flags_overlapping_statements():
    groups = [
        _group("stmt_a", [], "2025-04-01", "2025-04-30"),
        _group("stmt_b", [], "2025-04-15", "2025-05-15"),  # overlaps stmt_a
    ]
    warnings = check_continuity(groups)
    assert len(warnings) == 1
    assert "OVERLAPPING/OUT-OF-ORDER" in warnings[0]


def test_check_continuity_flags_undated_statements():
    groups = [
        _group("stmt_apr", [], "2025-04-01", "2025-04-30"),
        _group("stmt_unreadable", [], None, None),
    ]
    warnings = check_continuity(groups)
    assert any("no readable dates" in w and "stmt_unreadable" in w for w in warnings)


def test_check_continuity_small_gap_within_tolerance_is_fine():
    groups = [
        _group("stmt_a", [], "2025-04-05", "2025-05-04"),
        _group("stmt_b", [], "2025-05-05", "2025-06-04"),
    ]
    assert check_continuity(groups) == []


def test_consolidate_surfaces_gap_warning_via_public_api():
    """End-to-end through consolidate(): out-of-order input with a gap must
    still be date-sorted AND produce the gap warning (acceptance gate 3)."""
    june = _group("stmt_b_june", [{"m": "jun"}], "2025-06-01", "2025-06-30")
    april = _group("stmt_a_april", [{"m": "apr"}], "2025-04-01", "2025-04-30")

    result = consolidate([june, april])

    assert result.rows == [{"m": "apr"}, {"m": "jun"}]
    assert len(result.warnings) == 1
    assert "POSSIBLE MISSING STATEMENT" in result.warnings[0]


def test_consolidate_surfaces_overlap_warning_via_public_api():
    """Overlapping periods must produce an overlap warning (acceptance gate 4)."""
    b = _group("stmt_b", [{"m": "b"}], "2025-04-15", "2025-05-15")
    a = _group("stmt_a", [{"m": "a"}], "2025-04-01", "2025-04-30")

    result = consolidate([b, a])

    assert len(result.warnings) == 1
    assert "OVERLAPPING/OUT-OF-ORDER" in result.warnings[0]
