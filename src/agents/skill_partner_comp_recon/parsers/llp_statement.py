"""
llp_statement.py -- L5 parser: the LLP's own annual statement of account for
one partner, as at 31 March of a financial year (a CAPITAL account block --
opening balance, capital introduced during the year, closing balance -- and
a CURRENT account block -- remuneration, interest on capital, profit share,
total additions, drawings, transfer to capital, closing balance). This is
the ANCHOR document for this skill: the only authoritative source for the
year's profit share and both closing balances -- see AGENT.md's Stage 2
section and engine.build_report()'s `external.return_closing_capital` /
`advisory.stated_closing_capital` fields, which this parser's output is
meant to eventually feed.

Kept in the same two layers as payout_advice.py (L1) and advisory.py (L3) --
read those modules' docstrings first, this one mirrors their conventions
throughout:

  - `parse_l5_text()` is PURE: it takes already-extracted page text (a str)
    and returns a plain dict. No filesystem, no pdfplumber, no password.
    Every test in tests/test_skill_partner_comp_recon.py drives this
    function directly against synthetic text blocks -- no real LLP
    statement PDF is ever generated or committed (there are no real
    specimens available to this codebase; see AGENT.md's Stage 2 section
    and CLAUDE.md's privacy constraint).
  - `parse(path, password)` is the thin shell: opens the PDF with
    pdfplumber (password-aware), extracts page text, and calls
    `parse_l5_text()`. This is the only function in this module that
    touches the filesystem.

Document layout (given only as PROSE in the H3.5 task spec this module was
written against -- unlike L1/L3, no real specimen has been read for this
module; see the ASSUMPTIONS paragraph below):

  A CAPITAL account block: opening balance, introduced (capital contributed
  during the year), closing balance.

  A CURRENT account block: remuneration, interest on capital, profit share,
  total additions, drawings, transfer to capital, closing balance.

This module isolates the CAPITAL block from the CURRENT block by scanning
for "CAPITAL ACCOUNT" / "CURRENT ACCOUNT" section-header lines and treating
everything between (and after) them as belonging to that block -- mirroring
advisory.py's `_split_sections`. These two headers, together with an
"as at 31 March YYYY" / "year ended 31 March YYYY" phrase, double as this
module's *content* dispatch markers, distinguishing an L5 statement from an
L1 payout certificate ("To Whomsoever It may concern" / "Particulars"), an
L2 payroll salary statement ("SALARY STATEMENT FOR"), and an L3 Compensation
Advisory (its "PAYMENTS"/"SCHEDULE" headers plus its own "year ended 31
March" phrase).

*** ASSUMPTIONS, NOT VERIFIED FACTS (rule j) ***
No real, de-identified LLP statement specimen has been read for this
module -- the block-header wording ("CAPITAL ACCOUNT" / "CURRENT ACCOUNT",
`_CAPITAL_HEADER_RE` / `_CURRENT_HEADER_RE`), every row-label pattern below
(`_CAPITAL_ROW_PATTERNS` / `_CURRENT_ROW_PATTERNS`), and the FY-phrase
wording (`_FY_PHRASE_RE`, "as at 31 March YYYY" / "year ended 31 March
YYYY") are ASSUMPTIONS transcribed from the task's prose description only.
If a real specimen later shows different header/label/FY-phrase wording,
the fix is to update those constants (and the guard tests pinning them) --
never to loosen the content-dispatch check in `parse_l5_text()` into
something that could misparse an L1/L2/L3 document as an L5 statement, and
never to widen a row pattern so loosely that it could swallow an unrelated
label.

Design rules enforced in this file (every one is mandatory -- see the H3.5
task spec this module was written against):

  - THIS IS THE ANCHOR DOCUMENT. Parse cleanly and expose every figure this
    document prints; never derive, infer, or plug a figure that isn't
    printed on the statement itself.
  - THE REPORTED FINANCIAL YEAR COMES FROM THE DOCUMENT TEXT, never the
    folder name and never the filename -- same convention as L1/L3. This
    module looks only for an "as at 31 March YYYY" or "year ended 31 March
    YYYY" phrase in the body.
  - TWO SEPARATE CLOSING BALANCES. The CAPITAL block and the CURRENT block
    each carry their own "Closing Balance" row; they are parsed from two
    independently-split sections and stored under distinct keys
    (`capital_closing_balance` / `current_closing_balance`) so neither can
    overwrite the other -- the body is split into the two blocks by
    locating the block headers FIRST, before any label is mapped, never by
    mapping labels over the whole document in one pass.
  - "INTEREST ON CAPITAL" APPEARS ONLY IN LATER YEARS. Absent is NOT zero --
    same `None` convention as L1's TDS row and L3's own interest-on-capital
    row. Every field on this record is `float | None`.
  - NEGATIVES ARE PARENTHESISED: "(30,000)" -> -30000. Thousands separators
    are commas, including Indian grouping ("1,20,000"). NEVER `abs()` an
    amount anywhere in this module. Drawings and transfer-to-capital may
    legitimately print as a positive magnitude in one year's template and
    a parenthesised negative in another's -- this module parses each
    exactly as printed and lets the arithmetic-check diagnostics (below)
    surface any sign mismatch; it never "corrects" a sign with `abs()`.
  - MAP BY LABEL, NEVER ROW POSITION. Matched case-insensitively and
    tolerantly (optional "Add."/"Less" prefixes, optional trailing colon),
    with more specific labels listed before general ones so one can never
    swallow another. An unrecognised label-looking line is collected onto
    `unknown_labels`, never silently dropped.
  - pdfplumber can render box-drawing rules and currency glyphs as U+FFFD;
    runs of U+FFFD are stripped as separators, and every non-digit/
    non-sign/non-dot/non-comma character is stripped before a number is
    parsed. These helpers (`_FFFD_RUN_RE`, `_NUMBER_TOKEN_RE`,
    `_parse_amount`) are IMPORTED from payout_advice.py (the L1 parser)
    rather than duplicated.
  - ARITHMETIC CHECKS ARE DIAGNOSTICS, NEVER EXCEPTIONS, NEVER A SILENT
    PLUG (0.01 tolerance). This module checks at least:
      (1) capital: opening_balance + introduced == closing_balance
      (2) current: remuneration + interest_on_capital + profit_share ==
          total_additions
      (3) current: total_additions + drawings + transfer_to_capital ==
          closing_balance
    Each check is skipped (never raised, never guessed) when any of its
    components is None -- and when a check is skipped, a "NOTE: ..."
    diagnostic says so explicitly, naming which component is missing, so
    an absent-but-expected interest-on-capital row (see above) never
    produces a spurious "ERROR: ..." and never passes silently either. A
    genuine mismatch (all components present, sums disagree) becomes an
    "ERROR: ..." diagnostic naming the actual figures and the difference.
  - NO RATE IS EVER HARDCODED HERE. This module parses printed AMOUNTS
    only -- never a rate, and never a value computed from one.
  - CONTENT-BASED DISPATCH, NEVER FILENAME. `parse_l5_text()` raises
    `NotAnL5DocumentError` (mirroring L3's `NotAnL3DocumentError`) with a
    specific reason when the text is not recognisable as this document --
    naming L1/L2/L3 explicitly when their own markers are found, so an
    Advisory or payout certificate sharing a directory with this statement
    is skipped cleanly rather than misparsed.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from .payout_advice import _FFFD_RUN_RE, _NUMBER_TOKEN_RE, _parse_amount

# ---------------------------------------------------------------------------
# Content-dispatch markers. See the ASSUMPTIONS paragraph above -- none of
# these have been checked against a real specimen.
# ---------------------------------------------------------------------------

_FY_PHRASE_RE = re.compile(
    r"(?:as\s+at|year\s+ended)\s+31\s+march\s+(\d{4})", re.IGNORECASE
)
_CAPITAL_HEADER_RE = re.compile(r"^\s*capital\s+account\b", re.IGNORECASE)
_CURRENT_HEADER_RE = re.compile(r"^\s*current\s+account\b", re.IGNORECASE)

# Markers deliberately duplicated (not imported -- these are dispatch-only,
# self-contained strings, the same way advisory.py duplicates its own
# rather than reaching into payout_advice.py's internals) from the other
# document families, so this module can name what it actually found.
_NON_L5_L1_HEADING_RE = re.compile(r"to\s+whomsoever\s+it\s+may\s+concern", re.IGNORECASE)
_NON_L5_SALARY_RE = re.compile(r"salary\s+statement\s+for", re.IGNORECASE)
_NON_L5_PAYMENTS_HEADER_RE = re.compile(r"^\s*payments\b", re.IGNORECASE | re.MULTILINE)
_NON_L5_SCHEDULE_HEADER_RE = re.compile(r"^\s*schedule\b", re.IGNORECASE | re.MULTILINE)


class NotAnL5DocumentError(ValueError):
    """Raised by parse_l5_text()/parse() when the supplied text/PDF is not
    an L5 LLP statement of account. A directory-walking caller should catch
    this and skip the file cleanly -- never crash, never misparse it as
    this document."""


# ---------------------------------------------------------------------------
# Row labels -> model field, one list per block. More specific labels are
# listed first so e.g. "capital introduced" can never be mistaken for a
# bare "introduced", and an optional "Add."/"Less" prefix is tolerated
# without ever being used to flip a sign (see the module docstring's sign
# convention note).
# ---------------------------------------------------------------------------

_PREFIX = r"(?:add\.?:?\s*|less\.?:?\s*)?"

_CAPITAL_ROW_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("opening_balance", re.compile(rf"^\s*{_PREFIX}opening\s+balance\b", re.IGNORECASE)),
    (
        "introduced",
        re.compile(rf"^\s*{_PREFIX}capital\s+introduced\b", re.IGNORECASE),
    ),
    ("closing_balance", re.compile(rf"^\s*{_PREFIX}closing\s+balance\b", re.IGNORECASE)),
]

_CURRENT_ROW_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    (
        "interest_on_capital",
        re.compile(rf"^\s*{_PREFIX}interest\s+on\s+capital\b", re.IGNORECASE),
    ),
    (
        "profit_share",
        re.compile(rf"^\s*{_PREFIX}(?:share\s+of\s+profit|profit\s+share)\b", re.IGNORECASE),
    ),
    (
        "total_additions",
        re.compile(rf"^\s*{_PREFIX}total\s+additions?\b", re.IGNORECASE),
    ),
    (
        "transfer_to_capital",
        re.compile(rf"^\s*{_PREFIX}transfer\s+to\s+capital\b", re.IGNORECASE),
    ),
    ("drawings", re.compile(rf"^\s*{_PREFIX}drawings\b", re.IGNORECASE)),
    ("remuneration", re.compile(rf"^\s*{_PREFIX}remuneration\b", re.IGNORECASE)),
    ("closing_balance", re.compile(rf"^\s*{_PREFIX}closing\s+balance\b", re.IGNORECASE)),
]

# A line that "looks like" a labelled amount row -- starts with a word and
# carries a trailing numeric token -- used to decide whether an unmatched
# line belongs on `unknown_labels`, vs. being ordinary prose or a section
# header that carries no figure at all.
_LOOKS_LIKE_LABEL_ROW_RE = re.compile(r"^\s*[A-Za-z]")


# ---------------------------------------------------------------------------
# Record shape.
# ---------------------------------------------------------------------------

@dataclass
class L5StatementRecord:
    financial_year: str | None                    # e.g. "2025-26", from the BODY only
    source_name: str = ""
    # CAPITAL account block.
    capital_opening_balance: float | None = None
    capital_introduced: float | None = None
    capital_closing_balance: float | None = None
    # CURRENT account block.
    current_remuneration: float | None = None
    current_interest_on_capital: float | None = None  # absent pre-later-years; never 0.0
    current_profit_share: float | None = None
    current_total_additions: float | None = None
    current_drawings: float | None = None
    current_transfer_to_capital: float | None = None
    current_closing_balance: float | None = None
    unknown_labels: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "financial_year": self.financial_year,
            "source_name": self.source_name,
            "capital_opening_balance": self.capital_opening_balance,
            "capital_introduced": self.capital_introduced,
            "capital_closing_balance": self.capital_closing_balance,
            "current_remuneration": self.current_remuneration,
            "current_interest_on_capital": self.current_interest_on_capital,
            "current_profit_share": self.current_profit_share,
            "current_total_additions": self.current_total_additions,
            "current_drawings": self.current_drawings,
            "current_transfer_to_capital": self.current_transfer_to_capital,
            "current_closing_balance": self.current_closing_balance,
            "unknown_labels": list(self.unknown_labels),
            "diagnostics": list(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

def _extract_numbers(line: str) -> list[float | None]:
    """All number-like tokens on a line, in order, each run through the
    same parenthesis/comma/#N-A rules as `_parse_amount`. Reused (not
    duplicated) from payout_advice.py's `_NUMBER_TOKEN_RE`."""
    text = _FFFD_RUN_RE.sub(" ", line)
    return [_parse_amount(tok) for tok in _NUMBER_TOKEN_RE.findall(text)]


