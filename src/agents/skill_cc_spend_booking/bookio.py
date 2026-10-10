"""Read-only view of the GnuCash book for the card-spend booking skill.

Nothing here writes to the book. The book is parsed with the same reader the
partner and AIS skills use (skill_itr_workbook/scripts/parse_gnucash.py) and
reduced to plain records: a transaction is its date, its Num and its splits as
(colon account path, signed 2dp amount), debit positive.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

_ITR_SCRIPTS = Path(__file__).resolve().parent.parent / "skill_itr_workbook" / "scripts"
if str(_ITR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ITR_SCRIPTS))
import parse_gnucash  # noqa: E402

CCP_PATH = "Expense:Withdrawals:Credit Card Payment"


@dataclass(frozen=True)
class BookTxn:
    guid: str
    date: date
    description: str
    num: str
    splits: tuple          # ((colon path, signed amount rounded to 2dp), ...)

    def amount_on(self, path: str) -> float:
        return round(sum(v for p, v in self.splits if p == path), 2)

    def has_path(self, path: str) -> bool:
        return any(p == path for p, _ in self.splits)


@dataclass
class BookView:
    txns: list = field(default_factory=list)
    paths: set = field(default_factory=set)


def colon_paths(book) -> dict:
    """guid -> ':'-separated account path (root never included)."""
    paths: dict = {}

    def _resolve(guid: str) -> str:
        if guid in paths:
            return paths[guid]
        acct = book.accounts[guid]
        parent = book.accounts.get(acct.parent_guid) if acct.parent_guid else None
        if parent is None or parent.type == "ROOT":
            paths[guid] = acct.name
        else:
            paths[guid] = f"{_resolve(acct.parent_guid)}:{acct.name}"
        return paths[guid]

    for guid in book.accounts:
        _resolve(guid)
    return paths


def read_book(gnucash_path: str) -> BookView:
    """Parse the book read-only. Raises on an unreadable book (the caller turns
    that into a named error; a book that cannot be read is never skipped)."""
    book = parse_gnucash.parse_book(gnucash_path)
    paths = colon_paths(book)
    view = BookView(paths=set(paths.values()))
    for t in book.transactions:
        splits = tuple((paths.get(s.account_guid, "?"), round(float(s.value), 2)) for s in t.splits)
        view.txns.append(BookTxn(t.guid, t.date_posted, t.description or "", t.num or "", splits))
    return view
