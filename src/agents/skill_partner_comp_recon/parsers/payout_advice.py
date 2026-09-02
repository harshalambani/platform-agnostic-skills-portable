"""
payout_advice.py -- L1 parser: the firm's monthly partner payout
certificate (a one-page, password-protected PDF headed "To Whomsoever It
may concern", body a two-column "Particulars" / "AMOUNTS" table).

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
# Pure core.
# ---------------------------------------------------------------------------

def parse_l1_text(text: str, source_name: str = "") -> dict:
    """PURE: takes one L1 document's extracted page text and returns the
    record dict (see L1PayoutRecord.to_dict()). Raises NotAnL1DocumentError
    if `text` is not an L1 payout certificate -- callers walking a
    directory of mixed documents should catch that and skip the file.
    """
    cleaned = _FFFD_RUN_RE.sub(" ", text)

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
