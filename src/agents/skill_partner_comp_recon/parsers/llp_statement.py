"""
llp_statement.py -- L5 parser: the LLP's own annual "Statement of Account"
for a partner (an internal accounting-system export of the partner's
CAPITAL ACCOUNT and CURRENT ACCOUNT movements for a financial year ending
31 March, issued months after that year-end).

Kept in the same two layers as payout_advice.py / advisory.py (read those
modules' docstrings first, this one mirrors their conventions):

  - `parse_l5_words()` is PURE: it takes a list of word dicts shaped like
    pdfplumber's `page.extract_words()` output (`{"text", "x0", "x1",
    "top", "bottom"}`) and returns a plain dict. No filesystem, no
    pdfplumber, no password. Every test in
    tests/test_skill_partner_comp_recon.py drives this function directly
    against a synthetic, invented word list -- no real specimen is ever
    generated or committed (see AGENT.md's Stage 2 section and CLAUDE.md's
    privacy constraint). This module is coordinate-based, not text-based,
    because the real document's layout traps (below) can only be handled
    correctly with word-box geometry -- `page.extract_text()` throws that
    geometry away.
  - `parse(path, password)` is the thin shell: opens the PDF with
    pdfplumber (password-aware), extracts every page's words, and calls
    `parse_l5_words()`. This is the only function in this module that
    touches the filesystem.

Document layout (authoritative; transcribed from first-hand reading of a
real specimen -- no real specimen is used anywhere in code or tests):

  A single page, headed by a combined "CAPITAL ACCOUNT" / "CURRENT
  ACCOUNT" column-header row. Every row below it prints a label on the
  left and, depending on the label, an amount under CAPITAL ACCOUNT,
  under CURRENT ACCOUNT, or under both. "Interest on Capital" is a
  separate, labelled row that prints under CAPITAL ACCOUNT only -- unlike
  the L1 payout certificate, where interest on capital has NO separate
  label and is folded into "Add. Share of Profit" (see parsers/__init__.py
  for that L1-specific fact). Withdrawal amounts print parenthesised
  (negative). Section markers "ADDITIONS:-" and "WITHDRAWALS:-" bound the
  rows that must be summed to check against the printed "Total additions"
  / "Total withdrawals" rows; the row set inside each section is NOT
  fixed -- an unrecognised row inside a section still counts toward that
  section's sum (and is separately reported in `unknown_labels`, never
  silently dropped).

Four confirmed parsing traps, each handled explicitly below (do not
"simplify" any of these away -- each one corrupts real figures silently
if skipped):

  (a) A single printed number can be split across x-adjacent word tokens
      with no space between them, including a split-off leading "(" --
      e.g. "(" and "12,345,678)" as two separate tokens. Tokens on the
      same row are joined by x-adjacency (a gap under ~1.5pt), never by
      whitespace -- see `_merge_row_tokens()`.
  (b) A row's label and its amount can disagree in `top` by about 1pt.
      Rows are grouped by a +/-2pt top tolerance, never exact equality --
      see `_group_rows()`.
  (c) A printed "-" means the figure is 0.0 (a nil that WAS printed) --
      distinct from a label that never appears on the page at all, which
      stays None (absent). Never coerce one into the other.
  (d) The statement is issued months after the financial year-end, so the
      `llp_statement` input to this skill stays OPTIONAL end-to-end;
      absent, the capital sign-off leg degrades to an explicit "not
      available" note (see agent.py's `_resolve_optional_leg`) -- this
      module does not change that.

Column boundary is DERIVED AT RUNTIME from the header row's own word
x-coordinates every time (the midpoint between the CAPITAL ACCOUNT
heading's right edge and the CURRENT ACCOUNT heading's left edge) -- it
is never a hardcoded pixel/point constant, because different exports of
this same LLP-side system are not guaranteed to lay the columns out at
identical coordinates.

Design notes carried over from parsers/__init__.py's module docstring
(every one of them is enforced here too):

  - MAP BY LABEL, NEVER ROW POSITION.
  - DISPATCH ON DOCUMENT CONTENT, NEVER FILENAME: this module identifies
    an L5 document by requiring BOTH a combined CAPITAL ACCOUNT / CURRENT
    ACCOUNT header row AND a "STATEMENT OF ACCOUNT OF" or "AS ON <day>
    MARCH, <year>" phrase; anything else raises `NotAnL5DocumentError`
    naming what is missing (or, for a recognisable L1/L2/L3 document,
    naming which family it looks like instead) so a directory-walking
    caller can skip it cleanly.
  - THE ROW SET CHANGES BETWEEN YEARS -- an absent label is None, never 0.
  - NEGATIVES ARE PARENTHESISED; thousands separators are commas -- reuses
    `payout_advice._parse_amount()` rather than re-implementing that
    parsing.
  - NO RATE IS EVER HARDCODED HERE.
  - The financial year is derived from the "AS ON <day> MARCH, <year>"
    phrase (FY = (year-1)-yy); the printed "ASSESSMENT YEAR : YYYY-YY"
    line is cross-checked against that derived FY+1 and a disagreement is
    a fail-loud "ERROR: ..." diagnostic, never silently trusted or
    silently ignored. The year is NEVER inferred from the filename.
"""
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from .payout_advice import _FFFD_RUN_RE, _parse_amount

