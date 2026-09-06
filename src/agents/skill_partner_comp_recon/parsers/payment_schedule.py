"""
payment_schedule.py -- L4 parser: the firm's "payment schedule", issued per
financial year, per partner (password-protected, typically 2+ pages). This
is NOT a narrow instalment-dates-and-amounts list -- it is a full
month-by-month schedule of that entire financial year's compensation and
recovery lines, of which prior-year incentive instalments ("Previous Year
PLMIs") are only one row among many.

Kept in the same two layers as llp_statement.py (read that module's
docstring first, this one mirrors its conventions and reuses its geometry
helpers):

  - `parse_payment_schedule_pages()` is PURE: it takes `pages`, a list of
    per-page word lists, each word shaped like pdfplumber's
    `page.extract_words()` output (`{"text", "x0", "x1", "top", "bottom"}`).
    No filesystem, no pdfplumber, no password. Every test in
    tests/test_skill_partner_comp_recon.py drives this function directly
    against a synthetic, invented word list -- no real specimen is ever
    generated or committed.
  - `parse(path, password)` is the thin shell: opens the PDF with
    pdfplumber (password-aware), extracts every page's words, and calls
    `parse_payment_schedule_pages()`. This is the only function in this
    module that touches the filesystem.

Document layout (authoritative; transcribed from first-hand reading of a
real specimen -- no real specimen is used anywhere in code or tests):

  Page 1 is a wide landscape grid: one row per compensation/recovery line,
  one column per month, plus a printed "Total" column. Directly under the
  month header row, a flag row carries a literal "Actual" or "Forecast"
  per month -- a schedule issued mid-year is "Actual" for elapsed months
  and "Forecast" for the rest, and a caller MUST be able to refuse to book
  a forecast month, so this is captured per month in `month_status`. Three
  printed subtotal rows -- "Total Gross Payment", "Total Recovery", "Total
  Payout" -- close the page-1 arithmetic:
  `Total Gross Payment + Total Recovery == Total Payout`, per month and for
  the year. Two distinct firm-tax rows exist ("Firm Tax on SOP", attaching
  to the current year's share of profit, and "Firm Tax (Others)",
  attaching to prior-year incentive instalments) and must NOT be merged --
  collapsing them into one field is a known, specifically-rejected design,
  because they attach to different things even when they carry the same
  rate.

  Later page(s) carry a second, structurally identical monthly grid under
  a heading containing "CTC Structuring" -- salary-structuring components
  (car lease rentals, car insurance, telephone/mobile reimbursement, meal
  card, devices, and so on) with their own printed "CTC Structuring" total
  row. This block is a reconciliation-to-CTC item ONLY: it sits entirely
  OUTSIDE the page-1 payout arithmetic (page 1's Total Gross Payment +
  Total Recovery == Total Payout closes without it) and its figures are
  NEVER posted as income or expense. It is parsed into a separate
  top-level key, `ctc_structuring`, with its own `diagnostics` nested
  inside it -- deliberately kept apart from the page-1 `diagnostics` list
  so a CTC-side mismatch can never be mistaken for a page-1 payout-
  arithmetic failure (see `_consume_ctc_grid()`). If the block is absent,
  `ctc_structuring` is `None`.

Confirmed parsing traps, each handled explicitly below (do not "simplify"
any of these away):

  (a) `page.extract_text()` is insufficient here and silently corrupts
      figures -- on the real specimen it merged a two-word row label
      across lines and orphaned that row's figures. This module is
      coordinate-based throughout, like llp_statement.py, and reuses its
      `_group_rows()` / `_merge_row_tokens()` (a single printed number can
      arrive as several x-adjacent word tokens, including a split-off
      leading "(").
  (b) Column x-positions are NEVER hardcoded. They are derived from the
      header row's own month-name words and its "Total" word every time,
      independently per page -- the CTC page's columns are not guaranteed
      to land at the same coordinates as page 1's.
  (c) Recovery rows print negative, either parenthesised ("(30,000)") or
      minus-prefixed ("-30,000") -- both parse to the same value via
      `payout_advice._parse_amount()`.
  (d) A printed "-" cell means the figure is 0.0 (a nil that WAS printed)
      -- distinct from a row that never appears on the page at all, whose
      fields all stay `None` (absent). "#N/A" is a template artefact and
      is skipped outright: the cell is left absent (`None`), never read as
      zero.
  (e) The row set changes between years -- e.g. a confirmed real example
      has "Interest on Capital" one year and not the next, while the next
      year adds an "Arrears Share of Profit" row instead. An absent row is
      `None` everywhere, never `0.0`. Row labels are matched
      case-insensitively, on normalised whitespace, via a small
      `regex -> field_name` table (`_ROW_FIELD_MAP`) -- never by row
      position. An unrecognised row is never dropped silently; it lands in
      `unknown_labels` with its parsed monthly figures.
  (f) "Total Gross Payment", "Total Recovery" and "Total Payout" are
      printed subtotal rows, not derived ones -- the printed figure is
      always returned as-is. This module ALSO recomputes each from its
      component rows (the rows printed between the previous subtotal
      marker and this one -- the same marker-bounded-section technique
      llp_statement.py uses for its ADDITIONS:-/WITHDRAWALS:- sections,
      generalised to sections bounded by the subtotal rows themselves) and
      cross-checks; a disagreement is an "ERROR: ..." diagnostic. Nothing
      here ever raises on a reconciliation failure or silently corrects
      the printed figure -- both the printed and the recomputed figures
      are always available (the printed one on the row, the recomputed one
      in the diagnostic text).

Design notes carried over from parsers/__init__.py's module docstring
(every one of them is enforced here too):

  - MAP BY LABEL, NEVER ROW POSITION.
  - DISPATCH ON DOCUMENT CONTENT, NEVER FILENAME: this module identifies a
    payment schedule by requiring BOTH a header row containing
    "Particulars" and at least three month names, AND at least one of the
    anchor labels "Total Payout", "Total Gross Payment", or "Total
    Recovery" anywhere in the document; anything else raises
    `NotAPaymentScheduleError` naming what is missing (or, for a
    recognisable L1/L3/L5 document, naming which family it looks like
    instead) so a directory-walking caller can skip it cleanly.
  - THE ROW SET CHANGES BETWEEN YEARS -- an absent label is None, never 0.
  - NEGATIVES ARE PARENTHESISED OR MINUS-PREFIXED; thousands separators
    are commas -- reuses `payout_advice._parse_amount()` rather than
    re-implementing that parsing.
  - "#N/A" IS A TEMPLATE ARTEFACT, NEVER A VALUE.
  - NO RATE IS EVER HARDCODED HERE.
  - `financial_year`, when derivable, comes from the document's own text
    (a "Financial Year"/"FY" phrase) -- NEVER from the filename, and never
    guessed from the month column headers.

Note on scope: this module implements ONLY the parser and is deliberately
NOT wired into `agent.py` / `engine.py` / `skill.yaml` -- that wiring is a
separate decision the user has not made yet (see AGENT.md's Stage 2
section, which still describes the *wiring* as absent).
"""
from __future__ import annotations