def _first_amount_on_or_after(lines: list[str], idx: int, rest: str) -> float | None:
    """Mirrors payout_advice.py/advisory.py's lookahead: if the label's own
    line has no amount after the label, look at the next couple of
    non-empty lines (some layouts print the label and the figure on
    separate lines)."""
    if rest.strip():
        return _parse_amount(rest)
    for lookahead in lines[idx + 1: idx + 3]:
        if lookahead.strip():
            return _parse_amount(lookahead.strip())
    return None


def _split_blocks(lines: list[str]) -> tuple[list[str], list[str]]:
    """Splits the document body into (capital_lines, current_lines) by
    locating the "CAPITAL ACCOUNT" and "CURRENT ACCOUNT" block-header
    lines FIRST -- labels are never mapped over the whole document in one
    pass, so a "Closing Balance" row can never be attributed to the wrong
    block. Both header lines themselves are excluded from every block."""
    capital_idx = next((i for i, ln in enumerate(lines) if _CAPITAL_HEADER_RE.match(ln)), None)
    current_idx = next((i for i, ln in enumerate(lines) if _CURRENT_HEADER_RE.match(ln)), None)

    if capital_idx is None and current_idx is None:
        return [], []
    if capital_idx is None:
        return [], lines[current_idx + 1:]
    if current_idx is None:
        return lines[capital_idx + 1:], []
    if current_idx < capital_idx:
        return lines[capital_idx + 1:], lines[current_idx + 1: capital_idx]
    return lines[capital_idx + 1: current_idx], lines[current_idx + 1:]


