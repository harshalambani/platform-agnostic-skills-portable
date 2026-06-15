#!/usr/bin/env python3
"""
extract_bob_statement.py
------------------------
Convert a Bank of Baroda "Transaction Details" PDF statement into a clean CSV.

Handles the two quirks that break naive extractors:

1. The transaction table overflows to page 2 (and beyond) WITHOUT the
   column-header row being repeated. The script detects the column x-ranges
   ONCE from page 1 and reuses them on every subsequent page.

2. Each row shows only one amount (either withdrawal OR deposit) plus a
   balance. Because the empty column produces no text token, we must use
   the amount's x-coordinate to decide which column it belongs to.

Usage:
    python extract_bob_statement.py <input.pdf> <output.csv>
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


# ---- Config --------------------------------------------------------------

# If True, emit a synthetic first row labelled "Opening Balance" whose
# Narration is the stub narration from the PDF's first row and whose
# withdrawal/deposit columns are blank. If False, skip that row entirely.
INCLUDE_OPENING_BALANCE = True

# Rows whose "top" (y-coordinate) differ by less than this are considered
# to be on the same visual line. BoB statements use tight line spacing so
# 3.0 points is comfortable.
BASELINE_TOL = 3.0

# Literal strings that mark footer content we want to drop.
FOOTER_MARKERS = (
    "https://",
    "Page Total:",
    "Grand Total:",
    "END OF STATEMENT",
    "Note: Cheques received",
    "Unless the constituent",
    "within 15 days",
    "transaction(s) in the statement",
)

# Matches a transaction DATE token at column-0 of a row: DD-MM-YY.
DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{2}$")

# Matches an amount token like 20,000.00 / 1,57,950.00 / 32.00 / 1,50,000.00Cr .
AMOUNT_RE = re.compile(r"^[\d,]+\.\d{2}(Cr|Dr)?$")


# ---- Column geometry -----------------------------------------------------


@dataclass
class ColumnMap:
    """x-coordinate boundaries of each column, learned from page 1's header."""

    date_left: float
    particulars_left: float
    chq_left: float
    withdrawals_left: float
    withdrawals_right: float
    deposits_left: float
    deposits_right: float
    balance_left: float

    @classmethod
    def from_header_words(cls, words) -> "ColumnMap":
        """Build a ColumnMap from the first occurrence of the header row."""
        positions: dict[str, tuple[float, float]] = {}
        for w in words:
            t = w["text"]
            if t in (
                "DATE",
                "PARTICULARS",
                "CHQ.NO.",
                "WITHDRAWALS",
                "DEPOSITS",
                "BALANCE",
            ) and t not in positions:
                positions[t] = (w["x0"], w["x1"])

        missing = {
            "DATE",
            "PARTICULARS",
            "CHQ.NO.",
            "WITHDRAWALS",
            "DEPOSITS",
            "BALANCE",
        } - positions.keys()
        if missing:
            raise RuntimeError(
                f"Could not locate header columns on page 1: missing {sorted(missing)}. "
                "Is this really a Bank of Baroda statement?"
            )

        # Column *center* defines the boundary — amounts are right-aligned
        # so their x1 tends to sit near each column's x1. We use a generous
        # band around the header text center to catch the amount text too.
        def center(col: str) -> float:
            lo, hi = positions[col]
            return (lo + hi) / 2

        # Use midpoints between adjacent header columns as boundaries.
        withdraw_c = center("WITHDRAWALS")
        deposit_c = center("DEPOSITS")
        balance_c = center("BALANCE")

        withdrawals_left = (center("CHQ.NO.") + withdraw_c) / 2
        withdrawals_right = (withdraw_c + deposit_c) / 2
        deposits_left = withdrawals_right
        deposits_right = (deposit_c + balance_c) / 2

        return cls(
            date_left=positions["DATE"][0],
            particulars_left=positions["PARTICULARS"][0],
            chq_left=positions["CHQ.NO."][0],
            withdrawals_left=withdrawals_left,
            withdrawals_right=withdrawals_right,
            deposits_left=deposits_left,
            deposits_right=deposits_right,
            balance_left=positions["BALANCE"][0] - 10,
        )


# ---- Row parsing ---------------------------------------------------------


@dataclass
class Row:
    date: str
    narration: str
    cheque: str
    withdrawal: str
    deposit: str
    balance: str = ""

    def as_csv(self) -> list[str]:
        return [self.date, self.narration, self.cheque, self.withdrawal, self.deposit, self.balance]


def _expand_date(dd_mm_yy: str) -> str:
    """04-04-24 → 04-04-2024 (assumes 20YY, statements are contemporary)."""
    dd, mm, yy = dd_mm_yy.split("-")
    return f"{dd}-{mm}-20{yy}"


def _clean_amount(raw: str) -> str:
    """'1,57,950.00' or '1,57,950.00Cr' → '157950.00'."""
    raw = raw.replace(",", "").replace("Cr", "").replace("Dr", "").strip()
    return raw


def _is_footer_line(text: str) -> bool:
    return any(m in text for m in FOOTER_MARKERS)


