#!/usr/bin/env python3
"""
extract_kotak_statement.py
---------------------------
Convert a Kotak Mahindra Bank savings-account "Transaction Details" PDF
statement into a list of transaction rows.

Kotak statements are bordered/grid tables, so this uses pdfplumber's
``extract_tables()`` (line-based table detection) rather than BoB's
word-position approach. Handles the quirks that break naive extractors:

1. The transaction table overflows to page 2+ WITHOUT the column-header
   row being repeated -- but because pdfplumber detects the table from
   ruling lines (not text), no column-geometry-learning step is needed;
   every table on every page yields rows in the same 7-column order:
   # | Date | Description | Chq/Ref. No. | Withdrawal (Dr.) | Deposit (Cr.) | Balance

2. The LAST page carries an abbreviation LEGEND that ``extract_tables()``
   also returns as a table (~14 rows, 2 columns: Code | Meaning). It is
   NOT transaction data. Because it has fewer than 7 columns, it is
   rejected purely structurally -- no keyword/marker list is needed.

3. The first data row is a synthetic "Opening Balance" pseudo-row: no
   ``#``, no ``Date``, only a ``Balance``. It is captured (so callers can
   see it) but excluded from the canonical transaction rows by the caller,
   mirroring BoB's "Opening Balance" row-skip convention.

4. Real transaction rows are anchored on a parseable ``DD Mon YYYY`` Date
   cell (e.g. "03 Jun 2026"); rows whose Date cell doesn't parse (and
   aren't the opening-balance row) are rejected rather than silently
   corrupting the output.

Usage:
    python extract_kotak_statement.py <input.pdf> <output.csv>
"""

from __future__ import annotations

import csv
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

try:
    import pdfplumber
except ImportError:
    sys.stderr.write(
        "pdfplumber is required. Install with: pip install pdfplumber --break-system-packages\n"
    )
    sys.exit(2)

# Runs as a subprocess entry point (not `python -m`), so agents.* isn't on
# sys.path automatically -- bootstrap it the same way suggest.py does.
_SRC_ROOT = Path(__file__).resolve().parents[3]
if str(_SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(_SRC_ROOT))

from agents.bank_common import normalize as _normalize  # noqa: E402
from agents.bank_common import password as _password  # noqa: E402
from agents.bank_common import text_quality as _text_quality  # noqa: E402

# Anchors that must be present for the page-1 text layer to be considered
# usable (garbled/scanned PDFs fail this and raise rather than silently
# producing empty output).
_KOTAK_TEXT_ANCHORS = (r'\bdate\b', r'description')

# A real transaction row's Date cell: "03 Jun 2026".
_DATE_RE = re.compile(r'^\d{2}\s+[A-Za-z]{3}\s+\d{4}$')

# The number of columns a real transaction table row has. Any table row
# with fewer columns (e.g. the 2-column legend) cannot be a transaction
# row and is rejected purely structurally.
_TXN_COLS = 7

# Header-row cell tokens (lower-cased) -- used only to skip the literal
# header text when it appears as a data row (page 1's own header).
_HEADER_TOKENS = {"#", "date", "description", "chq/ref. no.", "chq/ref.no.",
                   "withdrawal (dr.)", "deposit (cr.)", "balance"}


@dataclass
class Row:
    serial: str
    date: str            # ISO YYYY-MM-DD, or "" for the opening-balance row
    description: str
    chq_ref: str
    withdrawal: str
    deposit: str
    balance: str
    is_opening_balance: bool = False

    def as_csv(self) -> list[str]:
        return [self.serial, self.date, self.description, self.chq_ref,
                self.withdrawal, self.deposit, self.balance]


def _clean_amount(raw: str) -> str:
    """'1,25,000.00' -> '125000.00'. Blank/None -> ''."""
    if raw is None:
        return ""
    raw = str(raw).strip()
    if not raw:
        return ""
    return _normalize.clean_amount(raw, blank_zero=False)


def _is_header_row(cells: list[str]) -> bool:
    lowered = {c.strip().lower() for c in cells if c and c.strip()}
    return bool(lowered) and lowered <= _HEADER_TOKENS


def _parse_row(cells: list[str]) -> Optional[Row]:
    """Classify one raw table row (>=7 cells) as a transaction row, the
    opening-balance pseudo-row, or reject it (returns None)."""
    cells = [(c or "").strip() for c in cells[:_TXN_COLS]]
    if len(cells) < _TXN_COLS:
        return None
    serial, date_s, desc, chq_ref, wdl, dep, bal = cells

    if _is_header_row(cells):
        return None

    iso_date = _normalize.parse_space_month_date(date_s)
    if iso_date is not None:
        return Row(
            serial=serial,
            date=iso_date,
            description=re.sub(r"\s+", " ", desc).strip(),
            chq_ref=chq_ref,
            withdrawal=_clean_amount(wdl),
            deposit=_clean_amount(dep),
            balance=_clean_amount(bal),
            is_opening_balance=False,
        )

    # Opening Balance pseudo-row: no #, no Date, description mentions
    # "opening balance", only a Balance cell populated.
    if not serial and not date_s and "opening balance" in desc.lower() and bal.strip():
        return Row(
            serial="",
            date="",
            description=re.sub(r"\s+", " ", desc).strip(),
            chq_ref="",
            withdrawal="",
            deposit="",
            balance=_clean_amount(bal),
            is_opening_balance=True,
        )

    # Anything else with an unparseable date (legend text that happened to
    # land in a 7-cell row, a footer/disclaimer line, etc.) is rejected --
    # fail loud by simply not emitting a row, per the parsing-gotcha spec.
    return None


def extract(pdf_path: Path, password: Optional[str] = None) -> list[Row]:
    rows: list[Row] = []

    try:
        pdf_cm = pdfplumber.open(str(pdf_path), password=password or "")
    except RuntimeError:
        raise
    except Exception as e:  # noqa: BLE001 -- classify password vs. other failures
        if _password.is_password_error(e):
            raise RuntimeError(_password.password_error_message()) from e
        raise

    with pdf_cm as pdf:
        if pdf.pages:
            first_page_text = pdf.pages[0].extract_text() or ""
            if not _text_quality.text_layer_usable(first_page_text, _KOTAK_TEXT_ANCHORS):
                raise RuntimeError(
                    "PDF text layer is not usable for parsing (garbled or scanned "
                    "text) -- no OCR fallback is available for Kotak statements."
                )

        for page in pdf.pages:
            for table in page.extract_tables():
                for raw_row in table:
                    if raw_row is None:
                        continue
                    row = _parse_row(raw_row)
                    if row is not None:
                        rows.append(row)

    return rows


def write_csv(rows: list[Row], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["#", "DATE", "DESCRIPTION", "CHQ/REF.NO.", "WITHDRAWAL", "DEPOSIT", "BALANCE"])
        for r in rows:
            w.writerow(r.as_csv())


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        sys.stderr.write(f"Usage: {argv[0]} <input.pdf> <output.csv>\n")
        return 2
    inp = Path(argv[1])
    out = Path(argv[2])
    if not inp.exists():
        sys.stderr.write(f"Input not found: {inp}\n")
        return 2
    rows = extract(inp)
    write_csv(rows, out)
    sys.stdout.write(f"Wrote {len(rows)} rows to {out}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