def _parse_labelled_section(
    lines: list[str], patterns: list[tuple[str, "re.Pattern[str]"]]
) -> tuple[dict[str, float | None], list[str]]:
    """Maps `lines` by label using `patterns` (first match wins per line,
    patterns tried in list order so more specific labels win over general
    ones). Returns (field -> value, unrecognised label lines). A line is
    only ever flagged as "unrecognised" if it looks like a labelled amount
    row (starts with a letter and carries a trailing numeric token) --
    plain prose/blank lines are never flagged."""
    fields_in_order: list[str] = []
    for f, _ in patterns:
        if f not in fields_in_order:
            fields_in_order.append(f)
    values: dict[str, float | None] = {f: None for f in fields_in_order}
    unknown: list[str] = []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        matched = False
        for field_name, pattern in patterns:
            m = pattern.match(line)
            if not m:
                continue
            matched = True
            rest = line[m.end():].strip()
            values[field_name] = _first_amount_on_or_after(lines, idx, rest)
            break
        if matched:
            continue
        if _LOOKS_LIKE_LABEL_ROW_RE.match(line) and _extract_numbers(line):
            unknown.append(line.strip())
    return values, unknown


def _reconcile(
    diagnostics: list[str],
    label: str,
    components: dict[str, float | None],
    formula,
    target_name: str,
    target: float | None,
) -> None:
    """Shared reconciliation helper: if any named component (including the
    target) is None, appends a "NOTE: ... skipped" diagnostic naming what's
    missing and returns without raising or guessing. Otherwise computes
    `formula(components)` and, if it disagrees with `target` beyond a 0.01
    tolerance, appends an "ERROR: ..." diagnostic naming the actual figures
    and the difference. Never raises, never silently plugs a value."""
    missing = [name for name, value in components.items() if value is None]
    if target is None:
        missing.append(target_name)
    if missing:
        diagnostics.append(
            f"NOTE: {label} reconciliation skipped -- "
            f"{', '.join(missing)} not present on the statement."
        )
        return
    computed = formula(components)
    if abs(computed - target) > 0.01:
        parts = ", ".join(f"{name} ({value})" for name, value in components.items())
        diagnostics.append(
            f"ERROR: {label} does not reconcile -- {parts} = {computed}, but "
            f"the printed {target_name} is {target} (diff {computed - target})."
        )


