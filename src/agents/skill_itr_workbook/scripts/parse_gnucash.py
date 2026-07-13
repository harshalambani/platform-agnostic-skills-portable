"""
parse_gnucash.py -- gzipped GnuCash-v2 XML book -> account/transaction model.

Per the plan (2026-07-12-itr-workbook-skill-plan.md, section 1.2):
  - accounts: guid, name, type, parent-chain path, commodity
  - transactions/splits: ISO date-posted, description, memo, account guid,
    value and quantity kept as exact rationals (Fraction) internally; float
    only at the model boundary (normalize_value / account_fy_sum)
  - income-year window filter (plan section 5.1, D19): canonical year key is
    the income year, e.g. "2024-25" -> 2024-04-01..2025-03-31
  - sign normalization (plan section 3.2): book credits on INCOME/LIABILITY/
    EQUITY/EXPENSE accounts are flipped to match the eguile HTML's
    presentation signs (verified empirically against the real corpus in
    Batch 2: TDS on Interest books +74,053.00 raw for FY24-25 but the HTML
    shows -74,053.00 -- EXPENSE flips too, not just INCOME/LIABILITY/EQUITY
    as the plan prose names).
"""
from __future__ import annotations

import gzip
from dataclasses import dataclass, field
from datetime import date, datetime
from fractions import Fraction
from pathlib import Path
from xml.etree import ElementTree as ET

NS = {
    "gnc": "http://www.gnucash.org/XML/gnc",
    "act": "http://www.gnucash.org/XML/act",
    "book": "http://www.gnucash.org/XML/book",
    "cmdty": "http://www.gnucash.org/XML/cmdty",
    "trn": "http://www.gnucash.org/XML/trn",
    "split": "http://www.gnucash.org/XML/split",
    "ts": "http://www.gnucash.org/XML/ts",
    "slot": "http://www.gnucash.org/XML/slot",
}

# Account types whose raw booked (debit-normal-negated / credit) value must be
# flipped to match the eguile HTML's presentation sign. ASSET/STOCK/MUTUAL/
# BANK/CASH/RECEIVABLE/ROOT/TRADING are debit-normal and already match.
FLIP_TYPES = {"INCOME", "EXPENSE", "LIABILITY", "EQUITY", "CREDIT", "PAYABLE"}


@dataclass
class Account:
    guid: str
    name: str
    type: str
    parent_guid: str | None
    commodity_space: str | None = None
    commodity_id: str | None = None
    path: str = ""  # filled in by _build_paths() after the full tree loads


@dataclass
class Split:
    guid: str
    account_guid: str
    value: Fraction
    quantity: Fraction
    action: str | None = None
    reconciled_state: str | None = None


@dataclass
class Transaction:
    guid: str
    date_posted: date
    description: str
    splits: list[Split] = field(default_factory=list)


@dataclass
class Book:
    accounts: dict[str, Account]
    transactions: list[Transaction]


def _parse_fraction(text: str) -> Fraction:
    num, den = text.split("/")
    return Fraction(int(num), int(den))


def _build_paths(accounts: dict[str, Account]) -> None:
    def _resolve(guid: str) -> str:
        acct = accounts[guid]
        if acct.path:
            return acct.path
        parent = accounts.get(acct.parent_guid) if acct.parent_guid else None
        if parent is None or parent.type == "ROOT":
            acct.path = acct.name
        else:
            acct.path = f"{_resolve(acct.parent_guid)}/{acct.name}"
        return acct.path

    for guid in accounts:
        _resolve(guid)


def _is_gzip(path: Path) -> bool:
    with open(path, "rb") as f:
        return f.read(2) == b"\x1f\x8b"


def parse_book(path: str | Path) -> Book:
    path = Path(path)
    opener = gzip.open if _is_gzip(path) else open
    with opener(path, "rt", encoding="utf-8", errors="replace") as f:
        text = f.read()
    return parse_book_text(text)


def parse_book_text(xml_text: str) -> Book:
    root = ET.fromstring(xml_text)

    accounts: dict[str, Account] = {}
    for acc_el in root.iter(f"{{{NS['gnc']}}}account"):
        guid = acc_el.find("act:id", NS).text
        name = acc_el.find("act:name", NS).text or ""
        typ = acc_el.find("act:type", NS).text or ""
        parent_el = acc_el.find("act:parent", NS)
        parent_guid = parent_el.text if parent_el is not None else None
        space = commodity_id = None
        cmdty_el = acc_el.find("act:commodity", NS)
        if cmdty_el is not None:
            space_el = cmdty_el.find("cmdty:space", NS)
            id_el = cmdty_el.find("cmdty:id", NS)
            space = space_el.text if space_el is not None else None
            commodity_id = id_el.text if id_el is not None else None
        accounts[guid] = Account(
            guid=guid, name=name, type=typ, parent_guid=parent_guid,
            commodity_space=space, commodity_id=commodity_id,
        )
    _build_paths(accounts)

    transactions: list[Transaction] = []
    for txn_el in root.iter(f"{{{NS['gnc']}}}transaction"):
        guid = txn_el.find("trn:id", NS).text
        date_el = txn_el.find("trn:date-posted/ts:date", NS)
        dt = datetime.strptime(date_el.text[:10], "%Y-%m-%d").date()
        desc_el = txn_el.find("trn:description", NS)
        desc = desc_el.text if desc_el is not None else ""
        splits: list[Split] = []
        for sp_el in txn_el.findall("trn:splits/trn:split", NS):
            sguid = sp_el.find("split:id", NS).text
            val = _parse_fraction(sp_el.find("split:value", NS).text)
            qty = _parse_fraction(sp_el.find("split:quantity", NS).text)
            acct_el = sp_el.find("split:account", NS)
            action_el = sp_el.find("split:action", NS)
            recon_el = sp_el.find("split:reconciled-state", NS)
            splits.append(Split(
                guid=sguid, account_guid=acct_el.text, value=val, quantity=qty,
                action=action_el.text if action_el is not None else None,
                reconciled_state=recon_el.text if recon_el is not None else None,
            ))
        transactions.append(Transaction(guid=guid, date_posted=dt, description=desc, splits=splits))

    transactions.sort(key=lambda t: t.date_posted)
    return Book(accounts=accounts, transactions=transactions)


def fy_window(year_key: str) -> tuple[date, date]:
    """Canonical income-year key (plan section 5.1, D19), e.g. '2024-25' ->
    (2024-04-01, 2025-03-31)."""
    start_year = int(year_key[:4])
    return date(start_year, 4, 1), date(start_year + 1, 3, 31)


def normalize_value(value: Fraction, account_type: str) -> float:
    v = float(value)
    return -v if account_type in FLIP_TYPES else v


def fy_transactions(book: Book, year_key: str) -> list[Transaction]:
    start, end = fy_window(year_key)
    return [t for t in book.transactions if start <= t.date_posted <= end]


def account_fy_sum(book: Book, guid: str, year_key: str) -> float:
    """Sign-normalized FY sum of all splits posted to account `guid`."""
    acct = book.accounts[guid]
    total = Fraction(0)
    for txn in fy_transactions(book, year_key):
        for sp in txn.splits:
            if sp.account_guid == guid:
                total += sp.value
    return normalize_value(total, acct.type)


def accounts_by_name_substring(book: Book, substring: str) -> list[Account]:
    needle = substring.lower()
    return [a for a in book.accounts.values() if needle in a.name.lower()]
