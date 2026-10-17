"""CC-22: reader for the ICICI credit card statement CSV export.

Shared by CC Sort (routing + completeness) and Credit Card Transactions
(rows). Standard library only.

File layout (shapes, not values):
  - a short preamble, then ONE header line of exactly seven columns:
    Date, Sr.No., Transaction Details, Reward Point Header, Intl.Amount,
    Amount(in Rs), BillingAmountSign
  - per card, a single-cell line with the masked card number in four
    space-separated groups, then that card's dated rows
  - a footer (a heading line, a list line, numbered notes) that is never rows

Amount(in Rs) decides the direction: a leading minus is a credit, no minus is
a debit. BillingAmountSign must be identical to it on every row; if it is not,
the file is refused. The file carries no statement totals, so there is no
balance tie-out: coverage and debit/credit counts are reported instead.

A file whose header does not match is refused whole (CsvRefused), never
half-read.
"""
from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

HEADER = ["Date", "Sr.No.", "Transaction Details", "Reward Point Header",
          "Intl.Amount", "Amount(in Rs)", "BillingAmountSign"]

MASK_RX = re.compile(r"^(\d{4})\s+([Xx*]{4})\s+([Xx*]{4})\s+(\d{3,4})$")
AMOUNT_RX = re.compile(r"^-?\d{1,3}(?:,\d{3})*\.\d{2}$|^-?\d+\.\d{2}$")
INT_RX = re.compile(r"^-?\d+$")

# Sapphiro is two cards (one Amex, one Mastercard) on one account and one bill.
# These digit groups mark a Sapphiro card when a file carries only one of them.
SAPPHIRO_DIGITS = ("4315", "3747")


class CsvRefused(ValueError):
    """The file is not an ICICI statement CSV this reader can read exactly."""


@dataclass
class CsvRow:
    card_mask: str
    network: str
    when: date
    sr_no: int
    description: str
    reward: int
    intl: str
    amount: float          # always positive
    direction: str         # Dr | Cr


@dataclass
class CsvStatement:
    rows: list[CsvRow] = field(default_factory=list)
    masks: list[str] = field(default_factory=list)       # distinct, in file order

    @property
    def first(self) -> date | None:
        return min((r.when for r in self.rows), default=None)

    @property
    def last(self) -> date | None:
        return max((r.when for r in self.rows), default=None)

    @property
    def debits(self) -> int:
        return sum(1 for r in self.rows if r.direction == "Dr")

    @property
    def credits(self) -> int:
        return sum(1 for r in self.rows if r.direction == "Cr")


def network_of(mask: str) -> str:
    """Card network from the masked number: Amex prints a 3-digit last group."""
    m = MASK_RX.match(mask.strip())
    if not m:
        return "Unknown"
    first, last = m.group(1), m.group(4)
    if len(last) == 3 or first.startswith(("34", "37")):
        return "Amex"
    if first[0] in "25":
        return "Mastercard"
    if first[0] == "4":
        return "Visa"
    if first[0] == "6":
        return "RuPay"
    return "Unknown"


def _text(path) -> str:
    raw = Path(path).read_bytes()
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise CsvRefused("the file cannot be decoded as text")


def _norm(cells) -> list[str]:
    return [c.strip() for c in cells]


def _is_blank(cells) -> bool:
    return not any(c.strip() for c in cells)


def _header_like(cells) -> bool:
    joined = " ".join(c.lower() for c in cells)
    return "transaction details" in joined or "billingamountsign" in joined \
        or "reward point header" in joined


def looks_like_icici_csv(path) -> bool:
    """Cheap routing test: does any line mention the ICICI export's column names?
    Strict validation happens in parse_icici_csv."""
    try:
        text = _text(path)
    except Exception:
        return False
    return any(_header_like(c) for c in csv.reader(io.StringIO(text)))


def _parse_amount(text: str, where: str) -> float:
    if not AMOUNT_RX.match(text):
        raise CsvRefused(f"{where}: amount '{text}' is not a number with two decimals")
    return float(text.replace(",", ""))


