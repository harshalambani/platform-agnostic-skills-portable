"""
payout_advice.py -- L1 parser: the firm's monthly partner payout
certificate (a one-page, password-protected PDF headed "To Whomsoever It
may concern", body a two-column "Particulars" / "AMOUNTS" table).

The firm replaced this document entirely from 1 April 2025 onward with a
differently-shaped "PAYOUT STATEMENT FOR <MONTH> <YYYY>" form (Class B,
below) that carries a rich header block and, in months with adjustments, a
second sub-table. Class A (the original "To Whomsoever It may concern"
certificate) remains the only form for FY2024-25 and earlier and is
unchanged. `parse_l1_text()` dispatches on content to the right body
parser; a document matching neither still raises `NotAnL1DocumentError`.

Class B design notes:

  - Class B extracts cleanly (single column, plain extract_text(), no
    digit-splitting, no parenthesised negatives) -- negatives are a plain
    leading minus ("-30,000"), which `_parse_amount()` already handles.
  - MAP BY LABEL, NEVER ROW POSITION, same as Class A. `Additional Share
    of profit` is checked before `Share of profit` so the more specific
    label wins. `TDS on Rem/IOC` and `TDS on Remuneration` are the SAME
    field (`tds`) under two spellings seen in different months; the
    verbatim spelling is retained as `tds_label` because it is the only
    in-document signal for whether the TDS line nets out an interest
    withholding folded into `additional_share_of_profit` (see the real
    relationships this module deliberately does NOT derive, below).
  - `Miscellaneous Adjustments Amount in INR` is a SUB-TABLE HEADER, not a
    second main-table row -- it must be checked before the main-table
    `Miscellaneous Adjustments` row pattern (which would otherwise also
    match it) so the header line is never miscounted as a row.
  - The sub-table's `Net Pay` is its own total, not the payout -- it is
    cross-checked against the main table's `misc_adjustments` figure
    (diagnostic on mismatch, never a raise).
  - Two real relationships are intentionally NOT derived here, only
    recorded raw for the caller to reconcile: (1) Class B `share_of_profit`
    is stated net of the firm's tax (the annual schedule states it gross);
    (2) `additional_share_of_profit` is either a prior-year incentive
    instalment net of firm's tax, or, in the FY's final month, interest on
    capital net of its own TDS -- the two are never told apart here.
  - Header metadata is a sequence of label-line/value-line pairs. Two of
    them need anchored extraction rather than a naive whitespace split:
    the entity row (multi-word entity name before two trailing
    DD-MM-YYYY dates) and the employee row (multi-word partner name
    between a leading numeric id and a trailing "...@..." email).

Kept in two layers, mirroring skill_mf_cas/parser.py's split:

  - `parse_l1_text()` is PURE: it takes already-extracted page text (a
    str) and returns a plain dict. No filesystem, no pdfplumber, no
    password. Every test in tests/test_skill_partner_comp_recon.py drives
    this function directly against synthetic text blocks -- no encrypted
    PDF fixture is ever generated or committed.
  - `parse(path, password)` is the thin shell: opens the PDF with
    pdfplumber (password-aware), extracts page 1's text, and calls
    `parse_l1_text()`. This is the only function in this module that
    touches the filesystem.

Design notes carried over from parsers/__init__.py's module docstring
(read that first) -- restated here because every one of them is enforced
in this file:

  - MAP BY LABEL, NEVER ROW POSITION. An absent label means that figure is
    ABSENT this month -- never coerced to zero. Modelled explicitly as a
    field that is `None` when the label was not found.
  - THE ROW SET CHANGES BETWEEN YEARS. "TDS on Remuneration" (s.194T)
    exists only from FY2025-26 onward; a document without that row is
    normal, not a parse failure.
  - DISPATCH ON CONTENT, NEVER FILENAME. A different document family (a
    payroll salary statement, "SALARY STATEMENT FOR") can sit in the same
    directory as these certificates. This module identifies an L1
    document by its heading ("To Whomsoever It may concern") together
    with the "Particulars" table header, case-insensitively -- observed
    casing is inconsistent between years. Anything else raises
    `NotAnL1DocumentError` so a directory-walking caller can skip it
    cleanly instead of crashing or misparsing it as a payout certificate.
  - NEGATIVES ARE PARENTHESISED: "(30,000)" -> -30000. Thousands
    separators are commas. NEVER `abs()` an amount anywhere in this
    module -- a parenthesised figure stays negative all the way through.
  - "#N/A" IS A TEMPLATE ARTEFACT, NEVER A VALUE. An earlier-year template
    leaks a literal "#N/A" where the amount-in-words line later sits (and
    it can equally appear in place of a row's amount); it is skipped
    outright, never parsed as zero or NaN.
  - THE AMOUNT-IN-WORDS LINE CARRIES PAISE; TOTAL IS ROUNDED. When a
    words line is present, cross-check it against Total to +/- 1.00 --
    never exact equality.
  - NEVER READ "Misc Adjustments" INTO THE MODEL. It is parsed and kept as
    `misc_printed` for reconciliation (engine.derive_misc() computes the
    figure this skill actually uses), but it is intentionally absent from
    the model's *input* side.
  - Non-Total label rows (Remuneration, Share of Profit, Add. Share of
    Profit, TDS on Remuneration when present, Misc Adjustments) are
    asserted to sum to Total. A mismatch never raises and never silently
    passes -- it becomes an "ERROR: ..." diagnostic carried on the
    returned record, mirroring this codebase's fail-loud convention (see
    agent.py's `_require`).
  - Each record is keyed off the Mon-YY parsed from the document BODY,
    never off the filename -- the same month can arrive under a different
    filename in a different folder.
  - pdfplumber can render box-drawing rules and currency glyphs as
    U+FFFD; runs of U+FFFD are stripped as separators, and every
    non-digit/non-sign/non-dot/non-comma character is stripped before a
    number is parsed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

# ---------------------------------------------------------------------------
# Content-dispatch markers.
# ---------------------------------------------------------------------------

_L1_HEADING_RE = re.compile(r"to\s+whomsoever\s+it\s+may\s+concern", re.IGNORECASE)
_L1_TABLE_RE = re.compile(r"particulars", re.IGNORECASE)
_NON_L1_SALARY_RE = re.compile(r"salary\s+statement\s+for", re.IGNORECASE)


class NotAnL1DocumentError(ValueError):
    """Raised by parse_l1_text()/parse() when the supplied text/PDF is not
    an L1 monthly payout certificate. A directory-walking caller should
    catch this and skip the file cleanly -- never crash, never misparse
    it as a payout certificate."""


# ---------------------------------------------------------------------------
# Row labels -> model field. Order matters only in that more specific
# labels are listed first; each pattern is anchored to the start of a
# line so "TDS on Remuneration" can never be mistaken for "Remuneration".
# ---------------------------------------------------------------------------

_ROW_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("tds", re.compile(r"^\s*tds\s+on\s+remuneration\b", re.IGNORECASE)),
    ("additional_share_of_profit", re.compile(r"^\s*add\.?\s+share\s+of\s+profit\b", re.IGNORECASE)),
    ("share_of_profit_gross", re.compile(r"^\s*share\s+of\s+profit\b", re.IGNORECASE)),
    ("remuneration", re.compile(r"^\s*remuneration\b", re.IGNORECASE)),
    ("misc_printed", re.compile(r"^\s*misc\.?\s+adjustments?\b", re.IGNORECASE)),
    ("total_paid", re.compile(r"^\s*total\b", re.IGNORECASE)),
]

_MODEL_FIELDS = [f for f, _ in _ROW_PATTERNS]

# ---------------------------------------------------------------------------
# Month / date patterns.
# ---------------------------------------------------------------------------

_MONTH_ABBR = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# "for the month of Jan-26" -- the preferred, explicit source of the body month.
_MONTH_PHRASE_RE = re.compile(
    r"for\s+the\s+month\s+of\s+([A-Za-z]{3})-(\d{2})\b", re.IGNORECASE
)
# A bare "Mon-YY" token anywhere in the body (fallback).
_MONTH_YY_RE = re.compile(r"\b([A-Za-z]{3})-(\d{2})\b")
# "DD-Mon-YY" -- the issue date, which is NOT the payout month.
_ISSUE_DATE_RE = re.compile(r"\b(\d{1,2})-([A-Za-z]{3})-(\d{2})\b")

# A line naming the amount-in-words figure.
_WORDS_LINE_RE = re.compile(r"\bwords\b", re.IGNORECASE)
_NUMBER_TOKEN_RE = re.compile(r"\(?-?[0-9][0-9,]*\.?[0-9]*\)?")

# U+FFFD runs (box-drawing rules / currency glyphs pdfplumber cannot map).
_FFFD_RUN_RE = re.compile("�+")
# Everything that is not part of a signed decimal number.
_NUM_STRIP_RE = re.compile(r"[^0-9+\-.]")

_NA_TOKEN_RE = re.compile(r"#\s*N/?A", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Record shape.
# ---------------------------------------------------------------------------

@dataclass
class L1PayoutRecord:
    month: str | None                      # canonical "YYYY-MM", from the BODY
    period_label: str | None               # raw "Mon-YY" as printed
    issue_date: str | None                 # raw "DD-Mon-YY" as printed (never the payout month)
    remuneration: float | None = None
    share_of_profit_gross: float | None = None
    additional_share_of_profit: float | None = None
    tds: float | None = None               # absent (None) pre-FY2025-26; never coerced to 0
    total_paid: float | None = None
    misc_printed: float | None = None      # CHECK figure only -- never a model input
    amount_in_words_value: float | None = None
    source_name: str = ""
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "month": self.month,
            "period_label": self.period_label,
            "issue_date": self.issue_date,
            "remuneration": self.remuneration,
            "share_of_profit_gross": self.share_of_profit_gross,
            "additional_share_of_profit": self.additional_share_of_profit,
            "tds": self.tds,
            "total_paid": self.total_paid,
            "misc_printed": self.misc_printed,
            "amount_in_words_value": self.amount_in_words_value,
            "source_name": self.source_name,
            "diagnostics": list(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# Amount parsing.
# ---------------------------------------------------------------------------

def _parse_amount(raw: str) -> float | None:
    """"(30,000)" -> -30000.0; "1,20,000.45" -> 120000.45; "#N/A" -> None
    (template artefact, never a value); "" / no digits -> None (absent).
    Never abs()es a negative -- a parenthesised figure stays negative.
    """
    if raw is None:
        return None
    text = _FFFD_RUN_RE.sub(" ", raw)
    if _NA_TOKEN_RE.search(text):
        return None
    negative = "(" in text and ")" in text
    cleaned = _NUM_STRIP_RE.sub("", text.replace(",", ""))
    cleaned = cleaned.strip("+")
    if not cleaned or cleaned in ("-", "."):
        return None
    try:
        value = float(cleaned)
    except ValueError:
        return None
    if negative:
        value = -value
    return value


def _extract_last_number(line: str) -> float | None:
    text = _FFFD_RUN_RE.sub(" ", line)
    if _NA_TOKEN_RE.search(text):
        return None
    matches = _NUMBER_TOKEN_RE.findall(text)
    if not matches:
        return None
    return _parse_amount(matches[-1])


# ---------------------------------------------------------------------------
# Month / issue-date extraction.
# ---------------------------------------------------------------------------

def _extract_month(text: str) -> tuple[str | None, str | None]:
    """Returns (canonical "YYYY-MM", raw "Mon-YY" label) parsed from the
    BODY -- never the filename. Prefers the explicit "for the month of
    Mon-YY" phrase; falls back to the first bare "Mon-YY" token that is
    not part of a "DD-Mon-YY" issue-date match.
    """
    phrase = _MONTH_PHRASE_RE.search(text)
    if phrase:
        abbr, yy = phrase.group(1), phrase.group(2)
        return _canonical_month(abbr, yy), f"{abbr.title()}-{yy}"

    issue_spans = [m.span() for m in _ISSUE_DATE_RE.finditer(text)]

    def _within_issue_date(span: tuple[int, int]) -> bool:
        return any(s <= span[0] and span[1] <= e for s, e in issue_spans)

    for m in _MONTH_YY_RE.finditer(text):
        if _within_issue_date(m.span()):
            continue
        abbr, yy = m.group(1), m.group(2)
        if abbr.lower() not in _MONTH_ABBR:
            continue
        return _canonical_month(abbr, yy), f"{abbr.title()}-{yy}"
    return None, None


def _canonical_month(abbr: str, yy: str) -> str | None:
    month_num = _MONTH_ABBR.get(abbr.lower())
    if month_num is None:
        return None
    # 2-digit year: 00-68 -> 2000s, 69-99 -> 1900s (Python's own convention);
    # in practice these documents are always 20xx.
    year = 2000 + int(yy) if int(yy) < 69 else 1900 + int(yy)
    return f"{year:04d}-{month_num:02d}"


def _extract_issue_date(text: str) -> str | None:
    m = _ISSUE_DATE_RE.search(text)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2).title()}-{m.group(3)}"


# ---------------------------------------------------------------------------
# Class B ("PAYOUT STATEMENT FOR <MONTH> <YYYY>") -- content-dispatch
# marker, row labels, header labels and record shape.
# ---------------------------------------------------------------------------

_CLASS_B_TITLE_RE = re.compile(r"^payout\s+statement\s+for\s+([A-Za-z]+)\s+(\d{4})$", re.IGNORECASE)

# Main-table row labels -> model field. "additional_share_of_profit" is
# listed before "share_of_profit" so the more specific label wins (same
# ordering hazard the module docstring documents for Class A). The two TDS
# spellings map to the SAME field; the matching label text is recorded
# verbatim as tds_label by the caller.
_CLASS_B_ADDITIONAL_RE = re.compile(r"^\s*additional\s+share\s+of\s+profit\b", re.IGNORECASE)
_CLASS_B_SHARE_RE = re.compile(r"^\s*share\s+of\s+profit\b", re.IGNORECASE)
_CLASS_B_TDS_REMIOC_RE = re.compile(r"^\s*tds\s+on\s+rem\s*/\s*ioc\b", re.IGNORECASE)
_CLASS_B_TDS_REM_RE = re.compile(r"^\s*tds\s+on\s+remuneration\b", re.IGNORECASE)
_CLASS_B_MISC_RE = re.compile(r"^\s*miscellaneous\s+adjustments\b", re.IGNORECASE)
_CLASS_B_REM_RE = re.compile(r"^\s*remuneration\b", re.IGNORECASE)
_CLASS_B_TOTAL_RE = re.compile(r"^\s*total\b", re.IGNORECASE)

_CLASS_B_ROW_SPECS: list[tuple[str, "re.Pattern[str]", str | None]] = [
    ("additional_share_of_profit", _CLASS_B_ADDITIONAL_RE, None),
    ("share_of_profit", _CLASS_B_SHARE_RE, None),
    ("tds", _CLASS_B_TDS_REMIOC_RE, "TDS on Rem/IOC"),
    ("tds", _CLASS_B_TDS_REM_RE, "TDS on Remuneration"),
    ("misc_adjustments", _CLASS_B_MISC_RE, None),
    ("remuneration", _CLASS_B_REM_RE, None),
    ("total", _CLASS_B_TOTAL_RE, None),
]

# The sub-table HEADER line -- must be checked before _CLASS_B_MISC_RE
# above, which would otherwise also match it (the trap: "Miscellaneous
# Adjustments" appears once as a main-table row with a figure, once as
# this header line followed by "Amount in INR").
_CLASS_B_SUBTABLE_HEADER_RE = re.compile(
    r"^\s*miscellaneous\s+adjustments\s+amount\s+in\s+inr\s*$", re.IGNORECASE
)

_CLASS_B_SUB_ROW_SPECS: list[tuple[str, "re.Pattern[str]"]] = [
    ("medical_topup", re.compile(r"^\s*medical\s+topup\b", re.IGNORECASE)),
    ("transferred_to_capital", re.compile(r"^\s*transferred\s+to\s+capital\b", re.IGNORECASE)),
    ("net_pay", re.compile(r"^\s*net\s+pay\b", re.IGNORECASE)),
]

# The amount-in-words line ("Rupees ... Only.") -- ignored, never a row.
_CLASS_B_WORDS_LINE_RE = re.compile(r"^\s*rupees\b", re.IGNORECASE)

# Header label-line / value-line pairs.
_HDR_EMP_LABEL_RE = re.compile(r"^\s*employee\s+id\s+name\s+email\s*$", re.IGNORECASE)
_HDR_DESIG_LABEL_RE = re.compile(r"^\s*designation\s+location\s+function\s*$", re.IGNORECASE)
_HDR_ENTITY_LABEL_RE = re.compile(
    r"^\s*entity\s+date\s+of\s+joining\s+doj\s+as\s+partner\s*$", re.IGNORECASE
)
_HDR_BANK_LABEL_RE = re.compile(r"^\s*bank\s+account\s+number\s*$", re.IGNORECASE)

# "40199 A. N. Other another@example.com" -- anchor the leading numeric id
# and the trailing "...@..." token; the multi-word name is the remainder.
_HDR_EMP_VALUE_RE = re.compile(r"^\s*(\d+)\s+(.*?)\s+(\S+@\S+)\s*$")
# "Meridian Consulting Services LLP 28-09-2020 01-04-2022" -- anchor the
# two trailing DD-MM-YYYY dates; the multi-word entity name is the
# remainder. A naive whitespace split is wrong here (Requirement 6).
_HDR_ENTITY_VALUE_RE = re.compile(r"^\s*(.*?)\s+(\d{2}-\d{2}-\d{4})\s+(\d{2}-\d{2}-\d{4})\s*$")

# "Please find below payout details for the month of April '25" -- a
# cross-check against the title line's month/four-digit year.
_HDR_MONTH_CROSSCHECK_RE = re.compile(
    r"please\s+find\s+below\s+payout\s+details\s+for\s+the\s+month\s+of\s+"
    r"([A-Za-z]+)\s*'\s*(\d{2})",
    re.IGNORECASE,
)


@dataclass
class L1ClassBMiscBreakdown:
    """Sub-record for the 'Miscellaneous Adjustments Amount in INR' block
    -- present only in months that carry adjustments. `net_pay` here is
    the SUB-TABLE's own total, not the month's payout; it is cross-checked
    against the main table's `misc_adjustments` figure (diagnostic on
    mismatch, never a raise)."""
    medical_topup: float | None = None
    transferred_to_capital: float | None = None
    net_pay: float | None = None

    def to_dict(self) -> dict:
        return {
            "medical_topup": self.medical_topup,
            "transferred_to_capital": self.transferred_to_capital,
            "net_pay": self.net_pay,
        }


@dataclass
class L1ClassBRecord:
    """Record for the post-1-April-2025 'PAYOUT STATEMENT FOR ...' form.
    Deliberately does NOT derive: (1) whether `share_of_profit` is gross
    or net (it is always net of firm's tax in this form -- the caller
    reconciles against the annual schedule, which states it gross); (2)
    whether `additional_share_of_profit` is a prior-year incentive
    instalment or year-end interest on capital -- `tds_label` is the only
    in-document signal and is retained verbatim for the caller to use."""
    month: str | None                      # from the title line, e.g. "April"
    year: int | None                       # from the title line, four digits
    employee_id: str | None = None
    partner_name: str | None = None
    email: str | None = None
    designation: str | None = None
    location: str | None = None
    function: str | None = None
    entity_name: str | None = None
    date_of_joining: str | None = None
    doj_as_partner: str | None = None
    bank_name: str | None = None
    bank_account_number: str | None = None
    remuneration: float | None = None
    share_of_profit: float | None = None
    additional_share_of_profit: float | None = None
    tds: float | None = None
    tds_label: str | None = None           # verbatim: "TDS on Rem/IOC" or "TDS on Remuneration"
    misc_adjustments: float | None = None
    total: float | None = None
    misc_breakdown: L1ClassBMiscBreakdown | None = None
    source_name: str = ""
    unknown_labels: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "doc_class": "B",
            "month": self.month,
            "year": self.year,
            "employee_id": self.employee_id,
            "partner_name": self.partner_name,
            "email": self.email,
            "designation": self.designation,
            "location": self.location,
            "function": self.function,
            "entity_name": self.entity_name,
            "date_of_joining": self.date_of_joining,
            "doj_as_partner": self.doj_as_partner,
            "bank_name": self.bank_name,
            "bank_account_number": self.bank_account_number,
            "remuneration": self.remuneration,
            "share_of_profit": self.share_of_profit,
            "additional_share_of_profit": self.additional_share_of_profit,
            "tds": self.tds,
            "tds_label": self.tds_label,
            "misc_adjustments": self.misc_adjustments,
            "total": self.total,
            "misc_breakdown": self.misc_breakdown.to_dict() if self.misc_breakdown else None,
            "source_name": self.source_name,
            "unknown_labels": list(self.unknown_labels),
            "diagnostics": list(self.diagnostics),
        }


def _next_nonempty(lines: list[str], start: int) -> tuple[str | None, int]:
    """Returns (stripped text, index) of the first non-blank line at or
    after `start`, or (None, len(lines) - 1) if there is none."""
    for idx in range(start, len(lines)):
        if lines[idx].strip():
            return lines[idx].strip(), idx
    return None, max(len(lines) - 1, start)


def _parse_class_b_header(
    lines: list[str], start_idx: int
) -> tuple[dict, int, tuple[str, str] | None]:
    """Scans the Class B header block (label-line/value-line pairs)
    starting at `start_idx` (the line after the title). Stops at the
    "Particulars" table header line (matched the same way as Class A's
    table marker) and returns (header fields, index of the first
    main-table row, optional (month, two-digit-year) cross-check tuple
    parsed from the "Please find below..." line).
    """
    header = {
        "employee_id": None, "partner_name": None, "email": None,
        "designation": None, "location": None, "function": None,
        "entity_name": None, "date_of_joining": None, "doj_as_partner": None,
        "bank_name": None, "bank_account_number": None,
    }
    crosscheck: tuple[str, str] | None = None
    table_start_idx = len(lines)
    i = start_idx
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if _L1_TABLE_RE.search(stripped):
            table_start_idx = i + 1
            break
        cc = _HDR_MONTH_CROSSCHECK_RE.search(stripped)
        if cc:
            crosscheck = (cc.group(1), cc.group(2))
            i += 1
            continue
        if _HDR_EMP_LABEL_RE.match(stripped):
            value_line, next_i = _next_nonempty(lines, i + 1)
            if value_line:
                m = _HDR_EMP_VALUE_RE.match(value_line)
                if m:
                    header["employee_id"] = m.group(1)
                    header["partner_name"] = m.group(2).strip()
                    header["email"] = m.group(3)
            i = next_i + 1
            continue
        if _HDR_DESIG_LABEL_RE.match(stripped):
            value_line, next_i = _next_nonempty(lines, i + 1)
            if value_line:
                tokens = value_line.split()
                if len(tokens) >= 1:
                    header["designation"] = tokens[0]
                if len(tokens) >= 2:
                    header["location"] = tokens[1]
                if len(tokens) > 2:
                    header["function"] = " ".join(tokens[2:])
            i = next_i + 1
            continue
        if _HDR_ENTITY_LABEL_RE.match(stripped):
            value_line, next_i = _next_nonempty(lines, i + 1)
            if value_line:
                m = _HDR_ENTITY_VALUE_RE.match(value_line)
                if m:
                    header["entity_name"] = m.group(1).strip()
                    header["date_of_joining"] = m.group(2)
                    header["doj_as_partner"] = m.group(3)
            i = next_i + 1
            continue
        if _HDR_BANK_LABEL_RE.match(stripped):
            value_line, next_i = _next_nonempty(lines, i + 1)
            if value_line:
                tokens = value_line.split()
                if len(tokens) == 1:
                    header["bank_account_number"] = tokens[0]
                elif len(tokens) > 1:
                    header["bank_account_number"] = tokens[-1]
                    header["bank_name"] = " ".join(tokens[:-1])
            i = next_i + 1
            continue
        i += 1
    return header, table_start_idx, crosscheck


def _parse_class_b_table(
    lines: list[str], start_idx: int
) -> tuple[dict[str, float | None], str | None, list[str], int | None]:
    """Parses the Class B main table starting at `start_idx` (the line
    after the "Particulars" header). Stops -- without consuming it into
    the main table -- at the sub-table header line, if present. Returns
    (values-by-field, tds_label, unknown_labels, sub-table start index or
    None if no sub-table is present).
    """
    values: dict[str, float | None] = {
        f: None for f in
        ("remuneration", "share_of_profit", "additional_share_of_profit", "tds",
         "misc_adjustments", "total")
    }
    tds_label: str | None = None
    unknown_labels: list[str] = []
    subtable_start_idx: int | None = None
    i = start_idx
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if _CLASS_B_SUBTABLE_HEADER_RE.match(stripped):
            subtable_start_idx = i + 1
            break
        if _CLASS_B_WORDS_LINE_RE.match(stripped):
            i += 1
            continue
        matched = False
        for field_name, pattern, label_text in _CLASS_B_ROW_SPECS:
            m = pattern.match(stripped)
            if not m:
                continue
            matched = True
            rest = stripped[m.end():].strip()
            if not rest:
                for lookahead in lines[i + 1: i + 3]:
                    if lookahead.strip():
                        rest = lookahead.strip()
                        break
            values[field_name] = _parse_amount(rest)
            if field_name == "tds" and label_text:
                tds_label = label_text
            break
        if not matched:
            unknown_labels.append(stripped)
        i += 1
    return values, tds_label, unknown_labels, subtable_start_idx


def _parse_class_b_subtable(
    lines: list[str], start_idx: int
) -> tuple[dict[str, float | None], list[str]]:
    """Parses the 'Miscellaneous Adjustments Amount in INR' sub-table
    starting at `start_idx` (the line after its header)."""
    values: dict[str, float | None] = {
        "medical_topup": None, "transferred_to_capital": None, "net_pay": None
    }
    unknown_labels: list[str] = []
    i = start_idx
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not stripped:
            i += 1
            continue
        if _CLASS_B_WORDS_LINE_RE.match(stripped):
            i += 1
            continue
        matched = False
        for field_name, pattern in _CLASS_B_SUB_ROW_SPECS:
            m = pattern.match(stripped)
            if not m:
                continue
            matched = True
            rest = stripped[m.end():].strip()
            if not rest:
                for lookahead in lines[i + 1: i + 3]:
                    if lookahead.strip():
                        rest = lookahead.strip()
                        break
            values[field_name] = _parse_amount(rest)
            break
        if not matched:
            unknown_labels.append(stripped)
        i += 1
    return values, unknown_labels


def _parse_class_b_text(cleaned: str, title_match: "re.Match[str]", source_name: str) -> dict:
    """PURE Class B body parser -- see module docstring for the class's
    design notes. Never raises; diagnostics only."""
    lines = cleaned.splitlines()
    month_title = title_match.group(1).strip().title()
    year_title = int(title_match.group(2))

    first_idx = next((idx for idx, ln in enumerate(lines) if ln.strip()), 0)
    header, table_start_idx, crosscheck = _parse_class_b_header(lines, first_idx + 1)

    values, tds_label, main_unknown, subtable_start_idx = _parse_class_b_table(
        lines, table_start_idx
    )

    misc_breakdown: L1ClassBMiscBreakdown | None = None
    sub_unknown: list[str] = []
    if subtable_start_idx is not None:
        sub_values, sub_unknown = _parse_class_b_subtable(lines, subtable_start_idx)
        misc_breakdown = L1ClassBMiscBreakdown(**sub_values)

    diagnostics: list[str] = []

    if crosscheck is not None:
        cc_month, cc_yy = crosscheck
        cc_year_full = 2000 + int(cc_yy)
        if cc_month.strip().lower() != month_title.lower() or cc_year_full != year_title:
            diagnostics.append(
                "ERROR: Class B title line month/year "
                f"({month_title} {year_title}) disagrees with the body line "
                f"({cc_month} '{cc_yy} -> {cc_year_full})."
            )

    row_fields = ["remuneration", "share_of_profit", "additional_share_of_profit", "tds", "misc_adjustments"]
    present = [(f, values[f]) for f in row_fields if values[f] is not None]
    total = values["total"]
    if present and total is not None:
        computed = sum(v for _, v in present)
        if abs(computed - total) > 0.01:
            diagnostics.append(
                "ERROR: Class B rows "
                f"({', '.join(f'{f}={v}' for f, v in present)}) sum to {computed} "
                f"but the printed Total is {total} (diff {computed - total})."
            )
    elif total is None:
        diagnostics.append("ERROR: Class B document has no 'Total' row -- cannot verify the row sum.")

    if (
        misc_breakdown is not None
        and misc_breakdown.net_pay is not None
        and values["misc_adjustments"] is not None
        and abs(misc_breakdown.net_pay - values["misc_adjustments"]) > 0.01
    ):
        diagnostics.append(
            "ERROR: Miscellaneous Adjustments sub-table Net Pay "
            f"({misc_breakdown.net_pay}) does not equal the main table's "
            f"Miscellaneous Adjustments figure ({values['misc_adjustments']}) "
            f"(diff {misc_breakdown.net_pay - values['misc_adjustments']})."
        )

    record = L1ClassBRecord(
        month=month_title,
        year=year_title,
        employee_id=header["employee_id"],
        partner_name=header["partner_name"],
        email=header["email"],
        designation=header["designation"],
        location=header["location"],
        function=header["function"],
        entity_name=header["entity_name"],
        date_of_joining=header["date_of_joining"],
        doj_as_partner=header["doj_as_partner"],
        bank_name=header["bank_name"],
        bank_account_number=header["bank_account_number"],
        remuneration=values["remuneration"],
        share_of_profit=values["share_of_profit"],
        additional_share_of_profit=values["additional_share_of_profit"],
        tds=values["tds"],
        tds_label=tds_label,
        misc_adjustments=values["misc_adjustments"],
        total=total,
        misc_breakdown=misc_breakdown,
        source_name=source_name,
        unknown_labels=[*main_unknown, *sub_unknown],
        diagnostics=diagnostics,
    )
    return record.to_dict()


# ---------------------------------------------------------------------------
# Pure core.
# ---------------------------------------------------------------------------

def parse_l1_text(text: str, source_name: str = "") -> dict:
    """PURE: takes one L1 document's extracted page text and returns the
    record dict (see L1PayoutRecord.to_dict()). Raises NotAnL1DocumentError
    if `text` is not an L1 payout certificate -- callers walking a
    directory of mixed documents should catch that and skip the file.
    """
    cleaned = _FFFD_RUN_RE.sub(" ", text)

    first_nonblank = next((ln.strip() for ln in cleaned.splitlines() if ln.strip()), "")
    class_b_match = _CLASS_B_TITLE_RE.match(first_nonblank)
    if class_b_match:
        return _parse_class_b_text(cleaned, class_b_match, source_name)

    if not (_L1_HEADING_RE.search(cleaned) and _L1_TABLE_RE.search(cleaned)):
        if _NON_L1_SALARY_RE.search(cleaned):
            raise NotAnL1DocumentError(
                f"{source_name or '<text>'}: looks like a salary statement "
                "(\"SALARY STATEMENT FOR\"), not an L1 payout certificate -- skipped."
            )
        raise NotAnL1DocumentError(
            f"{source_name or '<text>'}: missing the L1 heading (\"To Whomsoever It "
            "may concern\") and/or the \"Particulars\" table header -- not an L1 "
            "payout certificate, skipped."
        )

    month, period_label = _extract_month(cleaned)
    issue_date = _extract_issue_date(cleaned)

    values: dict[str, float | None] = {f: None for f in _MODEL_FIELDS}
    lines = cleaned.splitlines()
    for idx, line in enumerate(lines):
        for field_name, pattern in _ROW_PATTERNS:
            m = pattern.match(line)
            if not m:
                continue
            rest = line[m.end():].strip()
            if not rest:
                # Amount may be on the following non-empty line.
                for lookahead in lines[idx + 1: idx + 3]:
                    if lookahead.strip():
                        rest = lookahead.strip()
                        break
            values[field_name] = _parse_amount(rest)
            break  # first matching pattern wins this line

    amount_in_words_value = None
    for line in lines:
        if _WORDS_LINE_RE.search(line) and not _NA_TOKEN_RE.search(line):
            amount_in_words_value = _extract_last_number(line)
            if amount_in_words_value is not None:
                break

    diagnostics: list[str] = []

    row_fields = [f for f in _MODEL_FIELDS if f != "total_paid"]
    present = [(f, values[f]) for f in row_fields if values[f] is not None]
    total_paid = values["total_paid"]
    if present and total_paid is not None:
        computed = sum(v for _, v in present)
        if abs(computed - total_paid) > 0.01:
            diagnostics.append(
                "ERROR: L1 rows "
                f"({', '.join(f'{f}={v}' for f, v in present)}) sum to {computed} "
                f"but the printed Total is {total_paid} (diff {computed - total_paid})."
            )
    elif total_paid is None:
        diagnostics.append("ERROR: L1 document has no 'Total' row -- cannot verify the row sum.")

    if amount_in_words_value is not None and total_paid is not None:
        if abs(amount_in_words_value - total_paid) > 1.00:
            diagnostics.append(
                "ERROR: amount-in-words figure "
                f"({amount_in_words_value}) differs from the printed Total "
                f"({total_paid}) by more than +/-1.00 (diff "
                f"{amount_in_words_value - total_paid})."
            )

    record = L1PayoutRecord(
        month=month,
        period_label=period_label,
        issue_date=issue_date,
        remuneration=values["remuneration"],
        share_of_profit_gross=values["share_of_profit_gross"],
        additional_share_of_profit=values["additional_share_of_profit"],
        tds=values["tds"],
        total_paid=total_paid,
        misc_printed=values["misc_printed"],
        amount_in_words_value=amount_in_words_value,
        source_name=source_name,
        diagnostics=diagnostics,
    )
    return record.to_dict()


# ---------------------------------------------------------------------------
# Thin PDF-opening shell -- the only function here that touches the
# filesystem / pdfplumber.
# ---------------------------------------------------------------------------

def parse(path: str, password: str | None = None) -> dict:
    """Open the one-page L1 payout-certificate PDF at `path` (password-
    protected; `password` may be None/empty for an unprotected file),
    extract its text, and return the parsed record -- see parse_l1_text().
    Raises NotAnL1DocumentError if the PDF is not an L1 document (e.g. a
    salary statement sharing the same directory).
    """
    with pdfplumber.open(str(path), password=password or "") as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_l1_text(text, source_name=Path(path).name)
