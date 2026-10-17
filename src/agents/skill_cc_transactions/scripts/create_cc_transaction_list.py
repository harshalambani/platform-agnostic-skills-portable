#!/usr/bin/env python3
"""
Create CC Transaction List - Implementation Script (CC-02)

Parses bank-specific credit card statement PDFs into one Excel workbook:
complete, dated, classified rows plus a per-statement tie-out.

Text comes from pdfplumber, page by page, split on newlines (row-wise text).
The old pdftotext default output serialised each table column by column and
the re-zip of dates, descriptions and amounts shifted whenever a column had an
extra or missing cell; row-wise text keeps each table row on one line.

One row parser per layout:
  Axis (Flipkart, IndianOil)   dd/mm/yyyy DESC CATEGORY AMOUNT Dr|Cr [CASHBACK Dr|Cr]
  HDFC old (Regalia, Tata Neu to Aug 2025)   dd/mm/yyyy [HH:MM:SS] DESC AMOUNT[Cr]
  HDFC new (Tata Neu from Sep 2025)          dd/mm/yyyy| HH:MM DESC [+ [COINS]] C AMOUNT l
  ICICI (Sapphiro, Amazon)     dd/mm/yyyy SERNO DESC POINTS [FX CUR] AMOUNT [CR]
  HSBC                         ddMON DESC [CITY COUNTRY] AMOUNT [CR]   (year from the period)
  SBM                          d-Mon-yyyy DESC DEBIT -   |   d-Mon-yyyy DESC - CREDIT
  YES                          dd/mm/yyyy DESC AMOUNT Dr|Cr

Every row gets a real date and a Kind (spend | payment | refund | cashback |
fee). Every statement is tied out: previous balance, less credits, plus debits
must equal the printed total. A statement that does not tie is reported, never
forced to pass.
"""
from __future__ import annotations

import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill

# Standalone-script bootstrap: make ``src`` importable (same convention as the
# other direct-mode scripts), so the period patterns used by the completeness
# check are shared rather than copied.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from agents.skill_cc_sort.completeness import _month_back, classify_text, parse_date  # noqa: E402
from agents.skill_cc_sort.icici_csv import CsvRefused, identify_card, parse_icici_csv  # noqa: E402

# ---------------------------------------------------------------------------
# Numbers and small helpers
# ---------------------------------------------------------------------------

AMT = r"\d[\d,]*\.\d{2}"                 # an amount printed with paise
NUM = r"\d[\d,]*(?:\.\d+)?"              # an amount that may have dropped its paise
MON3 = "JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC"
MONTHS = {m: i for i, m in enumerate(MON3.split("|"), 1)}
DATE_START = re.compile(rf"^\d\d/\d\d/\d{{4}}\b|^\d{{1,2}}-[A-Za-z]{{3,9}}-\d{{4}}\b|^\d\d(?:{MON3})\b")


# Tie-out tolerances (K). A statement that prints amounts with paise must tie to
# the paisa; HDFC and a few others print some totals without paise, so those
# are allowed a rupee. Named here so nothing is tuned inline.
TOL_STRICT = 0.01
TOL_LOOSE = 1.0
# A "chained" tie-out (previous statement's total + this statement's rows =
# this total) uses the strict tolerance, and the previous statement must end at
# most this many days before this one starts.
CHAIN_MAX_GAP_DAYS = 3
RESULT_PASS = "PASS"
RESULT_CHAINED = "PASS (chained)"
RESULT_NO_PRIOR = "NOT AVAILABLE (no prior statement)"


def is_pass(result: str) -> bool:
    """PASS and PASS (chained) both count as a tie-out that holds."""
    return result in (RESULT_PASS, RESULT_CHAINED)


def num(text: str) -> float:
    return float(text.replace(",", ""))


def signed(value: float, flag: str | None) -> float:
    """A balance printed with Cr is a credit balance: negative."""
    return -value if flag and flag.upper().startswith("CR") else value


def norm_dir(flag: str | None) -> str:
    return "Cr" if flag and flag.upper().startswith("CR") else "Dr"


