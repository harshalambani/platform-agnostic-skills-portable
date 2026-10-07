"""
excel_writer.py -- writes the Coverage Gap Detector's report workbook.

Two sheets, in plain language (same font and header habits as the other
GnuCash-family skills):

  1. "Missing months"   -- the opening line on top, then one row per account
                           with empty months: Account | Months with no
                           transactions | What it means. Accounts that are
                           only checked on request ("Other accounts") sit in
                           their own labelled section underneath.
  2. "Accounts checked" -- one row per checked account, including the ones
                           with nothing missing: transactions in the year,
                           months with transactions out of months checked,
                           and the first/last transaction date in the year.
"""
from __future__ import annotations

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

FONT_NAME = "Arial"

_HEADER_FILL = PatternFill("solid", fgColor="D9D9D9")
_HEADER_FONT = Font(name=FONT_NAME, bold=True)

_RED_FILL = PatternFill("solid", fgColor="FFC7CE")
_RED_FONT_COLOR = "9C0006"
_YELLOW_FILL = PatternFill("solid", fgColor="FFEB9C")
_YELLOW_FONT_COLOR = "9C6500"


def _font(bold: bool = False, color: str | None = None) -> Font:
    return Font(name=FONT_NAME, bold=bold, color=color)


def _set(ws, row: int, col: int, value, *, bold: bool = False,
         fill: PatternFill | None = None, color: str | None = None) -> None:
    cell = ws.cell(row=row, column=col, value=value)
    cell.font = _font(bold=bold, color=color)
    if fill is not None:
        cell.fill = fill


def _write_header(ws, row: int, headers: list[str]) -> None:
    for col, text in enumerate(headers, start=1):
        cell = ws.cell(row=row, column=col, value=text)
        cell.font = _HEADER_FONT
        cell.fill = _HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
    ws.freeze_panes = ws.cell(row=row + 1, column=1).coordinate


def _autosize(ws, ncols: int, min_width: int = 10, max_width: int = 44) -> None:
    for col in range(1, ncols + 1):
        letter = get_column_letter(col)
        longest = 0
        for cell in ws[letter]:
            if cell.value is not None:
                longest = max(longest, len(str(cell.value)))
        ws.column_dimensions[letter].width = max(min_width, min(max_width, longest + 2))


def _write_missing_sheet(wb: Workbook, scan) -> None:
    ws = wb.create_sheet("Missing months")
    _set(ws, 1, 1, scan.opening_line(), bold=True)
    headers = ["Account", "Months with no transactions", "What it means"]
    _write_header(ws, 3, headers)

    row = 4
    any_gap = False
    for idx, (title, rows) in enumerate(scan.sections()):
        if not rows:
            continue
        any_gap = True
        if idx > 0 or scan.include_other:
            _set(ws, row, 1, title, bold=True, fill=_YELLOW_FILL, color=_YELLOW_FONT_COLOR)
            ws.cell(row=row, column=2).fill = _YELLOW_FILL
            ws.cell(row=row, column=3).fill = _YELLOW_FILL
            row += 1
        for r in rows:
            name = r.account_path if scan.books_scanned <= 1 else f"{r.account_path} [{r.book}]"
            _set(ws, row, 1, name)
            _set(ws, row, 2, r.months_text,
                 fill=(_RED_FILL if r.trailing_months else None),
                 color=(_RED_FONT_COLOR if r.trailing_months else None))
            _set(ws, row, 3, r.meaning)
            for col in (2, 3):
                ws.cell(row=row, column=col).alignment = Alignment(wrap_text=True, vertical="top")
            row += 1
    if not any_gap:
        _set(ws, row, 1, "No account has months with no transactions.")
    for w, letter in ((46, "A"), (46, "B"), (70, "C")):
        ws.column_dimensions[letter].width = w


def _write_checked_sheet(wb: Workbook, scan) -> None:
    ws = wb.create_sheet("Accounts checked")
    headers = ["Account", "Book", "Kind of account", "Financial year",
               "Transactions in the year", "Months with transactions",
               "First transaction in the year", "Last transaction in the year"]
    _write_header(ws, 1, headers)
    row = 2
    order = sorted(scan.results, key=lambda r: (not r.is_core,
                                                r.entity, r.book, r.account_path))
    for r in order:
        _set(ws, row, 1, r.account_path)
        _set(ws, row, 2, r.book)
        _set(ws, row, 3, "Bank or card" if r.is_core else "Other")
        _set(ws, row, 4, r.fy_key)
        _set(ws, row, 5, r.txns_in_fy)
        _set(ws, row, 6, f"{r.months_with_txns} of {r.months_checked}")
        _set(ws, row, 7, r.first_in_fy or "-")
        _set(ws, row, 8, r.last_in_fy or "-")
        row += 1
    if not scan.results:
        _set(ws, row, 1, "No accounts with transactions were found to check.")
    _autosize(ws, len(headers))


def write_report_workbook(scan, out_path: str) -> None:
    wb = Workbook()
    wb.remove(wb.active)
    _write_missing_sheet(wb, scan)
    _write_checked_sheet(wb, scan)
    wb.save(out_path)