# ---------------------------------------------------------------------------
# Content-dispatch markers.
# ---------------------------------------------------------------------------

_HEADER_ROW_RE = re.compile(r"capital\s+account.*current\s+account", re.IGNORECASE)
_STATEMENT_OF_ACCOUNT_RE = re.compile(r"statement\s+of\s+account\s+of", re.IGNORECASE)
_AS_ON_MARCH_RE = re.compile(r"as\s+on\s+(\d{1,2})\s+march,?\s*(\d{4})", re.IGNORECASE)
_PREVIOUS_YEAR_RE = re.compile(r"previous\s+year\s*:\s*31\s+march,?\s*(\d{4})", re.IGNORECASE)
_ASSESSMENT_YEAR_RE = re.compile(r"assessment\s+year\s*:\s*(\d{4})-(\d{2})", re.IGNORECASE)
_STATEMENT_DATE_RE = re.compile(r"\b(\d{1,2})-([A-Za-z]{3})-(\d{4})\b")

_CURRENT_WORD_RE = re.compile(r"^current$", re.IGNORECASE)

_FOOTER_RE = re.compile(
    r"kpmg\s+india\s+services\s+llp|computer\s+generated\s+advice", re.IGNORECASE
)

_AMOUNT_CANDIDATE_RE = re.compile(r"^\(?[0-9][0-9,]*\.?[0-9]*\)?$|^-$")

# Markers for a recognisable non-L5 document, so a directory-walking
# caller gets a descriptive rejection rather than a generic one.
_NON_L5_L1_HEADING_RE = re.compile(r"to\s+whomsoever\s+it\s+may\s+concern", re.IGNORECASE)
_NON_L5_SALARY_RE = re.compile(r"salary\s+statement\s+for", re.IGNORECASE)
_NON_L5_L3_PAYMENTS_RE = re.compile(r"^\s*payments\b", re.IGNORECASE | re.MULTILINE)
_NON_L5_L3_SCHEDULE_RE = re.compile(r"^\s*schedule\b", re.IGNORECASE | re.MULTILINE)


class NotAnL5DocumentError(ValueError):
    """Raised by parse_l5_words()/parse() when the supplied words/PDF are
    not an L5 LLP Statement of Account. A directory-walking caller should
    catch this and skip the file cleanly -- never crash, never misparse
    it as a statement of account."""


# ---------------------------------------------------------------------------
# Row labels -> (capital field name, current field name). `None` in
# either slot means that column is not modelled for this label. Order
# matters only in that more specific labels ("Total additions") are
# listed before less specific ones that could otherwise shadow them.
# ---------------------------------------------------------------------------