def squash(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def tokens(line: str) -> list[tuple[float, str | None]]:
    """Every number on a line with the Cr/Dr flag glued or following it."""
    return [(num(m.group(1)), m.group(2))
            for m in re.finditer(rf"({NUM})\s?(Cr|Dr|CR|DR)?(?![\w])", line)]


# ---------------------------------------------------------------------------
# Kind classification
# ---------------------------------------------------------------------------

PAYMENT_RX = re.compile(
    r"BBPS\s+PAYMENT\s+RECEIVED|BBPS\s+PMT|BPPY\s+CC\s+PAYMENT|^REPAYMENT\b|"
    r"PAYMENT\s+RECEIVED|TELE\s*TRANSFER\s+CREDIT", re.I)
CREDIT_BALANCE_REFUND_RX = re.compile(r"CREDIT\s+BALANCE\s+REFUND", re.I)
CASHBACK_RX = re.compile(r"CASHBACK\s+(?:CREDIT|DEBIT)", re.I)
FEE_RX = re.compile(
    r"ANNUAL\s+FEE|JOINING\s+FEE|RENEWAL\s+FEE|MEMBERSHIP\s+FEE|CARD\s+FEE|"
    r"LATE\s+(?:PAYMENT\s+)?(?:FEE|CHARGE)|LATE\s+PAYMENT|OVER[\s-]?LIMIT|"
    r"CASH\s+(?:ADVANCE|WITHDRAWAL)\s+(?:FEE|CHARGE)|FINANCE\s+CHARGE|INTEREST|"
    r"FOREX|CROSS[\s-]?CURRENCY|CURRENCY\s+(?:CONVERSION|MARK[\s-]?UP)|MARK[\s-]?UP|"
    r"PROCESSING\s+FEE|CONVENIENCE\s+(?:FEE|CHARGE)|SERVICE\s+TAX|FUEL\s+SURCHARGE|"
    r"\b[ICS]?GST\b", re.I)
# A credit that undoes a fee: fee-like wording, or an explicit waiver/reversal.
FEE_REVERSAL_RX = re.compile(FEE_RX.pattern + r"|WAIVER|WAIVED|REVERSAL", re.I)


# CC-18: Axis prints reward redemptions ("SMS BASED REDEMPTION/...") as credit rows but leaves
# them out of the summary's Credits figure.
REDEMPTION_RX = re.compile(r"\bREDEMPTION\b", re.I)


# EMI wording (J). UNVALIDATED against a real statement: no real EMI specimen
# was available when these were written. TODO(CC-04 follow-up): confirm each
# pattern against a real EMI statement before relying on it. Only EXPLICIT
# wording is matched; anything else that mentions EMI becomes
# "emi_unclassified" and is listed loudly, never treated as a spend.
EMI_WORD_RX = re.compile(r"\bEMI\b", re.I)
EMI_PATTERNS = [
    ("gst_on_emi", re.compile(r"\b[ICS]?GST\b.*\bEMI\b|\bEMI\b.*\b[ICS]?GST\b", re.I)),
    ("emi_processing_fee", re.compile(r"\bEMI\b.*PROCESSING\s+FEE|PROCESSING\s+FEE.*\bEMI\b", re.I)),
    ("emi_interest", re.compile(r"\bEMI\s+INTEREST\b|\bINTEREST\s+(?:ON\s+)?EMI\b", re.I)),
    ("emi_principal", re.compile(r"\bEMI\s+PRINCIPAL\b|\bPRINCIPAL\s+(?:OF\s+)?EMI\b", re.I)),
    ("emi_conversion", re.compile(r"CONVERTED\s+TO\s+EMI|\bEMI\s+CONVERSION\b|CONVERT\s+TO\s+EMI", re.I)),
]
EMI_KINDS = tuple(k for k, _ in EMI_PATTERNS) + ("emi_unclassified",)

# CC-06 addendum: EMI can turn up on ANY card, in wording we have not seen. A row
# whose description (or HSBC continuation line) carries instalment / loan / EMI
# wording and is not an explicit shape is "emi_unclassified": listed loudly,
# never booked as a spend or a refund. Whole words only, so a merchant such as
# "BALLOONS AND MORE" is untouched. Patterns:
#   INSTAL+MENTS?          INSTALMENT / INSTALLMENT(S)
#   N OF N counter        1ST OF 3, 2 OF 6 (digits on both sides of OF)
#   LOAN                   whole word only
#   EMI                    whole word only
EMI_LIKE_RX = re.compile(
    r"\bINSTAL+MENTS?\b|\b\d+(?:ST|ND|RD|TH)?\s+OF\s+\d+\b|\bLOAN\b|\bEMI\b", re.I)

# HSBC prints the instalment kind on the line AFTER a row, e.g.
# "1ST OF 3 INSTALLMENTS PRINCIPAL". Explicit HSBC shapes only.
HSBC_INSTALMENT_RX = re.compile(
    r"^(\d+)(?:ST|ND|RD|TH)\s+OF\s+(\d+)\s+INSTAL+MENTS?\s+(PRINCIPAL|INTEREST)\s*$", re.I)
HSBC_INSTALMENT_LINE_RX = re.compile(r"\bOF\s+\d+\s+INSTAL+MENTS?\b", re.I)
HSBC_FEE_RX = re.compile(r"\bINSTALLMENT\s+PROCESSING\s+FEE", re.I)   # no trailing \b: the card reference is glued on


def explicit_emi_kind(description: str) -> str | None:
    d = squash(description)
    for kind, rx in EMI_PATTERNS:
        if rx.search(d):
            return kind
    return None


def classify(description: str, direction: str) -> str:
    """Kind for one row. Order matters: a payment, a cashback row or a fee is
    never a spend or refund. An EMI row is never a spend: explicit wording gets
    its own kind, any other mention of EMI is emi_unclassified."""
    d = squash(description)
    emi = explicit_emi_kind(d)
    if emi:
        return emi
    if EMI_LIKE_RX.search(d):
        return "emi_unclassified"
    if CASHBACK_RX.search(d):
        return "cashback"
    if CREDIT_BALANCE_REFUND_RX.search(d):
        return "payment" if direction == "Dr" else "refund"
    if PAYMENT_RX.search(d):
        return "payment"
    if direction == "Dr" and FEE_RX.search(d):
        return "fee"
    return "refund" if direction == "Cr" else "spend"


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------

@dataclass
class Row:
    bank: str
    card_type: str
    date: datetime | None
    description: str
    amount: float
    direction: str            # Dr | Cr
    kind: str = "spend"
    statement: str = ""
    period: str = ""
    source: str = ""
    instalment: str = ""      # HSBC: the continuation line ("1ST OF 3 INSTALLMENTS PRINCIPAL")
    explained: bool = False   # an HSBC offset leg (instalment credit, loan conversion credit): never booked, understood
    network: str = ""         # CC-22: ICICI CSV only (Amex / Mastercard on one Sapphiro account); blank for PDFs

    @property
    def card(self) -> str:
        return f"{self.bank}-{self.card_type}"

    @property
    def signed(self) -> float:
        """+ for money owed (debit), - for money received (credit)."""
        return self.amount if self.direction == "Dr" else -self.amount


@dataclass
class Loan:
    """One row of an HSBC "Loan Summary Table"."""
    merchant: str
    booked: date
    principal: float
    interest: float
    instalment_amount: float
    done: int
    total: int


@dataclass
class Parsed:
    rows: list[Row] = field(default_factory=list)
    summary: dict = field(default_factory=dict)        # prev, ..., total (balances signed)
    printed_closing: float | None = None               # closing recomputed from the printed parts
    tol_loose: bool = False                            # total printed without paise
    missing: list[str] = field(default_factory=list)   # summary figures not found
    errors: list[str] = field(default_factory=list)    # unparsed lines, bad dates
    rows_net_expected: float | None = None             # printed (debits + fees - credits): rows must equal it exactly
    note: str = ""                                     # layout note carried into the tie-out note
    loans: list = field(default_factory=list)          # HSBC Loan Summary Table rows (Loan)
    outstanding_parts: tuple | None = None             # HSBC (TOTAL PURCHASE OUTSTANDING, TOTAL LOAN OUTSTANDING)
    proof_failed: str = ""                             # a layout read by arithmetic proof: why the proof did not hold


@dataclass
class Statement:
    bank: str
    card_type: str
    source: str
    period: tuple[date, date] | None
    period_note: str
    parsed: Parsed
    lines: list = field(default_factory=list)          # the page text, for the gap report

    @property
    def card(self) -> str:
        return f"{self.bank}-{self.card_type}"

    @property
    def label(self) -> str:
        if self.period is None:
            return "period not found"
        s, e = self.period
        return f"{s:%d %b %Y} to {e:%d %b %Y}"

    @property
    def key(self):
        if self.period is None:
            return (self.card, "file", self.source)
        return (self.card, self.period[0], self.period[1])


def _mk_row(bank, card, when, desc, amount, direction) -> Row:
    desc = squash(desc)
    return Row(bank, card, when, desc, amount, direction, classify(desc, direction))


def _dmy(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%d/%m/%Y")
    except ValueError:
        return None


def _unparsed(line: str) -> bool:
    """A line that starts like a transaction and carries an amount but that no
    row pattern took: it must be reported, not silently dropped."""
    return bool(DATE_START.match(line)) and bool(re.search(AMT, line))


# ---------------------------------------------------------------------------
# Layout parsers: lines -> Parsed
# ---------------------------------------------------------------------------

def parse_axis(lines, bank, card, period) -> Parsed:
    p = Parsed()
    # Some old-layout amounts carry a "*" glyph before them.
    row_rx = re.compile(
        rf"^(\d\d/\d\d/\d{{4}})\s+(.*?)\s+\*?\s*({AMT})\s+(Dr|Cr)(?:\s+\*?\s*({AMT})\s+(Dr|Cr))?\s*$", re.I)
    # The label line wraps in the old layout and the values sit two lines below,
    # so the values line is recognised by its own shape. The previous balance
    # and the total each carry Dr/Cr or no suffix at all (no suffix = debit). A
    # Cr previous balance is a NEGATIVE opening.
    sum_rx = re.compile(
        rf"^({AMT})\s*(Dr|Cr)?\s+({AMT})\s+({AMT})\s+({AMT})\s+({AMT})\s+({AMT})\s+({AMT})\s*(Dr|Cr)?(?:\s+[A-Za-z].*)?\s*$", re.I)
    # CC-04: the values line can be followed by unrelated text from the page's right-hand
    # column ("... 3,220.50 Dr Reward points ..."); the text must start with a letter, so a
    # ninth number can never be swallowed.
    for line in lines:
        line = line.strip()
        if re.match(r"^\d\d/\d\d/\d{4}\s+-\s", line):
            continue                      # the period header line
        m = row_rx.match(line)
        if m:
            # The FIRST amount+flag is the transaction. A second pair is
            # cashback earned: informational, never the amount, never a row.
            when = _dmy(m.group(1))
            if when is None:
                p.errors.append(f"unreadable date: {line[:70]}")
                continue
            p.rows.append(_mk_row(bank, card, when, m.group(2), num(m.group(3)), norm_dir(m.group(4))))
        elif DATE_START.match(line):
            if _unparsed(line):
                p.errors.append(f"unparsed line: {line[:70]}")
        elif not p.summary:
            s = sum_rx.search(line)
            if s:
                prev = signed(num(s.group(1)), s.group(2))
                pay, cred, purch, cash, other = (num(s.group(i)) for i in (3, 4, 5, 6, 7))
                total = signed(num(s.group(8)), s.group(9))
                p.summary = dict(prev=prev, payments=pay, credits=cred, purchases=purch,
                                 cash=cash, other=other, total=total)
                p.printed_closing = prev - pay - cred + purch + cash + other
    if not p.summary:
        p.missing.append("summary line")
    return p


def parse_hdfc_old(lines, bank, card, period) -> Parsed:
    p = Parsed(tol_loose=True)
    row_rx = re.compile(rf"^(\d\d/\d\d/\d{{4}})(?:\s+\d\d:\d\d:\d\d)?\s+(.*?)\s+({AMT})\s*(Cr)?\s*$", re.I)
    # Account Summary: the labels may be split over three lines ("Opening
    # Payment/ Purchase/ Finance" / "Total x" / "Balance Credits Debits
    # Charges"), then ONE values line: opening (can be negative), payments,
    # purchases, finance charges, total dues.
    vals_rx = re.compile(rf"^(-?{AMT})(?:\s+(-?{AMT})){{4}}\s*(?:Cr|Dr)?\s*$", re.I)
    for i, line in enumerate(lines):
        line = line.strip()
        m = row_rx.match(line)
        if m:
            when = _dmy(m.group(1))
            if when is None:
                p.errors.append(f"unreadable date: {line[:70]}")
                continue
            p.rows.append(_mk_row(bank, card, when, m.group(2), num(m.group(3)), norm_dir(m.group(4))))
        elif DATE_START.match(line):
            if _unparsed(line):
                p.errors.append(f"unparsed line: {line[:70]}")
        elif not p.summary and vals_rx.match(line) and any(
                "OPENING" in ln.upper() for ln in lines[max(0, i - 4):i]):
            t = [num(x) for x in re.findall(rf"-?{AMT}", line)]
            opening, pay, purch, fin, total = t[:5]
            p.summary = dict(prev=opening, payments=pay, purchases=purch, finance=fin, total=total)
            p.printed_closing = opening - pay + purch + fin
    if not p.summary:
        p.missing.append("summary line")
    return p


def strip_emi_marker(desc: str) -> str:
    """The HDFC new layout prints "EMI " before an ordinary merchant on many
    rows. That is the statement's "convert to EMI" marker column, NOT an EMI
    row, so it is stripped. Explicit EMI wording ("EMI PRINCIPAL", "EMI
    INTEREST", "EMI PROCESSING FEE", "EMI CONVERSION" ...) is left intact so
    classify() sees it."""
    d = squash(desc)
    if explicit_emi_kind(d):
        return d
    return re.sub(r"^EMI\s+", "", d, flags=re.I)


def parse_hdfc_new(lines, bank, card, period) -> Parsed:
    p = Parsed(tol_loose=True)
    rupee = "[C\u20b9]"
    row_rx = re.compile(
        rf"^(\d\d/\d\d/\d{{4}})\|\s*\d\d:\d\d\s+(.*?)(?:\s+\+(?:\s*\d+)?)?\s+{rupee}\s*({AMT})(?:\s*l)?\s*$")
    for i, line in enumerate(lines):
        line = line.strip()
        m = row_rx.match(line)
        if m:
            when = _dmy(m.group(1))
            if when is None:
                p.errors.append(f"unreadable date: {line[:70]}")
                continue
            # "+ C amt" (the plus directly before the rupee glyph) = credit.
            # "+ 219 C amt" is the NeuCoins earned on a DEBIT, so it stays a debit.
            credit = re.search(rf"\+\s*{rupee}\s*\d", line) is not None
            p.rows.append(_mk_row(bank, card, when, strip_emi_marker(m.group(2)), num(m.group(3)),
                                  "Cr" if credit else "Dr"))
        elif DATE_START.match(line) or re.match(r"^\d\d/\d\d/\d{4}\|", line):
            if re.search(AMT, line):
                p.errors.append(f"unparsed line: {line[:70]}")
        elif not p.summary and "PREVIOUS STATEMENT" in line.upper():
            # CC-04: the lone total line can sit BEFORE or AFTER the four-value line.
            comp = total = None
            for j in range(i + 1, min(i + 9, len(lines))):
                nxt = lines[j].strip()
                vals = re.findall(rf"{rupee}\s*({NUM})", nxt)
                if comp is None and len(vals) >= 4:
                    comp = [num(v) for v in vals[:4]]
                else:
                    tm = re.match(rf"^[_\-\u2013\u2014]?\s*{rupee}\s*({NUM})\s*$", nxt)
                    if tm and total is None:
                        total = num(tm.group(1))
                if comp is not None and total is not None:
                    prev, pay, purch, fin = comp
                    p.summary = dict(prev=prev, payments=pay, purchases=purch, finance=fin, total=total)
                    p.printed_closing = prev - pay + purch + fin
                    break
    if not p.summary:
        p.missing.append("summary figures")
    return p


def parse_icici(lines, bank, card, period) -> Parsed:
    p = Parsed()
    # Row: date, 11-digit reference, description, [IN], [reward points, may be
    # negative], [foreign amount + currency], INR amount, [CR]. The reward
    # points sit right before the amount: they are NEVER part of it.
    row_rx = re.compile(
        rf"^(\d\d/\d\d/\d{{4}})\s+\d+\s+(.*?)\s+(?:(-?\d+)\s+)?(?:`?({AMT})\s+[A-Z]{{3}}\s+)?`?({AMT})(?:\s+(CR))?\s*$",
        re.I)
    # CC-04: a sidebar's text can be printed before the date on the same line. Accepted only
    # with the row's own serial (8+ digits) straight after the date, so a line that merely
    # mentions a date and an amount is never a row.
    side_rx = re.compile(
        rf"^\S.*?\s(\d\d/\d\d/\d{{4}})\s+\d{{8,}}\s+(.*?)\s+(?:(-?\d+)\s+)?(?:`?({AMT})\s+[A-Z]{{3}}\s+)?`?({AMT})(?:\s+(CR))?\s*$",
        re.I)
    prev = purch = cash = pay = total = None
    for i, line in enumerate(lines):
        line = line.strip()
        m = row_rx.match(line) or (None if DATE_START.match(line) else side_rx.match(line))
        if m:
            when = _dmy(m.group(1))
            if when is None:
                p.errors.append(f"unreadable date: {line[:70]}")
                continue
            # The LAST amount is the INR amount; a foreign amount precedes it.
            p.rows.append(_mk_row(bank, card, when, m.group(2), num(m.group(5)), norm_dir(m.group(6))))
        elif DATE_START.match(line):
            if _unparsed(line):
                p.errors.append(f"unparsed line: {line[:70]}")
        else:
            # Components: only from a label line that names the components
            # themselves. The 4-value line under "Credit Limit (x cash)
            # Available Credit ..." is credit LIMITS, never the summary.
            up = line.upper()
            if (prev is None and "PREVIOUS BALANCE" in up and "PURCHASE" in up
                    and "CREDIT LIMIT" not in up and "AVAILABLE CREDIT" not in up):
                for nxt in lines[i + 1:i + 4]:
                    vals = re.findall(rf"`\s*({NUM})", nxt)
                    if len(vals) >= 4:
                        prev, purch, cash, pay = (num(v) for v in vals[:4])
                        break
            if total is None:
                tm = re.search(rf"Total Amount due\D{{0,12}}({NUM})", line, re.I)
                if tm:
                    total = num(tm.group(1))
                elif re.match(r"^Total Amount due\s*:?\s*$", line, re.I):
                    # The value is on its own line below, followed by "= + + -".
                    for nxt in lines[i + 1:i + 3]:
                        vm = re.match(rf"^\s*`?\s*({NUM})\b", nxt)
                        if vm:
                            total = num(vm.group(1))
                            break
    if total is None:
        p.missing.append("total not found")
    elif prev is not None:
        closing = prev + purch + cash - pay
        if abs(closing - total) < TOL_STRICT + 1e-9:
            p.summary = dict(prev=prev, purchases=purch, cash=cash, payments=pay, total=total)
            p.printed_closing = closing
        else:
            # The components found do not reproduce the total (interest, fees
            # or a reordered block): do not trust them. Fall back to the total
            # alone, which the chained tie-out can use.
            p.summary = dict(total=total)
            p.missing.append("summary components do not add to the total")
    else:
        p.summary = dict(total=total)
        p.missing.append("summary components")
    return p


def _hsbc_year(month: int, day: int, period: tuple[date, date] | None) -> int | None:
    if period is None:
        return None
    end = period[1]
    # A Dec row on a statement ending in Jan belongs to the earlier year.
    return end.year - 1 if (month, day) > (end.month, end.day) else end.year


_LEGAL_WORDS = frozenset({"PVT", "PRIV", "PRIVATE", "LTD", "LIMITED", "LLP", "INDIA", "CO", "INC"})
LOAN_ROW_RX = re.compile(
    rf"^(.+?)\s+(\d{{1,2}})\s+({MON3})\s+(\d{{4}})\s+({AMT})\s+({AMT})\s+({AMT})\s+(\d+)\s*/\s*(\d+)\s*$", re.I)


def _merchant_tokens(text: str) -> list:
    """Significant upper-case words of a merchant name: the card reference
    (CC + digits), punctuation and legal-form words are dropped."""
    words = re.sub(r"[^A-Z0-9 ]+", " ", (text or "").upper()).split()
    return [w for w in words if not re.fullmatch(r"CC\d+", w) and w not in _LEGAL_WORDS]


def merchants_match(row_desc: str, loan_merchant: str) -> bool:
    """The statement prints the merchant short on the purchase and in full on the
    conversion credit. They match when the shorter list of significant words is a
    prefix of the longer one (never on the amount alone)."""
    a, b = _merchant_tokens(row_desc), _merchant_tokens(loan_merchant)
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return a[:n] == b[:n]


def hsbc_instalment_kind(instalment: str) -> str | None:
    m = HSBC_INSTALMENT_RX.match(squash(instalment))
    if not m:
        return None
    return "emi_principal" if m.group(3).upper() == "PRINCIPAL" else "emi_interest"


def read_loan_summary(lines) -> list:
    """The "Loan Summary Table" block: merchant, booking date, principal, total
    interest, instalment amount and tenure done/total."""
    loans, in_table = [], False
    for raw in lines:
        line = raw.strip()
        if re.match(r"^Loan Summary Table\b", line, re.I):
            in_table = True
            continue
        if not in_table:
            continue
        m = LOAN_ROW_RX.match(line)
        if m:
            try:
                booked = date(int(m.group(4)), MONTHS[m.group(3).upper()], int(m.group(2)))
            except ValueError:
                continue
            loans.append(Loan(squash(m.group(1)), booked, num(m.group(5)), num(m.group(6)),
                              num(m.group(7)), int(m.group(8)), int(m.group(9))))
    return loans


def loans_booked_in_period(parsed: "Parsed", period) -> list:
    if period is None:
        return []
    return [ln for ln in parsed.loans if period[0] <= ln.booked <= period[1]]


def parse_hsbc(lines, bank, card, period) -> Parsed:
    p = Parsed()
    row_rx = re.compile(rf"^(\d\d)({MON3})\s+(.*?)\s+({AMT})(?:\s+(CR))?\s*$")
    net_rx = re.compile(rf"^\d\d(?:{MON3})\s+NET OUTSTANDING BALANCE\s+({AMT})(?:\s*(CR))?", re.I)
    open_rx = re.compile(rf"OPENING BALANCE\s+({AMT})(?:\s*(CR))?", re.I)
    tpo_rx = re.compile(rf"^TOTAL PURCHASE OUTSTANDING\s+({AMT})(?:\s*(CR))?", re.I)
    tlo_rx = re.compile(rf"^TOTAL LOAN OUTSTANDING\s+({AMT})(?:\s*(CR))?", re.I)
    opening = total = tpo = tlo = None
    prev_was_row = False
    for line in lines:
        line = line.strip()
        was_row, prev_was_row = prev_was_row, False
        n = net_rx.match(line)
        if n:
            total = signed(num(n.group(1)), n.group(2))
            continue
        o = open_rx.search(line)
        if o and not DATE_START.match(line):
            opening = signed(num(o.group(1)), o.group(2))
            continue
        tp = tpo_rx.match(line)
        if tp:
            tpo = signed(num(tp.group(1)), tp.group(2))
            continue
        tl = tlo_rx.match(line)
        if tl:
            tlo = signed(num(tl.group(1)), tl.group(2))
            continue
        if was_row and HSBC_INSTALMENT_LINE_RX.search(line):
            # the instalment text belongs to the row just above it
            r = p.rows[-1]
            r.instalment = squash(line)
            kind = hsbc_instalment_kind(line)
            r.kind = kind or "emi_unclassified"
            r.explained = bool(kind) and r.direction == "Cr"
            continue
        m = row_rx.match(line)
        if m:
            day, mon = int(m.group(1)), MONTHS[m.group(2)]
            year = _hsbc_year(mon, day, period)
            when = None
            if year is not None:
                try:
                    when = datetime(year, mon, day)
                except ValueError:
                    when = None
            if when is None:
                p.errors.append(f"unreadable date (year not found): {line[:70]}")
                continue
            row = _mk_row(bank, card, when, m.group(3), num(m.group(4)), norm_dir(m.group(5)))
            if row.direction == "Dr" and HSBC_FEE_RX.search(row.description):
                row.kind = "emi_processing_fee"
            p.rows.append(row)
            prev_was_row = True
        elif _unparsed(line):
            p.errors.append(f"unparsed line: {line[:70]}")
    if opening is None or total is None:
        p.missing.append("opening/closing balance")
    else:
        p.summary = dict(prev=opening, total=total)
    p.loans = read_loan_summary(lines)
    if tpo is not None and tlo is not None:
        p.outstanding_parts = (tpo, tlo)
    # A credit is a conversion ONLY when it is a Loan Summary Table loan booked in
    # this period: same principal AND the same merchant. Anything else stays as it was.
    used: set = set()
    for ln in loans_booked_in_period(p, period):
        for k, r in enumerate(p.rows):
            if (k not in used and r.direction == "Cr" and r.kind == "refund" and not r.instalment
                    and abs(r.amount - ln.principal) <= 0.005 and merchants_match(r.description, ln.merchant)):
                r.kind = "emi_conversion"
                r.explained = True
                used.add(k)
                break
    return p


SBM_DUE_RX = re.compile(r"^(?:TOTAL|MINIMUM)\s+AMOUNT\s+DUE\b|^PAYMENT\s+DUE\b|^DUE\s+DATE\b", re.I)

RUPEE = "\u20b9"
_SBM_AMT = r"[\d,]+(?:\.\d+)?"
SBM_DUE_VALUES_RX = re.compile(
    rf"^\d{{1,2}}-[A-Za-z]{{3,9}}-\d{{4}}\s+(?:{RUPEE}\s*)?{_SBM_AMT}\s+(?:{RUPEE}\s*)?{_SBM_AMT}\s*$")


def sbm_due_value_indexes(lines) -> set:
    """CC-19: indexes of the payment-due VALUES line ("16-May-2025 120.00 20.00",
    or the same with a rupee sign before each amount in the older layout). It is
    a date plus two amounts and is never a transaction. It is recognised by what
    it follows: the "Payment Due Date / Total Amount Due / Minimum Amount Due"
    header line (or, failing that, two rupee-marked amounts straight after the
    date). A date-led line with a description is never taken for it."""
    out = set()
    prev = ""
    for i, raw in enumerate(lines):
        line = raw.strip()
        if not line:
            continue
        if SBM_DUE_VALUES_RX.match(line):
            two_rupees = line.count(RUPEE) == 2 and re.match(rf"^\S+\s+{RUPEE}", line)
            if SBM_DUE_RX.match(prev) or two_rupees:
                out.add(i)
        prev = line
    return out


# CC-16: the older SBM layout. The statement date is month-year only
# ("Statement Date : April-2025"), the five summary figures sit on one line under
# one label line, and each row carries a single rupee amount with no "-" marker:
# the direction is read from the wording.
SBM_OLD_DATE_RX = re.compile(r"^Statement\s+Date\s*:\s*([A-Za-z]{3,9})\s*-\s*(\d{4})\s*$", re.I)
SBM_OLD_ROW_RX = re.compile(
    rf"^(\d{{1,2}}-[A-Za-z]{{3,9}}-\d{{4}})\s+([^{RUPEE}]*?\S)\s+{RUPEE}\s*({_SBM_AMT})\s*$")
SBM_OLD_CREDIT_RX = re.compile(
    r"\b(?:REPAYMENT|PAYMENT|REFUND(?:ED)?|REVERSAL|REVERSED|CREDIT|CASHBACK|CASH\s+BACK|WAIVER|WAIVED)\b", re.I)


def is_sbm_older_layout(lines) -> bool:
    has_date = any(SBM_OLD_DATE_RX.match(ln.strip()) for ln in lines)
    return has_date and any(RUPEE in ln for ln in lines)


def sbm_older_row_dates(lines) -> list:
    out = []
    for ln in lines:
        m = SBM_OLD_ROW_RX.match(ln.strip())
        if m:
            d = parse_date(m.group(1), "%d-%b-%Y")
            if d:
                out.append(d)
    return out


def sbm_older_period(lines):
    """(start, end) of an older-layout SBM statement, or None: the earlier of the month start and
    the first row date, to the end of the printed month. CC Sort (CC-21) uses this too."""
    if not is_sbm_older_layout(lines):
        return None
    for ln in lines:
        om = SBM_OLD_DATE_RX.match(ln.strip())
        first = parse_date(f"1-{om.group(1)}-{om.group(2)}", "%d-%b-%Y") if om else None
        if first:
            nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
            return min([first] + sbm_older_row_dates(lines)), nxt - timedelta(days=1)
    return None


def parse_sbm_older(lines, bank, card, period) -> Parsed:
    """The older SBM layout, read by label order and PROVEN by arithmetic:
      - debit rows (everything not worded as a credit) sum to Purchase/Debits +
        Finance Charges (finance charges are rows of the statement);
      - credit rows (Repayment, Payment, Refund, Reversal, Credit, Cashback,
        Waiver) sum to Payment/Credit;
      - Opening + Purchase/Debits + Finance Charges - Payment/Credit = Total Due.
    Wording that is not a credit word is a debit, never a silent credit; a row
    of unknown wording that is really a credit makes the proof fail."""
    p = Parsed()
    due = sbm_due_value_indexes(lines)
    figures = None
    for i, raw in enumerate(lines):
        line = raw.strip()
        if i in due:
            continue
        m = SBM_OLD_ROW_RX.match(line)
        if m:
            d = parse_date(m.group(1), "%d-%b-%Y")
            if d is None:
                p.errors.append(f"unreadable date: {line[:70]}")
                continue
            direction = "Cr" if SBM_OLD_CREDIT_RX.search(m.group(2)) else "Dr"
            p.rows.append(_mk_row(bank, card, datetime(d.year, d.month, d.day), m.group(2),
                                  num(m.group(3)), direction))
        elif DATE_START.match(line) and re.search(AMT, line):
            p.errors.append(f"unparsed line: {line[:70]}")
        elif figures is None and i > 0:
            bare = line.replace(RUPEE, " ")
            nums = re.findall(_SBM_AMT, bare)
            rest = re.sub(_SBM_AMT, "", bare).strip()
            label = " ".join(lines[i - 1].upper().split())
            if len(nums) == 5 and not rest and "OPENING BALANCE" in label and "TOTAL AMOUNT DUE" in label:
                figures = [num(x) for x in nums]
    if figures is None:
        p.missing.append("summary line")
        return p
    opening, purch, fin, pay, total = figures
    p.summary = dict(prev=opening, purchases=purch, finance=fin, payments=pay, total=total)
    p.printed_closing = round(opening + purch + fin - pay, 2)
    p.rows_net_expected = round(purch + fin - pay, 2)
    debits = round(sum(r.amount for r in p.rows if r.direction == "Dr"), 2)
    credits = round(sum(r.amount for r in p.rows if r.direction == "Cr"), 2)
    why = []
    if abs(debits - (purch + fin)) > TOL_STRICT + 1e-9:
        why.append(f"debit rows total {debits:,.2f} but Purchase/Debits + Finance Charges is {purch + fin:,.2f}")
    if abs(credits - pay) > TOL_STRICT + 1e-9:
        why.append(f"credit rows total {credits:,.2f} but Payment/Credit is {pay:,.2f}")
    if why:
        p.proof_failed = "older SBM layout not proven by arithmetic: " + "; ".join(why)
    p.note = "older SBM layout: rows read as debit or credit by wording, proven against the printed figures"
    return p


def parse_sbm(lines, bank, card, period) -> Parsed:
    if is_sbm_older_layout(lines):
        return parse_sbm_older(lines, bank, card, period)
    p = Parsed()
    due_values = sbm_due_value_indexes(lines)
    amt = r"[\d,]+(?:\.\d+)?"
    sbm_values: list = []
    row_rx = re.compile(rf"^(\d{{1,2}}-[A-Za-z]{{3,9}}-\d{{4}})\s+(.*?)\s+({amt}|-)\s+({amt}|-)\s*$")
    for i, line in enumerate(lines):
        line = line.strip()
        if i in due_values:
            continue                      # CC-19: the due-values line under the due header
        dm = re.match(r"^\d{1,2}-[A-Za-z]{3,9}-\d{4}\s+(.*)$", line)
        if dm and SBM_DUE_RX.match(dm.group(1)):
            continue                      # CC-04: the payment-due line is a date + amounts, never a row
        m = row_rx.match(line)
        if m and (m.group(3) == "-") != (m.group(4) == "-"):
            d = parse_date(m.group(1), "%d-%b-%Y")      # tries %b then %B
            if d is None:
                p.errors.append(f"unreadable date: {line[:70]}")
                continue
            if m.group(3) != "-":
                value, direction = num(m.group(3)), "Dr"
            else:
                value, direction = num(m.group(4)), "Cr"
            p.rows.append(_mk_row(bank, card, datetime(d.year, d.month, d.day), m.group(2), value, direction))
        elif re.match(r"^\d{1,2}-[A-Za-z]{3,9}-\d{4}\s", line) and \
                re.search(rf"\s(?:{amt}|-)\s+(?:{amt}|-)\s*$", line):
            p.errors.append(f"unparsed line: {line[:70]}")
        elif (not p.summary and re.match(rf"^({amt})(?:\s+({amt})){{4}}\s*$", line)
              and any("OPENING" in ln.upper() for ln in lines[max(0, i - 4):i])):
            vals = [num(x) for x in re.findall(amt, line)][:5]
            sbm_values.append(vals)
    if sbm_values:
        vals = sbm_values[0]
        opening, total = vals[0], vals[4]
        roles = resolve_sbm_roles(opening, vals[1:4], total, p.rows)
        if roles is None:
            p.missing.append("summary figures: the order of purchases / payments / finance charges "
                             "could not be proven by arithmetic")
        else:
            purch, pay, fin = roles
            closing = opening + purch + fin - pay
            if abs(total) <= TOL_STRICT and closing < -TOL_STRICT:
                # CC-04: SBM prints a credit balance as 0.00. The arithmetic proves the credit
                # (roles are only accepted when it does), so the balance is the proven credit.
                total = round(closing, 2)
                p.note = (f"SBM printed the credit balance as 0.00; the figures prove a credit balance of "
                          f"{-total:,.2f}")
            p.summary = dict(prev=opening, purchases=purch, finance=fin, payments=pay, total=total)
            p.printed_closing = closing
    if not p.summary and not p.missing:
        p.missing.append("summary line")
    return p


def resolve_sbm_roles(opening, middle, total, rows):
    """SBM prints five values: opening, three middle figures, total due. The
    middle three are purchases, payments and finance charges in an order the
    labels do not make certain, so the order is PROVEN, not assumed: try every
    assignment and keep those where opening + purchases - payments + finance
    equals the total. If several assignments tie, the one whose payments figure
    equals the credit rows (then whose purchases equal the debit rows) wins;
    if that still leaves more than one, nothing is chosen. Returns
    (purchases, payments, finance) or None."""
    from itertools import permutations
    seen, good = set(), []
    for perm in permutations(middle):
        if perm in seen:
            continue
        seen.add(perm)
        purch, pay, fin = perm
        got = opening + purch - pay + fin
        # a printed 0.00 may stand for a credit balance (SBM does not print negatives)
        if abs(got - total) <= TOL_STRICT + 1e-9 or (abs(total) <= TOL_STRICT and got < -TOL_STRICT):
            good.append(perm)
    if len(good) == 1:
        return good[0]
    if not good:
        return None
    credits = round(sum(r.amount for r in rows if r.direction == "Cr"), 2)
    debits = round(sum(r.amount for r in rows if r.direction == "Dr"), 2)
    by_credit = [g for g in good if abs(g[1] - credits) <= TOL_STRICT + 1e-9]
    if len(by_credit) == 1:
        return by_credit[0]
    pool = by_credit or good
    by_debit = [g for g in pool if abs(g[0] - debits) <= TOL_STRICT + 1e-9]
    if len(by_debit) == 1:
        return by_debit[0]
    # Purchases and finance charges are interchangeable in the arithmetic
    # (both add): the fee-kind rows decide which figure is the finance charge.
    fee_sum = round(sum(r.amount for r in rows if r.direction == "Dr" and r.kind == "fee"), 2)
    spend_sum = round(sum(r.amount for r in rows if r.direction == "Dr" and r.kind == "spend"), 2)
    by_rows = [g for g in pool if abs(g[2] - fee_sum) <= TOL_STRICT + 1e-9
               and abs(g[0] - spend_sum) <= TOL_STRICT + 1e-9]
    return by_rows[0] if len(by_rows) == 1 else None


MONEY_LABEL_WORDS = r"balance|purchase|payment|credit|amount|due|charge|limit|interest|fee"


# SBI (BPCL Octane). Dates print as "DD Mon YY" (space, 2-digit year) and the
# trailing letter is the sign (C = credit, D = debit). The reader is keyed on
# bank == "SBI" so the date shape cannot change any other bank's parse.
SBI_ROUNDING_NOTE = ("SBI rounds the printed total to the rupee: the total tie-out allows a difference "
                     "strictly below 1.00 and FAILS at 1.00 or more; the rows must equal the printed "
                     "debits + fees - credits exactly")
SBI_DATE = r"\d\d (?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) \d\d"
SBI_ROW_RX = re.compile(rf"^({SBI_DATE})\s+(.*\S)\s+({AMT})\s+([CD])\s*$")
SBI_GST_RX = re.compile(rf"^(IGST\b.*?)\s+({AMT})\s+([CD])\s*$")
SBI_DATE_START = re.compile(rf"^{SBI_DATE}\b")
SBI_LEGEND = "Purchases & Other Debits;"
SBI_SUMMARY_RX = re.compile(rf"^{AMT}(?:\s+CR)?(?:\s+{AMT}(?:\s+CR)?){{4}}$")


def sbi_date(text: str) -> datetime | None:
    try:
        return datetime.strptime(text, "%d %b %y")
    except ValueError:
        return None


def is_sbi_row(line: str) -> bool:
    """A dated SBI transaction row (guard use): date, description, amount, C|D."""
    return bool(SBI_ROW_RX.match(line.strip()))


def parse_sbi(lines, bank, card, period) -> Parsed:
    p = Parsed(tol_loose=True, note=SBI_ROUNDING_NOTE)
    in_block = False
    last_date: datetime | None = None
    for i, raw in enumerate(lines):
        line = raw.strip()
        if SBI_LEGEND in line:
            in_block = False
            continue
        if re.match(r"^Date\s+Transaction Details\s+Amount", line) or re.match(r"^for Statement Period\s*:", line, re.I):
            in_block = True
            continue
        if not p.summary and SBI_SUMMARY_RX.match(line) and any(
                "Previous Balance" in ln for ln in lines[max(0, i - 4):i]):
            parts = re.findall(rf"({AMT})(\s+CR)?", line)
            vals = [num(a) for a, _ in parts]
            crs = [bool(c) for _, c in parts]
            for idx, label in ((1, "payments/credits"), (2, "purchases/debits"), (3, "fees/taxes/interest")):
                if crs[idx]:
                    p.errors.append(f"unexpected CR on the {label} figure of the summary")
            prev, total = (-vals[0] if crs[0] else vals[0]), (-vals[4] if crs[4] else vals[4])
            p.summary = dict(prev=prev, payments=vals[1], purchases=vals[2], finance=vals[3], total=total)
            p.printed_closing = round(prev - vals[1] + vals[2] + vals[3], 2)
            p.rows_net_expected = round(vals[2] + vals[3] - vals[1], 2)
            continue
        if not in_block:
            continue
        m = SBI_ROW_RX.match(line)
        if m:
            when = sbi_date(m.group(1))
            if when is None:
                p.errors.append(f"unreadable date: {line[:70]}")
                continue
            last_date = when
            p.rows.append(_mk_row(bank, card, when, m.group(2), num(m.group(3)), "Cr" if m.group(4) == "C" else "Dr"))
            continue
        g = SBI_GST_RX.match(line)
        if g:
            if last_date is None:
                p.errors.append(f"tax row before any dated row: {line[:70]}")
                continue
            p.rows.append(_mk_row(bank, card, last_date, g.group(1), num(g.group(2)), "Cr" if g.group(3) == "C" else "Dr"))
            continue
        if SBI_DATE_START.match(line) and re.search(AMT, line):
            p.errors.append(f"unparsed line: {line[:70]}")
    if not p.summary:
        p.missing.append("summary line")
    return p


YES_PERIOD_LINE_RX = re.compile(r"^\d\d/\d\d/\d{4}\s+(?:to|-)\s+\d\d/\d\d/\d{4}\b\s*", re.I)


YES_RS = rf"Rs\.?\s*({NUM})\s*(Cr|Dr)?"
YES_REF_RX = re.compile(r"^Ref(?:erence)?\.?\s*(?:No|Number)\b", re.I)


def read_yes_positional(lines):
    """CC-17: the YES summary as printed in two columns, labels and values
    interleaved across lines:
        Previous Balance :                          -> the Rs. line under it
        Available Credit Limit: & Other Charges :   -> "Statement Date : d Rs. <limit> Rs. <purchases> [Dr]"
        Total Amount Due: Cash Limit: Points Earned : n  -> "Rs. <total due> Rs. <cash limit>"
        Payment & Credits Received :                -> trailing "Rs. <payments> [Cr]" of the
                                                       "Minimum Amount Due: Available Cash Limit:" line
    Returns None when this layout is not on the page, else (figures, why):
    figures holds the four numbers found by position, and why is "" only when
    they satisfy previous + purchases - payments = total. A figure that is not
    found, or a set that does not satisfy the equation, is reported, never
    guessed and never taken as zero."""
    pay_idx = next((i for i, ln in enumerate(lines)
                    if re.match(rf"^Minimum Amount Due\s*:.*Available Cash Limit\s*:\s*{YES_RS}\s*$",
                                ln.strip(), re.I)), None)
    stmt_idx = next((i for i, ln in enumerate(lines)
                     if re.match(rf"^Statement Date\s*:\s*\d\d/\d\d/\d{{4}}\s+{YES_RS}\s+{YES_RS}\s*$",
                                 ln.strip(), re.I)), None)
    if pay_idx is None and stmt_idx is None:
        return None
    found: dict = {}
    if pay_idx is not None:
        pm = re.search(rf"{YES_RS}\s*$", lines[pay_idx].strip(), re.I)
        found["payments"] = num(pm.group(1))
    if stmt_idx is not None:
        vals = list(re.finditer(YES_RS, lines[stmt_idx], re.I))
        found["purchases"] = num(vals[1].group(1))        # the 1st Rs. is the available credit limit
    for i, ln in enumerate(lines):
        t = ln.strip()
        if re.match(r"^Previous Balance\s*:?\s*$", t, re.I):
            nxt = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
            vm = re.match(rf"^{YES_RS}\s*$", nxt, re.I)
            if vm:
                found["prev"] = signed(num(vm.group(1)), vm.group(2))
        elif re.match(r"^Total Amount Due\s*:.*Cash Limit\s*:", t, re.I):
            nxt = next((x.strip() for x in lines[i + 1:] if x.strip()), "")
            vals = list(re.finditer(YES_RS, nxt, re.I))
            if len(vals) >= 2 and not re.sub(YES_RS, "", nxt, flags=re.I).strip():
                found["total"] = signed(num(vals[0].group(1)), vals[0].group(2))   # the 1st Rs. is total due
    absent = [k for k in ("prev", "purchases", "payments", "total") if k not in found]
    if absent:
        return found, "not found: " + ", ".join(absent)
    got = round(found["prev"] + found["purchases"] - found["payments"], 2)
    if abs(got - found["total"]) > TOL_STRICT + 1e-9:
        return found, (f"previous balance {found['prev']:,.2f} + purchases {found['purchases']:,.2f} - payments "
                       f"{found['payments']:,.2f} is {got:,.2f}, not the printed total {found['total']:,.2f}")
    return found, ""


def parse_yes(lines, bank, card, period) -> Parsed:
    p = Parsed()
    positional = read_yes_positional(lines)
    row_rx = re.compile(rf"^(\d\d/\d\d/\d{{4}})\s+(.*?)\s+({AMT})\s+(Dr|Cr)\s*$", re.I)
    row_at = {i for i, ln in enumerate(lines) if row_rx.match(ln.strip())}
    labels = {
        "prev": r"Previous Balance",
        "purchases": r"Current Purchases\s*/\s*Cash Advance",
        "payments": r"Payment\s*&\s*Credits Received",
        "total": r"Total Amount Due",
    }
    found: dict[str, float] = {}
    label_queue: list = []
    value_queue: list = []
    for idx, line in enumerate(lines):
        line = line.strip()
        m = row_rx.match(line)
        if m:
            when = _dmy(m.group(1))
            if when is None:
                p.errors.append(f"unreadable date: {line[:70]}")
                continue
            p.rows.append(_mk_row(bank, card, when, m.group(2), num(m.group(3)), norm_dir(m.group(4))))
            continue
        # CC-17: a wrapped row prints its description on the line above and "Ref No ..." below;
        # neither is a summary label or value.
        if YES_REF_RX.match(line) or (p.rows and (idx + 1) in row_at):
            continue
        pm = YES_PERIOD_LINE_RX.match(line)
        if pm:
            # CC-04: "01/09/2025 To 30/09/2025 <summary text>" is the period header, never a row
            # and never an unparsed row; whatever follows it is read as summary text.
            line = line[pm.end():].strip()
        elif DATE_START.match(line):
            if _unparsed(line):
                p.errors.append(f"unparsed line: {line[:70]}")
            continue
        matched_inline = False
        for key, rx in labels.items():
            if key in found:
                continue
            lm = re.search(rx + rf"\s*:?\s*(?:Rs\.?\s*)?({NUM})\s*(Cr|Dr)?", line, re.I)
            if lm:
                found[key] = signed(num(lm.group(1)), lm.group(2)) if key in ("prev", "total") else num(lm.group(1))
                matched_inline = True
        if matched_inline or pm:
            continue
        # Interleaved layout: labels and values are on separate lines, in the
        # same order. A label line is money-wording ending in ":" with no digit
        # (date labels are skipped); a value line is just "Rs. 999.99 [Dr|Cr]".
        if (":" in line and not re.search(r"\d", line) and not re.search(r"date", line, re.I)
                and re.search(MONEY_LABEL_WORDS, line, re.I)):
            # CC-04: a two-column layout prints several "Label :" on one line; each colon ends
            # one label, taken in order. An unknown label keeps its place as None.
            for chunk in line.split(":")[:-1] if line.count(":") > 1 else [line]:
                key = next((k for k, rx in labels.items() if re.search(rx, chunk, re.I)), None)
                if key is None and re.search(r"Other Charges", chunk, re.I) and re.search(r"Purchases", chunk, re.I):
                    key = "purchases"
                label_queue.append(key)
            continue
        vtoks = list(re.finditer(rf"Rs\.?\s*({NUM})\s*(Cr|Dr)?", line, re.I))
        if vtoks and not re.sub(rf"Rs\.?\s*{NUM}\s*(?:Cr|Dr)?", "", line, flags=re.I).strip():
            for vm in vtoks:
                value_queue.append((num(vm.group(1)), vm.group(2)))
    if positional is not None:
        # The two-column layout is read by position and only taken when the four figures satisfy
        # previous + purchases - payments = total. The queue reader is not trusted here: its
        # labels and values do not line up on this layout.
        figs, why = positional
        if why:
            p.missing.append("summary figures: " + why)
            return p
        p.summary = dict(figs)
        p.printed_closing = round(figs["prev"] + figs["purchases"] - figs["payments"], 2)
        return p
    for key, val in zip(label_queue, value_queue):
        if key and key not in found:
            found[key] = signed(val[0], val[1]) if key in ("prev", "total") else val[0]
    absent = [k for k in labels if k not in found]
    if absent:
        p.missing.append("summary figures: " + ", ".join(absent))
    else:
        p.summary = dict(found)
        p.printed_closing = found["prev"] + found["purchases"] - found["payments"]
    return p


# ---------------------------------------------------------------------------
# Text extraction (row-wise) and statement assembly
# ---------------------------------------------------------------------------

def read_lines(pdf_path) -> list[str]:
    """Row-wise text: pdfplumber page.extract_text() per page, split on
    newlines. Raises on an unreadable PDF; the caller reports it."""
    import pdfplumber
    lines: list[str] = []
    with pdfplumber.open(str(pdf_path)) as pdf:
        for page in pdf.pages:
            lines.extend((page.extract_text() or "").splitlines())
    return lines


def get_bank_and_card_type(pdf_path):
    parent = Path(pdf_path).parent.name
    if "-" in parent:
        bank = parent.split("-", 1)[0]
        return bank, parent[len(bank) + 1:]
    return "Unknown", "Unknown"


SBM_PERIOD_RX = re.compile(
    r"Statement\s+\w+\s*:\s*(\d{1,2}-[A-Za-z]{3,9}-\d{4})\s+to\s+(\d{1,2}-[A-Za-z]{3,9}-\d{4})", re.I)


SBM_MONTH_RX = re.compile(r"Statement\s+Month\s*:?\s*([A-Za-z]{3,9})[\s,\-/]+(\d{4})", re.I)


def detect_period(lines):
    """((start, end) | None, note). note is non-empty when only a statement date
    was printed (HDFC old, SBM): the period is then the month ending on that
    date, the same rule the completeness check uses. SBM prints a real period
    ("Statement Period : 9-Dec-2025 to 31-Dec-2025") next to the statement
    date; that is preferred."""
    text = "\n".join(lines)
    sm = SBM_PERIOD_RX.search(text)
    if sm:
        s, e = parse_date(sm.group(1), "%d-%b-%Y"), parse_date(sm.group(2), "%d-%b-%Y")
        if s and e and s <= e:
            return (s, e), ""
    older = sbm_older_period(lines)
    if older:
        return older, (
            "period derived: the statement prints the month only; it runs from the earlier of "
            "the month start and the first row date to the end of that month")
    mm = SBM_MONTH_RX.search(text)
    if mm:
        # CC-04: older SBM statements print the month only; the period is that whole month.
        first = parse_date(f"1-{mm.group(1)}-{mm.group(2)}", "%d-%b-%Y")
        if first:
            nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
            return (first, nxt - timedelta(days=1)), "statement month only"
    info = classify_text(text)
    if info is None:
        return None, ""
    if info.kind == "period":
        return (info.start, info.end), ""
    return (_month_back(info.end) + timedelta(days=1), info.end), "statement date only"


NO_TEXT_LAYER = "No text layer: cannot read"


def non_statement_reason(lines, bank: str | None = None) -> str | None:
    """A document that is not a statement and must be skipped by title (A),
    never parsed to 0 rows and never counted as NOT AVAILABLE:
      - a "Most Important Terms and Conditions" document (fee table, no
        transaction-date rows);
      - an HDFC "Year End Statement & Summary" (a yearly roll-up with an
        "Account Summary for the period from X to Y"), which carries no
        statement period or statement date of its own.
    A real statement is never skipped: it has transaction-date rows (MITC) or a
    printed statement period / date (year-end)."""
    text = "\n".join(lines)
    up = text.upper()
    has_txn_rows = any(DATE_START.match(ln.strip()) and re.search(AMT, ln) for ln in lines)
    if not has_txn_rows and (bank or "").lower() == "sbi":
        has_txn_rows = any(is_sbi_row(ln) for ln in lines)
    if ("MOST IMPORTANT TERMS" in up or re.search(r"\bMITC\b", up)) and not has_txn_rows:
        return "Most Important Terms and Conditions"
    # CC-04: a leaflet TITLED "Important Terms and Conditions" (no "Most"): the title is one of the
    # first lines and is short. A statement that merely mentions it in a footer has rows or a
    # period and is never skipped.
    heads = [ln.strip().upper() for ln in lines if ln.strip()][:5]
    if not has_txn_rows and any(len(h) <= 60 and re.search(r"\bIMPORTANT\s+TERMS\s+(?:AND|&)\s+CONDITIONS\b", h)
                                for h in heads):
        return "Important Terms and Conditions"
    if (re.search(r"YEAR\s+END\s+STATEMENT", up)
            or re.search(r"ACCOUNT\s+SUMMARY\s+FOR\s+THE\s+PERIOD\s+FROM", up)) \
            and classify_text(text) is None:
        return "Year End Statement & Summary"
    return None


def parse_statement(lines, bank, card_type, source) -> Statement:
    period, note = detect_period(lines)
    b = bank.lower()
    if b == "axis":
        parsed = parse_axis(lines, bank, card_type, period)
    elif b == "hdfc":
        is_new = any("Billing Period" in ln for ln in lines) or any(
            re.match(r"^\d\d/\d\d/\d{4}\|", ln.strip()) for ln in lines)
        parsed = (parse_hdfc_new if is_new else parse_hdfc_old)(lines, bank, card_type, period)
    elif b == "icici":
        parsed = parse_icici(lines, bank, card_type, period)
    elif b == "hsbc":
        parsed = parse_hsbc(lines, bank, card_type, period)
    elif b == "sbm":
        parsed = parse_sbm(lines, bank, card_type, period)
    elif b == "sbi":
        parsed = parse_sbi(lines, bank, card_type, period)
    elif b == "yes":
        parsed = parse_yes(lines, bank, card_type, period)
    else:
        parsed = Parsed(errors=[f"no parser for bank folder '{bank}'"])
    st = Statement(bank, card_type, source, period, note, parsed, list(lines))
    for r in parsed.rows:
        r.period = st.label
        r.statement = f"{st.card} | {st.label}"
        r.source = source
    return st


# ---------------------------------------------------------------------------
# Tie-out
# ---------------------------------------------------------------------------

@dataclass
class TieOut:
    card: str
    period: str
    source: str
    previous: float | None
    payments: float
    credits: float
    spends: float
    fees: float
    other: float
    computed: float | None
    printed: float | None
    difference: float | None
    result: str          # PASS | PASS (chained) | FAIL | NOT AVAILABLE ...
    note: str = ""
    gap: str = ""        # gap report (I): filled for every statement that is not a PASS


def find_prior(st: Statement, pool: list) -> Statement | None:
    """The same card's statement that ends just before this one starts (at most
    CHAIN_MAX_GAP_DAYS between them; 0 is allowed: a cycle change starts the new
    period on the day the old one ended, while a real overlap never chains). None when there is none: a chained
    tie-out then reports NOT AVAILABLE, it never guesses."""
    if st.period is None:
        return None
    best = None
    for other in pool:
        if other is st or other.card != st.card or other.period is None:
            continue
        gap = (st.period[0] - other.period[1]).days
        if 0 <= gap <= CHAIN_MAX_GAP_DAYS and (best is None or other.period[1] > best.period[1]):
            best = other
    return best


def tie_out(st: Statement, prior: Statement | None = None) -> TieOut:
    rows = st.parsed.rows
    pay = round(sum(r.amount if r.direction == "Cr" else -r.amount for r in rows if r.kind == "payment"), 2)
    cred = round(sum(r.amount for r in rows if r.direction == "Cr" and r.kind in ("refund", "cashback")), 2)
    spend = round(sum(r.amount for r in rows if r.direction == "Dr" and r.kind == "spend"), 2)
    fees = round(sum(r.amount for r in rows if r.direction == "Dr" and r.kind == "fee"), 2)
    other = round(sum(r.amount for r in rows
                      if r.direction == "Dr" and (r.kind == "cashback" or r.kind in EMI_KINDS)), 2)
    cred += round(sum(r.amount for r in rows if r.direction == "Cr" and r.kind in EMI_KINDS), 2)
    s = st.parsed.summary
    prev, total = s.get("prev"), s.get("total")
    # HSBC: a loan booked in this period adds its whole principal + interest to the
    # statement total with no row of its own.
    new_loans = loans_booked_in_period(st.parsed, st.period)
    loan_add = round(sum(ln.principal + ln.interest for ln in new_loans), 2)
    base = dict(card=st.card, period=st.label, source=st.source, payments=pay, credits=cred,
                spends=spend, fees=fees, other=other)
    notes = []
    if st.parsed.missing:
        notes.append("not found: " + "; ".join(st.parsed.missing))
    within = (lambda d: abs(d) < TOL_LOOSE) if st.parsed.tol_loose else (lambda d: abs(d) <= TOL_STRICT)
    if total is None:
        computed = round(prev + sum(r.signed for r in rows), 2) if prev is not None else None
        return TieOut(previous=prev, computed=computed, printed=None, difference=None,
                      result="NOT AVAILABLE", note="; ".join(notes) or "summary not found", **base)
    if prev is None:
        # Chained tie-out (G): only the total is printed. The previous
        # statement's total plus this statement's rows must equal this total.
        # Never PASS on the closing total alone.
        if prior is None:
            return TieOut(previous=None, computed=None, printed=total, difference=None,
                          result=RESULT_NO_PRIOR,
                          note="; ".join(notes + ["no prior statement of this card in the run"]), **base)
        ptotal = prior.parsed.summary.get("total")
        if ptotal is None:
            return TieOut(previous=None, computed=None, printed=total, difference=None,
                          result="NOT AVAILABLE (prior statement total not found)",
                          note="; ".join(notes + [f"prior statement {prior.source} has no printed total"]), **base)
        computed = round(ptotal + sum(r.signed for r in rows) + loan_add, 2)
        diff = round(computed - total, 2)
        ok = abs(diff) <= TOL_STRICT
        if not ok:
            notes.append("prior statement's total plus the parsed rows does not reproduce the printed total: "
                         "a row is dropped, doubled or misread")
        return TieOut(previous=ptotal, computed=computed, printed=total, difference=diff,
                      result=RESULT_CHAINED if ok else "FAIL",
                      note="; ".join(notes + [f"chained to {prior.source}"]), **base)
    computed = round(prev + sum(r.signed for r in rows) + loan_add, 2)
    diff = round(computed - total, 2)
    ok = within(diff)
    if new_loans:
        notes.append(f"includes {len(new_loans)} new loan(s) booked in the period "
                     f"(principal + interest {loan_add:,.2f})")
    parts = st.parsed.outstanding_parts
    if parts is not None and abs(round(total - (parts[0] + parts[1]), 2)) > TOL_STRICT:
        ok = False
        notes.append(f"net outstanding {total:,.2f} is not total purchase outstanding {parts[0]:,.2f} "
                     f"+ total loan outstanding {parts[1]:,.2f}")
    if not ok:
        notes.append("parsed rows do not reproduce the printed total: a row is dropped, doubled or misread")
    if st.parsed.printed_closing is not None and not within(round(st.parsed.printed_closing - total, 2)):
        # CC-18: Axis leaves reward redemption credits out of the summary's Credits. That explains
        # the mismatch only when it equals EXACTLY the sum of those rows; anything else still fails.
        redeemed = [r for r in rows if r.direction == "Cr" and REDEMPTION_RX.search(r.description)]
        gap = round(st.parsed.printed_closing - total, 2)
        if (st.bank.lower() == "axis" and redeemed
                and abs(gap - round(sum(r.amount for r in redeemed), 2)) <= TOL_STRICT + 1e-9):
            notes.append(f"the bank's summary leaves {len(redeemed)} reward redemption credit(s) out of Credits")
        else:
            ok = False
            notes.append("printed summary figures do not add up to the printed total "
                         "(the summary line may be misread)")
    exp = st.parsed.rows_net_expected
    if exp is not None and abs(round(sum(r.signed for r in rows) - exp, 2)) > TOL_STRICT:
        ok = False
        notes.append(f"parsed rows net {sum(r.signed for r in rows):,.2f} but the printed summary implies {exp:,.2f}: "
                     "a row is dropped, doubled or misread")
    if st.parsed.proof_failed:
        ok = False
        notes.append(st.parsed.proof_failed)
    if st.parsed.note:
        notes.append(st.parsed.note)
    return TieOut(previous=prev, computed=computed, printed=total, difference=diff,
                  result=RESULT_PASS if ok else "FAIL", note="; ".join(notes), **base)


# ---------------------------------------------------------------------------
# Gap report (I)
# ---------------------------------------------------------------------------

_LOOSE_AMT = re.compile(r"\d[\d,]*\.\d+")


def _line_amounts(line: str) -> list:
    return [num(x) for x in _LOOSE_AMT.findall(line)]


def _is_header_date_line(line: str) -> bool:
    return bool(re.match(r"^\d\d/\d\d/\d{4}\s+-\s", line))


def unparsed_date_lines(st: Statement) -> list:
    """Date-led lines that carry an amount and that no parsed row accounts for.
    Matching is parser-independent: a line is accounted for when an unused row
    has an amount that appears on it."""
    used: set = set()
    left = []
    due = sbm_due_value_indexes(st.lines) if st.bank.lower() == "sbm" else set()
    for idx, raw in enumerate(st.lines):
        line = raw.strip()
        if idx in due:
            continue
        if not (DATE_START.match(line) or re.match(r"^\d\d/\d\d/\d{4}\|", line)):
            continue
        if _is_header_date_line(line):
            continue
        amts = _line_amounts(line)
        if not amts:
            continue
        hit = next((k for k, r in enumerate(st.parsed.rows)
                    if k not in used and any(abs(r.amount - a) < 0.005 for a in amts)), None)
        if hit is None:
            left.append(line)
        else:
            used.add(hit)
    return left


def gap_report(st: Statement, t: TieOut) -> str:
    """Why a statement is not a PASS, in one line: the printed total against
    what the parsed rows give, the summary component that is missing, whether
    the difference equals an amount on the page that was not parsed as a row
    (a likely missed row), and how many date-led lines were not parsed."""
    parts = []
    rows_net = round(sum(r.signed for r in st.parsed.rows), 2)
    if t.printed is None:
        parts.append(f"printed total not found (parsed rows net {_money(rows_net)})")
    elif t.computed is None:
        parts.append(f"printed total {_money(t.printed)}; no opening balance to add the rows "
                     f"(parsed rows net {_money(rows_net)})")
    else:
        parts.append(f"printed total {_money(t.printed)} vs {_money(t.computed)} from "
                     f"{'prior total' if 'chained to' in t.note else 'opening'}"
                     f" + parsed rows (difference {_money(t.difference or 0.0)})")
    missing = list(st.parsed.missing)
    if st.parsed.summary and "prev" not in st.parsed.summary:
        missing.append("previous balance")
    if missing:
        parts.append("missing: " + "; ".join(missing))
    left = unparsed_date_lines(st)
    diff = abs(t.difference) if t.difference else None
    if diff:
        hit = next((ln for ln in left if any(abs(a - diff) < 0.005 for a in _line_amounts(ln))), None)
        if hit:
            parts.append(f"likely missed row: the difference {_money(diff)} equals an amount on an "
                         f"unparsed line ({hit[:60]})")
        else:
            row_amts = {round(r.amount, 2) for r in st.parsed.rows}
            elsewhere = next((ln.strip() for ln in st.lines
                              if any(abs(a - diff) < 0.005 and round(a, 2) not in row_amts
                                     for a in _line_amounts(ln))), None)
            if elsewhere:
                parts.append(f"the difference {_money(diff)} also appears on the page but not as a row "
                             f"({elsewhere[:60]}); possible missed row")
    parts.append(f"{len(left)} date-led line(s) not parsed")
    return "; ".join(parts)


# ---------------------------------------------------------------------------
# Fees (8b)
# ---------------------------------------------------------------------------

@dataclass
class FeeLine:
    row: Row
    reversed_by: Row | None = None


def collect_fees(statements: list[Statement]) -> list[FeeLine]:
    fees = [FeeLine(r) for st in statements for r in st.parsed.rows
            if r.kind == "fee" and r.direction == "Dr"]
    # A later credit that reverses or waives a fee: same card, fee-like wording,
    # same amount. A merchant refund of the same amount is NOT a reversal.
    credits = [r for st in statements for r in st.parsed.rows
               if r.direction == "Cr" and r.kind == "refund" and FEE_REVERSAL_RX.search(r.description)]
    used: set[int] = set()
    for f in fees:
        for c in credits:
            if id(c) in used or c.card != f.row.card:
                continue
            if abs(c.amount - f.row.amount) > 0.005:
                continue
            if f.row.date and c.date and c.date < f.row.date:
                continue
            f.reversed_by = c
            used.add(id(c))
            break
    fees.sort(key=lambda f: (f.row.card, f.row.date or datetime.min))
    return fees


def _money(v: float) -> str:
    return f"{v:,.2f}"


def fee_block(fees: list[FeeLine]) -> str:
    if not fees:
        return ""
    n_rev = sum(1 for f in fees if f.reversed_by is not None)
    head = f"CARD FEES CHARGED ({len(fees)})"
    if n_rev:
        head += (f" - fees not marked reversed are money the card cost you; "
                 f"{n_rev} reversed fee(s) are shown for the record and are not in the totals:")
    else:
        head += " - every line here is money the card cost you:"
    lines = [head]
    totals: dict[str, float] = defaultdict(float)
    reversed_totals: dict[str, float] = defaultdict(float)
    for f in fees:
        r = f.row
        when = r.date.strftime("%d %b %Y") if r.date else "undated"
        tail = ""
        if f.reversed_by is not None:
            c = f.reversed_by
            tail = f"  [reversed: credit {c.date:%d %b %Y} {c.description[:40]}]" if c.date else "  [reversed]"
        lines.append(f"  {r.card} | {r.period} | {when} | {r.description[:60]} | {_money(r.amount)}{tail}")
        # CC-20: a reversed fee did not cost the card holder anything; it stays visible but is not totalled
        if f.reversed_by is None:
            totals[r.card] += r.amount
        else:
            reversed_totals[r.card] += r.amount
            totals[r.card] += 0.0
    for card in sorted(totals):
        extra = f" (excludes {_money(reversed_totals[card])} reversed)" if reversed_totals.get(card) else ""
        lines.append(f"  Total fees {card}: {_money(totals[card])}{extra}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Whole-folder run
# ---------------------------------------------------------------------------

@dataclass
class Result:
    message: str
    issues: list[str]
    rows: list[Row]
    ties: list[TieOut]
    fees: list[FeeLine]
    duplicates: list[str]
    output: str = ""
    skipped: list[str] = field(default_factory=list)       # not statements, skipped by title
    statements: list = field(default_factory=list)         # the Statement objects behind ties (same order)
    emi_rows: list = field(default_factory=list)
    pool: list = field(default_factory=list)               # every distinct statement parsed (prior-statement lookups)
    csv_statements: list = field(default_factory=list)     # CC-22: ICICI CSV exports read (rows kept after overlap removal)
    csv_ties: list = field(default_factory=list)           # CC-22: one coverage line per CSV (no totals, so no tie-out)


CSV_NO_TOTALS = "NO TOTALS (CSV export)"


def _csv_key(r: Row) -> tuple:
    return (r.card, r.date.date() if r.date else None, round(r.amount, 2), r.direction)


def read_icici_csv(path: Path, bank: str, card: str) -> tuple[Statement | None, str]:
    """CC-22: (Statement, "") or (None, why it was refused). The period is the first to the last
    transaction date: the export prints no statement period and no totals."""
    try:
        parsed = parse_icici_csv(path)
    except CsvRefused as e:
        return None, f"refused - {e}"
    ctype, why = identify_card(parsed.masks)
    if ctype is None:
        return None, f"refused - cannot tell which card this is ({why}); not read"
    if ctype.lower() != card.lower():
        return None, (f"refused - the card numbers in the file say ICICI-{ctype} but it sits in "
                      f"{bank}-{card}; not read")
    rows = []
    for cr in parsed.rows:
        r = _mk_row(bank, card, datetime(cr.when.year, cr.when.month, cr.when.day),
                    cr.description, cr.amount, cr.direction)
        r.network = cr.network
        rows.append(r)
    period = (parsed.first, parsed.last) if parsed.rows else None
    st = Statement(bank, card, path.name, period,
                   "CSV export: period is the first to the last transaction date", Parsed(rows=rows))
    for r in rows:
        r.period = st.label
        r.statement = f"{st.card} | CSV | {st.label}"
        r.source = path.name
    return st, ""


def csv_coverage_tie(st: Statement, dropped: int) -> TieOut:
    rows = st.parsed.rows
    debits = sum(1 for r in rows if r.direction == "Dr")
    credits = len(rows) - debits
    note = (f"CSV export carries no statement totals, so there is no balance tie-out. "
            f"Coverage {st.label}; {debits} debit(s), {credits} credit(s)")
    if dropped:
        note += f"; {dropped} row(s) already in another statement not counted again"
    return TieOut(card=st.card, period=st.label, source=st.source, previous=None, payments=0.0,
                  credits=0.0, spends=0.0, fees=0.0, other=0.0, computed=None, printed=None,
                  difference=None, result=CSV_NO_TOTALS, note=note)


def _overlaps(period, start, end) -> bool:
    if period is None or start is None or end is None:
        return True
    return period[0] <= end and period[1] >= start


def row_signature(st: Statement) -> tuple:
    """What makes two statement files "the same statement": the same rows."""
    return tuple(sorted((r.date.isoformat() if r.date else "", r.description, round(r.amount, 2), r.direction)
                        for r in st.parsed.rows))


def emi_block(emi_rows: list) -> str:
    if not emi_rows:
        return ""
    lines = [f"EMI-LIKE ROWS ({len(emi_rows)}) - not treated as spends, awaiting a decision:"]
    for r in emi_rows:
        when = r.date.strftime("%d %b %Y") if r.date else "undated"
        extra = f" | {r.instalment}" if r.instalment else ""
        lines.append(f"  {r.card} | {r.period} | {when} | {r.kind} | {r.description[:60]}{extra} | "
                     f"{_money(r.amount)} {r.direction}")
    return "\n".join(lines)


def md_list(text: str) -> str:
    """UI-39: the result panel is Markdown without line breaks, so indented single-newline lines run
    together. Keep the first line as the heading and make every following line its own list item.
    Only the panel message uses this; the Excel and the issues list are untouched."""
    head, *rest = text.split("\n")
    if not rest:
        return text
    return head + "\n\n" + "\n".join(f"- {ln.strip()}" for ln in rest if ln.strip())


def no_activity(st: Statement) -> bool:
    """CC-19: a statement with no rows whose printed summary figures are ALL zero
    (within the tie-out tolerance) had no activity. Any non-zero printed figure,
    or a summary that was not found, means rows may be missing: that is never
    "no activity"."""
    figs = st.parsed.summary
    return bool(figs) and not st.parsed.missing and all(abs(v) <= TOL_STRICT for v in figs.values())


def run_extraction(pdf_dir, output_excel, start: date | None = None, end: date | None = None,
                   label: str = "all statements") -> Result:
    pdf_dir = Path(pdf_dir)
    seen_names, pdfs = set(), []
    for p in sorted(pdf_dir.rglob("*.pdf")) + sorted(pdf_dir.rglob("*.PDF")):
        k = (p.parent.name, p.name.lower())
        if k not in seen_names:
            seen_names.add(k)
            pdfs.append(p)

    issues: list[str] = []
    statements: list[Statement] = []       # in the range: reported
    pool: list[Statement] = []             # every distinct statement parsed: chained tie-outs look here
    duplicates: list[str] = []
    skipped: list[str] = []
    outside: list[str] = []
    seen_keys: dict = {}

    if not pdfs and not (list(pdf_dir.rglob("*.csv")) + list(pdf_dir.rglob("*.CSV"))):
        issues.append(f"no PDFs found under {pdf_dir}")

    for pdf in pdfs:
        bank, card = get_bank_and_card_type(pdf)
        try:
            lines = read_lines(pdf)
        except Exception as e:  # unreadable PDF: say so, never skip silently
            issues.append(f"{pdf.name}: could not be read ({type(e).__name__})")
            continue
        if not any(ln.strip() for ln in lines):
            # A scanned statement: never 0 rows, never PASS.
            issues.append(f"{pdf.name}: {NO_TEXT_LAYER}")
            continue
        reason = non_statement_reason(lines, bank)
        if reason:
            skipped.append(f"{pdf.name} ({reason})")
            continue
        st = parse_statement(lines, bank, card, pdf.name)
        if st.key in seen_keys:
            first_name, first = seen_keys[st.key]
            if row_signature(st) == row_signature(first):
                duplicates.append(f"{st.card} {st.label}: {pdf.name} - Duplicate of {first_name} (counted once)")
            else:
                issues.append(f"{pdf.name}: same card and period as {first_name} but different rows; "
                              f"only {first_name} is used - check which one is right")
            continue
        seen_keys[st.key] = (pdf.name, st)
        pool.append(st)
        if not _overlaps(st.period, start, end):
            outside.append(pdf.name)
            continue
        statements.append(st)
        if st.period is None:
            issues.append(f"{pdf.name}: statement period not found")
        if not st.parsed.rows:
            if no_activity(st):
                issues.append(f"{pdf.name}: no activity (every printed figure is zero)")
            elif not st.parsed.summary:
                # CC-22: 0 rows AND no statement summary to say why: never clean.
                issues.append(f"{pdf.name}: no transactions parsed (0 rows and no statement summary found) - NOT clean; "
                              "the statement was not understood (a CSV export of the same card may carry its rows)")
            else:
                issues.append(f"{pdf.name}: no transactions parsed")
        for e in st.parsed.errors:
            issues.append(f"{pdf.name}: {e}")

    # CC-22: ICICI CSV exports. A row already booked from a PDF statement (or an earlier CSV)
    # is counted once: rows match on card + date + amount + direction, with multiplicity, and the
    # PDF row wins. A CSV has no totals, so it gets a coverage line instead of a tie-out.
    csv_seen, csv_files = set(), []
    for p in sorted(pdf_dir.rglob("*.csv")) + sorted(pdf_dir.rglob("*.CSV")):
        k = (p.parent.name, p.name.lower())
        if k not in csv_seen:
            csv_seen.add(k)
            csv_files.append(p)
    csv_statements: list[Statement] = []
    csv_ties: list[TieOut] = []
    booked: dict = defaultdict(int)
    for st in statements:
        for r in st.parsed.rows:
            booked[_csv_key(r)] += 1
    pdf_periods = [(st.card, st.period) for st in statements if st.period and st.parsed.rows]
    csv_keys_seen: dict = {}
    for cpath in csv_files:
        bank, card = get_bank_and_card_type(cpath)
        if bank.lower() != "icici":
            skipped.append(f"{cpath.name} (a CSV statement is only read for ICICI)")
            continue
        cst, why = read_icici_csv(cpath, bank, card)
        if cst is None:
            issues.append(f"{cpath.name}: {why}")
            continue
        if cst.key in csv_keys_seen:
            first_name, first = csv_keys_seen[cst.key]
            if row_signature(cst) == row_signature(first):
                duplicates.append(f"{cst.card} {cst.label}: {cpath.name} - Duplicate of {first_name} (counted once)")
            else:
                issues.append(f"{cpath.name}: same card and coverage as {first_name} but different rows; "
                              f"only {first_name} is used - check which one is right")
            continue
        csv_keys_seen[cst.key] = (cpath.name, cst)
        if not cst.parsed.rows:
            issues.append(f"{cpath.name}: CSV read but holds no transactions - NOT clean")
            continue
        if not _overlaps(cst.period, start, end):
            outside.append(cpath.name)
            continue
        in_range = [r for r in cst.parsed.rows
                    if (start is None or r.date.date() >= start) and (end is None or r.date.date() <= end)]
        groups: dict = defaultdict(list)
        for r in in_range:
            groups[_csv_key(r)].append(r)
        kept, extra = [], []
        for key, grp in groups.items():
            take = max(0, len(grp) - booked[key])
            kept += grp[:take]
            booked[key] += take
            if take and any(c == key[0] and ps[0] <= key[1] <= ps[1] for c, ps in pdf_periods):
                extra += grp[:take]
        dropped = len(in_range) - len(kept)
        if extra:
            issues.append(f"{cpath.name}: {len(extra)} CSV row(s) fall inside a PDF statement of the same "
                          "card but are not in it (booked once, from the CSV): "
                          + "; ".join(f"{r.date:%d %b %Y} {_money(r.amount)} {r.direction}" for r in extra[:5]))
        out_st = Statement(cst.bank, cst.card_type, cst.source, cst.period, cst.period_note,
                           Parsed(rows=kept))
        csv_statements.append(out_st)
        csv_ties.append(csv_coverage_tie(out_st, dropped))

    rows = [r for st in statements for r in st.parsed.rows] + [r for st in csv_statements for r in st.parsed.rows]
    undated = [r for r in rows if r.date is None]
    if undated:
        issues.append(f"{len(undated)} row(s) without a date")
    ties = [tie_out(st, find_prior(st, pool)) for st in statements]
    gap_lines: list[str] = []
    for st, t in zip(statements, ties):
        if not is_pass(t.result):
            t.gap = gap_report(st, t)
            issues.append(f"tie-out {t.result}: {t.card} {t.period} ({t.source})"
                          + (f" - {t.note}" if t.note else "")
                          + (f"; difference {_money(t.difference)}" if t.difference else ""))
            gap_lines.append(f"{t.card} {t.period} ({t.source}): {t.gap}")
    fees = collect_fees(statements + csv_statements)
    # CC-20: the fee lines print once, in the fee block. The issues list carries one pointer for
    # the fees that were NOT reversed (a reversed fee is resolved and is not an issue).
    unreversed = [f for f in fees if f.reversed_by is None]
    if unreversed:
        issues.append(f"fee charged: {len(unreversed)} card fee(s) not reversed, total "
                      f"{_money(sum(f.row.amount for f in unreversed))} - listed in the CARD FEES CHARGED block")
    emi_rows = [r for r in rows if r.kind in EMI_KINDS]
    for r in emi_rows:
        when = f"{r.date:%d %b %Y} " if r.date else ""
        issues.append(f"EMI row ({r.kind}): {r.card} {when}{r.description[:40]} {_money(r.amount)}")

    out = ""
    if rows:
        write_excel(rows, ties + csv_ties, fees, duplicates, issues, label, output_excel, skipped)
        out = str(output_excel)

    by_card: dict[str, int] = defaultdict(int)
    for r in rows:
        by_card[r.card] += 1
    parts = []
    block = fee_block(fees)
    if block:
        parts.append(md_list(block))
    eb = emi_block(emi_rows)
    if eb:
        parts.append(md_list(eb))
    if issues:
        parts.append(f"Credit card transactions completed with {len(issues)} issue(s):\n\n"
                     + "\n".join(f"- {i}" for i in issues))
    else:
        parts.append("Credit card transactions completed successfully.")
    if gap_lines:
        parts.append("Gap report (statements that did not tie out):\n\n" + "\n".join(f"- {g}" for g in gap_lines))
    parts.append(f"Range run: {label}")
    parts.append(f"{len(rows)} transactions from {len(statements)} statement(s)"
                 + (f" and {len(csv_statements)} CSV export(s)" if csv_statements else "")
                 + (f"; {len(outside)} statement(s) outside the range skipped" if outside else ""))
    if csv_ties:
        parts.append("CSV exports (no statement totals, so no balance tie-out; coverage and counts instead):\n\n"
                     + "\n".join(f"- {t.card} {t.source}: {t.note}" for t in csv_ties))
    if by_card:
        parts.append("By card:\n\n" + "\n".join(f"- {k}: {by_card[k]}" for k in sorted(by_card)))
    passed = sum(1 for t in ties if is_pass(t.result))
    parts.append(f"Tie-out: {passed} of {len(ties)} statement(s) PASS (see the Tie-out sheet)")
    if skipped:
        parts.append(f"Skipped: not a statement ({len(skipped)}):\n\n" + "\n".join(f"- {d}" for d in skipped))
    if duplicates:
        parts.append("Duplicate statements:\n\n" + "\n".join(f"- {d}" for d in duplicates))
    parts.append(f"Output: {out}" if out else "No Excel written: no transactions were extracted.")
    return Result("\n\n".join(parts), issues, rows, ties, fees, duplicates, out,
                  skipped=skipped, statements=statements, emi_rows=emi_rows, pool=pool,
                  csv_statements=csv_statements, csv_ties=csv_ties)


# ---------------------------------------------------------------------------
# Excel output
# ---------------------------------------------------------------------------

HEADER_FILL = PatternFill(start_color="1F4E78", end_color="1F4E78", fill_type="solid")
HEADER_FONT = Font(bold=True, color="FFFFFF", size=11)


def _style_header(ws):
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")


def _fmt_cols(ws, cols, fmt):
    for row in ws.iter_rows(min_row=2, min_col=cols[0], max_col=cols[1]):
        for c in row:
            c.number_format = fmt


def write_excel(rows, ties, fees, duplicates, issues, label, output_path, skipped=()):
    wb = Workbook()
    ws = wb.active
    ws.title = "Transactions"
    ws.append(["Bank", "Card Type", "Date", "Description", "Amount", "Direction",
               "Kind", "Statement", "Source file", "Network"])
    _style_header(ws)
    for r in sorted(rows, key=lambda x: x.date or datetime.min, reverse=True):
        ws.append([r.bank, r.card_type, r.date, r.description, r.amount, r.direction,
                   r.kind, r.statement, r.source, r.network])
    _fmt_cols(ws, (3, 3), "dd-mmm-yyyy")
    _fmt_cols(ws, (5, 5), "#,##0.00")
    for col, w in zip("ABCDEFGHIJ", (12, 20, 14, 55, 14, 10, 10, 50, 40, 12)):
        ws.column_dimensions[col].width = w

    summary = wb.create_sheet("Summary")
    summary.append(["Bank-Card Type", "Count"])
    counts: dict[str, int] = defaultdict(int)
    for r in rows:
        counts[r.card] += 1
    for k in sorted(counts):
        summary.append([k, counts[k]])
    summary.append([])
    summary.append(["Range run", label])
    if duplicates:
        summary.append([])
        summary.append(["Duplicate statements (counted once)"])
        for d in duplicates:
            summary.append([d])
    if skipped:
        summary.append([])
        summary.append(["Skipped: not a statement"])
        for d in skipped:
            summary.append([d])
    if issues:
        summary.append([])
        summary.append(["Issues"])
        for i in issues:
            summary.append([i])
    summary.column_dimensions["A"].width = 40
    summary.column_dimensions["B"].width = 30

    tie = wb.create_sheet("Tie-out")
    tie.append(["Card", "Period", "Previous", "Payments", "Credits", "Spends", "Fees",
                "Other debits", "Computed closing", "Printed total", "Difference",
                "Result", "Note", "Source file", "Gap report"])
    _style_header(tie)
    for t in ties:
        tie.append([t.card, t.period, t.previous, t.payments, t.credits, t.spends, t.fees,
                    t.other, t.computed, t.printed, t.difference, t.result, t.note, t.source, t.gap])
    _fmt_cols(tie, (3, 11), "#,##0.00")
    for col, w in zip("ABCDEFGHIJKLMNO", (22, 28, 14, 14, 14, 14, 12, 12, 16, 14, 12, 16, 50, 40, 70)):
        tie.column_dimensions[col].width = w

    fee_ws = wb.create_sheet("Fees")
    fee_ws.append(["Card", "Statement", "Date", "Description", "Amount", "Reversed", "Reversed by"])
    _style_header(fee_ws)
    for f in fees:
        r, c = f.row, f.reversed_by
        by = ""
        if c is not None:
            by = f"{c.date:%d-%b-%Y} {c.description}" if c.date else c.description
        fee_ws.append([r.card, r.statement, r.date, r.description, r.amount,
                       "Yes" if c is not None else "No", by])
    _fmt_cols(fee_ws, (3, 3), "dd-mmm-yyyy")
    _fmt_cols(fee_ws, (5, 5), "#,##0.00")
    for col, w in zip("ABCDEFG", (22, 50, 14, 55, 14, 10, 45)):
        fee_ws.column_dimensions[col].width = w

    wb.save(output_path)
    return len(rows)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def _parse_cli(argv: list[str]):
    pos, opts, i = [], {}, 1
    while i < len(argv):
        a = argv[i]
        if a in ("--start", "--end", "--label") and i + 1 < len(argv):
            opts[a[2:]] = argv[i + 1]
            i += 2
        else:
            pos.append(a)
            i += 1
    return pos, opts


def main(argv: list[str]) -> int:
    pos, opts = _parse_cli(argv)
    if not pos:
        print("Usage: python create_cc_transaction_list.py <pdf_directory> [output_excel] "
              "[--start YYYY-MM-DD --end YYYY-MM-DD --label TEXT]")
        return 1
    output_excel = pos[1] if len(pos) > 1 else "Consolidated_Transactions.xlsx"
    if Path(output_excel).is_dir():
        output_excel = str(Path(output_excel) / "Consolidated_Transactions.xlsx")
    start = date.fromisoformat(opts["start"]) if opts.get("start") else None
    end = date.fromisoformat(opts["end"]) if opts.get("end") else None
    result = run_extraction(pos[0], output_excel, start, end, opts.get("label", "all statements"))
    print(result.message)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