def group_words_into_lines(words, tol: float = BASELINE_TOL):
    """Group word dicts that share the same visual baseline."""
    if not words:
        return []
    # Sort by vertical position, then horizontal.
    words = sorted(words, key=lambda w: (round(w["top"] / tol), w["x0"]))
    lines: list[list[dict]] = []
    current: list[dict] = []
    current_top: Optional[float] = None
    for w in words:
        if current_top is None or abs(w["top"] - current_top) <= tol:
            current.append(w)
            current_top = w["top"] if current_top is None else current_top
        else:
            lines.append(current)
            current = [w]
            current_top = w["top"]
    if current:
        lines.append(current)
    return lines


def parse_transaction_line(line_words, cols: ColumnMap) -> Optional[Row]:
    """Return a Row, or None if this line is not a transaction row."""
    if not line_words:
        return None

    # A transaction row must start with a DD-MM-YY date at column 0.
    first = line_words[0]
    if not DATE_RE.match(first["text"]):
        return None
    date = _expand_date(first["text"])

    # Classify remaining words by x-coordinate into columns.
    particulars_tokens: list[str] = []
    cheque = ""
    withdrawal = ""
    deposit = ""
    balance = ""

    for w in line_words[1:]:
        x0, x1, text = w["x0"], w["x1"], w["text"]

        # Balance column — capture it instead of skipping.
        if x0 >= cols.balance_left - 5:
            if AMOUNT_RE.match(text):
                balance = _clean_amount(text)
            continue

        # Cheque column — by x-range AND by looking like an integer.
        if cols.chq_left - 3 <= x0 <= cols.chq_left + 30 and text.isdigit() and len(text) <= 6:
            cheque = text
            continue

        # Amount columns — must *look* like an amount.
        if AMOUNT_RE.match(text):
            # Decide withdrawal vs deposit by the word's right edge,
            # since amounts are right-aligned to their column.
            if x1 <= cols.withdrawals_right + 2:
                withdrawal = _clean_amount(text)
            elif x1 <= cols.deposits_right + 2:
                deposit = _clean_amount(text)
            else:
                # Past deposit zone → must be the balance column.
                balance = _clean_amount(text)
            continue

        # Everything else falls into narration.
        particulars_tokens.append(text)

    narration = " ".join(particulars_tokens).strip()
    narration = re.sub(r"\s+", " ", narration)

    return Row(
        date=date,
        narration=narration,
        cheque=cheque,
        withdrawal=withdrawal,
        deposit=deposit,
        balance=balance,
    )


def parse_opening_balance(line_words, cols: ColumnMap) -> Optional[Row]:
    """
    The first transaction line usually has no withdrawal/deposit — only
    a balance. Detect it and mark it as an Opening Balance row.
    """
    if not line_words:
        return None
    first = line_words[0]
    if not DATE_RE.match(first["text"]):
        return None
    # If the line has NO amount-looking token in the withdrawal or deposit
    # x-range, it's an opening-balance row.
    has_wd_or_dep = False
    for w in line_words[1:]:
        if AMOUNT_RE.match(w["text"]) and w["x1"] <= cols.deposits_right + 2:
            has_wd_or_dep = True
            break
    if has_wd_or_dep:
        return None

    narration_tokens = []
    ob_balance = ""
    for w in line_words[1:]:
        if AMOUNT_RE.match(w["text"]) and w["x1"] > cols.deposits_right + 2:
            # Amount past deposit zone → balance column
            ob_balance = _clean_amount(w["text"])
        elif not AMOUNT_RE.match(w["text"]) and w["x0"] < cols.balance_left - 5:
            narration_tokens.append(w["text"])
    narration = " ".join(narration_tokens).strip() or "Opening Balance"
    return Row(
        date=_expand_date(first["text"]),
        narration=f"Opening Balance ({narration})" if narration != "Opening Balance" else narration,
        cheque="",
        withdrawal="",
        deposit="",
        balance=ob_balance,
    )


# ---- Main extraction -----------------------------------------------------


def extract(pdf_path: Path) -> list[Row]:
    rows: list[Row] = []
    cols: Optional[ColumnMap] = None
    opening_seen = False

    with pdfplumber.open(str(pdf_path)) as pdf:
        for page_idx, page in enumerate(pdf.pages):
            words = page.extract_words(keep_blank_chars=False)
            if cols is None:
                cols = ColumnMap.from_header_words(words)

            lines = group_words_into_lines(words)

            for line_words in lines:
                line_text = " ".join(w["text"] for w in line_words)
                if _is_footer_line(line_text):
                    continue
                # Skip the repeated header rows and page banners.
                if line_words and line_words[0]["text"] in (
                    "Transaction",
                    "BANK",
                    "NORTH",
                    "ADDRESS:",
                    "HELPLINE",
                    "BRANCH",
                    "MICR",
                    "A/C",
                    "Address",
                    "City",
                    "Tel",
                    "Joint",
                    "Statement",
                    "DATE",
                    "--------------------------------------------------------------------------------------------------------------------------------",
                ):
                    continue

                # Opening balance (first page only, first dated line with no amt).
                if not opening_seen:
                    ob = parse_opening_balance(line_words, cols)
                    if ob is not None:
                        opening_seen = True
                        if INCLUDE_OPENING_BALANCE:
                            rows.append(ob)
                        continue

                row = parse_transaction_line(line_words, cols)
                if row is not None:
                    rows.append(row)

    return rows


def write_csv(rows: list[Row], out_path: Path) -> None:
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(
            ["DATE", "PARTICULARS", "CHQ.NO.", "WITHDRAWALS", "DEPOSITS", "BALANCE"]
        )
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
