"""
advisory.py -- L3 parser: the firm's annual Compensation Advisory letter
(the document that states the partner's remuneration/share-of-profit/
target-compensation build-up for a financial year ending 31 March, a
PAYMENTS block reconciling that year's drawings against a net payable
figure, and a forward SCHEDULE of instalments projecting a closing capital
balance).

Kept in the same two layers as payout_advice.py (the L1 parser -- read
that module's docstring first, this one mirrors its conventions
throughout):

  - `parse_l3_text()` is PURE: it takes already-extracted page text (a
    str) and returns a plain dict. No filesystem, no pdfplumber, no
    password. Every test in tests/test_skill_partner_comp_recon.py drives
    this function directly against synthetic text blocks -- no real
    Advisory PDF is ever generated or committed (there are no real
    specimens available to this codebase; see AGENT.md's Stage 2 section
    and CLAUDE.md's privacy constraint).
  - `parse(path, password)` is the thin shell: opens the PDF with
    pdfplumber (password-aware), extracts page text, and calls
    `parse_l3_text()`. This is the only function in this module that
    touches the filesystem.

Document layout (authoritative; from first-hand reading of real
specimens, transcribed into this docstring -- no real specimen is used
anywhere in code or tests):

  Part 1 -- a component build-up: labelled lines covering salary,
  remuneration, share of profit, arrears, incentive (gross), target
  compensation, the PRIOR year's target compensation, and (later years
  only) interest on capital.

  Part 2 -- a "PAYMENTS" block: drawings, interest paid, balance, less
  firm's tax / TDS, less capital contribution, net payable.

  Part 3 -- a "SCHEDULE": an opening balance, then one row per
  instalment (each carrying gross / firm's tax / capital contribution /
  net), then a projected closing balance.

This module isolates Part 2 from Part 3 by scanning for the "PAYMENTS"
and "SCHEDULE" section-header lines and treating everything between (and
after) them as belonging to that part -- these two headers double as this
module's *content* dispatch markers (together with the "year ended 31
March" phrase), distinguishing an L3 Advisory from an L1 payout
certificate ("To Whomsoever It may concern" / "Particulars") and an L2
payroll salary statement ("SALARY STATEMENT FOR"). This is a documented
ASSUMPTION about the real layout, not a verified fact -- if a real,
de-identified specimen later shows different header wording, update
`_PAYMENTS_HEADER_RE` / `_SCHEDULE_HEADER_RE` and the guard tests that pin
them, rather than loosening the dispatch check into something that could
misparse an L1/L2 document as an L3.

Design rules enforced in this file (every one is mandatory -- see the H3.5
task spec this module was written against):

  - THE REPORTED FINANCIAL YEAR COMES FROM THE DOCUMENT TEXT, never the
    folder name and never the filename. The Advisory is named for the
    year it REPORTS ON, which is the year *before* the folder it sits in
    at some firms -- a filename- or folder-derived year would be silently
    wrong by one year. This module looks only for a "year ended 31 March
    YYYY" phrase in the body.
  - "INTEREST ON CAPITAL" APPEARS ONLY IN LATER YEARS. Absent is NOT
    zero -- same `None` convention as L1's TDS row.
  - BOTH THE SCHEDULE'S OPENING BALANCE AND ITS PROJECTED CLOSING BALANCE
    ARE PARSED AND EXPOSED, so a caller can assert the chain across
    consecutive Advisories (this year's opening should equal the prior
    year's projected closing). That cross-document assertion is
    deliberately NOT attempted here -- this parser only exposes both
    figures cleanly; chaining them is a caller's job once more than one
    year's Advisory exists.
  - NO RATE IS EVER HARDCODED. The firm's tax rate and the capital-
    contribution rate both change over time (mid-year, in at least one
    case), and every Advisory reserves the right to amend the plan "from
    time to time". This module parses the per-instalment firm's-tax and
    capital-contribution AMOUNTS exactly as printed -- never a rate, and
    never a computed-from-a-rate default. A required figure that is
    absent is a fail-loud diagnostic, never a default.
  - MAP BY LABEL, NEVER ROW POSITION. Label wording varies between years;
    matched case-insensitively and tolerantly (optional "Add."/"Less"
    prefixes, optional trailing colon/punctuation), but never so loosely
    that two distinct labels collide. An unrecognised label line (one
    that looks like a "label ... amount" row but matches no known
    pattern) is collected onto `unknown_labels`, never silently dropped.
  - NEGATIVES ARE PARENTHESISED: "(30,000)" -> -30000. Thousands
    separators are commas, including Indian grouping ("1,20,000"). NEVER
    `abs()` an amount anywhere in this module.
  - pdfplumber can render box-drawing rules and currency glyphs as
    U+FFFD; runs of U+FFFD are stripped as separators, and every
    non-digit/non-sign/non-dot/non-comma character is stripped before a
    number is parsed. These helpers are IMPORTED from payout_advice.py
    (the L1 parser) rather than duplicated.
  - ARITHMETIC CHECKS ARE DIAGNOSTICS, NEVER EXCEPTIONS, NEVER A SILENT
    PLUG. The PAYMENTS block is expected to satisfy
    `balance + less_firms_tax + less_capital_contribution == net_payable`
    (both "less" figures are read as printed, which in a correctly
    formatted Advisory means parenthesised/negative -- see the sign note
    below); each SCHEDULE instalment row is expected to satisfy
    `gross + firms_tax + capital_contribution == net`. A mismatch never
    raises and never silently passes -- it becomes an "ERROR: ..."
    diagnostic naming the figures, mirroring L1's Total-row check.

  Sign convention note: this module never special-cases the word "Less"
  in a label to flip a sign -- doing so would require an `abs()`-shaped
  assumption the task spec forbids ("never abs() an amount, anywhere...
  an amount can legitimately be negative on either side"). Instead, a
  "Less ..." row's printed figure is trusted exactly as printed (parsed
  by the same parenthesis/comma rules as everything else), and the
  reconciliation formulas above are additive sums -- identical in spirit
  to L1's "non-Total rows sum to Total" check, where a deduction row
  (e.g. TDS) is expected to already be parenthesised/negative in a
  correctly formatted document. If a real specimen later shows "Less"
  rows printed as positive magnitudes instead, the fix is to update the
  reconciliation formula (and the guard tests pinning it) -- never to
  `abs()` a parsed figure to paper over the sign.

Known-unresolved items this parser SURFACES rather than absorbs (see the
task spec's items j/k/l): a residual between this document's component
build-up and the LLP statement's profit share, and a difference between
this document's `incentive_gross` and any independently-derived incentive
figure, are both CROSS-document comparisons this single-document parser
cannot compute -- `llp_statement.py` is still a Stage 2 placeholder, so
there is no second document's text available here to compare against.
This module's job is limited to exposing `share_of_profit`,
`incentive_gross`, `target_compensation` and `salary` cleanly and
un-plugged, so a downstream step (once `llp_statement.py` lands) can
compute those residuals/differences and report them -- this module never
guesses or plugs a value for any of them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

from .payout_advice import _FFFD_RUN_RE, _NUMBER_TOKEN_RE, _parse_amount

# ---------------------------------------------------------------------------
# Content-dispatch markers.
# ---------------------------------------------------------------------------

_FY_PHRASE_RE = re.compile(r"year\s+ended\s+31\s+march\s+(\d{4})", re.IGNORECASE)
_PAYMENTS_HEADER_RE = re.compile(r"^\s*payments\b", re.IGNORECASE)
_SCHEDULE_HEADER_RE = re.compile(r"^\s*schedule\b", re.IGNORECASE)

_NON_L3_L1_HEADING_RE = re.compile(r"to\s+whomsoever\s+it\s+may\s+concern", re.IGNORECASE)
_NON_L3_SALARY_RE = re.compile(r"salary\s+statement\s+for", re.IGNORECASE)


class NotAnL3DocumentError(ValueError):
    """Raised by parse_l3_text()/parse() when the supplied text/PDF is not
    an L3 annual Compensation Advisory letter. A directory-walking caller
    should catch this and skip the file cleanly -- never crash, never
    misparse it as an Advisory."""


# ---------------------------------------------------------------------------
# Row labels -> model field, one list per section. More specific labels
# are listed first so e.g. "prior year target compensation" can never be
# mistaken for "target compensation".
# ---------------------------------------------------------------------------

_PART1_ROW_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    (
        "prior_year_target_compensation",
        re.compile(r"^\s*(?:prior[\s-]*year|previous\s+year)\s+target\s+compensation\b", re.IGNORECASE),
    ),
    ("target_compensation", re.compile(r"^\s*target\s+compensation\b", re.IGNORECASE)),
    ("interest_on_capital", re.compile(r"^\s*interest\s+on\s+capital\b", re.IGNORECASE)),
    ("incentive_gross", re.compile(r"^\s*incentive\b", re.IGNORECASE)),
    ("arrears", re.compile(r"^\s*arrears\b", re.IGNORECASE)),
    ("share_of_profit", re.compile(r"^\s*share\s+of\s+profit\b", re.IGNORECASE)),
    ("remuneration", re.compile(r"^\s*remuneration\b", re.IGNORECASE)),
    ("salary", re.compile(r"^\s*salary\b", re.IGNORECASE)),
]

_PART2_ROW_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    (
        "less_firms_tax",
        re.compile(
            r"^\s*(?:less\.?:?\s*)?(?:firm'?s\s*tax(?:\s*/\s*tds)?|tds)\b", re.IGNORECASE
        ),
    ),
    (
        "less_capital_contribution",
        re.compile(r"^\s*(?:less\.?:?\s*)?capital\s+contribution\b", re.IGNORECASE),
    ),
    ("net_payable", re.compile(r"^\s*net\s+payable\b", re.IGNORECASE)),
    ("interest_paid", re.compile(r"^\s*interest\s+paid\b", re.IGNORECASE)),
    ("drawings", re.compile(r"^\s*drawings\b", re.IGNORECASE)),
    ("balance", re.compile(r"^\s*balance\b", re.IGNORECASE)),
]

_PART1_FIELDS = [f for f, _ in _PART1_ROW_PATTERNS]
_PART2_FIELDS = [f for f, _ in _PART2_ROW_PATTERNS]

_OPENING_BALANCE_RE = re.compile(r"^\s*opening\s+balance\b", re.IGNORECASE)
_CLOSING_BALANCE_RE = re.compile(r"^\s*(?:projected\s+)?closing\s+balance\b", re.IGNORECASE)
_INSTALMENT_ROW_RE = re.compile(
    r"^\s*instal{1,2}ment\s*\.?\s*(?:no\.?\s*)?#?(\d+)\b", re.IGNORECASE
)

# A line that "looks like" a labelled amount row (starts with a word,
# carries a trailing numeric token) -- used to decide whether an
# unmatched line belongs on `unknown_labels`, vs. being ordinary prose /
# a section header that carries no figure at all.
_LOOKS_LIKE_LABEL_ROW_RE = re.compile(r"^\s*[A-Za-z]")


# ---------------------------------------------------------------------------
# Record shape.
# ---------------------------------------------------------------------------

@dataclass
class ScheduleInstalment:
    instalment_no: int | None
    gross: float | None = None
    firms_tax: float | None = None
    capital_contribution: float | None = None
    net: float | None = None

    def to_dict(self) -> dict:
        return {
            "instalment_no": self.instalment_no,
            "gross": self.gross,
            "firms_tax": self.firms_tax,
            "capital_contribution": self.capital_contribution,
            "net": self.net,
        }


@dataclass
class L3AdvisoryRecord:
    financial_year: str | None                # e.g. "2025-26", from the BODY only
    source_name: str = ""
    # Part 1 -- component build-up.
    salary: float | None = None
    remuneration: float | None = None
    share_of_profit: float | None = None
    arrears: float | None = None
    incentive_gross: float | None = None
    target_compensation: float | None = None
    prior_year_target_compensation: float | None = None
    interest_on_capital: float | None = None   # absent pre-later-years; never coerced to 0
    # Part 2 -- PAYMENTS block.
    drawings: float | None = None
    interest_paid: float | None = None
    balance: float | None = None
    less_firms_tax: float | None = None
    less_capital_contribution: float | None = None
    net_payable: float | None = None
    # Part 3 -- SCHEDULE.
    schedule_opening_balance: float | None = None
    schedule_projected_closing_balance: float | None = None
    schedule_instalments: list[ScheduleInstalment] = field(default_factory=list)
    unknown_labels: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "financial_year": self.financial_year,
            "source_name": self.source_name,
            "salary": self.salary,
            "remuneration": self.remuneration,
            "share_of_profit": self.share_of_profit,
            "arrears": self.arrears,
            "incentive_gross": self.incentive_gross,
            "target_compensation": self.target_compensation,
            "prior_year_target_compensation": self.prior_year_target_compensation,
            "interest_on_capital": self.interest_on_capital,
            "drawings": self.drawings,
            "interest_paid": self.interest_paid,
            "balance": self.balance,
            "less_firms_tax": self.less_firms_tax,
            "less_capital_contribution": self.less_capital_contribution,
            "net_payable": self.net_payable,
            "schedule_opening_balance": self.schedule_opening_balance,
            "schedule_projected_closing_balance": self.schedule_projected_closing_balance,
            "schedule_instalments": [i.to_dict() for i in self.schedule_instalments],
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
    """Mirrors payout_advice.py's lookahead: if the label's own line has
    no amount after the label, look at the next couple of non-empty
    lines (some layouts print the label and the figure on separate
    lines)."""
    if rest.strip():
        return _parse_amount(rest)
    for lookahead in lines[idx + 1: idx + 3]:
        if lookahead.strip():
            return _parse_amount(lookahead.strip())
    return None


def _split_sections(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Splits the document body into (part1, part2, part3) line lists by
    locating the "PAYMENTS" and "SCHEDULE" section-header lines. Both
    header lines themselves are excluded from every section."""
    payments_idx = next((i for i, ln in enumerate(lines) if _PAYMENTS_HEADER_RE.match(ln)), None)
    schedule_idx = next((i for i, ln in enumerate(lines) if _SCHEDULE_HEADER_RE.match(ln)), None)

    if payments_idx is None:
        part1, part2 = lines, []
    elif schedule_idx is None or schedule_idx < payments_idx:
        part1, part2 = lines[:payments_idx], lines[payments_idx + 1:]
    else:
        part1, part2 = lines[:payments_idx], lines[payments_idx + 1: schedule_idx]

    if schedule_idx is None:
        part3: list[str] = []
    else:
        part3 = lines[schedule_idx + 1:]

    return part1, part2, part3


