"""
tests/test_bank_common.py — Unit tests for agents/bank_common/ (P1 contracts
session): the shared, bank-agnostic utilities promoted out of skill_hdfc.

Run with:
    cd src && python -m pytest ../tests/test_bank_common.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import pytest

from agents.bank_common.normalize import clean_amount, normalise_date, parse_comma_month_date
from agents.bank_common.password import is_password_error, password_error_message
from agents.bank_common.tabular import find_header_row, map_columns
from agents.bank_common.text_quality import text_layer_usable


# ---------------------------------------------------------------------------
# normalize
# ---------------------------------------------------------------------------

def test_clean_amount_strips_commas():
    assert clean_amount("1,50,000.00") == "150000.00"


def test_clean_amount_zero_becomes_blank():
    assert clean_amount("0") == ""
    assert clean_amount("0.00") == ""


def test_clean_amount_non_numeric_passes_through():
    assert clean_amount("N/A") == "N/A"


def test_clean_amount_strips_cr_dr_suffix():
    assert clean_amount("1,57,950.00Cr") == "157950.00"
    assert clean_amount("20,000.00Dr") == "20000.00"


def test_clean_amount_blank_zero_false_keeps_zero_string():
    assert clean_amount("0.00", blank_zero=False) == "0.00"
    assert clean_amount("0", blank_zero=True) == ""


def test_normalise_date_ddmmyy_to_iso():
    assert normalise_date("05/04/25") == "2025-04-05"


def test_normalise_date_ddmmyyyy_to_iso():
    assert normalise_date("05/04/2025") == "2025-04-05"


def test_normalise_date_dash_ddmmyy_to_iso():
    assert normalise_date("05-04-25") == "2025-04-05"


def test_normalise_date_dash_ddmmyyyy_to_iso():
    assert normalise_date("05-04-2025") == "2025-04-05"


def test_normalise_date_already_iso_passes_through():
    assert normalise_date("2025-04-05") == "2025-04-05"


def test_normalise_date_unrecognized_passes_through():
    assert normalise_date("garbage") == "garbage"


# ---------------------------------------------------------------------------
# normalize.parse_comma_month_date (I-1 fix: 2-digit years, calendar
# validity -- shared helper, so run every bank's tests when touching it,
# even though only ICICI's agent.py calls it directly today).
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("01,Apr,2024", "2024-04-01"),
    ("31,Mar,2025", "2025-03-31"),
    # 2-digit year -> 2000+YY, matching normalise_date()'s existing
    # 2-digit-year convention elsewhere in this module (pinned decision).
    ("01,Apr,24", "2024-04-01"),
    ("31,Mar,25", "2025-03-31"),
    # Calendar-invalid dates return None, not a garbage ISO-shaped string.
    ("31,Feb,2024", None),
    ("31,Apr,2024", None),
    # Leap-year edge: 2024 is a leap year, 2023 is not.
    ("29,Feb,2024", "2024-02-29"),
    ("29,Feb,2023", None),
    # "Sept" (4-letter) is not a recognized abbreviation -- only the
    # 3-letter form ("Sep") is in MONTH_ABBR; pinned behaviour.
    ("01,Sept,2024", None),
    ("01,Sep,2024", "2024-09-01"),
    ("01,Foo,2024", None),
    ("", None),
    (None, None),
    ("not,a,date", None),
    ("01,Apr", None),  # wrong shape -- only 2 parts
])
def test_parse_comma_month_date_table(raw, expected):
    assert parse_comma_month_date(raw) == expected


# ---------------------------------------------------------------------------
# tabular
# ---------------------------------------------------------------------------

_DATE_ALIASES = ("date", "value date")
_DESC_ALIASES = ("narration", "description")


def test_find_header_row_locates_first_match():
    rows = [["preamble"], ["****"], ["Date", "Narration", "Balance"], ["01/04/25", "X", "1"]]
    assert find_header_row(rows, _DATE_ALIASES, _DESC_ALIASES) == 2


def test_find_header_row_returns_minus_one_when_absent():
    rows = [["a", "b"], ["c", "d"]]
    assert find_header_row(rows, _DATE_ALIASES, _DESC_ALIASES) == -1


def test_map_columns_first_match_wins():
    header = ["Value Date", "Narration", "Withdrawal Amt.", "Deposit Amt.", "Closing Balance"]
    col = map_columns(header, {
        "date": ["date"],
        "narration": ["narration"],
        "withdrawal": ["withdrawal"],
        "deposit": ["deposit"],
        "balance": ["balance"],
    })
    assert col == {"date": 0, "narration": 1, "withdrawal": 2, "deposit": 3, "balance": 4}


def test_map_columns_missing_field_is_none():
    header = ["Date", "Narration"]
    col = map_columns(header, {"date": ["date"], "balance": ["balance"]})
    assert col == {"date": 0, "balance": None}


# ---------------------------------------------------------------------------
# text_quality
# ---------------------------------------------------------------------------

_ANCHORS = (r'\bdate\b', r'narration')


def test_text_layer_usable_accepts_normal_text():
    text = "Date Narration Balance\n01/04/25 NEFT 100.00"
    assert text_layer_usable(text, _ANCHORS) is True


def test_text_layer_usable_rejects_dense_cid_junk():
    junk = "(cid:34)(cid:56) " * 20
    assert text_layer_usable("Date Narration\n" + junk, _ANCHORS) is False


def test_text_layer_usable_rejects_missing_anchors():
    assert text_layer_usable("some text with numbers 1.00 2.00 but no headers", _ANCHORS) is False


def test_text_layer_usable_rejects_empty():
    assert text_layer_usable("", _ANCHORS) is False


def test_text_layer_usable_no_anchors_required_skips_that_check():
    assert text_layer_usable("random ASCII text here", ()) is True


# ---------------------------------------------------------------------------
# password
# ---------------------------------------------------------------------------

class _FakePasswordError(Exception):
    pass


def test_is_password_error_detects_by_type_name():
    class PDFPasswordIncorrect(Exception):
        pass
    assert is_password_error(PDFPasswordIncorrect()) is True


def test_is_password_error_detects_by_message():
    assert is_password_error(ValueError("bad password supplied")) is True


def test_is_password_error_detects_via_cause_chain():
    try:
        try:
            raise _FakePasswordError("encrypted document")
        except _FakePasswordError as inner:
            raise RuntimeError("wrapped") from inner
    except RuntimeError as outer:
        assert is_password_error(outer) is True


def test_is_password_error_false_for_unrelated_error():
    assert is_password_error(ValueError("file not found")) is False


def test_password_error_message_with_hint():
    msg = password_error_message("for HDFC often the Cust ID")
    assert msg == "PDF is password-protected — supply the statement password (for HDFC often the Cust ID)."


def test_password_error_message_without_hint():
    msg = password_error_message()
    assert msg == "PDF is password-protected — supply the statement password."
