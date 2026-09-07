"""
advisory.py -- L3 parser: the firm's annual Compensation Advisory letter
(the document that states the partner's remuneration/share-of-profit/
target-compensation build-up for a financial year ending 31 March, a
PAYMENTS block reconciling that year's drawings against a net payable
figure, and a forward SCHEDULE of instalments projecting a closing capital
balance, and -- on a reissued/revision letter -- a Computation block
stating the capital-contribution percentage in force).

Kept in the same two layers as payout_advice.py (the L1 parser -- read
that module's docstring first, this one mirrors its conventions
throughout):

  - `parse_l3_text()` is PURE: it takes already-extracted page text (a
    str) and returns a plain dict. No filesystem, no pdfplumber, no
    password. Every test in tests/test_skill_partner_comp_recon.py drives
    this function directly against synthetic text blocks built from the
    document SHAPES described below -- no real Advisory PDF is ever
    generated or committed (see CLAUDE.md's privacy constraint).
  - `parse(path, password)` is the thin shell: opens the PDF with
    pdfplumber (password-aware), extracts page text, and calls
    `parse_l3_text()`. This is the only function in this module that
    touches the filesystem.

Document layout (repaired against real specimens -- the module used to
say "there are no real specimens available to this codebase"; real
specimens have since been read and this module's dispatch/parsing logic
was corrected against them, with every literal amount/name in this file
and in the test fixtures replaced by synthetic values):

  Part 1 -- a "Compensation details" component build-up: labelled lines
  covering salary, remuneration, share of profit, arrears, incentive
  (gross), monthly drawings (gross), PLMI (gross) / PLMI as Share of
  Profit (gross), target/total compensation for the current AND the
  prior year (told apart by the year printed in the label, matched
  against this document's own reported year -- never by position), and
  (later years only) interest on capital. The exact label vocabulary
  changes year to year; anything genuinely unrecognised lands on
  `unknown_labels`, never silently dropped.

  Part 2 -- a "Payments summary" block: drawings, interest paid, balance,
  less firm's tax / TDS, less capital contribution, net payable.

  Part 3 -- a schedule of the partner's capital balance and PLMI
  instalments. Its header is a descriptive sentence ENDING in the word
  "schedule" (e.g. "Capital balance and PLMI payment schedule"), not a
  line that begins with it. It carries: an opening balance (dated,
  possibly a printed nil), one row per instalment/arrears/addition
  (gross / firm's tax-TDS / capital contribution / net -- some rows carry
  only gross+net), an unlabelled TOTALS row preceded by a dashed
  separator, and a projected closing balance (also dated).

  Part 4 -- an OPTIONAL "Computation" block (seen so far only on a
  reissued/revision letter that carries no Part 1/Part 2 at all): the
  target compensation for the year, the capital contribution made to
  date, the contribution required, any shortfall/refund, the number of
  months over which the contribution is expected vs. achieved, and the
  contribution percentage required/achieved. THIS IS THE ONLY PLACE THE
  FIRM STATES THE CAPITAL-CONTRIBUTION PERCENTAGE, and that percentage is
  NOT a constant -- it has changed mid-year via exactly this kind of
  reissue. Nothing in this module ever hardcodes it.

A single Advisory letter may carry only SOME of these parts -- in
particular a revision letter has been observed with Part 3 and Part 4
only, no Part 1/Part 2 at all. `parse_l3_text()` requires the "year ended
31 March YYYY" phrase plus AT LEAST ONE of the Payments/Schedule section
headers; it never requires all three, and never fabricates a section that
is not in the document -- a record whose Part 1/Part 2 are absent carries
`None` fields there and a diagnostic naming the missing section. Because
of this, more than one Advisory can be authoritative for the same
financial year (an original plus one or more reissues); `merge_advisories`
(below) implements the newest-wins-per-section merge across such a set --
this module never picks "the most recent file" as a whole, since doing so
would throw away sections the later document does not carry.

The "PAYMENTS"/"SCHEDULE"/"Computation" section-header lines double as
this module's *content* dispatch markers (together with the "year ended
31 March" phrase), distinguishing an L3 Advisory from an L1 payout
certificate ("To Whomsoever It may concern" / "Particulars") and an L2
payroll salary statement ("SALARY STATEMENT FOR").

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
  - A PRINTED NIL ("-") IS NOT THE SAME AS AN ABSENT FIGURE. The
    schedule's opening/closing balance can be printed as a literal "-"
    (a real, stated nil, value 0.0) -- this is kept distinct from the
    balance never being mentioned at all (value `None`) via a paired
    `..._is_nil` flag. Same trap as `llp_statement.py`'s "printed dash
    means 0.0" note.
  - BOTH THE SCHEDULE'S OPENING BALANCE AND ITS PROJECTED CLOSING BALANCE
    ARE PARSED AND EXPOSED, along with the date each is "as on", so a
    caller can assert the chain across consecutive Advisories (this
    year's opening should equal the prior year's projected closing)
    without trusting a filename. That cross-document assertion is
    deliberately NOT attempted here.
  - NO RATE IS EVER HARDCODED. The firm's tax rate and the capital-
    contribution rate both change over time (mid-year, in at least one
    observed case), and every Advisory reserves the right to amend the
    plan "from time to time". This module parses the per-instalment
    firm's-tax and capital-contribution AMOUNTS, and the Computation
    block's PERCENTAGES, exactly as printed -- never a hardcoded rate,
    and never a computed-from-a-rate default. A required figure that is
    absent is a fail-loud diagnostic, never a default.
  - MAP BY LABEL, NEVER ROW POSITION. Label wording varies between years;
    matched case-insensitively and tolerantly (optional "Add."/"Less"
    prefixes, optional trailing colon/punctuation, both spellings of
    "achieved"/"achived"), but never so loosely that two distinct labels
    collide -- the more specific label (e.g. "PLMI as Share of Profit
    (gross)") is always listed/checked ahead of the more general one
    (e.g. "Share of Profit (gross)"). An unrecognised label line (one
    that looks like a "label ... amount" row but matches no known
    pattern) is collected onto `unknown_labels`, never silently dropped.
  - NEGATIVES ARE PARENTHESISED: "(30,000)" -> -30000. Thousands
    separators are commas, including Indian grouping ("1,20,000"). NEVER
    `abs()` an amount anywhere in this module.
  - pdfplumber can render box-drawing rules and currency glyphs as
    U+FFFD; runs of U+FFFD are stripped as separators, and every
    non-digit/non-sign/non-dot/non-comma character is stripped before a
    number is parsed. The core parenthesis/comma/#N-A parsing rule
    (`_parse_amount`) is IMPORTED from payout_advice.py (the L1 parser)
    rather than duplicated. HOWEVER: pdfplumber has also been observed to
    inject a stray space INSIDE a single printed number in this
    document's schedule/computation tables (e.g. "2 ,333,333" for
    2,333,333; "( 815,360)" for the negative 815,360, where the space
    after "(" would otherwise defeat sign detection; and a trailing
    "*"/"**" footnote marker). `payout_advice.py`'s shared
    `_NUMBER_TOKEN_RE` cannot be loosened to tolerate this without risking
    its own (in-production) L1 tokenisation, so this module carries its
    own LOCAL repair step (`_repair_l3_number_spacing`) that runs ahead of
    the shared tokeniser -- see that function's docstring.
  - ARITHMETIC CHECKS ARE DIAGNOSTICS, NEVER EXCEPTIONS, NEVER A SILENT
    PLUG. The PAYMENTS block is expected to satisfy
    `balance + less_firms_tax + less_capital_contribution == net_payable`
    (both "less" figures are read as printed, which in a correctly
    formatted Advisory means parenthesised/negative -- see the sign note
    below); each SCHEDULE row is expected to satisfy
    `gross + firms_tax + capital_contribution == net` (a two-figure row
    is expected to satisfy `gross == net`); the schedule's unlabelled
    TOTALS row is expected to equal the sum of the rows above it; the
    Computation block's `contribution_required` is expected to equal
    `percent_required/100 * tc_for_fy * months_achieved/months_expected`.
    A mismatch never raises and never silently passes -- it becomes an
    "ERROR: ..." diagnostic naming the figures, mirroring L1's Total-row
    check. A section that is simply ABSENT from a document (never
    required) produces a "NOTE: ..." diagnostic, not an ERROR.

  Sign convention note: this module never special-cases the word "Less"
  in a label to flip a sign -- doing so would require an `abs()`-shaped
  assumption the task spec forbids ("never abs() an amount, anywhere...
  an amount can legitimately be negative on either side"). Instead, a
  "Less ..." row's printed figure is trusted exactly as printed (parsed
  by the same parenthesis/comma rules as everything else), and the
  reconciliation formulas above are additive sums -- identical in spirit
  to L1's "non-Total rows sum to Total" check, where a deduction row
  (e.g. TDS) is expected to already be parenthesised/negative in a
  correctly formatted document.

Known-unresolved items this parser SURFACES rather than absorbs (see the
task spec's items j/k/l): a residual between this document's component
build-up and the LLP statement's profit share, and a difference between
this document's `incentive_gross` and any independently-derived incentive
figure, are both CROSS-document comparisons this single-document parser
cannot compute. This module's job is limited to exposing its fields
cleanly and un-plugged, so a downstream step can compute those
residuals/differences and report them -- this module never guesses or
plugs a value for any of them.
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
# The real schedule header is a descriptive sentence that ENDS in the word
# "schedule" ("Capital balance and PLMI payment schedule" / "Capital
# balance, PLMI payment and Special Incentive schedule"), not a line that
# begins with it. Match either that shape (a capital/PLMI cue somewhere
# before the word "schedule") or the older bare "Schedule" heading, so a
# dispatch stays strict enough that unrelated prose mentioning the word
# "schedule" cannot masquerade as this section.
_SCHEDULE_HEADER_RE = re.compile(
    r"^\s*schedule\b|\b(?:capital|plmi)\b.*\bschedule\b", re.IGNORECASE
)
_COMPUTATION_HEADER_RE = re.compile(r"^\s*computation\b", re.IGNORECASE)

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
# mistaken for "target compensation", and "PLMI as Share of Profit
# (gross)" can never be mistaken for "PLMI (gross)" or "Share of Profit
# (gross)".
# ---------------------------------------------------------------------------

# "Total compensation for the year ended 31 Mar NN" is handled OUTSIDE
# this list: the current-year and prior-year variants of that label are
# identical apart from the year, so they are told apart by comparing the
# printed year against the document's own reported financial year, not by
# which pattern matches or which line comes first. See `_parse_part1`.
_TOTAL_COMP_FOR_YEAR_RE = re.compile(
    r"^\s*total\s+compensation\s+for\s+the\s+year\s+ended\s+31\s+mar\s*(\d{2,4})\b",
    re.IGNORECASE,
)

_PART1_ROW_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    (
        "prior_year_target_compensation",
        re.compile(r"^\s*(?:prior[\s-]*year|previous\s+year)\s+target\s+compensation\b", re.IGNORECASE),
    ),
    ("target_compensation", re.compile(r"^\s*target\s+compensation\b", re.IGNORECASE)),
    (
        "plmi_share_of_profit_gross",
        re.compile(r"^\s*plmi\s+as\s+share\s+of\s+profit\b", re.IGNORECASE),
    ),
    ("plmi_gross", re.compile(r"^\s*plmi\s*\(\s*gross\s*\)", re.IGNORECASE)),
    ("share_of_profit", re.compile(r"^\s*share\s+of\s+profit\b", re.IGNORECASE)),
    (
        "compensation_paid_base_and_incentive",
        re.compile(
            r"^\s*compensation\s+paid\s+in\s+fy\s*[\d-]+\s*\(\s*base\s+pay", re.IGNORECASE
        ),
    ),
    (
        "salary",
        re.compile(r"^\s*(?:compensation\s+paid\s+as\s+salary|salary)\b", re.IGNORECASE),
    ),
    ("remuneration", re.compile(r"^\s*remuneration\b", re.IGNORECASE)),
    ("interest_on_capital", re.compile(r"^\s*interest\s+on\s+capital\b", re.IGNORECASE)),
    ("incentive_gross", re.compile(r"^\s*incentive\b", re.IGNORECASE)),
    ("arrears", re.compile(r"^\s*arrears\b", re.IGNORECASE)),
    ("monthly_drawings_gross", re.compile(r"^\s*monthly\s+drawings\b", re.IGNORECASE)),
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

_COMPUTATION_ROW_PATTERNS: list[tuple[str, "re.Pattern[str]"]] = [
    ("tc_for_fy", re.compile(r"^\s*tc\s+for\s+fy\s*[\d-]+\b", re.IGNORECASE)),
    (
        "capital_contribution_till_date",
        re.compile(r"^\s*capital\s+contribution\s+till\b", re.IGNORECASE),
    ),
    (
        "contribution_required_or_refunded",
        re.compile(r"^\s*contribution\s+required\s*/\s*to\s+be\s+refunded\b", re.IGNORECASE),
    ),
    ("contribution_required", re.compile(r"^\s*contribution\s+required\b", re.IGNORECASE)),
    (
        "months_expected",
        re.compile(
            r"^\s*total\s+months\s+over\s+which\s+capital\s+contribution\s+expected\b",
            re.IGNORECASE,
        ),
    ),
    (
        "months_achieved",
        re.compile(
            r"^\s*total\s+months\s+of\s+capital\s+contribution\s+achi?e?ved\b", re.IGNORECASE
        ),
    ),
    (
        "percent_required",
        re.compile(r"^\s*%\s*of\s+capital\s+contribution\s+required\b", re.IGNORECASE),
    ),
    (
        "percent_achieved",
        re.compile(r"^\s*%\s*of\s+capital\s+contribution\s+achi?e?ved\b", re.IGNORECASE),
    ),
]
_COMPUTATION_FIELDS = [f for f, _ in _COMPUTATION_ROW_PATTERNS]

_OPENING_BALANCE_RE = re.compile(r"^\s*opening\s+balance\b", re.IGNORECASE)
_CLOSING_BALANCE_RE = re.compile(r"^\s*(?:projected\s+)?closing\s+balance\b", re.IGNORECASE)
_BALANCE_DATE_RE = re.compile(r"\(\s*as\s+on\s+([^)]+)\)", re.IGNORECASE)

# Real instalment labels carry an ordinal word and the FY inside the
# label, e.g. "PLMI : FY 25 (1st instalment)". The older bare
# "Instalment No. N" shape (used by the pre-repair synthetic fixtures) is
# still accepted alongside it.
_INSTALMENT_ROW_RE_NEW = re.compile(
    r"^\s*plmi\s*:?\s*fy\s*[\d-]+\s*\(\s*(\d+)(?:st|nd|rd|th)\s+instal{1,2}ment\s*\)",
    re.IGNORECASE,
)
_INSTALMENT_ROW_RE_OLD = re.compile(
    r"^\s*instal{1,2}ment\s*\.?\s*(?:no\.?\s*)?#?(\d+)\b", re.IGNORECASE
)
_ARREARS_ROW_RE = re.compile(r"^\s*arrears\s+for\s+fy\s*[\d-]+\b", re.IGNORECASE)
_ADDITIONS_ROW_RE = re.compile(
    r"^\s*additions\s+pertaining\s+to\s+prior\s+year\b", re.IGNORECASE
)

# A lone "-" is a printed nil, not a separator and not "unknown".
_NIL_ROW_RE = re.compile(r"^\s*-\s*$")
# "- - -" / "- - - -" precedes the schedule's unlabelled TOTALS row.
_TOTALS_SEPARATOR_RE = re.compile(r"^\s*-(?:\s+-){1,}\s*$")
# A bare line of only number-ish tokens (the totals row itself carries no
# label at all).
_BARE_NUMBER_LINE_RE = re.compile(r"^[\s0-9,.()*-]+$")

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
    label: str | None = None      # set for non-numbered rows (Arrears, Additions, TOTAL)
    gross: float | None = None
    firms_tax: float | None = None
    capital_contribution: float | None = None
    net: float | None = None

    def to_dict(self) -> dict:
        return {
            "instalment_no": self.instalment_no,
            "label": self.label,
            "gross": self.gross,
            "firms_tax": self.firms_tax,
            "capital_contribution": self.capital_contribution,
            "net": self.net,
        }


@dataclass
class Computation:
    tc_for_fy: float | None = None
    capital_contribution_till_date: float | None = None
    contribution_required: float | None = None
    contribution_required_or_refunded: float | None = None
    months_expected: int | None = None
    months_achieved: int | None = None
    percent_required: float | None = None
    percent_achieved: float | None = None

    def to_dict(self) -> dict:
        return {
            "tc_for_fy": self.tc_for_fy,
            "capital_contribution_till_date": self.capital_contribution_till_date,
            "contribution_required": self.contribution_required,
            "contribution_required_or_refunded": self.contribution_required_or_refunded,
            "months_expected": self.months_expected,
            "months_achieved": self.months_achieved,
            "percent_required": self.percent_required,
            "percent_achieved": self.percent_achieved,
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
    monthly_drawings_gross: float | None = None
    plmi_gross: float | None = None
    plmi_share_of_profit_gross: float | None = None
    compensation_paid_base_and_incentive: float | None = None
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
    schedule_opening_balance_is_nil: bool = False
    schedule_opening_balance_date: str | None = None
    schedule_projected_closing_balance: float | None = None
    schedule_closing_balance_is_nil: bool = False
    schedule_closing_balance_date: str | None = None
    schedule_instalments: list[ScheduleInstalment] = field(default_factory=list)
    schedule_totals: ScheduleInstalment | None = None
    # Part 4 -- Computation (optional; absence is not a diagnostic).
    computation: Computation | None = None
    sections_present: dict = field(
        default_factory=lambda: {
            "part1": False,
            "part2": False,
            "schedule": False,
            "computation": False,
        }
    )
    section_sources: dict = field(
        default_factory=lambda: {
            "part1": None,
            "part2": None,
            "schedule": None,
            "computation": None,
        }
    )
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
            "monthly_drawings_gross": self.monthly_drawings_gross,
            "plmi_gross": self.plmi_gross,
            "plmi_share_of_profit_gross": self.plmi_share_of_profit_gross,
            "compensation_paid_base_and_incentive": self.compensation_paid_base_and_incentive,
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
            "schedule_opening_balance_is_nil": self.schedule_opening_balance_is_nil,
            "schedule_opening_balance_date": self.schedule_opening_balance_date,
            "schedule_projected_closing_balance": self.schedule_projected_closing_balance,
            "schedule_closing_balance_is_nil": self.schedule_closing_balance_is_nil,
            "schedule_closing_balance_date": self.schedule_closing_balance_date,
            "schedule_instalments": [i.to_dict() for i in self.schedule_instalments],
            "schedule_totals": self.schedule_totals.to_dict() if self.schedule_totals else None,
            "computation": self.computation.to_dict() if self.computation else None,
            "sections_present": dict(self.sections_present),
            "section_sources": dict(self.section_sources),
            "unknown_labels": list(self.unknown_labels),
            "diagnostics": list(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# Helpers.
# ---------------------------------------------------------------------------

_ASTERISK_RE = re.compile(r"\*+")
_OPEN_PAREN_SPACE_RE = re.compile(r"\(\s+")
# Exactly ONE space/tab, not a run: pdfplumber's spurious mid-number split
# has been observed only as a single stray space ("2 ,333,333", "1 1,125,000",
# "7 89,140"). A wide gap of several spaces is a genuine column separator
# between two DIFFERENT figures (e.g. the two plain positive amounts on an
# "Additions pertaining to prior year" row) and must never be merged into
# one number -- bounding this to one space/tab is what keeps those two
# cases apart.
_DIGIT_SPACE_JOIN_RE = re.compile(r"([0-9])[ \t](?=[0-9,])")


def _repair_l3_number_spacing(text: str) -> str:
    """LOCAL to this module -- does not touch payout_advice.py's shared
    tokeniser, so the L1 parser's behaviour is completely unaffected.

    pdfplumber has been observed, on this document's schedule and
    Computation tables specifically, to:
      - inject a stray space INSIDE a single printed number, splitting
        one figure into what looks like two digit-groups
        ("2 ,333,333" for 2,333,333; "1 1,125,000" for 11,125,000;
        "7 89,140" for 789,140);
      - inject a stray space between an opening parenthesis and the
        digits it encloses ("( 815,360)"), which would otherwise defeat
        `_parse_amount`'s "(" + ")" sign detection once the shared
        `_NUMBER_TOKEN_RE` tokenises around the space;
      - print a footnote reference ("*" or "**") directly after a
        figure ("( 510,083)**").

    This function repairs all three ahead of tokenisation: it strips
    asterisks outright (they never form part of a genuine amount in this
    document), closes the gap after an opening parenthesis, and merges a
    short digit run into whatever digit/comma run immediately follows it
    across a single space. It is intentionally applied line-by-line, only
    within this module, and only ahead of `_NUMBER_TOKEN_RE.findall()` --
    a single already-complete amount (e.g. one parsed via `_parse_amount`
    directly against a whole "label rest" string) does not need it, since
    `_parse_amount` already strips embedded whitespace on its own.
    """
    text = _ASTERISK_RE.sub("", text)
    text = _OPEN_PAREN_SPACE_RE.sub("(", text)
    prev = None
    while prev != text:
        prev = text
        text = _DIGIT_SPACE_JOIN_RE.sub(r"\1", text)
    return text


def _extract_numbers(line: str) -> list[float | None]:
    """All number-like tokens on a line, in order, each run through the
    same parenthesis/comma/#N-A rules as `_parse_amount`, after this
    module's local digit-spacing repair (see `_repair_l3_number_spacing`)
    -- required whenever more than one amount may sit on the same line
    (schedule rows, the Computation block), since `_NUMBER_TOKEN_RE`
    cannot by itself tell where a pdfplumber-split number ends."""
    text = _FFFD_RUN_RE.sub(" ", line)
    text = _repair_l3_number_spacing(text)
    return [_parse_amount(tok) for tok in _NUMBER_TOKEN_RE.findall(text)]


def _first_amount_on_or_after(lines: list[str], idx: int, rest: str) -> float | None:
    """Mirrors payout_advice.py's lookahead: if the label's own line has
    no amount after the label, look at the next couple of non-empty
    lines (some layouts print the label and the figure on separate
    lines).

    Several labels in the extended Part 1 vocabulary carry a decorative
    parenthetical annotation BEFORE the amount itself -- "PLMI (gross)",
    "PLMI as Share of Profit (gross)" -- so `rest` can be
    "(gross)            8,00,000". `_parse_amount`'s "(" + ")" sign check
    looks at the WHOLE string handed to it, so passing `rest` straight
    through would see the unrelated "(gross)" pair and wrongly read the
    figure as negative. The actual number token is isolated first via
    `_NUMBER_TOKEN_RE`, and only THAT token's own parentheses (if any)
    are allowed to decide the sign."""
    if rest.strip():
        repaired = _repair_l3_number_spacing(rest)
        token_match = _NUMBER_TOKEN_RE.search(repaired)
        if token_match:
            return _parse_amount(token_match.group(0))
        return _parse_amount(repaired)
    for lookahead in lines[idx + 1: idx + 3]:
        if lookahead.strip():
            repaired = _repair_l3_number_spacing(lookahead.strip())
            token_match = _NUMBER_TOKEN_RE.search(repaired)
            if token_match:
                return _parse_amount(token_match.group(0))
            return _parse_amount(repaired)
    return None


def _split_sections(lines: list[str]) -> tuple[list[str], list[str], list[str]]:
    """Splits the document body into (part1, part2, part3) line lists by
    locating the "PAYMENTS" and "SCHEDULE" section-header lines. Both
    header lines themselves are excluded from every section. Either
    header (or both) may be absent -- see Gap 2: a schedule-only revision
    letter must not have its schedule lines swallowed into part1."""
    payments_idx = next((i for i, ln in enumerate(lines) if _PAYMENTS_HEADER_RE.match(ln)), None)
    schedule_idx = next((i for i, ln in enumerate(lines) if _SCHEDULE_HEADER_RE.search(ln)), None)

    if payments_idx is None and schedule_idx is None:
        part1, part2 = lines, []
    elif payments_idx is None:
        part1, part2 = lines[:schedule_idx], []
    elif schedule_idx is None or schedule_idx < payments_idx:
        part1, part2 = lines[:payments_idx], lines[payments_idx + 1:]
    else:
        part1, part2 = lines[:payments_idx], lines[payments_idx + 1: schedule_idx]

    if schedule_idx is None:
        part3: list[str] = []
    else:
        part3 = lines[schedule_idx + 1:]

    return part1, part2, part3


def _split_off_computation(part3_lines: list[str]) -> tuple[list[str], list[str]]:
    """Splits the schedule's line list into (schedule_lines,
    computation_lines) at the "Computation" header, if present."""
    idx = next((i for i, ln in enumerate(part3_lines) if _COMPUTATION_HEADER_RE.match(ln)), None)
    if idx is None:
        return part3_lines, []
    return part3_lines[:idx], part3_lines[idx + 1:]


def _parse_part1(
    lines: list[str], report_year: int | None
) -> tuple[dict[str, float | None], list[str]]:
    """Like `_parse_labelled_section`, but additionally special-cases
    "Total compensation for the year ended 31 Mar NN": that label is used
    for BOTH the current and the prior year, identical apart from the
    year, so it is resolved by comparing the printed year to
    `report_year` -- never by pattern order or line position (Gap 6.1)."""
    values: dict[str, float | None] = {f: None for f in _PART1_FIELDS}
    unknown: list[str] = []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        # The document title line itself ("Compensation summary : Year
        # ended 31 March 2024 40199") carries the FY phrase and a
        # trailing employee id that must never be mistaken for a label
        # row's amount.
        if _FY_PHRASE_RE.search(line):
            continue

        total_comp_m = _TOTAL_COMP_FOR_YEAR_RE.match(line)
        if total_comp_m:
            yr2 = int(total_comp_m.group(1))
            yr_full = yr2 if yr2 > 999 else (2000 + yr2 if yr2 < 100 else yr2)
            rest = line[total_comp_m.end():].strip()
            amount = _first_amount_on_or_after(lines, idx, rest)
            if report_year is not None and yr_full == report_year:
                values["target_compensation"] = amount
            elif report_year is not None and yr_full == report_year - 1:
                values["prior_year_target_compensation"] = amount
            else:
                unknown.append(line.strip())
            continue

        matched = False
        for field_name, pattern in _PART1_ROW_PATTERNS:
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


def _parse_balance_row(
    lines: list[str], idx: int, label_match: "re.Match[str]"
) -> tuple[float | None, bool, str | None]:
    """Parses an opening/closing balance row: returns
    (amount, is_nil, as_on_date). `amount` is 0.0 (not None) when the row
    prints a literal "-" -- see the module docstring's nil-vs-absent
    note."""
    rest = label_match.string[label_match.end():].strip()
    date_m = _BALANCE_DATE_RE.search(rest)
    as_on_date = date_m.group(1).strip() if date_m else None
    amount_part = rest
    if date_m:
        amount_part = (rest[: date_m.start()] + rest[date_m.end():]).strip()

    def _resolve(text: str) -> tuple[float | None, bool]:
        if text.strip() == "-":
            return 0.0, True
        return _parse_amount(_repair_l3_number_spacing(text)), False

    if amount_part:
        amount, is_nil = _resolve(amount_part)
        return amount, is_nil, as_on_date

    for lookahead in lines[idx + 1: idx + 3]:
        s = lookahead.strip()
        if not s:
            continue
        amount, is_nil = _resolve(s)
        return amount, is_nil, as_on_date

    return None, False, as_on_date


def _parse_schedule_row_numbers(rest: str, lines: list[str], start_idx: int, need: int) -> list[float | None]:
    numbers = _extract_numbers(rest) if rest else []
    lookahead_idx = start_idx
    while len(numbers) < need and lookahead_idx + 1 < len(lines):
        lookahead_idx += 1
        nxt = lines[lookahead_idx].strip()
        if not nxt:
            continue
        if (
            _INSTALMENT_ROW_RE_NEW.match(nxt)
            or _INSTALMENT_ROW_RE_OLD.match(nxt)
            or _ARREARS_ROW_RE.match(nxt)
            or _ADDITIONS_ROW_RE.match(nxt)
            or _CLOSING_BALANCE_RE.match(nxt)
            or _TOTALS_SEPARATOR_RE.match(nxt)
        ):
            break
        numbers.extend(_extract_numbers(nxt))
    return numbers


def _parse_schedule(
    lines: list[str],
) -> tuple[
    float | None, bool, str | None,
    float | None, bool, str | None,
    list[ScheduleInstalment],
    ScheduleInstalment | None,
    list[str],
]:
    opening: float | None = None
    opening_is_nil = False
    opening_date: str | None = None
    closing: float | None = None
    closing_is_nil = False
    closing_date: str | None = None
    rows: list[ScheduleInstalment] = []
    totals: ScheduleInstalment | None = None
    unknown: list[str] = []
    expect_totals_next = False

    for idx, line in enumerate(lines):
        if not line.strip():
            continue

        if _NIL_ROW_RE.match(line):
            # A lone "-" is a blank/nil continuation row (e.g. the
            # firm's-tax/capital columns of an opening-balance line
            # printed on their own lines) -- skip, never "unknown".
            continue

        if _TOTALS_SEPARATOR_RE.match(line):
            expect_totals_next = True
            continue

        m = _OPENING_BALANCE_RE.match(line)
        if m:
            opening, opening_is_nil, opening_date = _parse_balance_row(lines, idx, m)
            continue

        m = _CLOSING_BALANCE_RE.match(line)
        if m:
            closing, closing_is_nil, closing_date = _parse_balance_row(lines, idx, m)
            continue

        if expect_totals_next and _BARE_NUMBER_LINE_RE.match(line) and _extract_numbers(line):
            numbers = _parse_schedule_row_numbers(line, lines, idx, 4)
            totals = ScheduleInstalment(
                instalment_no=None,
                label="TOTAL",
                gross=numbers[0] if len(numbers) > 0 else None,
                firms_tax=numbers[1] if len(numbers) > 1 else None,
                capital_contribution=numbers[2] if len(numbers) > 2 else None,
                net=numbers[3] if len(numbers) > 3 else None,
            )
            expect_totals_next = False
            continue
        expect_totals_next = False

        inst_m = _INSTALMENT_ROW_RE_NEW.match(line) or _INSTALMENT_ROW_RE_OLD.match(line)
        if inst_m:
            rest = line[inst_m.end():].strip()
            numbers = _parse_schedule_row_numbers(rest, lines, idx, 4)
            rows.append(
                ScheduleInstalment(
                    instalment_no=int(inst_m.group(1)),
                    gross=numbers[0] if len(numbers) > 0 else None,
                    firms_tax=numbers[1] if len(numbers) > 1 else None,
                    capital_contribution=numbers[2] if len(numbers) > 2 else None,
                    net=numbers[3] if len(numbers) > 3 else None,
                )
            )
            continue

        arrears_m = _ARREARS_ROW_RE.match(line)
        if arrears_m:
            rest = line[arrears_m.end():].strip()
            numbers = _parse_schedule_row_numbers(rest, lines, idx, 4)
            rows.append(
                ScheduleInstalment(
                    instalment_no=None,
                    label=line.strip().split("  ")[0].strip() or "Arrears",
                    gross=numbers[0] if len(numbers) > 0 else None,
                    firms_tax=numbers[1] if len(numbers) > 1 else None,
                    capital_contribution=numbers[2] if len(numbers) > 2 else None,
                    net=numbers[3] if len(numbers) > 3 else None,
                )
            )
            continue

        additions_m = _ADDITIONS_ROW_RE.match(line)
        if additions_m:
            rest = line[additions_m.end():].strip()
            numbers = _parse_schedule_row_numbers(rest, lines, idx, 2)
            rows.append(
                ScheduleInstalment(
                    instalment_no=None,
                    label="Additions pertaining to prior year",
                    gross=numbers[0] if len(numbers) > 0 else None,
                    firms_tax=None,
                    capital_contribution=None,
                    net=numbers[1] if len(numbers) > 1 else None,
                )
            )
            continue

        if _LOOKS_LIKE_LABEL_ROW_RE.match(line) and _extract_numbers(line):
            unknown.append(line.strip())

    return (
        opening, opening_is_nil, opening_date,
        closing, closing_is_nil, closing_date,
        rows, totals, unknown,
    )


def _parse_computation(lines: list[str]) -> tuple[dict, list[str]]:
    values: dict = {f: None for f in _COMPUTATION_FIELDS}
    unknown: list[str] = []
    for idx, line in enumerate(lines):
        if not line.strip():
            continue
        matched = False
        for field_name, pattern in _COMPUTATION_ROW_PATTERNS:
            m = pattern.match(line)
            if not m:
                continue
            matched = True
            rest = line[m.end():].strip()
            if field_name in ("months_expected", "months_achieved"):
                nums = _extract_numbers(rest) if rest else []
                if not nums:
                    for lookahead in lines[idx + 1: idx + 3]:
                        if lookahead.strip():
                            nums = _extract_numbers(lookahead.strip())
                            break
                values[field_name] = int(nums[0]) if nums and nums[0] is not None else None
            elif field_name in ("percent_required", "percent_achieved"):
                pct_m = re.search(r"([\d.]+)\s*%", rest)
                values[field_name] = float(pct_m.group(1)) if pct_m else None
            else:
                values[field_name] = _first_amount_on_or_after(lines, idx, rest)
            break
        if matched:
            continue
        if _LOOKS_LIKE_LABEL_ROW_RE.match(line) and _extract_numbers(line):
            unknown.append(line.strip())
    return values, unknown


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
    has_schedule = any(_SCHEDULE_HEADER_RE.search(ln) for ln in lines)

    if not (fy_match and (has_payments or has_schedule)):
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
    schedule_lines, computation_lines = _split_off_computation(part3_lines)

    part1_values, part1_unknown = _parse_part1(part1_lines, report_year)
    part2_values, part2_unknown = (
        _parse_labelled_section(part2_lines, _PART2_ROW_PATTERNS) if has_payments else ({f: None for f in _PART2_FIELDS}, [])
    )
    (
        opening, opening_is_nil, opening_date,
        closing, closing_is_nil, closing_date,
        instalments, totals, part3_unknown,
    ) = _parse_schedule(schedule_lines) if has_schedule else (None, False, None, None, False, None, [], None, [])
    computation_values, computation_unknown = (
        _parse_computation(computation_lines) if computation_lines else ({f: None for f in _COMPUTATION_FIELDS}, [])
    )

    diagnostics: list[str] = []

    if not has_payments:
        diagnostics.append(
            "NOTE: PAYMENTS section not present in this document -- "
            "Part 2 fields are None."
        )
    if not has_schedule:
        diagnostics.append(
            "NOTE: SCHEDULE section not present in this document -- "
            "Part 3 fields are None."
        )

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
        label = inst.label or f"instalment {inst.instalment_no}"
        if inst.firms_tax is None and inst.capital_contribution is None:
            # A two-figure row (e.g. "Additions pertaining to prior
            # year"): only gross and net are printed, and they are
            # expected to be equal.
            if inst.gross is not None and inst.net is not None:
                if abs(inst.gross - inst.net) > 0.01:
                    diagnostics.append(
                        f"ERROR: SCHEDULE row '{label}' does not reconcile -- "
                        f"gross ({inst.gross}) != net ({inst.net})."
                    )
            continue
        if None in (inst.gross, inst.firms_tax, inst.capital_contribution, inst.net):
            continue
        computed = inst.gross + inst.firms_tax + inst.capital_contribution
        if abs(computed - inst.net) > 0.01:
            diagnostics.append(
                f"ERROR: SCHEDULE row '{label}' does not "
                f"reconcile -- gross ({inst.gross}) + firms_tax "
                f"({inst.firms_tax}) + capital_contribution "
                f"({inst.capital_contribution}) = {computed}, but the "
                f"printed net is {inst.net} (diff {computed - inst.net})."
            )

    if totals is not None:
        sum_gross = sum(i.gross for i in instalments if i.gross is not None)
        sum_firms_tax = sum(i.firms_tax for i in instalments if i.firms_tax is not None)
        sum_capital = sum(i.capital_contribution for i in instalments if i.capital_contribution is not None)
        sum_net = sum(i.net for i in instalments if i.net is not None)
        for col_name, computed_sum, printed_total in (
            ("gross", sum_gross, totals.gross),
            ("firms_tax", sum_firms_tax, totals.firms_tax),
            ("capital_contribution", sum_capital, totals.capital_contribution),
            ("net", sum_net, totals.net),
        ):
            if printed_total is None:
                continue
            if abs(computed_sum - printed_total) > 0.01:
                diagnostics.append(
                    f"ERROR: SCHEDULE totals row does not reconcile on "
                    f"'{col_name}' -- sum of rows is {computed_sum}, but the "
                    f"printed total is {printed_total} "
                    f"(diff {computed_sum - printed_total})."
                )

    computation: Computation | None = None
    if computation_lines:
        computation = Computation(**computation_values)
        tc = computation.tc_for_fy
        pct_req = computation.percent_required
        months_ach = computation.months_achieved
        months_exp = computation.months_expected
        contribution_required = computation.contribution_required
        if None not in (tc, pct_req, months_ach, months_exp, contribution_required) and months_exp:
            expected = (pct_req / 100) * tc * (months_ach / months_exp)
            if abs(expected - contribution_required) > 0.01:
                diagnostics.append(
                    "ERROR: Computation block does not reconcile -- "
                    f"{pct_req}% x {tc} x {months_ach}/{months_exp} = "
                    f"{expected}, but the printed Contribution required is "
                    f"{contribution_required} (diff {expected - contribution_required})."
                )

    unknown_labels = [*part1_unknown, *part2_unknown, *part3_unknown, *computation_unknown]

    sections_present = {
        "part1": any(v is not None for v in part1_values.values()),
        "part2": has_payments,
        "schedule": has_schedule,
        "computation": computation is not None,
    }
    section_sources = {
        "part1": source_name if sections_present["part1"] else None,
        "part2": source_name if sections_present["part2"] else None,
        "schedule": source_name if sections_present["schedule"] else None,
        "computation": source_name if sections_present["computation"] else None,
    }

    record = L3AdvisoryRecord(
        financial_year=financial_year,
        source_name=source_name,
        salary=part1_values["salary"],
        remuneration=part1_values["remuneration"],
        share_of_profit=part1_values["share_of_profit"],
        arrears=part1_values["arrears"],
        incentive_gross=part1_values["incentive_gross"],
        monthly_drawings_gross=part1_values["monthly_drawings_gross"],
        plmi_gross=part1_values["plmi_gross"],
        plmi_share_of_profit_gross=part1_values["plmi_share_of_profit_gross"],
        compensation_paid_base_and_incentive=part1_values["compensation_paid_base_and_incentive"],
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
        schedule_opening_balance_is_nil=opening_is_nil,
        schedule_opening_balance_date=opening_date,
        schedule_projected_closing_balance=closing,
        schedule_closing_balance_is_nil=closing_is_nil,
        schedule_closing_balance_date=closing_date,
        schedule_instalments=instalments,
        schedule_totals=totals,
        computation=computation,
        sections_present=sections_present,
        section_sources=section_sources,
        unknown_labels=unknown_labels,
        diagnostics=diagnostics,
    )
    return record.to_dict()


# ---------------------------------------------------------------------------
# Multi-document merge (Gap 7).
# ---------------------------------------------------------------------------

_SCHEDULE_FIELD_NAMES = [
    "schedule_opening_balance",
    "schedule_opening_balance_is_nil",
    "schedule_opening_balance_date",
    "schedule_projected_closing_balance",
    "schedule_closing_balance_is_nil",
    "schedule_closing_balance_date",
    "schedule_instalments",
    "schedule_totals",
]
_COMPUTATION_FIELD_NAMES = ["computation"]

_SECTION_FIELD_GROUPS = {
    "part1": _PART1_FIELDS,
    "part2": _PART2_FIELDS,
    "schedule": _SCHEDULE_FIELD_NAMES,
    "computation": _COMPUTATION_FIELD_NAMES,
}


def merge_advisories(records: list[dict]) -> dict:
    """Merge parsed L3AdvisoryRecord dicts (see `parse_l3_text`) for ONE
    financial year, supplied OLDEST-FIRST. For each of the four sections
    (part1 / part2 / schedule / computation), the newest document that
    actually CARRIES that section wins for every field in it; an earlier
    document supplies whatever section a later one omits entirely. This
    is NOT "pick the most recent file" -- a schedule-only reissue must
    not discard an earlier document's Part 1/Part 2.

    Returns a dict shaped like `L3AdvisoryRecord.to_dict()`, plus
    `section_sources` recording which `source_name` supplied each
    section, and `diagnostics` extended with a "NOTE: ..." entry for
    every field two documents both carry with disagreeing values (the
    newer document's value is kept; the disagreement is never silently
    dropped).

    Does NOT glob the filesystem or infer order from filenames -- the
    caller is responsible for supplying `records` in the correct
    (oldest-first) order.
    """
    if not records:
        raise ValueError("merge_advisories() requires at least one record")

    merged = dict(records[0])
    sections_present = dict(records[0]["sections_present"])
    section_sources = {
        section: (records[0]["source_name"] if sections_present[section] else None)
        for section in _SECTION_FIELD_GROUPS
    }
    diagnostics = list(records[0].get("diagnostics", []))

    for rec in records[1:]:
        diagnostics.extend(rec.get("diagnostics", []))
        rec_sections_present = rec.get("sections_present", {})
        for section, field_names in _SECTION_FIELD_GROUPS.items():
            if not rec_sections_present.get(section):
                continue
            prior_source = section_sources[section]
            if prior_source is not None:
                for f in field_names:
                    old_val = merged.get(f)
                    new_val = rec.get(f)
                    if old_val is not None and new_val is not None and old_val != new_val:
                        diagnostics.append(
                            f"NOTE: field '{f}' disagrees between "
                            f"{prior_source} ({old_val!r}) and "
                            f"{rec.get('source_name')} ({new_val!r}) -- the "
                            "newer document's value is kept."
                        )
            for f in field_names:
                merged[f] = rec.get(f)
            section_sources[section] = rec.get("source_name")
            sections_present[section] = True

    merged["sections_present"] = sections_present
    merged["section_sources"] = section_sources
    merged["diagnostics"] = diagnostics
    merged["source_name"] = ", ".join(
        dict.fromkeys(r["source_name"] for r in records if r.get("source_name"))
    )
    return merged


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