import re
from pathlib import Path

import pdfplumber

from .llp_statement import _group_rows, _merge_row_tokens
from .payout_advice import _FFFD_RUN_RE, _NA_TOKEN_RE, _parse_amount

# ---------------------------------------------------------------------------
# Content-dispatch markers.
# ---------------------------------------------------------------------------

_PARTICULARS_RE = re.compile(r"\bparticulars\b", re.IGNORECASE)

_MONTH_NAMES = [
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
]
_MONTH_NAME_RE = re.compile(r"^(" + "|".join(_MONTH_NAMES) + r")$", re.IGNORECASE)
_MONTH_WORD_BOUNDARY_RES = [
    (name, re.compile(r"\b" + name + r"\b", re.IGNORECASE)) for name in _MONTH_NAMES
]

_ANCHOR_RE = re.compile(
    r"total\s+payout|total\s+gross\s+payment|total\s+recovery", re.IGNORECASE
)
_TOTAL_WORD_RE = re.compile(r"^total$", re.IGNORECASE)
_ACTUAL_FORECAST_RE = re.compile(r"^(actual|forecast)$", re.IGNORECASE)

_CTC_HEADING_RE = re.compile(r"ctc\s+structuring", re.IGNORECASE)
_CTC_TOTAL_ROW_RE = re.compile(r"^\s*ctc\s+structuring\s*$", re.IGNORECASE)

_ENTITY_LINE_RE = re.compile(r"^\s*entity\s*:?\s*(.+?)\s*$", re.IGNORECASE)
_FY_LABELLED_RE = re.compile(
    r"financial\s+year\s*:?\s*(\d{4})\s*[-/]\s*(\d{2,4})", re.IGNORECASE
)
_FY_SHORT_RE = re.compile(r"\bFY\s*[:\-]?\s*(\d{4})\s*[-/]\s*(\d{2,4})\b", re.IGNORECASE)

_AMOUNT_CANDIDATE_RE = re.compile(r"^-?\(?[0-9][0-9,]*\.?[0-9]*\)?$|^-$")

# Markers for a recognisable non-L4 document, so a directory-walking
# caller gets a descriptive rejection rather than a generic one.
_NON_L4_L1_HEADING_RE = re.compile(r"to\s+whomsoever\s+it\s+may\s+concern", re.IGNORECASE)
_NON_L4_SALARY_RE = re.compile(r"salary\s+statement\s+for", re.IGNORECASE)
_NON_L4_L3_PAYMENTS_RE = re.compile(r"^\s*payments\b", re.IGNORECASE | re.MULTILINE)
_NON_L4_L3_SCHEDULE_RE = re.compile(r"^\s*schedule\b", re.IGNORECASE | re.MULTILINE)
_NON_L4_L5_HEADER_RE = re.compile(r"capital\s+account.*current\s+account", re.IGNORECASE)


class NotAPaymentScheduleError(ValueError):
    """Raised by parse_payment_schedule_pages()/parse() when the supplied
    words/PDF are not an L4 payment schedule. A directory-walking caller
    should catch this and skip the file cleanly -- never crash, never
    misparse it as a payment schedule."""


# ---------------------------------------------------------------------------
# Row labels -> field name. Order matters only in that more specific
# labels ("Total Gross Payment") are listed before less specific ones that
# could otherwise shadow them ("Gross Share of Profit").
# ---------------------------------------------------------------------------

_ROW_FIELD_MAP: list[tuple[str, "re.Pattern[str]"]] = [
    ("total_gross_payment", re.compile(r"^\s*total\s+gross\s+payment\b", re.IGNORECASE)),
    ("total_recovery", re.compile(r"^\s*total\s+recovery\b", re.IGNORECASE)),
    ("total_payout", re.compile(r"^\s*total\s+payout\b", re.IGNORECASE)),
    ("remuneration", re.compile(r"^\s*remuneration\b", re.IGNORECASE)),
    ("gross_share_of_profit", re.compile(r"^\s*gross\s+share\s+of\s+profit\b", re.IGNORECASE)),
    ("arrears_share_of_profit", re.compile(r"^\s*arrears\b.*share\s+of\s+profit\b", re.IGNORECASE)),
    ("interest_on_capital", re.compile(r"^\s*interest\s+on\s+capital\b", re.IGNORECASE)),
    ("previous_year_plmis", re.compile(r"^\s*previous\s+year\s+plmis?\b", re.IGNORECASE)),
    ("firm_tax_on_sop", re.compile(r"^\s*firm\s+tax\s+on\s+sop\b", re.IGNORECASE)),
    ("firm_tax_others", re.compile(r"^\s*firm\s+tax\s*\(?\s*others?\)?\b", re.IGNORECASE)),
    ("tds_on_rem_ioc", re.compile(r"^\s*tds\s+on\s+(rem\s*/\s*ioc|remuneration)\b", re.IGNORECASE)),
    ("transferred_to_capital", re.compile(r"^\s*transferred\s+to\s+capital\b", re.IGNORECASE)),
    ("medical_topup", re.compile(r"^\s*medical\s+top[\s-]?up\b", re.IGNORECASE)),
]

