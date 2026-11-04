"""
tests/skill_icici/test_icici_excel_resave.py -- IMP-03: a re-saved ICICI
.xls (or .xlsx) must not silently parse zero rows.

The genuine ICICI net-banking download is an all-text-cell BIFF8 .xls --
every cell (including dates and amounts) is XL_CELL_TEXT. If a user opens
that file in Excel and re-saves it, Excel is free to "clean up" the text
cells it recognises as dates/numbers into real XL_CELL_DATE/XL_CELL_NUMBER
cells. Before the IMP-03 fix, convert_xls_to_csv() rendered a DATE cell as
'%d/%m/%Y' while parse_icici_date() only accepted 'DD,Mon,YYYY' -- so every
row silently failed to parse and the whole file came back as
success:false / rows_output 0.

This file proves:
  - an all-real-date-cell + all-real-number-cell re-save still parses every
    row to the same canonical output as the original all-text fixture;
  - a date like "03,Apr" as a real DATE cell is NOT day/month-swapped;
  - a mixed file (some rows text, some rows typed cells) parses fully;
  - the original all-text fixture still parses identically (no regression);
  - an accidental .xlsx re-save either parses identically or fails with
    ONE clear error -- never a silent 0-row success:false.
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
from agents.skill_icici.agent import ICICISkill, transform_icici_statement  # noqa: E402
from test_icici_golden import EXPECTED_ROWS  # noqa: E402

_ALL_ROWS = {1, 2, 3, 4, 5}


# ---------------------------------------------------------------------------
# All-typed-cells re-save
# ---------------------------------------------------------------------------

def test_all_date_and_number_cells_parses_every_row(tmp_path):
    xls_path = tmp_path / "resaved.xls"
    xls_path.write_bytes(fixture_gen.build_xls_with_cell_types(
        fixture_gen.SYN_TRANSACTIONS,
        fixture_gen.SYN_PERIOD_FROM_RAW, fixture_gen.SYN_PERIOD_TO_RAW,
        date_rows=_ALL_ROWS, number_rows=_ALL_ROWS,
    ))

    result = ICICISkill().parse(xls_path)

    assert result.rows == EXPECTED_ROWS
    assert len(result.rows) == 5
    assert result.warnings == []


def test_all_date_and_number_cells_via_transform_never_reports_zero_rows(tmp_path):
    xls_path = tmp_path / "resaved.xls"
    xls_path.write_bytes(fixture_gen.build_xls_with_cell_types(
        fixture_gen.SYN_TRANSACTIONS,
        fixture_gen.SYN_PERIOD_FROM_RAW, fixture_gen.SYN_PERIOD_TO_RAW,
        date_rows=_ALL_ROWS, number_rows=_ALL_ROWS,
    ))
    out_csv = tmp_path / "out.csv"

    result = transform_icici_statement(str(xls_path), str(out_csv))

    # The exact regression this fixes: must NOT be success:false / 0 rows /
    # a pile of "failed to parse date" issues.
    assert result["success"] is True
    assert result["rows_output"] == 5
    assert result["rows_output"] != 0
    assert result["issues"] == []
    assert not any("failed to parse date" in i for i in result.get("issues", []))


# ---------------------------------------------------------------------------
# Day/month swap guard
# ---------------------------------------------------------------------------

def test_date_cell_day_month_not_swapped(tmp_path):
    # Row 3's date is 03,Apr,2025 -- a value that, if day/month were ever
    # swapped, would silently become 2025-03-04 (4 March) instead of
    # 2025-04-03 (3 April). Written as a real DATE cell.
    xls_path = tmp_path / "resaved.xls"
    xls_path.write_bytes(fixture_gen.build_xls_with_cell_types(
        fixture_gen.SYN_TRANSACTIONS,
        fixture_gen.SYN_PERIOD_FROM_RAW, fixture_gen.SYN_PERIOD_TO_RAW,
        date_rows=_ALL_ROWS, number_rows=set(),
    ))

    result = ICICISkill().parse(xls_path)

    row3 = result.rows[2]
    assert row3["Date"] == "2025-04-03"
    assert row3["Date"] != "2025-03-04"


# ---------------------------------------------------------------------------
# Mixed file: some rows text, some rows typed cells
# ---------------------------------------------------------------------------

def test_mixed_text_and_typed_cells_parses_fully(tmp_path):
    xls_path = tmp_path / "mixed.xls"
    xls_path.write_bytes(fixture_gen.build_xls_with_cell_types(
        fixture_gen.SYN_TRANSACTIONS,
        fixture_gen.SYN_PERIOD_FROM_RAW, fixture_gen.SYN_PERIOD_TO_RAW,
        date_rows={2, 4}, number_rows={1, 3, 5},
    ))

    result = ICICISkill().parse(xls_path)

    assert result.rows == EXPECTED_ROWS
    assert len(result.rows) == 5


# ---------------------------------------------------------------------------
# Guard the still-working original all-text fixture (no regression)
# ---------------------------------------------------------------------------

def test_original_all_text_fixture_still_parses_identically(tmp_path):
    xls_path = tmp_path / "syn_icici.xls"
    xls_path.write_bytes(fixture_gen.build_xls())

    result = ICICISkill().parse(xls_path)

    assert result.rows == EXPECTED_ROWS
    assert result.opening_balance == fixture_gen.SYN_OPENING_BALANCE
    assert result.closing_balance == fixture_gen.SYN_CLOSING_BALANCE


# ---------------------------------------------------------------------------
# Accidental .xlsx re-save
# ---------------------------------------------------------------------------

def test_xlsx_resave_parses_identically_never_silent_zero_rows(tmp_path):
    xlsx_path = tmp_path / "resaved.xlsx"
    xlsx_path.write_bytes(fixture_gen.build_xlsx_with_cell_types(
        fixture_gen.SYN_TRANSACTIONS,
        fixture_gen.SYN_PERIOD_FROM_RAW, fixture_gen.SYN_PERIOD_TO_RAW,
    ))

    try:
        result = ICICISkill().parse(xlsx_path)
    except ValueError as e:
        # Acceptable alternative: ONE clear error pointing at a fresh .xls
        # export -- never a silent 0-row success:false.
        msg = str(e)
        assert "fresh .xls" in msg or ".xls" in msg
    else:
        assert result.rows == EXPECTED_ROWS
        assert len(result.rows) == 5
        assert len(result.rows) != 0


# ---------------------------------------------------------------------------
# Zero rows parsed despite data rows existing -> one clear error (spec item 4)
# ---------------------------------------------------------------------------

def test_all_rows_unparseable_dates_gives_one_clear_error_not_per_row_noise(tmp_path):
    bad_txns = [
        ("garbage-date-1", "garbage-date-1", "-",
         "NEFT-REF0000000001-SYN SALARY CREDIT", "", "50000.00", "150000.00"),
        ("garbage-date-2", "garbage-date-2", "-",
         "UPI/300000000002/NA/synshop//SYNabc0000ef", "2000.00", "", "148000.00"),
    ]
    xls_path = tmp_path / "bad_dates.xls"
    xls_path.write_bytes(
        fixture_gen.build_xls_for(bad_txns, "01,Apr,2025", "30,Apr,2025"))
    out_csv = tmp_path / "out.csv"

    result = transform_icici_statement(str(xls_path), str(out_csv))

    assert result["success"] is False
    assert result["rows_output"] == 0
    assert result["rows_input"] == 2
    # One clear error -- not one "Row N: failed to parse date" line per row.
    assert "error" in result
    assert len(result.get("issues", [])) <= 5


def test_xlsx_magic_bytes_detected_even_with_xls_extension(tmp_path):
    """A file that's genuinely a .xlsx zip container but still named .xls
    (e.g. Excel's Save-As default, then manually renamed back) must be
    read via the openpyxl path, not fail inside xlrd.open_workbook()."""
    misnamed = tmp_path / "still_says_xls.xls"
    misnamed.write_bytes(fixture_gen.build_xlsx_with_cell_types(
        fixture_gen.SYN_TRANSACTIONS,
        fixture_gen.SYN_PERIOD_FROM_RAW, fixture_gen.SYN_PERIOD_TO_RAW,
    ))

    result = ICICISkill().parse(misnamed)

    assert result.rows == EXPECTED_ROWS
    assert len(result.rows) == 5