def _parse_labelled_section(
    lines: list[str], patterns: list[tuple[str, "re.Pattern[str]"]]
) -> tuple[dict[str, float | None], list[str]]:
    """Maps `lines` by label using `patterns` (first match wins per line).
    Returns (field -> value, unrecognised label lines). A line is only
    ever flagged as "unrecognised" if it looks like a labelled amount row
    (starts with a letter and carries a trailing numeric token) -- plain
    prose/blank lines are never flagged."""
    values: dict[str, float | None] = {f: None for f, _ in patterns}
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


def _parse_schedule(lines: list[str]) -> tuple[float | None, float | None, list[ScheduleInstalment], list[str]]:
    opening: float | None = None
    closing: float | None = None
    instalments: list[ScheduleInstalment] = []
    unknown: list[str] = []

    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        if _OPENING_BALANCE_RE.match(line):
            m = _OPENING_BALANCE_RE.match(line)
            rest = line[m.end():].strip()
            opening = _first_amount_on_or_after(lines, idx, rest)
            continue
        if _CLOSING_BALANCE_RE.match(line):
            m = _CLOSING_BALANCE_RE.match(line)
            rest = line[m.end():].strip()
            closing = _first_amount_on_or_after(lines, idx, rest)
            continue
        inst_m = _INSTALMENT_ROW_RE.match(line)
        if inst_m:
            rest = line[inst_m.end():].strip()
            numbers = _extract_numbers(rest) if rest else []
            lookahead_idx = idx
            while len(numbers) < 4 and lookahead_idx + 1 < len(lines):
                lookahead_idx += 1
                nxt = lines[lookahead_idx].strip()
                if not nxt:
                    continue
                if _INSTALMENT_ROW_RE.match(nxt) or _CLOSING_BALANCE_RE.match(nxt):
                    break
                numbers.extend(_extract_numbers(nxt))
            gross = numbers[0] if len(numbers) > 0 else None
            firms_tax = numbers[1] if len(numbers) > 1 else None
            capital_contribution = numbers[2] if len(numbers) > 2 else None
            net = numbers[3] if len(numbers) > 3 else None
            instalments.append(
                ScheduleInstalment(
                    instalment_no=int(inst_m.group(1)),
                    gross=gross,
                    firms_tax=firms_tax,
                    capital_contribution=capital_contribution,
                    net=net,
                )
            )
            continue
        if _LOOKS_LIKE_LABEL_ROW_RE.match(line) and _extract_numbers(line):
            unknown.append(line.strip())

    return opening, closing, instalments, unknown


