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


def identify_card(masks: list[str], cards) -> tuple[str | None, str]:
    """(card_type, reason). Each masked number's last group is looked up in the card
    list (cc_cards.yaml): a 4-digit group by exact match, an Amex 3-digit group only
    when exactly one listed last4 ends with it. Every number must be listed, and all
    must belong to one ICICI card (Sapphiro lists two numbers). Never from row count,
    filename or a default: card_type is None when the file cannot be placed, and the
    reason says why (it never carries card digits)."""
    from agents import cc_cards
    masks = list(dict.fromkeys(masks))
    if not masks:
        return None, "no masked card number in the file"
    if cards is None:
        return None, "the card list (cc_cards.yaml) is not available"
    found = []
    for mk in masks:
        m = MASK_RX.match(mk.strip())
        if not m:
            return None, "a card number in the file is not in the expected masked form"
        last = m.group(4)
        card = (cc_cards.card_for_last4(cards, last) if len(last) == 4
                else cc_cards.card_for_tail(cards, last))
        if card is None:
            return None, "a card number in the file is not on the card list"
        if card not in found:
            found.append(card)
    if len(found) > 1:
        return None, f"{len(masks)} card numbers in one file belong to different cards"
    if found[0].bank != "ICICI":
        return None, "the card in the file is listed under a bank other than ICICI"
    return found[0].card, ""


def card_folder_why(path, cards) -> tuple[str, str, str]:
    """(bank, card_type, reason). ('Unknown', 'Unknown', why) when the file cannot be
    read or placed; Credit Card Transactions then reports it."""
    try:
        card_type, why = identify_card(parse_icici_csv(path).masks, cards)
    except CsvRefused as exc:
        return "Unknown", "Unknown", str(exc)
    if card_type is None:
        return "Unknown", "Unknown", why
    return "ICICI", card_type, ""


def card_folder(path, cards) -> tuple[str, str]:
    """(bank, card_type) for CC Sort; see card_folder_why for the reason."""
    return card_folder_why(path, cards)[:2]
