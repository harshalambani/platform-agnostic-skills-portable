"""
foreign_dividend_journal.py -- ITR-09: an importable GnuCash journal CSV for the
foreign dividends in a broker's tax report.

One balanced entry per payment, at the report's INR figures:

    foreign dividend cash account   Dr  net   (gross - tax withheld)
    foreign dividend tax account    Dr  tax withheld
    foreign dividend income account Cr  gross

A payment the book already holds (same date, and an amount equal to the gross or
the net) is left out and listed as "already booked". The three accounts come
from the entity's own settings; if any is missing, unknown to the book, hidden
or a placeholder, the run STOPS, emits nothing and says why. Nothing here
guesses an account.

The CSV is the same multi-split layout the 26AS journal builder writes
(build_tds_journals.write_csv): one signed Amount per split, debit positive,
the Date / Transaction ID / Number / Description repeated on every split row.
"""
from __future__ import annotations

import csv
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

HEADERS = ["Date", "Transaction ID", "Number", "Description", "Account", "Amount", "Currency"]
CURRENCY = "INR"

#: EntityProfile attribute -> the label used in messages, in posting order.
ACCOUNT_FIELDS = (
    ("foreign_dividend_cash_account", "Foreign dividend cash account"),
    ("foreign_dividend_tax_account", "Foreign dividend tax account"),
    ("foreign_dividend_income_account", "Foreign dividend income account"),
)

_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d-%b-%Y", "%d %b %Y", "%d-%B-%Y", "%d %B %Y")


def parse_date(text) -> date | None:
    """A report date as a date, or None when it cannot be read (never guessed)."""
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    s = str(text or "").strip()
    if not s:
        return None
    s = s.split(" 00:00:00")[0].split("T")[0].strip()
    for f in _DATE_FORMATS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    return None


def _cents(x: float) -> int:
    return int(round(float(x) * 100))


@dataclass
class Entry:
    txn_id: str
    when: date
    description: str
    gross: int          # cents
    tax: int            # cents
    net: int            # cents
    splits: list = field(default_factory=list)   # [(account path, signed cents)]


@dataclass
class JournalResult:
    entries: list = field(default_factory=list)
    already_booked: list = field(default_factory=list)   # [(payment label, gross cents, book description)]
    problems: list = field(default_factory=list)         # payments left out, with the reason
    stopped: str = ""                                    # non-empty == the run stopped, nothing emitted
    notes: list = field(default_factory=list)

    @property
    def rows(self) -> list:
        out = []
        for e in self.entries:
            for acct, cents in e.splits:
                out.append({
                    "Date": e.when.isoformat(), "Transaction ID": e.txn_id, "Number": e.txn_id,
                    "Description": e.description, "Account": acct,
                    "Amount": f"{cents / 100:.2f}", "Currency": CURRENCY,
                })
        return out

    @property
    def gross_total(self) -> float:
        return sum(e.gross for e in self.entries) / 100

    @property
    def tax_total(self) -> float:
        return sum(e.tax for e in self.entries) / 100

    @property
    def booked_gross_total(self) -> float:
        return sum(g for _l, g, _d in self.already_booked) / 100

    def summary_lines(self) -> list:
        if self.stopped:
            return [f"Foreign dividend journal: NOT written -- {self.stopped}"]
        lines = [
            f"Foreign dividend journal: {len(self.entries)} entry(ies) to import "
            f"(gross Rs {self.gross_total:,.2f}, tax withheld Rs {self.tax_total:,.2f}); "
            f"{len(self.already_booked)} already booked; {len(self.problems)} left out."
        ]
        for label, _g, desc in self.already_booked:
            lines.append(f"  already booked: {label} (matches '{desc}')")
        for p in self.problems:
            lines.append(f"  left out: {p}")
        lines.extend(f"  {n}" for n in self.notes)
        return lines


@dataclass(frozen=True)
class BookTxnLite:
    when: date
    description: str
    amounts: frozenset      # absolute split amounts, in cents


def book_txns_from_book(book) -> list:
    """The parse_gnucash.Book's transactions reduced to (date, description, split amounts)."""
    out = []
    for t in getattr(book, "transactions", []) or []:
        amts = frozenset(abs(_cents(float(s.value))) for s in t.splits)
        out.append(BookTxnLite(t.date_posted, t.description or "", amts))
    return out


def _stop(why: str) -> JournalResult:
    return JournalResult(stopped=why)


def check_accounts(accounts: dict, guard=None) -> str:
    """"" when the three accounts are usable, else the reason the run must stop."""
    missing = [label for key, label in ACCOUNT_FIELDS if not (accounts.get(key) or "").strip()]
    if missing:
        return ("no " + ", ".join(missing) + " is set for this entity -- set "
                + ("it" if len(missing) == 1 else "them")
                + " on the Entities tab (Foreign dividends). Nothing was written.")
    paths = [accounts[key].strip() for key, _l in ACCOUNT_FIELDS]
    if len(set(paths)) != len(paths):
        return "two of the foreign dividend accounts are the same account. Nothing was written."
    if guard is not None:
        for (key, label), path in zip(ACCOUNT_FIELDS, paths):
            if hasattr(guard, "has_path") and getattr(guard, "known", True) and not guard.has_path(path):
                return f"{label} '{path}' is not an account in the book. Nothing was written."
            reason = guard.blocked_target_reason(path)
            if reason:
                return f"{label} '{path}' cannot be used -- {reason}. Nothing was written."
    return ""


