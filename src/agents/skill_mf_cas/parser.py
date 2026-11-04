"""
parser.py -- PURE text parsing for the MF CAS (CAMS/KFintech Consolidated
Account Statement) skill. Takes plain extracted text LINES (no pdfplumber,
no password, no filesystem) and returns structured per-folio/scheme data.

Kept deliberately separate from agent.py's PDF-extraction boundary so every
test here feeds synthetic CAS-shaped text -- no encrypted PDF fixture is
ever generated or committed (see AGENT.md).

Recognised line shapes (one CAS "flavour" -- real CAMS/KFin exports vary in
exact wording/spacing; this is the shape this v1 targets, documented here
rather than guessed at silently):

    Folio No: <folio>
    AMC: <amc name>
    Scheme: <scheme name> (ISIN: <12-char ISIN>)
    Registrar: <RTA name>
    Scheme Type: EQUITY | NON-EQUITY | HYBRID
    Opening Unit Balance: <units>
    <DD-Mon-YYYY> <description...> <amount> <units> <nav> <balance>
    Closing Unit Balance: <units>
    RTA Realised Gain: <amount>                      (optional, cross-check)

A folio/scheme block ends at the next "Folio No:" line or end of input.
Never extracts or stores PAN, investor name, address, email, or mobile
number -- those lines, even if present in a real statement's text, are
simply not matched by any pattern below and fall through unrecognised.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

# Row classification -- ACQUISITION adds units, DISPOSAL removes units,
# NEITHER is a cash-only event (payout, charge) with no unit-balance effect.
ACQUISITION = "ACQUISITION"
DISPOSAL = "DISPOSAL"
NEITHER = "NEITHER"

_DISPOSAL_KEYWORDS = ("switch out", "switch-out", "redemption", "redeem", "sell", "sale")
_ACQUISITION_KEYWORDS = (
    "purchase", "sip", "systematic investment", "switch in", "switch-in",
    "dividend reinvestment", "idcw reinvestment",
)

_FOLIO_RE = re.compile(r'^Folio\s*No[:.]?\s*([\w/\-]+)\s*$', re.IGNORECASE)
_AMC_RE = re.compile(r'^AMC[:.]?\s*(.+?)\s*$', re.IGNORECASE)
_SCHEME_RE = re.compile(
    r'^Scheme[:.]?\s*(?P<name>.+?)\s*\(ISIN[:.]?\s*(?P<isin>[A-Z0-9]{12})\)\s*$',
    re.IGNORECASE,
)
_REGISTRAR_RE = re.compile(r'^Registrar[:.]?\s*(.+?)\s*$', re.IGNORECASE)
_SCHEME_TYPE_RE = re.compile(
    r'^Scheme\s*Type[:.]?\s*(EQUITY|NON-EQUITY|HYBRID)\s*$', re.IGNORECASE
)
_OPENING_RE = re.compile(r'^Opening\s*Unit\s*Balance[:.]?\s*(-?[\d,]+\.\d+)\s*$', re.IGNORECASE)
_CLOSING_RE = re.compile(r'^Closing\s*Unit\s*Balance[:.]?\s*(-?[\d,]+\.\d+)\s*$', re.IGNORECASE)
_RTA_GAIN_RE = re.compile(r'^RTA\s*Realised\s*Gain[:.]?\s*(-?[\d,]+\.\d+)\s*$', re.IGNORECASE)

_TXN_RE = re.compile(
    r'^(?P<date>\d{2}-[A-Za-z]{3}-\d{4})\s+'
    r'(?P<desc>.+?)\s+'
    r'(?P<amount>-?[\d,]+\.\d{2})\s+'
    r'(?P<units>-?[\d,]+\.\d{3,4})\s+'
    r'(?P<nav>[\d,]+\.\d{2,4})\s+'
    r'(?P<balance>-?[\d,]+\.\d{3,4})\s*$'
)


def _num(s: str) -> float:
    return float(s.replace(",", ""))


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%d-%b-%Y").date()


def classify_row(description: str, units: float) -> str:
    """ACQUISITION / DISPOSAL / NEITHER for one transaction row.

    A zero-unit row (dividend/IDCW payout, stamp duty, STT, other charges)
    is always NEITHER. Otherwise the description's keywords decide when they
    agree with the sign of the unit change; a positive-unit row with no
    disposal keyword, or a negative-unit row with no acquisition keyword,
    falls back to sign alone (covers unlabelled purchase/redemption rows) --
    never guessed beyond that: FIFO derivation flags anything it cannot
    attribute, it never invents an acquisition/disposal split here.
    """
    if units == 0:
        return NEITHER
    desc_l = description.lower()
    if units < 0 and any(k in desc_l for k in _DISPOSAL_KEYWORDS):
        return DISPOSAL
    if units > 0 and any(k in desc_l for k in _ACQUISITION_KEYWORDS):
        return ACQUISITION
    return ACQUISITION if units > 0 else DISPOSAL


@dataclass
class TxnRow:
    date: date
    description: str
    amount: float
    units: float
    nav: float
    balance: float
    kind: str  # ACQUISITION / DISPOSAL / NEITHER


@dataclass
class SchemeBlock:
    folio: str
    amc: str
    rta: str
    scheme_name: str
    isin: str
    scheme_type: str  # "" if the statement doesn't state it
    opening_units: float | None
    closing_units: float | None
    transactions: list[TxnRow] = field(default_factory=list)
    rta_realised_gain: float | None = None


def parse_cas_text(lines: list[str]) -> list[SchemeBlock]:
    """Pure parse: plain text lines -> a list of SchemeBlock. No I/O."""
    blocks: list[SchemeBlock] = []
    current: SchemeBlock | None = None
    pending_amc = ""
    pending_rta = ""

    for line in lines:
        line = line.strip()
        if not line:
            continue

        m = _FOLIO_RE.match(line)
        if m:
            current = SchemeBlock(
                folio=m.group(1), amc="", rta="", scheme_name="", isin="",
                scheme_type="", opening_units=None, closing_units=None,
            )
            blocks.append(current)
            continue

        m = _AMC_RE.match(line)
        if m:
            pending_amc = m.group(1)
            if current is not None:
                current.amc = pending_amc
            continue

        m = _SCHEME_RE.match(line)
        if m and current is not None:
            current.scheme_name = m.group("name")
            current.isin = m.group("isin")
            continue

        m = _REGISTRAR_RE.match(line)
        if m:
            pending_rta = m.group(1)
            if current is not None:
                current.rta = pending_rta
            continue

        m = _SCHEME_TYPE_RE.match(line)
        if m and current is not None:
            current.scheme_type = m.group(1).upper()
            continue

        m = _OPENING_RE.match(line)
        if m and current is not None:
            current.opening_units = _num(m.group(1))
            continue

        m = _CLOSING_RE.match(line)
        if m and current is not None:
            current.closing_units = _num(m.group(1))
            continue

        m = _RTA_GAIN_RE.match(line)
        if m and current is not None:
            current.rta_realised_gain = _num(m.group(1))
            continue

        m = _TXN_RE.match(line)
        if m and current is not None:
            units = _num(m.group("units"))
            desc = m.group("desc").strip()
            current.transactions.append(TxnRow(
                date=_parse_date(m.group("date")),
                description=desc,
                amount=_num(m.group("amount")),
                units=units,
                nav=_num(m.group("nav")),
                balance=_num(m.group("balance")),
                kind=classify_row(desc, units),
            ))
            continue
        # Anything else (including any PAN/name/address/contact lines a real
        # statement may carry) is intentionally unmatched and dropped -- see
        # module docstring.

    return blocks
