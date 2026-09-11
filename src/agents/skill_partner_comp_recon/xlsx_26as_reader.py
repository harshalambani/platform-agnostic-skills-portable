"""
xlsx_26as_reader.py -- Section A: reads the "Tax Deducted" column of a Form
26AS xlsx workbook's "Part I" sheet (as produced by
skill_26as/scripts/extract_26as_to_xlsx.py) and sums it into a single TDS
credit figure for the reporting FY, feeding engine.py's existing
external["form_26as_total_credit"] reconciliation leg. This module owns
only the READ; engine.py's consuming logic (field_or_reason /
reconcile_category around form_26as_total_credit) is unchanged.

Sheet geometry (verified against skill_26as/scripts/extract_26as_to_xlsx.py,
build_part_i() / P1_HEADERS -- do not re-derive this by parsing that
script again, it is settled):
  - the worksheet title is exactly "Part I".
  - the header row is row 3 (P1_HEADERS, 15 columns); data starts row 4.
  - column 7 ("Txn Sr.No.") is populated ONLY on genuine per-transaction
    rows. The per-deductor Sub-total row (columns 1/2/13/14/15 only) and
    the sheet's single Grand Total row (columns 2/13/14/15 only) both
    leave column 7 blank. Column 7 non-empty is therefore the correct
    discriminator for "this is a real transaction row" -- summing column
    14 only over those rows avoids double/triple counting the same tax
    figure via its own subtotal/grand-total rows.
  - column 14 (1-indexed) is "Tax Deducted ##" -- the per-transaction TDS
    amount. That is what feeds form_26as_total_credit -- never column 5
    ("Total Tax Deducted #"), which is the per-DEDUCTOR running total
    repeated on every row of that deductor's block and would double-count
    across rows if summed directly.
  - an empty Part I (no deductors at all) writes a single "No Transactions
    Present" marker cell at row 4, column 1 (merged across every column)
    and writes no data/subtotal/grand-total rows at all. This reader does
    not special-case that marker string: a sheet with zero genuine
    (column-7-populated) rows sums to 0.0 either way, which is the
    correct "no TDS credit this year" answer -- a successful read, not an
    error.

Read-only: opens the workbook with openpyxl in read_only mode and never
writes anything back or anywhere.

Contract (mirrors agent.py's _resolve_llp_leg / _resolve_schedule_leg):
returns (status note, total-or-None). No path, a workbook that cannot be
opened, a missing "Part I" sheet, or a row that cannot be parsed as a
number all degrade to a "not available" note with value None -- never an
exception, never a silently substituted 0.0. An empty-but-readable sheet
(or one with no genuine transaction rows) is a *successful* read that
returns 0.0, distinguished in the note text ("no TDS entries" vs "could
not be read") exactly as required -- "no TDS entries this year" and
"the workbook could not be read" must never be conflated into the same
0.0-with-no-explanation outcome.
"""
from __future__ import annotations

import openpyxl

_SHEET_TITLE = "Part I"
_DATA_START_ROW = 4
_COL_TXN_SR_NO = 7      # 1-indexed; blank on subtotal/grand-total rows
_COL_TAX_DEDUCTED = 14  # 1-indexed; "Tax Deducted ##"

_LABEL = "26AS TDS-credit tie-out"


def read_form_26as_tds_credit(xlsx_path: str) -> tuple[str, float | None]:
    """Read `xlsx_path`'s "Part I" sheet and sum column 14 ("Tax Deducted
    ##") over genuine transaction rows only (column 7 non-empty).

    Returns (status note, total-or-None):
      - xlsx_path == "" -> ("... not available (no workbook supplied).", None)
      - the file cannot be opened as an xlsx workbook -> ("... not
        available (could not open ...)", None)
      - the workbook has no "Part I" sheet -> ("... not available (... has
        no 'Part I' sheet).", None)
      - a row under column 14 cannot be read as a number -> ("... not
        available (could not read rows ...)", None)
      - "Part I" has zero genuine transaction rows (an explicit "No
        Transactions Present" sheet, or any other reason there are no
        column-7-populated rows) -> a successful note, 0.0
      - otherwise -> a successful note naming the transaction count and
        total, the summed total

    Never raises.
    """
    if not xlsx_path:
        return f"{_LABEL}: not available (no workbook supplied).", None

    try:
        wb = openpyxl.load_workbook(xlsx_path, data_only=True, read_only=True)
    except Exception as e:
        return f"{_LABEL}: not available (could not open {xlsx_path}: {e}).", None

    try:
        if _SHEET_TITLE not in wb.sheetnames:
            return (
                f"{_LABEL}: not available ({xlsx_path} has no "
                f"{_SHEET_TITLE!r} sheet)."
            ), None
        ws = wb[_SHEET_TITLE]

        total = 0.0
        txn_count = 0
        try:
            for row in ws.iter_rows(min_row=_DATA_START_ROW):
                if len(row) < _COL_TAX_DEDUCTED:
                    continue
                sr_no = row[_COL_TXN_SR_NO - 1].value
                if sr_no in (None, ""):
                    continue  # subtotal / grand-total / trailing blank row
                tax_value = row[_COL_TAX_DEDUCTED - 1].value
                total += 0.0 if tax_value is None else float(tax_value)
                txn_count += 1
        except Exception as e:
            return (
                f"{_LABEL}: not available (could not read rows from "
                f"{xlsx_path!r}'s {_SHEET_TITLE!r} sheet: {e})."
            ), None

        if txn_count == 0:
            return (
                f"{_LABEL}: 0.00 -- no TDS entries found in "
                f"{_SHEET_TITLE!r} ({xlsx_path})."
            ), 0.0
        return (
            f"{_LABEL}: {total:.2f} from {txn_count} transaction(s) in "
            f"{_SHEET_TITLE!r} ({xlsx_path})."
        ), total
    finally:
        wb.close()
