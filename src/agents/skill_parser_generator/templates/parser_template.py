"""
parse_FORMAT.py - fuzzy deterministic parser for the FORMAT statement.

GENERATED FROM parser_template.py by skill_parser_generator. Text is
reconstructed from pdfplumber word coordinates, rows are assembled with fuzzy
boundary rules, and a self-verifying balance oracle ties the recomputed closing
balance out against the statement's own printed closing balance.

Usage:
    python parse_FORMAT.py <input_path> <output_path>

Exit codes:
    0  success (recomputed closing balance reconciles)
    1  bad arguments / file not found / decryption failed
    2  parsed, but the recomputed closing balance does NOT match the printed
       one (output is still written and flagged for a human)

Only the BLANK sections below are format-specific. The control flow, the
balance oracle (verify_balance_invariant), recompute_closing, and the 0/1/2
exit contract in main() are FIXED - do not rewrite them.
"""
from __future__ import annotations

import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

# === BLANK: format identity ================================================
FORMAT_NAME = "FORMAT"

# === BLANK: row-detection regex ============================================
# A line whose first token matches DATE_RE starts a new logical row.
DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{4}$")

# === BLANK: column x0 ranges ===============================================
# Each (lo, hi) is the pdfplumber word-x0 band for that column. Tuning these
# is the usual fix when a column's values land in the wrong field.
COLUMN_X0 = {
    "debit": (0.0, 0.0),
    "credit": (0.0, 0.0),
    "balance": (0.0, 0.0),
    "drcr": (0.0, 0.0),
}

# === BLANK: lines to skip ==================================================
# Substrings marking headers/footers/boilerplate to drop before parsing.
BOILERPLATE_MARKERS: tuple[str, ...] = ()

# === BLANK: internal-transfer marker ======================================
# Rows whose text contains this marker are dropped as non-economic transfers.
# Leave as None when the format has no internal transfers.
INTERNAL_TRANSFER_MARKER: str | None = None


def _num(s: str | None) -> float | None:
    """Parse a money token (commas allowed) into a float, or None."""
    if not s:
        return None
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _cluster_lines(words: list[dict], tol: float = 2.0) -> list[list[dict]]:
    """Group pdfplumber words into logical lines by rounded `top` coordinate."""
    buckets: dict[float, list[dict]] = defaultdict(list)
    for w in words:
        key = round(w["top"] / tol) * tol
        buckets[key].append(w)
    return [sorted(buckets[k], key=lambda w: w["x0"]) for k in sorted(buckets.keys())]


def _column_of(x0: float) -> str | None:
    """Return the column whose BLANK x0 band contains x0, else None."""
    for name, (lo, hi) in COLUMN_X0.items():
        if lo <= x0 < hi:
            return name
    return None


def extract_rows(input_path: str) -> tuple[list[dict], dict | None]:
    """
    Reconstruct ledger rows from the statement.

    BLANK BODY - fill this in for the specific statement. Use DATE_RE for row
    boundaries and _column_of() to assign each word to a column. Return
    (rows, statement_totals) where each row is a dict with the signed-balance
    inputs the oracle needs - "debit", "credit", "balance" (floats or None) and
    "drcr" ("Dr"/"Cr") - plus any display fields. statement_totals must carry
    {"closing_signed": <float>} for the printed closing balance, or be None if
    it could not be located.

    Do NOT change verify_balance_invariant, recompute_closing, or main().
    """
    import pdfplumber

    rows: list[dict] = []
    statement_totals: dict | None = None
    with pdfplumber.open(input_path) as pdf:
        for page in pdf.pages:
            for line in _cluster_lines(page.extract_words()):
                text = " ".join(w["text"] for w in line)
                if any(m in text for m in BOILERPLATE_MARKERS):
                    continue
                # TODO(blank): assemble a row dict from `line` using _column_of()
                # and _num(), appending to `rows`; capture the printed closing
                # balance into statement_totals.
                _ = text
    return rows, statement_totals


def verify_balance_invariant(rows: list[dict]) -> list[str]:
    """
    FIXED ORACLE - do not edit.

    For every row carrying a printed balance, the signed balance (positive when
    drcr == "Cr", negative when "Dr") must equal the previous signed balance
    minus debit plus credit. Returns human-readable mismatch strings (empty when
    every row reconciles).
    """
    mismatches: list[str] = []
    prev_signed: float | None = None
    for i, r in enumerate(rows):
        bal = r.get("balance")
        if bal is None:
            continue
        signed = bal if r.get("drcr") == "Cr" else -bal
        debit = r.get("debit") or 0.0
        credit = r.get("credit") or 0.0
        if prev_signed is not None:
            expected = prev_signed - debit + credit
            if abs(expected - signed) > 0.02:
                mismatches.append(
                    f"row {i}: expected signed balance {expected:.2f}, got {signed:.2f}"
                )
        prev_signed = signed
    return mismatches


def recompute_closing(rows: list[dict]) -> float | None:
    """FIXED - signed closing balance from the last row that carries one."""
    for r in reversed(rows):
        bal = r.get("balance")
        if bal is not None:
            return bal if r.get("drcr") == "Cr" else -bal
    return None


def write_output(rows: list[dict], output_path: str) -> None:
    """
    BLANK BODY - write `rows` to output_path in whatever format the skill needs
    (CSV shown here as a safe default; swap for XLSX if required).
    """
    fields = ["date", "particulars", "debit", "credit", "balance", "drcr"]
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> int:
    """FIXED control flow + 0/1/2 exit contract - do not rewrite."""
    if len(sys.argv) < 3:
        print(f"Usage: parse_{FORMAT_NAME}.py <input_path> <output_path>", file=sys.stderr)
        return 1

    input_path, output_path = sys.argv[1], sys.argv[2]
    if not Path(input_path).is_file():
        print(f"ERROR: input not found: {input_path}", file=sys.stderr)
        return 1

    rows, statement_totals = extract_rows(input_path)
    if not rows:
        print("ERROR: no rows could be extracted from the statement.", file=sys.stderr)
        return 1

    if INTERNAL_TRANSFER_MARKER is not None:
        rows = [r for r in rows if INTERNAL_TRANSFER_MARKER not in r.get("particulars", "")]

    mismatches = verify_balance_invariant(rows)
    print(f"Extracted {len(rows)} rows.")
    if mismatches:
        print(f"WARNING: {len(mismatches)} row(s) failed the balance invariant:")
        for m in mismatches[:10]:
            print(f"  {m}")

    write_output(rows, output_path)
    recomputed = recompute_closing(rows)
    print(f"Wrote {len(rows)} rows to {output_path}")

    if statement_totals is None or recomputed is None:
        print("WARNING: could not locate a printed closing balance to verify against.")
        return 2

    printed = statement_totals.get("closing_signed")
    if printed is None or abs(printed - recomputed) > 0.02:
        print(
            f"WARNING: recomputed closing balance ({recomputed:.2f}) does NOT match "
            f"the printed closing balance ({printed})."
        )
        return 2

    print(f"Closing balance verified: recomputed {recomputed:.2f} matches printed {printed:.2f}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
