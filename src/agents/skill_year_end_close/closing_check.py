"""
closing_check.py -- step 4: is the NEW year's book properly closed for the
closing year? Read-only: the book is parsed from its bytes and never saved.

Both must hold:
  (a) every INCOME and EXPENSE account nets to zero as of 31 March of the
      closing FY;
  (b) closing entries exist: transactions dated 31 March of the closing FY
      that post to an Income/Expense account with the counter-side in Equity.

Failures list account paths only -- never amounts.
"""
from __future__ import annotations

import gzip
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from fractions import Fraction
from pathlib import Path

from agents.gnucash_accounts import load_accounts

_GNC = "{http://www.gnucash.org/XML/gnc}"
_TRN = "{http://www.gnucash.org/XML/trn}"
_SPLIT = "{http://www.gnucash.org/XML/split}"
_TS = "{http://www.gnucash.org/XML/ts}"


@dataclass
class CheckResult:
    ok: bool
    failed: list = field(default_factory=list)      # names of failed checks
    income_nonzero: list = field(default_factory=list)   # account paths
    expense_nonzero: list = field(default_factory=list)  # account paths
    closing_entries: int = 0
    note: str = ""


def _fraction(text: str) -> Fraction:
    try:
        return Fraction(text.strip())
    except (ValueError, ZeroDivisionError):
        return Fraction(0)


def _read_book(path: Path):
    raw = Path(path).read_bytes()
    data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
    return ET.fromstring(data)


def check_closed(book_path: str | Path, closing_end_year: int) -> CheckResult:
    """Run both checks on `book_path` for the FY ending 31 March
    `closing_end_year`."""
    cutoff = f"{closing_end_year:04d}-03-31"
    try:
        root = _read_book(Path(book_path))
    except Exception as exc:  # unreadable / not a GnuCash XML book
        return CheckResult(False, ["book could not be read"],
                           note=f"the book could not be read ({type(exc).__name__})")

    accounts = {a.id: a for a in load_accounts(book_path)}
    if not accounts:
        return CheckResult(False, ["book could not be read"],
                           note="the book has no accounts")

    book = root.find(f"{_GNC}book")
    if book is None:
        return CheckResult(False, ["book could not be read"], note="no book element")

    balance: dict[str, Fraction] = {}
    closing_entries = 0
    # Direct children only: scheduled-transaction templates also use the
    # gnc:transaction tag but live under gnc:template-transactions.
    for trn in book.findall(f"{_GNC}transaction"):
        posted = trn.find(f"{_TRN}date-posted/{_TS}date")
        day = (posted.text or "").strip()[:10] if posted is not None else ""
        if not day:
            continue
        splits = []
        for sp in trn.findall(f"{_TRN}splits/{_TRN}split"):
            acc = sp.find(f"{_SPLIT}account")
            # Quantity is in the account's own commodity; value is in the
            # transaction's currency. Balance uses quantity, value only as a fallback.
            val = sp.find(f"{_SPLIT}quantity")
            if val is None or not (val.text or "").strip():
                val = sp.find(f"{_SPLIT}value")
            if acc is None or val is None or (acc.text or "") not in accounts:
                continue
            splits.append((acc.text, _fraction(val.text or "0")))
        if day <= cutoff:
            for aid, value in splits:
                if accounts[aid].type in ("INCOME", "EXPENSE"):
                    balance[aid] = balance.get(aid, Fraction(0)) + value
        if day == cutoff:
            kinds = {accounts[aid].type for aid, _ in splits}
            if kinds & {"INCOME", "EXPENSE"} and "EQUITY" in kinds:
                closing_entries += 1

    income = sorted(accounts[a].path for a, v in balance.items()
                    if v != 0 and accounts[a].type == "INCOME")
    expense = sorted(accounts[a].path for a, v in balance.items()
                     if v != 0 and accounts[a].type == "EXPENSE")
    failed = []
    if income:
        failed.append("(a) Income accounts are not zero at 31 March")
    if expense:
        failed.append("(a) Expense accounts are not zero at 31 March")
    if closing_entries == 0:
        failed.append("(b) no closing entries dated 31 March")
    return CheckResult(not failed, failed, income, expense, closing_entries)