_SUBTOTAL_FIELDS = {"total_gross_payment", "total_recovery", "total_payout"}


# ---------------------------------------------------------------------------
# Geometry helpers specific to this module (column derivation / nearest-
# centroid assignment). Row grouping and x-adjacent token merging are
# reused from llp_statement.py -- see that module's trap (a)/(b) notes.
# ---------------------------------------------------------------------------

def _rows_text(rows: list[list[dict]]) -> str:
    return "\n".join(" ".join(w["text"] for w in row) for row in rows)


def _is_header_row_text(row_text: str) -> bool:
    """A header row contains "Particulars" AND at least three distinct
    month names -- see s.3.3's content-dispatch guard. The same threshold
    is used to locate the header row for column derivation, so a row that
    merely mentions one month name in passing is never mistaken for it."""
    if not _PARTICULARS_RE.search(row_text):
        return False
    found = sum(1 for _, pattern in _MONTH_WORD_BOUNDARY_RES if pattern.search(row_text))
    return found >= 3


def _build_columns(header_row: list[dict]) -> tuple[list[str], list[dict]]:
    """Derive column x-centroids from the header row's own month-name
    words and its "Total" word -- NEVER hardcoded pixel/point positions,
    derived fresh for every header row (page 1's grid and the CTC grid are
    not guaranteed to share coordinates)."""
    tokens = _merge_row_tokens(header_row)
    columns: list[dict] = []
    months: list[str] = []
    for tok in tokens:
        text = tok["text"].strip()
        centroid = (tok["x0"] + tok["x1"]) / 2.0
        if _MONTH_NAME_RE.match(text):
            name = text.title()
            columns.append({"name": name, "centroid": centroid})
            months.append(name)
        elif _TOTAL_WORD_RE.match(text):
            columns.append({"name": "Total", "centroid": centroid})
    columns.sort(key=lambda c: c["centroid"])
    months_order = [c["name"] for c in columns if c["name"] != "Total"]
    return months_order, columns


def _nearest_column(columns: list[dict], x0: float, x1: float) -> str:
    centroid = (x0 + x1) / 2.0
    best = min(columns, key=lambda c: abs(c["centroid"] - centroid))
    return best["name"]


def _label_zone_right_edge(columns: list[dict]) -> float:
    """The x-centroid below which a token is unambiguously part of the row
    label, never a value cell -- derived from the columns' own centroids
    (never hardcoded), so a lone hyphen/en-dash/slash/ampersand token
    inside a hyphenated label (e.g. "Arrears - Share of Profit") is never
    mistaken for a nil-value "-" cell just because it happens to match the
    amount-candidate pattern (see `_AMOUNT_CANDIDATE_RE`'s `^-$`
    alternative). Trap fixed here: a genuinely nil "-" cell always sits at
    a real column's position (at or past the leftmost column, "Total");
    a hyphen used as a word separator inside a label never does -- it
    sits well to the left, among the other label words."""
    if not columns:
        return float("inf")
    centroids = sorted(c["centroid"] for c in columns)
    if len(centroids) >= 2:
        gaps = [b - a for a, b in zip(centroids, centroids[1:]) if b > a]
        pitch = min(gaps) if gaps else 60.0
    else:
        pitch = 60.0
    return centroids[0] - pitch / 2.0


def _parse_grid_row(row_words: list[dict], columns: list[dict]) -> tuple[str, dict]:
    """Splits one data row into (label, values). `values` maps a column
    name ("Total" or a month name) to a parsed float -- only for columns
    where a numeric token was actually found and successfully parsed. A
    "#N/A" cell contributes nothing (the column stays absent from
    `values`, i.e. None downstream) -- never zero. A "-" cell parses to
    0.0 (a nil that WAS printed), distinct from a column with no token at
    all. The label is the leading run of tokens sitting to the left of
    `_label_zone_right_edge()` -- any token still in that label zone
    (including one that happens to match the amount-candidate pattern,
    such as a lone "-" used as a hyphenated-label separator) is always
    kept in the label, never misread as a value cell. Only once a token
    past that boundary is seen does value-zone parsing begin."""
    tokens = _merge_row_tokens(row_words)
    label_parts: list[str] = []
    values: dict[str, float] = {}
    in_value_zone = False
    label_zone_right_edge = _label_zone_right_edge(columns)
    for tok in tokens:
        text = tok["text"].strip()
        if not text:
            continue
        tok_centroid = (tok["x0"] + tok["x1"]) / 2.0
        if not in_value_zone and tok_centroid < label_zone_right_edge:
            label_parts.append(tok["text"])
            continue
        in_value_zone = True
        is_na = bool(_NA_TOKEN_RE.search(text))
        is_amount = bool(_AMOUNT_CANDIDATE_RE.match(text))
        if is_na:
            continue
        if not is_amount:
            continue
        val = 0.0 if text == "-" else _parse_amount(text)
        if val is None:
            continue
        col = _nearest_column(columns, tok["x0"], tok["x1"])
        values[col] = val
    label = " ".join(label_parts).strip()
    return label, values


