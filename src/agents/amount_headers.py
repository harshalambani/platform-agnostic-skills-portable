"""amount_headers.py -- the one place that knows every spelling of the amount headers.

Import-ready files written by the mapper use "Amount Negated (Deposit)" and
"Amount (Withdrawal)". Older files use "Deposit - Amount Negated" /
"Withdrawal - Amount", and canonical bank CSVs use plain "Deposit" / "Withdrawal".
Every reader goes through these helpers, so a header spelling is never matched by
hand-written string literals in more than one file.

This module imports nothing from the project (no cycle with canonical_io or
balance_utils).
"""
from __future__ import annotations

# MAP-27: self-describing amount headers on the GnuCash-IMPORT-READY file only.
# GnuCash's CSV importer maps each column by type. The mapper's Account is the
# CATEGORY split, not the bank, so a deposit must be imported as a NEGATED amount
# and a withdrawal as a plain amount. Naming the headers after the importer's own
# column types ("Amount Negated" / "Amount") makes the mapping obvious and lets
# GnuCash remember it as a preset. Canonical bank CSVs and every intermediate keep
# the plain "Deposit" / "Withdrawal" names (CANONICAL_FIELDS is unchanged).
IMPORT_DEPOSIT_HEADER = "Amount Negated (Deposit)"
IMPORT_WITHDRAWAL_HEADER = "Amount (Withdrawal)"

# Every spelling a reader must accept, newest first: the new pair, the pair of the
# earlier (1092991) import-ready files, and the plain canonical names.
DEPOSIT_HEADER_ALIASES: tuple[str, ...] = (
    IMPORT_DEPOSIT_HEADER, "Deposit - Amount Negated", "Deposit")
WITHDRAWAL_HEADER_ALIASES: tuple[str, ...] = (
    IMPORT_WITHDRAWAL_HEADER, "Withdrawal - Amount", "Withdrawal")


def find_deposit_key(keys) -> str | None:
    """The header (any accepted spelling) a file uses for its Deposit column."""
    present = set(keys)
    return next((k for k in DEPOSIT_HEADER_ALIASES if k in present), None)


def find_withdrawal_key(keys) -> str | None:
    """The header (any accepted spelling) a file uses for its Withdrawal column."""
    present = set(keys)
    return next((k for k in WITHDRAWAL_HEADER_ALIASES if k in present), None)


def import_ready_row(row: dict) -> dict:
    """Return ``row`` with its amount keys renamed to the import-ready headers.

    Key order and values are untouched (a rename in place), so a Deposit value can
    never move under the Withdrawal header or vice versa. A row that already uses
    the new names comes back unchanged. If a row somehow carries BOTH the new name
    and an older spelling, the older key is left alone rather than overwritten.
    """
    rename = {}
    for aliases, new in ((DEPOSIT_HEADER_ALIASES, IMPORT_DEPOSIT_HEADER),
                         (WITHDRAWAL_HEADER_ALIASES, IMPORT_WITHDRAWAL_HEADER)):
        if new in row:
            continue
        old = next((k for k in aliases if k in row), None)
        if old:
            rename[old] = new
    if not rename:
        return row
    return {rename.get(k, k): v for k, v in row.items()}


def import_ready_rows(rows) -> list[dict]:
    """:func:`import_ready_row` over a list of rows."""
    return [import_ready_row(r) for r in rows]


def row_deposit(row: dict):
    """The Deposit value of ``row`` under whichever spelling it uses (else None)."""
    k = find_deposit_key(row.keys())
    return row.get(k) if k else None


def row_withdrawal(row: dict):
    """The Withdrawal value of ``row`` under whichever spelling it uses (else None)."""
    k = find_withdrawal_key(row.keys())
    return row.get(k) if k else None
