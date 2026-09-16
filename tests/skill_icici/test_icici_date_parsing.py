"""
tests/skill_icici/test_icici_date_parsing.py -- parametrized coverage for
agents.skill_icici.agent.parse_icici_date(), incl.:
  - delegation to bank_common.normalize.parse_comma_month_date() for the
    native "DD,Mon,YYYY" text-cell shape (I-1: 2-digit years, calendar
    validity);
  - the IMP-03 ISO-passthrough addition for the shape convert_xls_to_csv()
    itself now emits for a real DATE cell;
  - that arbitrary slash-separated dates are NOT accepted -- a shape we
    didn't produce ourselves could be ambiguous (dd/mm vs mm/dd), so this
    guards against ever silently guessing and flipping day/month.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_icici.agent import parse_icici_date  # noqa: E402


@pytest.mark.parametrize("raw,expected", [
    # Native text-cell shape.
    ("01,Apr,2024", "2024-04-01"),
    ("31,Mar,2025", "2025-03-31"),
    # 2-digit year -> 2000+YY (pinned convention, matches normalize.py).
    ("01,Apr,24", "2024-04-01"),
    # Calendar-invalid -> None, not a garbage string.
    ("31,Feb,2024", None),
    # "Sept" (4-letter) is not recognized -- only "Sep" is (pinned).
    ("01,Sept,2024", None),
    ("01,Sep,2024", "2024-09-01"),
    # ISO passthrough -- what our own reader emits for a real DATE cell
    # after a user re-saves the statement in Excel (IMP-03).
    ("2025-04-03", "2025-04-03"),
    ("2025-04-03 ", "2025-04-03"),
    # ISO-shaped but calendar-invalid still returns None.
    ("2025-13-01", None),
    ("2025-02-30", None),
    # Arbitrary slash dates are NOT accepted -- would be ambiguous
    # (dd/mm vs mm/dd) for anything we didn't produce ourselves.
    ("03/04/2025", None),
    ("2025/04/03", None),
    ("", None),
    (None, None),
])
def test_parse_icici_date_table(raw, expected):
    assert parse_icici_date(raw) == expected


def test_parse_icici_date_iso_day_month_not_swapped():
    # 2025-04-03 must stay 3 April, never become 4 March.
    assert parse_icici_date("2025-04-03") == "2025-04-03"