_ROW_FIELD_MAP: list[tuple[str, "re.Pattern[str]", str | None, str | None]] = [
    ("total_additions", re.compile(r"^\s*total\s+additions\b", re.IGNORECASE),
     "capital_total_additions", "current_total_additions"),
    ("total_withdrawals", re.compile(r"^\s*total\s+withdrawals\b", re.IGNORECASE),
     "capital_total_withdrawals", "current_total_withdrawals"),
    ("opening_balance", re.compile(r"^\s*opening\s+balance\b", re.IGNORECASE),
     "capital_opening_balance", "current_opening_balance"),
    ("closing_balance", re.compile(r"^\s*closing\s+balance\b", re.IGNORECASE),
     "capital_closing_balance", "current_closing_balance"),
    ("introduced_transferred", re.compile(r"^\s*introduced\s*/\s*transferred\b", re.IGNORECASE),
     "capital_introduced_transferred", None),
    ("interest_on_capital", re.compile(r"^\s*interest\s+on\s+capital\b", re.IGNORECASE),
     "capital_interest_on_capital", None),
    ("profit_share", re.compile(r"^\s*profit\s+share\s+for\s+the\s+year\b", re.IGNORECASE),
     None, "current_profit_share"),
    ("remuneration", re.compile(r"^\s*remuneration\b", re.IGNORECASE),
     None, "current_remuneration"),
    ("transfer_to_capital", re.compile(r"^\s*transfer\s+to\s+capital\s+account\b", re.IGNORECASE),
     None, "current_transfer_to_capital"),
    ("drawings", re.compile(r"^\s*drawings\b", re.IGNORECASE),
     "capital_drawings", "current_drawings"),
]

_ADDITIONS_HEADER_RE = re.compile(r"^\s*additions\s*:?-*\s*$", re.IGNORECASE)
_WITHDRAWALS_HEADER_RE = re.compile(r"^\s*withdrawals\s*:?-*\s*$", re.IGNORECASE)

_CAPITAL_FIELD_NAMES = [
    "capital_opening_balance",
    "capital_introduced_transferred",
    "capital_interest_on_capital",
    "capital_total_additions",
    "capital_drawings",
    "capital_total_withdrawals",
    "capital_closing_balance",
]
_CURRENT_FIELD_NAMES = [
    "current_opening_balance",
    "current_remuneration",
    "current_profit_share",
    "current_total_additions",
    "current_drawings",
    "current_transfer_to_capital",
    "current_total_withdrawals",
    "current_closing_balance",
]


# ---------------------------------------------------------------------------
# Geometry helpers.
# ---------------------------------------------------------------------------

def _group_rows(words: list[dict], tol: float = 2.0) -> list[list[dict]]:
    """Group words into rows using a +/-`tol` tolerance on `top`, never
    exact equality -- trap (b): a row's label and amount can disagree by
    about 1pt. Rows are returned in top-to-bottom order, each row's words
    left-to-right by `x0`."""
    ordered = sorted(words, key=lambda w: w["top"])
    rows: list[list[dict]] = []
    row_top: float | None = None
    current: list[dict] = []
    for w in ordered:
        if row_top is None or (w["top"] - row_top) <= tol:
            current.append(w)
            if row_top is None:
                row_top = w["top"]
        else:
            rows.append(current)
            current = [w]
            row_top = w["top"]
    if current:
        rows.append(current)
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def _merge_row_tokens(row_words: list[dict], gap: float = 1.5) -> list[dict]:
    """Join x-adjacent word tokens (a gap under `gap` pt) into a single
    token with no separator -- trap (a): a printed number (including a
    split-off leading "(") can arrive as several word tokens. `row_words`
    must already be sorted left-to-right."""
    tokens: list[dict] = []
    for w in row_words:
        if tokens and (w["x0"] - tokens[-1]["x1"]) <= gap:
            tokens[-1] = {
                "text": tokens[-1]["text"] + w["text"],
                "x0": tokens[-1]["x0"],
                "x1": max(tokens[-1]["x1"], w["x1"]),
            }
        else:
            tokens.append({"text": w["text"], "x0": w["x0"], "x1": w["x1"]})
    return tokens


def _rows_text(rows: list[list[dict]]) -> str:
    return "\n".join(" ".join(w["text"] for w in row) for row in rows)


