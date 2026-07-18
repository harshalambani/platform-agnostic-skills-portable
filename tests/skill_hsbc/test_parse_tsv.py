"""
tests/skill_hsbc/test_parse_tsv.py -- unit tests for HSBC's parse_tsv.py fixes:

  - load_tsv_lines must tolerate Tesseract confidence emitted as a float
    string (e.g. "96.637047"), which previously crashed with a
    ZeroDivisionError (a defaultdict phantom-empty-list bug triggered by the
    int(row['conf']) parse failure).
  - check_statement_continuity flags missing/overlapping/undated statements
    given a sorted list of (name, period_start, period_end).
"""
from __future__ import annotations

import csv
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
SCRIPTS = ROOT / "src" / "agents" / "skill_hsbc" / "scripts"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import parse_tsv  # noqa: E402
from agents.bank_common.consolidate import StatementGroup, consolidate  # noqa: E402


# ---------------------------------------------------------------------------
# load_tsv_lines: float-confidence tolerance
# ---------------------------------------------------------------------------

_TSV_HEADER = ["level", "page_num", "block_num", "par_num", "line_num",
               "word_num", "left", "top", "width", "height", "conf", "text"]


def _write_tsv(path: Path, rows: list[list]):
    with open(path, "w", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(_TSV_HEADER)
        for r in rows:
            w.writerow(r)


def test_load_tsv_lines_tolerates_float_confidence(tmp_path):
    tsv_path = tmp_path / "page-1.tsv"
    _write_tsv(tsv_path, [
        [5, 1, 1, 1, 1, 1, 10, 20, 30, 15, "96.637047", "Hello"],
        [5, 1, 1, 1, 1, 2, 50, 20, 30, 15, "91.146080", "World"],
    ])
    result = parse_tsv.load_tsv_lines(tsv_path)
    assert len(result) == 1
    assert len(result[0]["words"]) == 2
    assert result[0]["words"][0]["conf"] == 96


def test_load_tsv_lines_no_phantom_empty_entries(tmp_path):
    """A row whose 'conf' fails to parse must not leave a stray empty-word
    line (previously caused ZeroDivisionError in the avg_top computation)."""
    tsv_path = tmp_path / "page-1.tsv"
    _write_tsv(tsv_path, [
        [5, 1, 1, 1, 1, 1, 10, 20, 30, 15, "not-a-number", "Ghost"],
        [5, 1, 2, 1, 1, 1, 10, 40, 30, 15, "97", "Real"],
    ])
    result = parse_tsv.load_tsv_lines(tsv_path)  # must not raise
    assert all(len(L["words"]) > 0 for L in result)
    assert len(result) == 1
    assert result[0]["words"][0]["text"] == "Real"


# ---------------------------------------------------------------------------
# check_statement_continuity
# ---------------------------------------------------------------------------

def test_continuity_no_warnings_for_back_to_back_statements():
    periods = [
        ("stmt_apr", "2025-04-01", "2025-04-30"),
        ("stmt_may", "2025-05-01", "2025-05-31"),
        ("stmt_jun", "2025-06-01", "2025-06-30"),
    ]
    assert parse_tsv.check_statement_continuity(periods) == []


def test_continuity_flags_missing_statement_gap():
    periods = [
        ("stmt_apr", "2025-04-01", "2025-04-30"),
        ("stmt_jun", "2025-06-01", "2025-06-30"),  # May is missing
    ]
    warnings = parse_tsv.check_statement_continuity(periods)
    assert len(warnings) == 1
    assert "POSSIBLE MISSING STATEMENT" in warnings[0]
    assert "stmt_apr" in warnings[0] and "stmt_jun" in warnings[0]


def test_continuity_flags_overlapping_statements():
    periods = [
        ("stmt_a", "2025-04-01", "2025-04-30"),
        ("stmt_b", "2025-04-15", "2025-05-15"),  # overlaps stmt_a
    ]
    warnings = parse_tsv.check_statement_continuity(periods)
    assert len(warnings) == 1
    assert "OVERLAPPING/OUT-OF-ORDER" in warnings[0]


def test_continuity_flags_undated_statements():
    periods = [
        ("stmt_apr", "2025-04-01", "2025-04-30"),
        ("stmt_unreadable", None, None),
    ]
    warnings = parse_tsv.check_statement_continuity(periods)
    assert any("no readable dates" in w and "stmt_unreadable" in w for w in warnings)


def test_continuity_small_gap_within_tolerance_is_fine():
    """A few days' slack (statement cycles don't always land on month
    boundaries) should not be flagged."""
    periods = [
        ("stmt_a", "2025-04-05", "2025-05-04"),
        ("stmt_b", "2025-05-05", "2025-06-04"),
    ]
    assert parse_tsv.check_statement_continuity(periods) == []


# ---------------------------------------------------------------------------
# Parity: HSBC's consolidation must equal bank_common.consolidate() exactly
# (acceptance gate 2 -- HSBC is the reference implementation the shared
# helper was lifted from, and now delegates to it for real).
# ---------------------------------------------------------------------------

def test_check_statement_continuity_delegates_to_shared_helper():
    """parse_tsv.check_statement_continuity(periods) must produce identical
    warnings to feeding the same periods straight into
    bank_common.consolidate.check_continuity via StatementGroup — proving
    HSBC's runtime is routed through the shared helper, not a parallel copy
    of the same logic."""
    periods = [
        ("stmt_apr", "2025-04-01", "2025-04-30"),
        ("stmt_jun", "2025-06-01", "2025-06-30"),  # gap
        ("stmt_unreadable", None, None),
    ]
    groups = [StatementGroup(name, [], start, end) for name, start, end in periods]

    assert parse_tsv.check_statement_continuity(periods) == consolidate(groups).warnings