def build(payments, accounts: dict, book_txns=(), guard=None, fy_label: str = "",
          report_total: float | None = None) -> JournalResult:
    """Build the journal. ``accounts`` maps the three EntityProfile attribute names
    to account paths. ``book_txns`` is a list of BookTxnLite used only to find
    payments the book already holds."""
    why = check_accounts(accounts, guard)
    if why:
        return _stop(why)
    cash = accounts["foreign_dividend_cash_account"].strip()
    taxa = accounts["foreign_dividend_tax_account"].strip()
    inc = accounts["foreign_dividend_income_account"].strip()

    res = JournalResult()
    consumed: set = set()
    pfx = re.sub(r"[^0-9]", "", fy_label or "")[:4] or "FD"
    seq = 0
    payments = list(payments or [])
    if not payments:
        res.notes.append("The report lists no foreign dividend payments; nothing to journal.")
        return res
    sum_gross = 0
    for p in payments:
        label = f"{p.company or '?'} on {p.date_received or '?'}"
        when = parse_date(p.date_received)
        if when is None:
            res.problems.append(f"{label}: the date is missing or unreadable.")
            continue
        if p.amount_inr is None or p.tax_inr is None:
            res.problems.append(f"{label}: the INR amount or the tax withheld is missing "
                                "(a blank is never read as 0).")
            continue
        gross, tax = _cents(p.amount_inr), _cents(p.tax_inr)
        if gross <= 0 or tax < 0 or tax > gross:
            res.problems.append(f"{label}: gross {gross / 100:.2f} / tax {tax / 100:.2f} is not a valid dividend.")
            continue
        net = gross - tax
        sum_gross += gross
        hit = None
        for i, bt in enumerate(book_txns):
            if i in consumed or bt.when != when:
                continue
            if gross in bt.amounts or net in bt.amounts:
                hit = i
                break
        if hit is not None:
            consumed.add(hit)
            res.already_booked.append((label, gross, book_txns[hit].description))
            continue
        seq += 1
        txn_id = f"{pfx}-FDIV{seq:02d}"
        desc = f"Foreign dividend - {p.company or 'unknown company'}" + (f" ({p.broker})" if p.broker else "")
        splits = [(cash, net)]
        if tax:
            splits.append((taxa, tax))
        splits.append((inc, -gross))
        assert sum(c for _a, c in splits) == 0
        res.entries.append(Entry(txn_id, when, desc, gross, tax, net, splits))
    if report_total is not None and not res.problems:
        if abs(sum_gross / 100 - float(report_total)) > 1.0:
            res.notes.append(
                f"The payments add up to Rs {sum_gross / 100:,.2f} but the report's dividend total is "
                f"Rs {float(report_total):,.2f}; check the report before importing.")
    return res


def write_csv(rows: list, out_path) -> None:
    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=HEADERS)
        w.writeheader()
        w.writerows(rows)


def journal_file_name(output_path: str, entity_key: str | None) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", entity_key or "")
    infix = f"-{safe}" if safe else ""
    return f"{output_path}{infix}-foreign-dividends-journal.csv"


def run_for_workbook(entity, foreign_report, book, book_file, output_path: str) -> list:
    """The workbook run's hook: summary lines, and the CSV written when there is
    something to import. Never raises; a problem becomes a visible line."""
    div = getattr(foreign_report, "dividend", None)
    payments = getattr(div, "payments", None) if div is not None else None
    if not payments:
        return []
    accounts = {key: getattr(entity, key, "") for key, _l in ACCOUNT_FIELDS}
    guard = None
    try:
        if book_file and all((accounts[k] or "").strip() for k, _l in ACCOUNT_FIELDS):
            import sys
            src = str(Path(__file__).resolve().parents[3])
            if src not in sys.path:
                sys.path.insert(0, src)
            from agents.gnucash_accounts import TargetGuard  # noqa: PLC0415
            guard = TargetGuard.from_book(book_file)
        txns = book_txns_from_book(book) if book is not None else []
        res = build(payments, accounts, txns, guard, getattr(div, "financial_year", "") or "",
                    getattr(div, "total", None))
    except Exception as e:  # noqa: BLE001
        return [f"Foreign dividend journal: NOT written -- {e}"]
    if not res.stopped and res.entries:
        out = journal_file_name(output_path, getattr(entity, "key", None))
        try:
            write_csv(res.rows, out)
            res.notes.append(f"Written to {Path(out).name}.")
        except OSError as e:
            return [f"Foreign dividend journal: NOT written -- {e}"]
    return res.summary_lines()