def parse_icici_csv(path) -> CsvStatement:
    """Read the whole file or raise CsvRefused. Never returns a partial read."""
    text = _text(path)
    cells_by_line = list(csv.reader(io.StringIO(text)))
    header_at = None
    near_miss = None
    early_dated = None
    want = [h.lower() for h in HEADER]
    for i, cells in enumerate(cells_by_line):
        norm = _norm(cells)
        if [c.lower() for c in norm] == want:
            header_at = i
            break
        if early_dated is None and len(norm) == len(HEADER) and _try_date(norm[0]) is not None:
            early_dated = i + 1
        if near_miss is None and _header_like(cells):
            near_miss = i + 1             # preamble lines such as "Transaction Details:" are skipped
    if header_at is None:
        if near_miss is not None:
            raise CsvRefused(
                f"line {near_miss}: the header does not match the ICICI statement export "
                f"(expected {len(HEADER)} columns: {', '.join(HEADER)}); the file was not read")
        raise CsvRefused("no ICICI statement header found; the file was not read")
    if early_dated is not None:
        raise CsvRefused(f"line {early_dated}: a dated row before any statement header; "
                         "the file was not read")

    out = CsvStatement()
    card: str | None = None
    in_footer = False
    for i in range(header_at + 1, len(cells_by_line)):
        raw = cells_by_line[i]
        if _is_blank(raw):
            continue
        cells = _norm(raw)
        n = len(cells)
        where = f"line {i + 1}"
        dated = n == len(HEADER) and _try_date(cells[0]) is not None
        if in_footer:
            if dated:
                raise CsvRefused(f"{where}: a dated row after the footer notes; the file was not read")
            continue
        non_empty = [c for c in cells if c]
        if len(non_empty) == 1 and n == 1:
            if MASK_RX.match(cells[0]):
                card = cells[0]
                if card not in out.masks:
                    out.masks.append(card)
                continue
            in_footer = True          # a heading line: the footer starts here
            continue
        if n != len(HEADER):
            raise CsvRefused(f"{where}: {n} columns where a statement row has {len(HEADER)}; "
                             "the file was not read")
        when = _try_date(cells[0])
        if when is None:
            raise CsvRefused(f"{where}: '{cells[0]}' is not a dd-Mon-yy date; the file was not read")
        if card is None:
            raise CsvRefused(f"{where}: a dated row before any masked card number; the file was not read")
        if not INT_RX.match(cells[1]):
            raise CsvRefused(f"{where}: Sr.No. '{cells[1]}' is not a whole number")
        if not INT_RX.match(cells[3]):
            raise CsvRefused(f"{where}: reward points '{cells[3]}' is not a whole number")
        amt_text, sign_text = cells[5], cells[6]
        value = _parse_amount(amt_text, where)
        if sign_text != amt_text:
            raise CsvRefused(
                f"{where}: BillingAmountSign '{sign_text}' differs from Amount(in Rs) '{amt_text}'; "
                "the direction cannot be trusted, so the file was not read")
        out.rows.append(CsvRow(
            card_mask=card, network=network_of(card), when=when, sr_no=int(cells[1]),
            description=" ".join(cells[2].split()), reward=int(cells[3]), intl=cells[4],
            amount=abs(value), direction="Cr" if amt_text.startswith("-") else "Dr"))
    return out


def _try_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%d-%b-%y").date()
    except ValueError:
        return None


def identify_card(masks: list[str]) -> tuple[str | None, str]:
    """(card_type, reason). Two cards on two networks = Sapphiro; one card =
    Amazon unless its digits are Sapphiro's. Never from row count or filename.
    card_type is None when the file cannot be placed (reason says why)."""
    masks = list(dict.fromkeys(masks))
    if not masks:
        return None, "no masked card number in the file"
    if len(masks) == 2:
        nets = {network_of(m) for m in masks}
        if len(nets) == 2 and "Unknown" not in nets:
            return "Sapphiro", ""
        return None, "two card numbers but not on two different networks"
    if len(masks) == 1:
        m = MASK_RX.match(masks[0])
        if m and (m.group(1) in SAPPHIRO_DIGITS or m.group(4) in SAPPHIRO_DIGITS):
            return "Sapphiro", ""
        return "Amazon", ""
    return None, f"{len(masks)} card numbers in one file"


def card_folder(path) -> tuple[str, str]:
    """(bank, card_type) for CC Sort. ('Unknown', 'Unknown') when the file cannot
    be read or placed; Credit Card Transactions then reports it."""
    try:
        card_type, _why = identify_card(parse_icici_csv(path).masks)
    except CsvRefused:
        return "Unknown", "Unknown"
    if card_type is None:
        return "Unknown", "Unknown"
    return "ICICI", card_type