# ---------------------------------------------------------------------------
# Reconciliation helpers -- s.6. Every check appends an "ERROR: ..."
# diagnostic on failure, or a "NOTE: ... skipped -- <names> not present"
# when a required component is absent (missing is not a failure). Nothing
# is ever raised; the record always comes back, diagnostics carried on it.
# ---------------------------------------------------------------------------

def _balance_check(diagnostics: list[str], fields: dict, prefix: str) -> None:
    names = {
        "opening": f"{prefix}_opening_balance",
        "additions": f"{prefix}_total_additions",
        "withdrawals": f"{prefix}_total_withdrawals",
        "closing": f"{prefix}_closing_balance",
    }
    values = {k: fields[v] for k, v in names.items()}
    missing = [names[k] for k, v in values.items() if v is None]
    if missing:
        diagnostics.append(
            f"NOTE: {prefix} balance roll-forward (opening + additions + "
            f"withdrawals == closing) skipped -- {', '.join(missing)} not present."
        )
        return
    computed = values["opening"] + values["additions"] + values["withdrawals"]
    closing = values["closing"]
    if abs(computed - closing) > 0.01:
        diagnostics.append(
            f"ERROR: {prefix} balance roll-forward does not reconcile -- "
            f"opening={values['opening']:,.2f}, additions={values['additions']:,.2f}, "
            f"withdrawals={values['withdrawals']:,.2f}; computed {computed:,.2f} vs "
            f"printed closing {closing:,.2f} (diff {computed - closing:,.2f})."
        )


def _section_sum_check(
    diagnostics: list[str], prefix: str, section_name: str, computed_sum: float,
    fields: dict, total_field: str,
) -> None:
    printed = fields[total_field]
    if printed is None:
        diagnostics.append(
            f"NOTE: {prefix} {section_name} row-sum check skipped -- "
            f"{total_field} not present."
        )
        return
    if abs(computed_sum - printed) > 0.01:
        diagnostics.append(
            f"ERROR: {prefix} {section_name} rows do not sum to the printed "
            f"total -- computed {computed_sum:,.2f} vs printed {printed:,.2f} "
            f"(diff {computed_sum - printed:,.2f})."
        )


# ---------------------------------------------------------------------------
# PURE core.
# ---------------------------------------------------------------------------