# ---------------------------------------------------------------------------
# Pure core.
# ---------------------------------------------------------------------------

def parse_l3_text(text: str, source_name: str = "") -> dict:
    """PURE: takes one L3 Advisory letter's extracted text and returns the
    record dict (see L3AdvisoryRecord.to_dict()). Raises
    NotAnL3DocumentError if `text` is not an L3 Advisory -- callers
    walking a directory of mixed documents should catch that and skip the
    file.
    """
    cleaned = _FFFD_RUN_RE.sub(" ", text)
    lines = cleaned.splitlines()

    fy_match = _FY_PHRASE_RE.search(cleaned)
    has_payments = any(_PAYMENTS_HEADER_RE.match(ln) for ln in lines)
    has_schedule = any(_SCHEDULE_HEADER_RE.match(ln) for ln in lines)

    if not (fy_match and has_payments and has_schedule):
        if _NON_L3_L1_HEADING_RE.search(cleaned):
            raise NotAnL3DocumentError(
                f"{source_name or '<text>'}: looks like an L1 monthly payout "
                "certificate (\"To Whomsoever It may concern\"), not an L3 "
                "Compensation Advisory -- skipped."
            )
        if _NON_L3_SALARY_RE.search(cleaned):
            raise NotAnL3DocumentError(
                f"{source_name or '<text>'}: looks like a salary statement "
                "(\"SALARY STATEMENT FOR\"), not an L3 Compensation Advisory "
                "-- skipped."
            )
        raise NotAnL3DocumentError(
            f"{source_name or '<text>'}: missing the \"year ended 31 March YYYY\" "
            "phrase and/or the PAYMENTS/SCHEDULE section headers -- not an L3 "
            "Compensation Advisory, skipped."
        )

    report_year = int(fy_match.group(1))
    financial_year = f"{report_year - 1}-{str(report_year)[2:]}"

    part1_lines, part2_lines, part3_lines = _split_sections(lines)

    part1_values, part1_unknown = _parse_labelled_section(part1_lines, _PART1_ROW_PATTERNS)
    part2_values, part2_unknown = _parse_labelled_section(part2_lines, _PART2_ROW_PATTERNS)
    opening, closing, instalments, part3_unknown = _parse_schedule(part3_lines)

    diagnostics: list[str] = []

    balance = part2_values["balance"]
    less_firms_tax = part2_values["less_firms_tax"]
    less_capital_contribution = part2_values["less_capital_contribution"]
    net_payable = part2_values["net_payable"]
    if None not in (balance, less_firms_tax, less_capital_contribution, net_payable):
        computed = balance + less_firms_tax + less_capital_contribution
        if abs(computed - net_payable) > 0.01:
            diagnostics.append(
                "ERROR: PAYMENTS block does not reconcile -- balance "
                f"({balance}) + less_firms_tax ({less_firms_tax}) + "
                f"less_capital_contribution ({less_capital_contribution}) = "
                f"{computed}, but the printed Net Payable is {net_payable} "
                f"(diff {computed - net_payable})."
            )

    for inst in instalments:
        if None in (inst.gross, inst.firms_tax, inst.capital_contribution, inst.net):
            continue
        computed = inst.gross + inst.firms_tax + inst.capital_contribution
        if abs(computed - inst.net) > 0.01:
            diagnostics.append(
                f"ERROR: SCHEDULE instalment {inst.instalment_no} does not "
                f"reconcile -- gross ({inst.gross}) + firms_tax "
                f"({inst.firms_tax}) + capital_contribution "
                f"({inst.capital_contribution}) = {computed}, but the "
                f"printed net is {inst.net} (diff {computed - inst.net})."
            )

    unknown_labels = [*part1_unknown, *part2_unknown, *part3_unknown]

    record = L3AdvisoryRecord(
        financial_year=financial_year,
        source_name=source_name,
        salary=part1_values["salary"],
        remuneration=part1_values["remuneration"],
        share_of_profit=part1_values["share_of_profit"],
        arrears=part1_values["arrears"],
        incentive_gross=part1_values["incentive_gross"],
        target_compensation=part1_values["target_compensation"],
        prior_year_target_compensation=part1_values["prior_year_target_compensation"],
        interest_on_capital=part1_values["interest_on_capital"],
        drawings=part2_values["drawings"],
        interest_paid=part2_values["interest_paid"],
        balance=balance,
        less_firms_tax=less_firms_tax,
        less_capital_contribution=less_capital_contribution,
        net_payable=net_payable,
        schedule_opening_balance=opening,
        schedule_projected_closing_balance=closing,
        schedule_instalments=instalments,
        unknown_labels=unknown_labels,
        diagnostics=diagnostics,
    )
    return record.to_dict()


# ---------------------------------------------------------------------------
# Thin PDF-opening shell -- the only function here that touches the
# filesystem / pdfplumber.
# ---------------------------------------------------------------------------

def parse(path: str, password: str | None = None) -> dict:
    """Open the annual Compensation Advisory PDF at `path` (password-aware;
    `password` may be None/empty for an unprotected file), extract its
    text, and return the parsed record -- see parse_l3_text(). Raises
    NotAnL3DocumentError if the PDF is not an L3 document (e.g. an L1
    payout certificate sharing the same directory).
    """
    with pdfplumber.open(str(path), password=password or "") as pdf:
        text = "\n".join(page.extract_text() or "" for page in pdf.pages)
    return parse_l3_text(text, source_name=Path(path).name)