def _extract_flag_row(row_words: list[dict], month_columns: list[dict]) -> dict | None:
    """The Actual/Forecast flag row is the row immediately following the
    header whose merged tokens are ALL "Actual" or "Forecast" -- assigned
    to a month by the same nearest-centroid rule (Total excluded)."""
    tokens = _merge_row_tokens(row_words)
    texts = [t["text"].strip() for t in tokens if t["text"].strip()]
    if not texts or not all(_ACTUAL_FORECAST_RE.match(t) for t in texts):
        return None
    status: dict[str, str] = {}
    for tok in tokens:
        text = tok["text"].strip()
        if not text:
            continue
        col = _nearest_column(month_columns, tok["x0"], tok["x1"])
        status[col] = text.title()
    return status


def _slugify(label: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", label.strip().lower())
    return slug.strip("_")


class _WrappedRow:
    """A synthetic row reassembled from a wrapped label -- see the
    "wrapped row labels" trap in the module docstring: a label fragment,
    then a numeric-only line, then the rest of the label. `value_words`
    is the original values-only physical row (fed to `_parse_grid_row()`
    for its numbers); `label` is the already-joined label text."""

    __slots__ = ("label", "value_words")

    def __init__(self, label: str, value_words: list[dict]):
        self.label = label
        self.value_words = value_words


def _line_has_amount_text(tokens: list[dict]) -> bool:
    texts = [t["text"].strip() for t in tokens if t["text"].strip()]
    return any(
        _NA_TOKEN_RE.search(t) or _AMOUNT_CANDIDATE_RE.match(t) for t in texts
    )


def _line_has_label_text(tokens: list[dict]) -> bool:
    texts = [t["text"].strip() for t in tokens if t["text"].strip()]
    return any(
        not (_NA_TOKEN_RE.search(t) or _AMOUNT_CANDIDATE_RE.match(t)) for t in texts
    )


def _merge_wrapped_label_rows(data_rows: list[list[dict]]) -> list:
    """Reassembles a label that wraps across physical lines with the
    numbers sitting on an intervening line -- trap (see module docstring):
    a row whose text line contains ONLY amounts and no label must be
    joined to the nearest label fragment rather than discarded or treated
    as a new row. A normal row (label AND values on the same physical
    line) passes through completely unchanged."""
    result: list = []
    pending_label_lines: list[str] = []
    open_wrapped: _WrappedRow | None = None

    for row in data_rows:
        tokens = _merge_row_tokens(row)
        if not any(t["text"].strip() for t in tokens):
            continue
        line_text = " ".join(t["text"] for t in tokens).strip()
        has_values = _line_has_amount_text(tokens)
        has_label = _line_has_label_text(tokens)

        if has_values and has_label:
            # A normal, complete row -- close out anything left open first.
            if open_wrapped is not None:
                result.append(open_wrapped)
                open_wrapped = None
            if pending_label_lines:
                merged_label = " ".join(pending_label_lines + [line_text]).strip()
                pending_label_lines = []
                result.append(_WrappedRow(merged_label, row))
            else:
                result.append(row)
            continue

        if has_values and not has_label:
            # A numbers-only line -- open (or continue) a wrapped row.
            if open_wrapped is not None:
                result.append(open_wrapped)
                open_wrapped = None
            label_text = " ".join(pending_label_lines).strip()
            pending_label_lines = []
            open_wrapped = _WrappedRow(label_text, row)
            continue

        if has_label and not has_values:
            if open_wrapped is not None:
                open_wrapped.label = (open_wrapped.label + " " + line_text).strip()
            else:
                pending_label_lines.append(line_text)
            continue

    if open_wrapped is not None:
        result.append(open_wrapped)
    return result


# ---------------------------------------------------------------------------
# Reconciliation helpers. Nothing here ever raises on a mismatch -- every
# check appends an "ERROR: ..." diagnostic and the record still comes
# back with both the printed and the recomputed figures available (the
# printed one unchanged on the row, the recomputed one in the diagnostic
# text). A row with an incomplete month set is skipped, not flagged.
# ---------------------------------------------------------------------------

def _cross_check_row_total(
    diagnostics: list[str], label: str, entry: dict, prefix: str = ""
) -> None:
    months_vals = entry["months"]
    if any(v is None for v in months_vals.values()):
        return
    printed = entry["total"]
    if printed is None:
        return
    computed = sum(months_vals.values())
    if abs(computed - printed) > 0.01:
        diagnostics.append(
            f"ERROR: {prefix}'{label}' printed Total ({printed:,.2f}) does not match "
            f"the sum of its monthly figures ({computed:,.2f}) (diff "
            f"{computed - printed:,.2f})."
        )


def _cross_check_bucket(
    diagnostics: list[str],
    subtotal_name: str,
    bucket_sum: dict[str, float],
    bucket_sum_total: float,
    entry: dict,
    months_order: list[str],
    prefix: str = "",
) -> None:
    printed_total = entry["total"]
    if printed_total is not None and abs(bucket_sum_total - printed_total) > 0.01:
        diagnostics.append(
            f"ERROR: {prefix}'{subtotal_name}' printed Total ({printed_total:,.2f}) does "
            f"not match the sum of its component rows ({bucket_sum_total:,.2f}) (diff "
            f"{bucket_sum_total - printed_total:,.2f})."
        )
    for m in months_order:
        printed_m = entry["months"].get(m)
        if printed_m is None:
            continue
        computed_m = bucket_sum.get(m, 0.0)
        if abs(computed_m - printed_m) > 0.01:
            diagnostics.append(
                f"ERROR: {prefix}'{subtotal_name}' printed {m} figure ({printed_m:,.2f}) "
                f"does not match the sum of its component rows for {m} "
                f"({computed_m:,.2f})."
            )


def _cross_check_payout_identity(
    diagnostics: list[str], subtotal_values: dict, months_order: list[str]
) -> None:
    gross, recovery, payout = (
        subtotal_values.get("gross"), subtotal_values.get("recovery"), subtotal_values.get("payout"),
    )
    if not (gross and recovery and payout):
        return
    if gross["total"] is not None and recovery["total"] is not None and payout["total"] is not None:
        computed = gross["total"] + recovery["total"]
        if abs(computed - payout["total"]) > 0.01:
            diagnostics.append(
                f"ERROR: Total Gross Payment ({gross['total']:,.2f}) + Total Recovery "
                f"({recovery['total']:,.2f}) = {computed:,.2f} does not match the "
                f"printed Total Payout ({payout['total']:,.2f})."
            )
    for m in months_order:
        g, r, p = gross["months"].get(m), recovery["months"].get(m), payout["months"].get(m)
        if g is None or r is None or p is None:
            continue
        computed = g + r
        if abs(computed - p) > 0.01:
            diagnostics.append(
                f"ERROR: for {m}, Total Gross Payment + Total Recovery "
                f"({computed:,.2f}) does not match the printed Total Payout "
                f"({p:,.2f})."
            )


def _resolve_cross_page_subtotal(
    diagnostics: list[str],
    subtotal_label: str,
    period_desc: str,
    detail_val: float,
    other_val: float,
    component_sum: float | None,
) -> float:
    """Gap-4 component-consistency precedence: on a cross-page disagreement
    for the same subtotal/period, the page whose printed figure equals the
    sum of that period's own component rows wins. If neither figure
    matches, or no component sum is available for that period, fall back
    to the detail page's figure as before. Always appends a diagnostic
    naming the chosen value, the rejected value, the component sum (when
    available) and the reason -- never raises, never silently rewrites a
    printed figure. Returns the value to use."""
    if component_sum is None:
        diagnostics.append(
            f"ERROR: '{subtotal_label}' printed {period_desc} on the detail page "
            f"({detail_val:,.2f}) does not match the figure on another page "
            f"({other_val:,.2f}); no component rows are available to arbitrate "
            f"for this period, falling back to the detail page's figure."
        )
        return detail_val

    matches_detail = abs(detail_val - component_sum) <= 0.01
    matches_other = abs(other_val - component_sum) <= 0.01

    if matches_other and not matches_detail:
        diagnostics.append(
            f"ERROR: '{subtotal_label}' printed {period_desc} on the detail page "
            f"({detail_val:,.2f}) does not match the figure on another page "
            f"({other_val:,.2f}); the detail page's figure does not match the sum "
            f"of its own component rows ({component_sum:,.2f}), so the other "
            f"page's figure ({other_val:,.2f}), which does match, is used instead."
        )
        return other_val

    if matches_detail:
        diagnostics.append(
            f"ERROR: '{subtotal_label}' printed {period_desc} on the detail page "
            f"({detail_val:,.2f}) does not match the figure on another page "
            f"({other_val:,.2f}); the detail page's figure matches the sum of its "
            f"own component rows ({component_sum:,.2f}), so the detail page's "
            f"figure is kept."
        )
        return detail_val

    diagnostics.append(
        f"ERROR: '{subtotal_label}' printed {period_desc} on the detail page "
        f"({detail_val:,.2f}) does not match the figure on another page "
        f"({other_val:,.2f}); neither figure matches the sum of the component "
        f"rows ({component_sum:,.2f}), falling back to the detail page's figure."
    )
    return detail_val


# ---------------------------------------------------------------------------
# Grid consumption -- the main payout grid (may appear on more than one
# page -- a summary page and a detail page, see the module docstring's
# multi-page section) and the CTC block.
# ---------------------------------------------------------------------------

def _parse_row_generic(row, columns: list[dict]) -> tuple[str, dict]:
    """Like `_parse_grid_row()`, but also accepts a `_WrappedRow` -- a
    label reassembled from a wrapped line (see `_merge_wrapped_label_rows()`)
    paired with the original values-only physical row for its numbers."""
    if isinstance(row, _WrappedRow):
        _, values = _parse_grid_row(row.value_words, columns)
        return row.label, values
    return _parse_grid_row(row, columns)


def _consume_main_grid(
    data_rows: list,
    columns: list[dict],
    months_order: list[str],
    diagnostics: list[str],
) -> tuple[dict, list[dict], bool, dict]:
    """Returns (rows_dict, unknown_labels, has_components, bucket_sum_by_field).
    `has_components` is False when the section between the header and each
    subtotal row carried NO component rows at all (a summary-only page), in
    which case a single STRUCTURAL diagnostic replaces what would otherwise
    be a per-month cross-check against an empty component set (see s.7 of
    the module docstring's multi-page section). `bucket_sum_by_field` maps
    each subtotal field (e.g. "total_gross_payment") to that PAGE's own
    component-row sums (`{"months": {...}, "total": ..., "has_rows": bool}`)
    -- captured right before the bucket resets -- used for the Gap-4
    component-consistency cross-page precedence rule."""
    rows_dict: dict = {}
    unknown_labels: list[dict] = []
    bucket_sum = {m: 0.0 for m in months_order}
    bucket_sum_total = 0.0
    section_row_count = 0
    any_components = False
    subtotal_values: dict = {}
    bucket_sum_by_field: dict[str, dict] = {}

    data_rows = _merge_wrapped_label_rows(data_rows)

    for row in data_rows:
        label, values = _parse_row_generic(row, columns)
        if not label or not values:
            continue
        entry = {
            "total": values.get("Total"),
            "months": {m: values.get(m) for m in months_order},
        }
        _cross_check_row_total(diagnostics, label, entry)

        matched_field = None
        for field, pattern in _ROW_FIELD_MAP:
            if pattern.match(label):
                matched_field = field
                break

        if matched_field is None:
            unknown_labels.append({"label": label, **entry})
        else:
            rows_dict[matched_field] = entry

        if matched_field not in _SUBTOTAL_FIELDS:
            section_row_count += 1
            any_components = True
            for m in months_order:
                v = entry["months"].get(m)
                if v is not None:
                    bucket_sum[m] += v
            if entry["total"] is not None:
                bucket_sum_total += entry["total"]
            continue

        subtotal_label = {
            "total_gross_payment": "Total Gross Payment",
            "total_recovery": "Total Recovery",
        }.get(matched_field)
        if subtotal_label is not None:
            if section_row_count == 0:
                diagnostics.append(
                    f"STRUCTURAL: no component rows found for '{subtotal_label}' -- "
                    "cross-check against its components skipped."
                )
            else:
                _cross_check_bucket(diagnostics, subtotal_label, bucket_sum, bucket_sum_total, entry, months_order)

        bucket_sum_by_field[matched_field] = {
            "months": dict(bucket_sum),
            "total": bucket_sum_total,
            "has_rows": section_row_count > 0,
        }

        if matched_field == "total_gross_payment":
            subtotal_values["gross"] = entry
        elif matched_field == "total_recovery":
            subtotal_values["recovery"] = entry
        elif matched_field == "total_payout":
            subtotal_values["payout"] = entry
            bucket_sum = {m: 0.0 for m in months_order}
            bucket_sum_total = 0.0
            section_row_count = 0
            break
        bucket_sum = {m: 0.0 for m in months_order}
        bucket_sum_total = 0.0
        section_row_count = 0

    return rows_dict, unknown_labels, any_components, bucket_sum_by_field


def _consume_ctc_grid(
    data_rows: list[list[dict]], columns: list[dict], months_order: list[str],
) -> dict:
    """CTC Structuring is a recon-only block, entirely outside the page-1
    payout arithmetic (see module docstring) -- it gets its own nested
    `diagnostics` list rather than feeding the page-1 one, so a CTC-side
    mismatch is never mistaken for a payout-arithmetic failure."""
    ctc_diagnostics: list[str] = []
    rows_dict: dict = {}
    unknown_labels: list[dict] = []
    component_sum = {m: 0.0 for m in months_order}
    component_sum_total = 0.0
    total_entry: dict | None = None

    data_rows = _merge_wrapped_label_rows(data_rows)

    for row in data_rows:
        label, values = _parse_row_generic(row, columns)
        if not label or not values:
            continue
        entry = {
            "total": values.get("Total"),
            "months": {m: values.get(m) for m in months_order},
        }
        prefix = "CTC Structuring: "
        _cross_check_row_total(ctc_diagnostics, label, entry, prefix=prefix)

        if _CTC_TOTAL_ROW_RE.match(label):
            # On the real document the CTC total row appears BEFORE its
            # component rows (immediately after the header), not after --
            # do not stop consuming the block here, or every component row
            # that follows is silently dropped and `ctc_structuring` comes
            # back empty. The heading line above the header carries the
            # same text but has no figures, so it was already filtered out
            # by the `not values` guard above and never reaches here twice.
            if total_entry is None:
                total_entry = entry
            continue

        rows_dict[_slugify(label)] = entry
        for m in months_order:
            v = entry["months"].get(m)
            if v is not None:
                component_sum[m] += v
        if entry["total"] is not None:
            component_sum_total += entry["total"]

    if total_entry is not None:
        _cross_check_bucket(
            ctc_diagnostics, "CTC Structuring", component_sum, component_sum_total,
            total_entry, months_order, prefix="",
        )

    return {
        "total": total_entry["total"] if total_entry else None,
        "months": total_entry["months"] if total_entry else {m: None for m in months_order},
        "rows": rows_dict,
        "unknown_labels": unknown_labels,
        "diagnostics": ctc_diagnostics,
    }


# ---------------------------------------------------------------------------
# Metadata extraction -- entity name and financial year, both from the
# document's own text, never from the filename and never guessed from the
# month columns.
# ---------------------------------------------------------------------------

def _extract_entity_name(full_text: str) -> str | None:
    for line in full_text.splitlines():
        m = _ENTITY_LINE_RE.match(line)
        if m:
            name = m.group(1).strip()
            if name:
                return name
    return None


def _extract_financial_year(full_text: str) -> str | None:
    m = _FY_LABELLED_RE.search(full_text) or _FY_SHORT_RE.search(full_text)
    if not m:
        return None
    start, end = m.group(1), m.group(2)
    end = end[-2:] if len(end) == 4 else end.zfill(2)
    return f"{start}-{end}"


# ---------------------------------------------------------------------------
# PURE core.
# ---------------------------------------------------------------------------

def parse_payment_schedule_pages(pages: list[list[dict]], source_name: str = "") -> dict:
    """PURE: takes one payment schedule's per-page extracted words (each a
    dict shaped like pdfplumber's `page.extract_words()`: `{"text", "x0",
    "x1", "top", "bottom"}`) and returns the record dict. Raises
    NotAPaymentScheduleError if `pages` is not a payment schedule --
    callers walking a directory of mixed documents should catch that and
    skip the file.
    """
    pages_rows: list[list[list[dict]]] = []
    page_texts: list[str] = []
    for page_words in pages:
        cleaned = []
        for w in page_words:
            text = _FFFD_RUN_RE.sub("", w["text"])
            if text:
                cleaned.append({**w, "text": text})
        rows = _group_rows(cleaned)
        pages_rows.append(rows)
        page_texts.append(_rows_text(rows))
    full_text = "\n".join(page_texts)

    header_locations: list[tuple[int, int]] = []
    for p_idx, rows in enumerate(pages_rows):
        for r_idx, row in enumerate(rows):
            row_text = " ".join(w["text"] for w in row)
            if _is_header_row_text(row_text):
                header_locations.append((p_idx, r_idx))

    has_anchor = bool(_ANCHOR_RE.search(full_text))

    if not header_locations or not has_anchor:
        if _NON_L4_L1_HEADING_RE.search(full_text):
            raise NotAPaymentScheduleError(
                f"{source_name or '<pages>'}: looks like an L1 monthly payout "
                "certificate (\"To Whomsoever It may concern\"), not a payment "
                "schedule -- skipped."
            )
        if _NON_L4_SALARY_RE.search(full_text):
            raise NotAPaymentScheduleError(
                f"{source_name or '<pages>'}: looks like a salary statement "
                "(\"SALARY STATEMENT FOR\"), not a payment schedule -- skipped."
            )
        if _NON_L4_L3_PAYMENTS_RE.search(full_text) and _NON_L4_L3_SCHEDULE_RE.search(full_text):
            raise NotAPaymentScheduleError(
                f"{source_name or '<pages>'}: looks like an L3 Compensation "
                "Advisory letter (PAYMENTS/SCHEDULE section headers), not a "
                "payment schedule -- skipped."
            )
        if _NON_L4_L5_HEADER_RE.search(full_text):
            raise NotAPaymentScheduleError(
                f"{source_name or '<pages>'}: looks like an L5 LLP Statement of "
                "Account (CAPITAL ACCOUNT / CURRENT ACCOUNT header), not a "
                "payment schedule -- skipped."
            )
        missing = []
        if not header_locations:
            missing.append('a header row with "Particulars" and at least three month names')
        if not has_anchor:
            missing.append(
                'an anchor label ("Total Payout", "Total Gross Payment", or '
                '"Total Recovery")'
            )
        raise NotAPaymentScheduleError(
            f"{source_name or '<pages>'}: missing " + " and ".join(missing) +
            " -- not a payment schedule, skipped."
        )

    main_locs: list[tuple[int, int]] = []
    ctc_loc = None
    for p_idx, r_idx in header_locations:
        rows = pages_rows[p_idx]
        preceded_by_ctc_heading = any(
            _CTC_HEADING_RE.search(" ".join(w["text"] for w in row))
            for row in rows[:r_idx]
        )
        if preceded_by_ctc_heading:
            if ctc_loc is None:
                ctc_loc = (p_idx, r_idx)
        else:
            main_locs.append((p_idx, r_idx))

    entity_name = _extract_entity_name(full_text)
    financial_year = _extract_financial_year(full_text)

    diagnostics: list[str] = []
    rows_dict: dict = {}
    unknown_labels: list[dict] = []
    months_order: list[str] = []
    month_status: dict = {}
    ctc_structuring = None

    # Every non-CTC header location (i.e. every page carrying a "Particulars"
    # grid outside the CTC Structuring block -- see the multi-page section of
    # the module docstring) is processed independently: its own columns, its
    # own flag row, its own component-row consumption. Reusing one page's
    # columns/month order for another page is exactly the original defect.
    page_results: list[dict] = []
    for p_idx, r_idx in main_locs:
        rows = pages_rows[p_idx]
        header_row = rows[r_idx]
        page_months_order, page_columns = _build_columns(header_row)
        page_month_columns = [c for c in page_columns if c["name"] != "Total"]
        page_flag = {m: None for m in page_months_order}
        has_flag_row = False

        next_idx = r_idx + 1
        if next_idx < len(rows):
            flag = _extract_flag_row(rows[next_idx], page_month_columns)
            if flag is not None:
                page_flag.update(flag)
                has_flag_row = True
                next_idx += 1

        if not has_flag_row and r_idx > 0:
            # The flag row can also sit ABOVE the header rather than below it
            # (see the module docstring's flag-row trap) -- on the real
            # document its tokens are left-aligned to the header row's own
            # month-name x-positions (not the right-aligned numeric data
            # columns), so `page_month_columns` -- derived from the header
            # row itself -- is exactly the right reference for both
            # directions; only the search direction differs.
            flag = _extract_flag_row(rows[r_idx - 1], page_month_columns)
            if flag is not None:
                page_flag.update(flag)
                has_flag_row = True

        end_idx = len(rows)
        for other_p, other_r in header_locations:
            if other_p == p_idx and other_r > r_idx:
                end_idx = min(end_idx, other_r)

        page_data_rows = rows[next_idx:end_idx]
        page_diagnostics: list[str] = []
        page_rows_dict, page_unknown_labels, has_components, page_bucket_sum_by_field = _consume_main_grid(
            page_data_rows, page_columns, page_months_order, page_diagnostics
        )
        page_results.append({
            "months_order": page_months_order,
            "rows_dict": page_rows_dict,
            "unknown_labels": page_unknown_labels,
            "has_components": has_components,
            "bucket_sum_by_field": page_bucket_sum_by_field,
            "month_status": page_flag,
            "has_flag_row": has_flag_row,
            "diagnostics": page_diagnostics,
        })

    if page_results:
        # The "detail" page is the one whose components are actually being
        # reconciled -- per the precedence policy, IT wins on any subtotal
        # disagreement. Prefer the last page with real component rows (a
        # summary page has none); if none has components, fall back to the
        # last main page so a summary-only document still returns a result
        # (with a STRUCTURAL diagnostic, not a crash -- requirement 7/test 2).
        detail = None
        for pr in page_results:
            if pr["has_components"]:
                detail = pr
        if detail is None:
            detail = page_results[-1]

        months_order = detail["months_order"]
        rows_dict = dict(detail["rows_dict"])
        unknown_labels = list(detail["unknown_labels"])
        month_status = dict(detail["month_status"])
        diagnostics = list(detail["diagnostics"])

        for pr in page_results:
            if pr is detail:
                continue
            unknown_labels.extend(pr["unknown_labels"])
            # Backfill month_status only where the detail page itself carried
            # no flag row / no value for that month -- the detail page's own
            # flag row always wins when both are present.
            if not detail["has_flag_row"]:
                for m, v in pr["month_status"].items():
                    if month_status.get(m) is None and v is not None:
                        month_status[m] = v
            # Requirement 3 / Gap 4: cross-check the same subtotal appearing
            # on more than one page. Precedence is no longer a fixed
            # "detail page always wins" -- it is a component-consistency
            # test: whichever page's printed figure equals the sum of that
            # period's own component rows wins; if neither matches, or no
            # components exist for that period, fall back to the detail
            # page as before. Every disagreement -- whichever way it
            # resolves -- is recorded as a diagnostic naming both values,
            # the component sum, and the reason; nothing is ever applied
            # silently.
            subtotal_label_by_field = {
                "total_gross_payment": "Total Gross Payment",
                "total_recovery": "Total Recovery",
                "total_payout": "Total Payout",
            }
            detail_bucket_by_field = detail.get("bucket_sum_by_field", {})
            other_bucket_by_field = pr.get("bucket_sum_by_field", {})
            for field, subtotal_label in subtotal_label_by_field.items():
                detail_entry = rows_dict.get(field)
                other_entry = pr["rows_dict"].get(field)
                if detail_entry is None or other_entry is None:
                    continue
                detail_bucket = detail_bucket_by_field.get(field)
                other_bucket = other_bucket_by_field.get(field)

                for m in months_order:
                    dv = detail_entry["months"].get(m)
                    ov = other_entry["months"].get(m)
                    if dv is None or ov is None or abs(dv - ov) <= 0.005:
                        continue
                    component_sum_m = None
                    if detail_bucket and detail_bucket.get("has_rows"):
                        component_sum_m = detail_bucket["months"].get(m)
                    elif other_bucket and other_bucket.get("has_rows"):
                        component_sum_m = other_bucket["months"].get(m)
                    chosen = _resolve_cross_page_subtotal(
                        diagnostics, subtotal_label, f"{m} figure", dv, ov, component_sum_m,
                    )
                    rows_dict[field]["months"][m] = chosen

                dt, ot = detail_entry.get("total"), other_entry.get("total")
                if dt is not None and ot is not None and abs(dt - ot) > 0.005:
                    component_sum_t = None
                    if detail_bucket and detail_bucket.get("has_rows"):
                        component_sum_t = detail_bucket.get("total")
                    elif other_bucket and other_bucket.get("has_rows"):
                        component_sum_t = other_bucket.get("total")
                    chosen_total = _resolve_cross_page_subtotal(
                        diagnostics, subtotal_label, "annual total", dt, ot, component_sum_t,
                    )
                    rows_dict[field]["total"] = chosen_total

        subtotal_values = {
            "gross": rows_dict.get("total_gross_payment"),
            "recovery": rows_dict.get("total_recovery"),
            "payout": rows_dict.get("total_payout"),
        }
        _cross_check_payout_identity(diagnostics, subtotal_values, months_order)

    if ctc_loc is not None:
        p_idx, r_idx = ctc_loc
        rows = pages_rows[p_idx]
        header_row = rows[r_idx]
        ctc_months_order, ctc_columns = _build_columns(header_row)

        next_idx = r_idx + 1
        ctc_month_columns = [c for c in ctc_columns if c["name"] != "Total"]
        if next_idx < len(rows):
            flag = _extract_flag_row(rows[next_idx], ctc_month_columns)
            if flag is not None:
                next_idx += 1

        end_idx = len(rows)
        for other_p, other_r in header_locations:
            if other_p == p_idx and other_r > r_idx:
                end_idx = min(end_idx, other_r)

        data_rows = rows[next_idx:end_idx]
        ctc_structuring = _consume_ctc_grid(data_rows, ctc_columns, ctc_months_order)

    return {
        "source_name": source_name,
        "document_type": "payment_schedule",
        "entity_name": entity_name,
        "financial_year": financial_year,
        "months": months_order,
        "month_status": month_status,
        "rows": rows_dict,
        "ctc_structuring": ctc_structuring,
        "unknown_labels": unknown_labels,
        "diagnostics": diagnostics,
    }


# ---------------------------------------------------------------------------
# Thin PDF-opening shell -- the only function here that touches the
# filesystem / pdfplumber.
# ---------------------------------------------------------------------------

def parse(path: str, password: str | None = None) -> dict:
    """Open the payment schedule PDF at `path` (password-aware; `password`
    may be None/empty for an unprotected file), extract every page's words
    (coordinate-preserving -- `page.extract_words()`, never
    `page.extract_text()`, since this layout needs word-box geometry --
    see the trap notes in this module's docstring), and return the parsed
    record -- see parse_payment_schedule_pages(). Raises
    NotAPaymentScheduleError if the PDF is not a payment schedule (e.g. an
    L1/L3/L5 document sharing the same directory).
    """
    pages: list[list[dict]] = []
    with pdfplumber.open(str(path), password=password or "") as pdf:
        for page in pdf.pages:
            pages.append(page.extract_words())
    return parse_payment_schedule_pages(pages, source_name=Path(path).name)