# ---------------------------------------------------------------------------
# Pure core.
# ---------------------------------------------------------------------------

def parse_l5_text(text: str, source_name: str = "") -> dict:
    """PURE: takes one L5 LLP statement of account's extracted text and
    returns the record dict (see L5StatementRecord.to_dict()). Raises
    NotAnL5DocumentError if `text` is not an L5 statement -- callers
    walking a directory of mixed documents should catch that and skip the
    file.
    """
    cleaned = _FFFD_RUN_RE.sub(" ", text)
    lines = cleaned.splitlines()

    fy_match = _FY_PHRASE_RE.search(cleaned)
    has_capital = any(_CAPITAL_HEADER_RE.match(ln) for ln in lines)
    has_current = any(_CURRENT_HEADER_RE.match(ln) for ln in lines)

    if not (fy_match and has_capital and has_current):
        if _NON_L5_L1_HEADING_RE.search(cleaned):
            raise NotAnL5DocumentError(
                f"{source_name or '<text>'}: looks like an L1 monthly payout "
                "certificate (\"To Whomsoever It may concern\"), not an L5 "
                "LLP statement of account -- skipped."
            )
        if _NON_L5_SALARY_RE.search(cleaned):
            raise NotAnL5DocumentError(
                f"{source_name or '<text>'}: looks like an L2 salary "
                "statement (\"SALARY STATEMENT FOR\"), not an L5 LLP "
                "statement of account -- skipped."
            )
        if _NON_L5_PAYMENTS_HEADER_RE.search(cleaned) and _NON_L5_SCHEDULE_HEADER_RE.search(cleaned):
            raise NotAnL5DocumentError(
                f"{source_name or '<text>'}: looks like an L3 Compensation "
                "Advisory (PAYMENTS/SCHEDULE section headers), not an L5 "
                "LLP statement of account -- skipped."
            )
        raise NotAnL5DocumentError(
            f"{source_name or '<text>'}: missing the \"as at 31 March YYYY\" "
            "/ \"year ended 31 March YYYY\" phrase and/or the CAPITAL "
            "ACCOUNT/CURRENT ACCOUNT block headers -- not an L5 LLP "
            "statement of account, skipped."
        )

    report_year = int(fy_match.group(1))
    financial_year = f"{report_year - 1}-{str(report_year)[2:]}"

    capital_lines, current_lines = _split_blocks(lines)
    capital_values, capital_unknown = _parse_labelled_section(capital_lines, _CAPITAL_ROW_PATTERNS)
    current_values, current_unknown = _parse_labelled_section(current_lines, _CURRENT_ROW_PATTERNS)

    diagnostics: list[str] = []

    _reconcile(
        diagnostics,
        "CAPITAL account block",
        {
            "capital_opening_balance": capital_values["opening_balance"],
            "capital_introduced": capital_values["introduced"],
        },
        lambda c: c["capital_opening_balance"] + c["capital_introduced"],
        "capital closing balance",
        capital_values["closing_balance"],
    )

    _reconcile(
        diagnostics,
        "CURRENT account additions",
        {
            "current_remuneration": current_values["remuneration"],
            "current_interest_on_capital": current_values["interest_on_capital"],
            "current_profit_share": current_values["profit_share"],
        },
        lambda c: c["current_remuneration"] + c["current_interest_on_capital"] + c["current_profit_share"],
        "current total additions",
        current_values["total_additions"],
    )

    _reconcile(
        diagnostics,
        "CURRENT account closing balance",
        {
            "current_total_additions": current_values["total_additions"],
            "current_drawings": current_values["drawings"],
            "current_transfer_to_capital": current_values["transfer_to_capital"],
        },
        lambda c: c["current_total_additions"] + c["current_drawings"] + c["current_transfer_to_capital"],
        "current closing balance",
        current_values["closing_balance"],
    )

    unknown_labels = [*capital_unknown, *current_unknown]

    record = L5StatementRecord(
        financial_year=financial_year,
        source_name=source_name,
        capital_opening_balance=capital_values["opening_balance"],
        capital_introduced=capital_values["introduced"],
        capital_closing_balance=capital_values["closing_balance"],
        current_remuneration=current_values["remuneration"],
        current_interest_on_capital=current_values["interest_on_capital"],
        current_profit_share=current_values["profit_share"],
        current_total_additions=current_values["total_additions"],
        current_drawings=current_values["drawings"],
        current_transfer_to_capital=current_values["transfer_to_capital"],
        current_closing_balance=current_values["closing_balance"],
        unknown_labels=unknown_labels,
        diagnostics=diagnostics,
    )
    return record.to_dict()


# ---------------------------------------------------------------------------
# Thin PDF-opening shell -- the only function here that touches the
# filesystem / pdfplumber.
# ---------------------------------------------------------------------------

def parse(path: str, password: str | None = None) -> dict:
    """Open the LLP statement of account PDF at `path` (password-aware;
    `password` may be None/empty for an unprotected file), extract its
    text, and return the parsed record -- see parse_l5_text(). Raises
    NotAnL5DocumentError if the PDF is not an L5 document (e.g. an L1
    payout certificate or L3 Advisory sharing the same directory).
    """
    with pdfplumber.open(str(path), password=password or "") as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_l5_text(text, source_name=Path(path).name)
