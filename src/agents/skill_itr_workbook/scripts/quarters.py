"""
quarters.py -- generic 234C date-bucket function for dividend/interest
receipts (plan section 1.2, point 1).

Five 234C windows: <=15-Jun, 16-Jun..15-Sep, 16-Sep..15-Dec, 16-Dec..15-Mar,
16-Mar..31-Mar. 31-03-dated entries are additionally flagged as TDS gross-up
candidates -- attribution is a later batch; this module never reattributes
them, it only reports the flag list (plan OQ-5 note).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from as26 import classify_section
from parse_gnucash import Book, fy_window, normalize_value


@dataclass
class QuarterBuckets:
    buckets: list[float]  # index 0..4, one per 234C window
    total: float
    gross_up_flags: list[dict] = field(default_factory=list)


def _bucket_index(d: date, fy_start_year: int) -> int:
    thresholds = [
        date(fy_start_year, 6, 15),
        date(fy_start_year, 9, 15),
        date(fy_start_year, 12, 15),
        date(fy_start_year + 1, 3, 15),
        date(fy_start_year + 1, 3, 31),
    ]
    for i, t in enumerate(thresholds):
        if d <= t:
            return i
    return 4


def bucket_receipts(book: Book, account_guids: set[str], year_key: str) -> QuarterBuckets:
    """Bucket every split posted to `account_guids` within the FY window for
    `year_key` into the five 234C windows. Also collects a flag list of
    31-03-dated entries as TDS gross-up candidates (never reattributed)."""
    start, end = fy_window(year_key)
    fy_start_year = start.year
    fy_end_date = date(fy_start_year + 1, 3, 31)

    buckets = [0.0] * 5
    flags: list[dict] = []

    for txn in book.transactions:
        if not (start <= txn.date_posted <= end):
            continue
        for sp in txn.splits:
            if sp.account_guid not in account_guids:
                continue
            acct = book.accounts[sp.account_guid]
            amount = normalize_value(sp.value, acct.type)
            idx = _bucket_index(txn.date_posted, fy_start_year)
            buckets[idx] += amount
            if txn.date_posted == fy_end_date:
                flags.append({
                    "txn_guid": txn.guid,
                    "date": txn.date_posted.isoformat(),
                    "description": txn.description,
                    "account": acct.name,
                    "amount": amount,
                })

    return QuarterBuckets(buckets=buckets, total=sum(buckets), gross_up_flags=flags)


def bucket_as26_transactions(transactions: list, year_key: str, category: str, tds_sections: dict) -> QuarterBuckets:
    """CF5: bucket a 26AS Part I transaction list (scripts/as26.py) into the
    same five 234C windows, using each transaction's OWN date (as reported
    to TRACES) rather than the book's posting date -- the override path
    described in plan D17. `category` is 'dividend' or 'interest'; only
    transactions whose section classifies into that category (via the
    Rules-config tds_sections map) are counted. 31-03-dated entries are
    flagged as TDS gross-up candidates, same convention as bucket_receipts."""
    start, end = fy_window(year_key)
    fy_start_year = start.year
    fy_end_date = date(fy_start_year + 1, 3, 31)

    buckets = [0.0] * 5
    flags: list[dict] = []

    for txn in transactions:
        if txn.txn_date is None or not (start <= txn.txn_date <= end):
            continue
        if classify_section(txn.section, tds_sections) != category:
            continue
        idx = _bucket_index(txn.txn_date, fy_start_year)
        buckets[idx] += txn.amount
        if txn.txn_date == fy_end_date:
            flags.append({
                "deductor_name": txn.deductor_name,
                "tan": txn.tan,
                "date": txn.txn_date.isoformat(),
                "amount": txn.amount,
            })

    return QuarterBuckets(buckets=buckets, total=sum(buckets), gross_up_flags=flags)