def parse_l5_words(words: list[dict], source_name: str = "") -> dict:
    """PURE: takes one L5 LLP Statement of Account's extracted words (each
    a dict shaped like pdfplumber's `page.extract_words()`: `{"text",
    "x0", "x1", "top", "bottom"}`) and returns the record dict. Raises
    NotAnL5DocumentError if `words` is not an L5 statement -- callers
    walking a directory of mixed documents should catch that and skip the
    file.
    """
    cleaned_words = []
    for w in words:
        text = _FFFD_RUN_RE.sub("", w["text"])
        if text:
            cleaned_words.append({**w, "text": text})

    rows = _group_rows(cleaned_words)
    full_text = _rows_text(rows)

    header_row_idx = None
    for i, row in enumerate(rows):
        row_text = " ".join(w["text"] for w in row)
        if _HEADER_ROW_RE.search(row_text):
            header_row_idx = i
            break

    has_statement_phrase = bool(
        _STATEMENT_OF_ACCOUNT_RE.search(full_text) or _AS_ON_MARCH_RE.search(full_text)
    )

    if header_row_idx is None or not has_statement_phrase:
        if _NON_L5_L1_HEADING_RE.search(full_text):
            raise NotAnL5DocumentError(
                f"{source_name or '<words>'}: looks like an L1 monthly payout "
                "certificate (\"To Whomsoever It may concern\"), not an L5 LLP "
                "Statement of Account -- skipped."
            )
        if _NON_L5_SALARY_RE.search(full_text):
            raise NotAnL5DocumentError(
                f"{source_name or '<words>'}: looks like a salary statement "
                "(\"SALARY STATEMENT FOR\"), not an L5 LLP Statement of Account "
                "-- skipped."
            )
        if _NON_L5_L3_PAYMENTS_RE.search(full_text) and _NON_L5_L3_SCHEDULE_RE.search(full_text):
            raise NotAnL5DocumentError(
                f"{source_name or '<words>'}: looks like an L3 Compensation "
                "Advisory letter (PAYMENTS/SCHEDULE section headers), not an "
                "L5 LLP Statement of Account -- skipped."
            )
        missing = []
        if header_row_idx is None:
            missing.append('a combined "CAPITAL ACCOUNT" / "CURRENT ACCOUNT" header row')
        if not has_statement_phrase:
            missing.append(
                'a "STATEMENT OF ACCOUNT OF" or "AS ON <day> MARCH, <year>" phrase'
            )
        raise NotAnL5DocumentError(
            f"{source_name or '<words>'}: missing " + " and ".join(missing) +
            " -- not an L5 LLP Statement of Account, skipped."
        )

    header_row = rows[header_row_idx]
    current_word = next((w for w in header_row if _CURRENT_WORD_RE.match(w["text"])), None)
    if current_word is None:
        raise NotAnL5DocumentError(
            f"{source_name or '<words>'}: header row matched the CAPITAL "
            "ACCOUNT / CURRENT ACCOUNT text but no standalone \"CURRENT\" word "
            "token was found to derive the column boundary from -- not an L5 "
            "LLP Statement of Account, skipped."
        )
    capital_words = [w for w in header_row if w["x0"] < current_word["x0"]]
    if not capital_words:
        raise NotAnL5DocumentError(
            f"{source_name or '<words>'}: header row's CAPITAL ACCOUNT heading "
            "words could not be located left of CURRENT -- not an L5 LLP "
            "Statement of Account, skipped."
        )
    region_start = min(w["x0"] for w in capital_words)
    capital_right_edge = max(w["x1"] for w in capital_words)
    boundary = (capital_right_edge + current_word["x0"]) / 2.0

    footer_row_idx = len(rows)
    for i in range(header_row_idx + 1, len(rows)):
        row_text = " ".join(w["text"] for w in rows[i])
        if _FOOTER_RE.search(row_text):
            footer_row_idx = i
            break
    table_rows = rows[header_row_idx + 1: footer_row_idx]

    diagnostics: list[str] = []

    as_on_match = _AS_ON_MARCH_RE.search(full_text)
    prev_match = _PREVIOUS_YEAR_RE.search(full_text)
    ay_match = _ASSESSMENT_YEAR_RE.search(full_text)
    stmt_date_match = _STATEMENT_DATE_RE.search(full_text)

    as_on_date = None
    financial_year = None
    if as_on_match:
        day, year = as_on_match.group(1), as_on_match.group(2)
        as_on_date = f"{year}-03-{int(day):02d}"
        year_i = int(year)
        financial_year = f"{year_i - 1}-{str(year_i)[2:]}"

    previous_year_end = None
    if prev_match:
        previous_year_end = f"{int(prev_match.group(1))}-03-31"

    assessment_year = None
    if ay_match:
        assessment_year = f"{ay_match.group(1)}-{ay_match.group(2)}"
        if as_on_match is not None:
            as_on_year = int(as_on_match.group(2))
            expected_ay = f"{as_on_year}-{str(as_on_year + 1)[2:]}"
            if assessment_year != expected_ay:
                diagnostics.append(
                    f"ERROR: ASSESSMENT YEAR ({assessment_year}) does not follow "
                    f"the AS ON year ({as_on_year}) -- expected {expected_ay}."
                )

    statement_date = None
    if stmt_date_match:
        statement_date = (
            f"{stmt_date_match.group(1)}-{stmt_date_match.group(2)}-"
            f"{stmt_date_match.group(3)}"
        )

    fields: dict[str, float | None] = {
        name: None for name in (*_CAPITAL_FIELD_NAMES, *_CURRENT_FIELD_NAMES)
    }
    unknown_labels: list[str] = []
    section: str | None = None
    addition_sum = {"capital": 0.0, "current": 0.0}
    withdrawal_sum = {"capital": 0.0, "current": 0.0}

    for row in table_rows:
        tokens = _merge_row_tokens(row)
        label_parts: list[str] = []
        capital_val: float | None = None
        current_val: float | None = None
        for tok in tokens:
            if tok["x0"] < region_start:
                label_parts.append(tok["text"])
                continue
            text = tok["text"].strip()
            if text == "-":
                val = 0.0
            elif _AMOUNT_CANDIDATE_RE.match(text):
                val = _parse_amount(text)
                if val is None:
                    label_parts.append(tok["text"])
                    continue
            else:
                label_parts.append(tok["text"])
                continue
            mid = (tok["x0"] + tok["x1"]) / 2.0
            if mid < boundary:
                capital_val = val
            else:
                current_val = val

        label = " ".join(label_parts).strip()
        if not label:
            continue

        if _ADDITIONS_HEADER_RE.match(label):
            section = "additions"
            continue
        if _WITHDRAWALS_HEADER_RE.match(label):
            section = "withdrawals"
            continue

        matched = None
        for key, pattern, cap_field, cur_field in _ROW_FIELD_MAP:
            if pattern.match(label):
                matched = (key, cap_field, cur_field)
                break

        if matched is not None:
            key, cap_field, cur_field = matched
            if cap_field and capital_val is not None:
                fields[cap_field] = capital_val
            if cur_field and current_val is not None:
                fields[cur_field] = current_val
            if key in ("total_additions", "total_withdrawals"):
                section = None
                continue
            if key in ("opening_balance", "closing_balance"):
                continue
        else:
            if capital_val is not None or current_val is not None:
                unknown_labels.append(label)

        if section == "additions":
            if capital_val is not None:
                addition_sum["capital"] += capital_val
            if current_val is not None:
                addition_sum["current"] += current_val
        elif section == "withdrawals":
            if capital_val is not None:
                withdrawal_sum["capital"] += capital_val
            if current_val is not None:
                withdrawal_sum["current"] += current_val

    _balance_check(diagnostics, fields, "capital")
    _balance_check(diagnostics, fields, "current")
    _section_sum_check(diagnostics, "capital", "additions", addition_sum["capital"], fields, "capital_total_additions")
    _section_sum_check(diagnostics, "current", "additions", addition_sum["current"], fields, "current_total_additions")
    _section_sum_check(diagnostics, "capital", "withdrawals", withdrawal_sum["capital"], fields, "capital_total_withdrawals")
    _section_sum_check(diagnostics, "current", "withdrawals", withdrawal_sum["current"], fields, "current_total_withdrawals")

    transfer = fields.get("current_transfer_to_capital")
    introduced = fields.get("capital_introduced_transferred")
    if transfer is not None and introduced is not None:
        matches = abs(abs(transfer) - introduced) <= 0.01
        diagnostics.append(
            f"NOTE: abs(current_transfer_to_capital)={abs(transfer):,.2f} "
            f"{'matches' if matches else 'does not match'} "
            f"capital_introduced_transferred={introduced:,.2f}."
        )

    record = {
        "source_name": source_name,
        "document_type": "llp_statement",
        "statement_date": statement_date,
        "as_on_date": as_on_date,
        "previous_year_end": previous_year_end,
        "financial_year": financial_year,
        "assessment_year": assessment_year,
        "unknown_labels": unknown_labels,
        "diagnostics": diagnostics,
    }
    record.update({name: fields[name] for name in _CAPITAL_FIELD_NAMES})
    record.update({name: fields[name] for name in _CURRENT_FIELD_NAMES})
    return record


# ---------------------------------------------------------------------------
# Thin PDF-opening shell -- the only function here that touches the
# filesystem / pdfplumber.
# ---------------------------------------------------------------------------

def parse(path: str, password: str | None = None) -> dict:
    """Open the LLP Statement of Account PDF at `path` (password-aware;
    `password` may be None/empty for an unprotected file), extract every
    page's words (coordinate-preserving -- `page.extract_words()`, never
    `page.extract_text()`, since this layout needs word-box geometry --
    see the trap notes in this module's docstring), and return the parsed
    record -- see parse_l5_words(). Raises NotAnL5DocumentError if the PDF
    is not an L5 document (e.g. an L1/L3 document sharing the same
    directory).
    """
    words: list[dict] = []
    with pdfplumber.open(str(path), password=password or "") as pdf:
        for page in pdf.pages:
            words.extend(page.extract_words())
    return parse_l5_words(words, source_name=Path(path).name)
