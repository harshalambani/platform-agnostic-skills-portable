"""
cg_parser.py -- PURE parsing for the KFintech Capital Gain / Loss Statement
(xlsx). Takes an already-decrypted, already-loaded openpyxl Workbook and
returns structured facts. No I/O, no password, no msoffcrypto -- the
decrypt-and-load boundary lives in agent.py, mirroring this skill's existing
hard split at the file-access boundary (see AGENT.md).

This is NOT a CAS (Consolidated Account Statement) parser -- see AGENT.md for
why the directory is still named skill_mf_cas. It targets the KFintech
"Capital Gain / Loss Statement", a different RTA-issued document that
already contains RTA-matched buy/sell lots and RTA-computed gains, rather
than a raw transaction history requiring FIFO reconstruction (that older,
still-supported input shape lives in parser.py + lots.py, kept intact for a
future CAMS/MF-Central transaction-level statement -- see AGENT.md).

Four required sheets (KFintech's own typo included -- 'Trasaction_Details'):
    Summary - Equity
    Summary - NonEquity
    Scheme_Level_Summary
    Trasaction_Details        (tolerates the corrected spelling too)

Never extracts holder name or PAN from Trasaction_Details rows 1-2 -- those
rows are skipped outright (never read into any field) because they are PII.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime

SHEET_SUMMARY_EQUITY = "Summary - Equity"
SHEET_SUMMARY_NONEQUITY = "Summary - NonEquity"
SHEET_SCHEME_SUMMARY = "Scheme_Level_Summary"
SHEET_TXN_DETAILS = "Trasaction_Details"          # KFintech's own spelling
SHEET_TXN_DETAILS_ALT = "Transaction_Details"     # tolerated, in case KFintech ever fixes it

REQUIRED_SHEETS = (SHEET_SUMMARY_EQUITY, SHEET_SUMMARY_NONEQUITY, SHEET_SCHEME_SUMMARY)

# The five s.234C advance-tax instalment period buckets, in column order.
BUCKET_LABELS = [
    "01/04 to 15/06", "16/06 to 15/09", "16/09 to 15/12",
    "16/12 to 15/03", "16/03 to 31/03",
]

_SECTION_ST = "Short Term Capital Gain"
_SECTION_LT_WITH_INDEX = "Long Term Capital Gain with Indexation"
_SECTION_LT_WITHOUT_INDEX = "Long Term Capital Gain without Indexation"
_SUMMARY_SECTIONS = (_SECTION_ST, _SECTION_LT_WITH_INDEX, _SECTION_LT_WITHOUT_INDEX)

# The final gain/loss row label per section -- what the three-way
# reconciliation's "Summary sheet Total" leg is built from.
_SECTION_GAIN_ROW_LABEL = {
    _SECTION_ST: "Short Term Capital Gain/Loss",
    _SECTION_LT_WITH_INDEX: "LongTermWithIndex-CapitalGain/Loss",
    _SECTION_LT_WITHOUT_INDEX: "LongTermWithOutIndex-CapitalGain/Loss",
}

# Reconciliation categories, mapped consistently across all three legs.
CATEGORY_ST = "Short Term"
CATEGORY_LT_WITH_INDEX = "Long Term With Indexation"
CATEGORY_LT_WITHOUT_INDEX = "Long Term Without Indexation"
CATEGORIES = (CATEGORY_ST, CATEGORY_LT_WITH_INDEX, CATEGORY_LT_WITHOUT_INDEX)

_SECTION_TO_CATEGORY = {
    _SECTION_ST: CATEGORY_ST,
    _SECTION_LT_WITH_INDEX: CATEGORY_LT_WITH_INDEX,
    _SECTION_LT_WITHOUT_INDEX: CATEGORY_LT_WITHOUT_INDEX,
}

# Amount-reconciliation tolerance in rupees -- named per repo convention
# (see lots.py's UNITS_RECONCILIATION_TOLERANCE for the sibling constant).
AMOUNT_RECONCILIATION_TOLERANCE = 0.01

CANNOT_RECONCILE = "cannot reconcile -- a required figure was not found in this workbook"

_ISIN_RE = re.compile(r'\(\s*(?P<isin>[A-Z0-9]{12})\s*\)\s*$')


class CGParseError(ValueError):
    """Raised when the workbook does not look like a KFintech Capital
    Gain / Loss Statement -- always states what was expected and what was
    actually found, never fails silently with an empty report."""


def _norm(s) -> str:
    """Normalise a header/label cell for matching: collapse NBSP/newlines/
    runs of whitespace to single spaces, strip, lowercase."""
    if s is None:
        return ""
    s = str(s).replace("\xa0", " ").replace("\n", " ")
    return re.sub(r"\s+", " ", s).strip().lower()


def _num(v) -> float | None:
    """Coerce a cell value to float. Scheme_Level_Summary and
    Trasaction_Details store some numeric columns as text strings (verified
    against the real KFintech workbook) -- tolerate both. None/blank -> None
    (never guessed as 0; callers decide when None should be treated as 0)."""
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).replace(",", "").strip())
    except ValueError:
        return None


def _num0(v) -> float:
    """Same as _num but coerces missing -> 0.0, for summation contexts where
    the RTA leaves an all-zero Total cell blank (verified: a Summary sheet
    row whose five buckets are all 0 has Total == None, not 0)."""
    n = _num(v)
    return 0.0 if n is None else n


def extract_isin(raw_name: str) -> tuple[str, str | None]:
    """Split 'Some Fund - Regular Plan ( INF247L01EG4)' into
    ('Some Fund - Regular Plan', 'INF247L01EG4'). Returns the raw string
    unchanged (and isin=None) if no trailing parenthesised ISIN is found."""
    if not raw_name:
        return raw_name, None
    text = str(raw_name).strip()
    m = _ISIN_RE.search(text)
    if not m:
        return text, None
    isin = m.group("isin")
    name = text[: m.start()].strip()
    return name, isin


def find_sheet(wb, *names: str):
    for n in names:
        if n in wb.sheetnames:
            return wb[n]
    return None


def validate_workbook_shape(wb) -> None:
    """Raise CGParseError naming exactly what was expected vs found if this
    workbook does not have the four sheets a KFintech Capital Gain / Loss
    Statement must have. Never returns an empty report for an unrecognised
    workbook (see AGENT.md 2.4 / the skill's original silent-empty defect)."""
    found = list(wb.sheetnames)
    missing = [s for s in REQUIRED_SHEETS if s not in found]
    has_txn_sheet = SHEET_TXN_DETAILS in found or SHEET_TXN_DETAILS_ALT in found
    if not has_txn_sheet:
        missing.append(f"{SHEET_TXN_DETAILS} (or {SHEET_TXN_DETAILS_ALT})")
    if missing:
        raise CGParseError(
            "This workbook does not look like a KFintech Capital Gain / Loss "
            "Statement -- expected sheets " + ", ".join(REQUIRED_SHEETS + (SHEET_TXN_DETAILS,))
            + f", missing: {', '.join(missing)}. Sheets actually found: {', '.join(found) or '(none)'}."
        )


# ---------------------------------------------------------------------------
# Summary - Equity / Summary - NonEquity
# ---------------------------------------------------------------------------

@dataclass
class SummaryFieldRow:
    section: str
    label: str
    buckets: dict  # bucket label -> float
    total: float | None  # as given -- may be None even when buckets are 0


@dataclass
class SummarySheetData:
    sheet_label: str  # "Equity" / "NonEquity"
    rows: list = field(default_factory=list)  # list[SummaryFieldRow]

    def gain_loss_row(self, section: str) -> SummaryFieldRow | None:
        label = _SECTION_GAIN_ROW_LABEL[section]
        for r in self.rows:
            if r.section == section and r.label == label:
                return r
        return None


def _find_header_row(ws, expected_first_cell: str = "summary of capital gains", max_scan: int = 10) -> int:
    for r in range(1, max_scan + 1):
        if _norm(ws.cell(row=r, column=1).value) == expected_first_cell:
            return r
    raise CGParseError(
        f"Could not locate the Summary sheet's header row (expected column A "
        f"row text 'Summary Of Capital Gains' within the first {max_scan} rows "
        f"of sheet '{ws.title}')."
    )


def parse_summary_sheet(ws, sheet_label: str) -> SummarySheetData:
    """Label-matched parse of a Summary - Equity/NonEquity sheet. Never reads
    by hardcoded row number -- walks rows, tracks the current section from
    the three section-header rows, and matches each field row's column-1
    label text against the expected label for that section."""
    header_row = _find_header_row(ws)
    bucket_cols = list(range(2, 2 + len(BUCKET_LABELS)))  # c2..c6
    total_col = 2 + len(BUCKET_LABELS)  # c7

    headers = [_norm(ws.cell(row=header_row, column=c).value) for c in bucket_cols]
    expected = [_norm(b) for b in BUCKET_LABELS]
    if headers != expected:
        raise CGParseError(
            f"Sheet '{ws.title}': expected the five s.234C period bucket headers "
            f"{BUCKET_LABELS} in row {header_row}, cols B:F -- found {headers}."
        )

    section_labels_norm = {_norm(s): s for s in _SUMMARY_SECTIONS}
    rows: list[SummaryFieldRow] = []
    current_section: str | None = None

    r = header_row + 1
    max_row = ws.max_row
    while r <= max_row:
        label_raw = ws.cell(row=r, column=1).value
        label_n = _norm(label_raw)
        if not label_n:
            r += 1
            continue
        if label_n in section_labels_norm:
            current_section = section_labels_norm[label_n]
            r += 1
            continue
        if current_section is not None:
            buckets = {
                BUCKET_LABELS[i]: _num0(ws.cell(row=r, column=bucket_cols[i]).value)
                for i in range(len(BUCKET_LABELS))
            }
            total = _num(ws.cell(row=r, column=total_col).value)
            rows.append(SummaryFieldRow(
                section=current_section, label=str(label_raw).strip(),
                buckets=buckets, total=total,
            ))
        r += 1

    missing_sections = [s for s in _SUMMARY_SECTIONS if s not in {row.section for row in rows}]
    if missing_sections or current_section is None:
        raise CGParseError(
            f"Sheet '{ws.title}': did not find all three expected sections "
            f"({', '.join(_SUMMARY_SECTIONS)}) below the header row -- this "
            "sheet does not match the expected KFintech Capital Gain / Loss "
            "Statement layout."
        )

    return SummarySheetData(sheet_label=sheet_label, rows=rows)


# ---------------------------------------------------------------------------
# Scheme_Level_Summary
# ---------------------------------------------------------------------------

@dataclass
class SchemeSummaryRow:
    scheme_name: str
    isin: str | None
    count: float | None
    outflow_amount: float
    net_value: float
    fair_market_nav: float | None
    fair_market_value: float | None
    short_gain: float
    long_gain_with_index: float
    long_gain_without_index: float


_SCHEME_SUMMARY_HEADERS = [
    "scheme name", "count", "outflow amount", "net value", "fair market nav",
    "fair market value", "short gain", "long gain with index", "long gain without index",
]


def parse_scheme_level_summary(ws) -> tuple[list, SchemeSummaryRow | None]:
    """Returns (per-scheme rows, Total row-or-None). Header row is located by
    matching column-1 text 'Scheme Name' (not a hardcoded row number)."""
    header_row = None
    for r in range(1, 11):
        if _norm(ws.cell(row=r, column=1).value) == "scheme name":
            header_row = r
            break
    if header_row is None:
        raise CGParseError(
            f"Sheet '{ws.title}': could not locate the header row (expected "
            "column A text 'Scheme Name' within the first 10 rows)."
        )

    ncols = len(_SCHEME_SUMMARY_HEADERS)
    headers = [_norm(ws.cell(row=header_row, column=c).value) for c in range(1, ncols + 1)]
    if headers != _SCHEME_SUMMARY_HEADERS:
        raise CGParseError(
            f"Sheet '{ws.title}': expected header row {_SCHEME_SUMMARY_HEADERS} "
            f"at row {header_row} -- found {headers}."
        )

    rows: list[SchemeSummaryRow] = []
    total_row: SchemeSummaryRow | None = None
    r = header_row + 1
    while r <= ws.max_row:
        c1 = ws.cell(row=r, column=1).value
        c1_n = _norm(c1)
        if not c1_n:
            r += 1
            continue
        vals = [ws.cell(row=r, column=c).value for c in range(1, ncols + 1)]
        if c1_n == "total":
            total_row = SchemeSummaryRow(
                scheme_name="Total", isin=None, count=_num(vals[1]),
                outflow_amount=_num0(vals[2]), net_value=_num0(vals[3]),
                fair_market_nav=_num(vals[4]), fair_market_value=_num(vals[5]),
                short_gain=_num0(vals[6]), long_gain_with_index=_num0(vals[7]),
                long_gain_without_index=_num0(vals[8]),
            )
        else:
            name, isin = extract_isin(str(c1))
            rows.append(SchemeSummaryRow(
                scheme_name=name, isin=isin, count=_num(vals[1]),
                outflow_amount=_num0(vals[2]), net_value=_num0(vals[3]),
                fair_market_nav=_num(vals[4]), fair_market_value=_num(vals[5]),
                short_gain=_num0(vals[6]), long_gain_with_index=_num0(vals[7]),
                long_gain_without_index=_num0(vals[8]),
            ))
        r += 1

    return rows, total_row


# ---------------------------------------------------------------------------
# Trasaction_Details -- matched-pair rows (RTA has already FIFO-matched)
# ---------------------------------------------------------------------------

@dataclass
class MatchedLotRow:
    """One RTA-matched acquisition/outflow/gain triple -- one row per
    consumed buy lot, the same shape lots.DisposalLot models for the
    FIFO-derived (CAS-text) path, but carrying the additional
    grandfathering/tax fields this statement supplies directly."""
    fund_code: str
    fund_name: str
    folio: str
    scheme_name: str
    isin: str | None
    # Section A -- acquisition (as matched by the RTA)
    acq_trxn_type: str
    acq_date: date | None
    current_units: float | None
    source_scheme_units: float | None
    original_purchase_cost: float | None   # per-unit cost/NAV at acquisition
    original_cost_amount: float | None
    grandfathered_nav: float | None
    grandfathered_cost_value: float | None
    it_applicable_nav: float | None
    it_applicable_cost_value: float | None
    # Section B -- outflow
    outflow_trxn_type: str
    outflow_date: date | None
    units: float | None
    amount: float | None
    price: float | None
    tax_perc: float | None
    tax: float | None
    # Section C -- gains/losses, reported as given (not reclassified)
    short_term: float
    indexed_cost: float | None
    long_term_with_index: float
    long_term_without_index: float


# (normalised header text, field name) -- position-independent within its
# section but never confused across sections (Trxn.Type/Date appear in both
# Section A and Section B) because the header row is segmented into
# common / Section A / Section B / Section C column ranges first, using the
# "Section A/B/C" banner row above it, per AGENT.md.
_COMMON_HEADERS = [
    ("fund", "fund_code"), ("fund name", "fund_name"),
    ("folio number", "folio"), ("scheme name", "scheme_name"),
]
_SECTION_A_HEADERS = [
    ("trxn.type", "acq_trxn_type"), ("date", "acq_date"),
    ("current units", "current_units"), ("source scheme units", "source_scheme_units"),
    ("original purchase cost", "original_purchase_cost"),
    ("original cost amount", "original_cost_amount"),
    ("grandfathered nav as on", "grandfathered_nav"),  # prefix match, date varies
    ("grandfathered cost value", "grandfathered_cost_value"),
    ("it applicable nav", "it_applicable_nav"),
    ("it applicable cost value", "it_applicable_cost_value"),
]
_SECTION_B_HEADERS = [
    ("trxn.type", "outflow_trxn_type"), ("date", "outflow_date"),
    ("units", "units"), ("amount", "amount"), ("price", "price"),
    ("tax perc", "tax_perc"), ("tax", "tax"),
]
_SECTION_C_HEADERS = [
    ("short term", "short_term"), ("indexed cost", "indexed_cost"),
    ("long term with index", "long_term_with_index"),
    ("long term without index", "long_term_without_index"),
]


def _match_headers(ws, header_row: int, col_start: int, col_end: int,
                    label_specs: list) -> dict:
    """Map field name -> column index within [col_start, col_end], matching
    each label_specs entry's normalised text against the (normalised)
    header cell -- exact match, or prefix match for entries whose label
    text ends without a fixed suffix (the grandfathered-NAV column header
    embeds a date: 'Grandfathered NAV as on 31/01/2018')."""
    col_map: dict = {}
    for col in range(col_start, col_end + 1):
        header_n = _norm(ws.cell(row=header_row, column=col).value)
        if not header_n:
            continue
        for label, field_name in label_specs:
            if field_name in col_map:
                continue
            if header_n == label or header_n.startswith(label):
                col_map[field_name] = col
                break
    missing = [f for _, f in label_specs if f not in col_map]
    return col_map, missing


def _parse_date_cell(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    for fmt in ("%d/%m/%Y", "%d-%m-%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_transaction_details(ws) -> list:
    """Rows 1-2 (holder name, PAN) are never read -- deliberately skipped,
    they are PII and this skill must never extract them. Row 3 carries the
    Section A/B/C banners (used to segment the header row so the duplicate
    'Trxn.Type'/'Date' columns in Section A and Section B are never
    confused). Row 4 is the real header. Data starts row 5."""
    banner_row, header_row, data_start = 3, 4, 5

    banner_cols = []
    for c in range(1, ws.max_column + 1):
        v = ws.cell(row=banner_row, column=c).value
        if v and _norm(v).startswith(("section a", "section b", "section c")):
            banner_cols.append(c)
    if len(banner_cols) != 3:
        raise CGParseError(
            f"Sheet '{ws.title}': expected three 'Section A/B/C' banners on "
            f"row {banner_row} -- found {len(banner_cols)}: {banner_cols}."
        )
    a_start, b_start, c_start = banner_cols
    c_end = ws.max_column

    common_map, common_missing = _match_headers(ws, header_row, 1, a_start - 1, _COMMON_HEADERS)
    a_map, a_missing = _match_headers(ws, header_row, a_start, b_start - 1, _SECTION_A_HEADERS)
    b_map, b_missing = _match_headers(ws, header_row, b_start, c_start - 1, _SECTION_B_HEADERS)
    c_map, c_missing = _match_headers(ws, header_row, c_start, c_end, _SECTION_C_HEADERS)

    all_missing = common_missing + a_missing + b_missing + c_missing
    if all_missing:
        raise CGParseError(
            f"Sheet '{ws.title}': could not find expected column(s) "
            f"{all_missing} in the header row ({header_row})."
        )

    def cell(row, colmap, key):
        return ws.cell(row=row, column=colmap[key]).value

    rows: list[MatchedLotRow] = []
    r = data_start
    while r <= ws.max_row:
        folio_v = cell(r, common_map, "folio")
        if folio_v is None or str(folio_v).strip() == "":
            r += 1
            continue
        scheme_raw = cell(r, common_map, "scheme_name")
        scheme_name, isin = extract_isin(str(scheme_raw).strip() if scheme_raw else "")

        rows.append(MatchedLotRow(
            fund_code=str(cell(r, common_map, "fund_code") or "").strip(),
            fund_name=str(cell(r, common_map, "fund_name") or "").strip(),
            folio=str(folio_v).strip(),
            scheme_name=scheme_name, isin=isin,
            acq_trxn_type=str(cell(r, a_map, "acq_trxn_type") or "").strip(),
            acq_date=_parse_date_cell(cell(r, a_map, "acq_date")),
            current_units=_num(cell(r, a_map, "current_units")),
            source_scheme_units=_num(cell(r, a_map, "source_scheme_units")),
            original_purchase_cost=_num(cell(r, a_map, "original_purchase_cost")),
            original_cost_amount=_num(cell(r, a_map, "original_cost_amount")),
            grandfathered_nav=_num(cell(r, a_map, "grandfathered_nav")),
            grandfathered_cost_value=_num(cell(r, a_map, "grandfathered_cost_value")),
            it_applicable_nav=_num(cell(r, a_map, "it_applicable_nav")),
            it_applicable_cost_value=_num(cell(r, a_map, "it_applicable_cost_value")),
            outflow_trxn_type=str(cell(r, b_map, "outflow_trxn_type") or "").strip(),
            outflow_date=_parse_date_cell(cell(r, b_map, "outflow_date")),
            units=_num(cell(r, b_map, "units")),
            amount=_num(cell(r, b_map, "amount")),
            price=_num(cell(r, b_map, "price")),
            tax_perc=_num(cell(r, b_map, "tax_perc")),
            tax=_num(cell(r, b_map, "tax")),
            short_term=_num0(cell(r, c_map, "short_term")),
            indexed_cost=_num(cell(r, c_map, "indexed_cost")),
            long_term_with_index=_num0(cell(r, c_map, "long_term_with_index")),
            long_term_without_index=_num0(cell(r, c_map, "long_term_without_index")),
        ))
        r += 1

    return rows


# ---------------------------------------------------------------------------
# Three-way reconciliation
# ---------------------------------------------------------------------------

@dataclass
class ReconciliationResult:
    category: str
    txn_details_total: float | None
    scheme_summary_total: float | None
    summary_sheet_total: float | None
    agree: bool | None       # None when not derivable -- see `note`
    note: str | None = None  # explicit reason, set whenever agree is None,
    # or a human-readable variance detail when agree is False.


def reconcile(
    summary_equity: SummarySheetData,
    summary_nonequity: SummarySheetData,
    scheme_rows: list,
    scheme_total: SchemeSummaryRow | None,
    txn_rows: list,
) -> list:
    """Three-way check per gain category:
      (a) sum of Section C gains across Trasaction_Details rows,
      (b) the Scheme_Level_Summary Total row (or, if absent, a summed
          fallback over its per-scheme rows -- explicitly noted either way),
      (c) Summary - Equity Total + Summary - NonEquity Total for that
          category's gain/loss row.
    Never leaves a blank/absent result for a category that could not be
    reconciled -- an explicit CANNOT_RECONCILE note is emitted instead."""
    results: list[ReconciliationResult] = []

    a_totals = {c: 0.0 for c in CATEGORIES}
    for t in txn_rows:
        a_totals[CATEGORY_ST] += t.short_term
        a_totals[CATEGORY_LT_WITH_INDEX] += t.long_term_with_index
        a_totals[CATEGORY_LT_WITHOUT_INDEX] += t.long_term_without_index

    if scheme_total is not None:
        b_totals = {
            CATEGORY_ST: scheme_total.short_gain,
            CATEGORY_LT_WITH_INDEX: scheme_total.long_gain_with_index,
            CATEGORY_LT_WITHOUT_INDEX: scheme_total.long_gain_without_index,
        }
    elif scheme_rows:
        b_totals = {
            CATEGORY_ST: sum(s.short_gain for s in scheme_rows),
            CATEGORY_LT_WITH_INDEX: sum(s.long_gain_with_index for s in scheme_rows),
            CATEGORY_LT_WITHOUT_INDEX: sum(s.long_gain_without_index for s in scheme_rows),
        }
    else:
        b_totals = None

    for section, category in _SECTION_TO_CATEGORY.items():
        eq_row = summary_equity.gain_loss_row(section)
        neq_row = summary_nonequity.gain_loss_row(section)
        if eq_row is None or neq_row is None:
            results.append(ReconciliationResult(
                category=category, txn_details_total=a_totals[category],
                scheme_summary_total=(b_totals[category] if b_totals else None),
                summary_sheet_total=None, agree=None, note=CANNOT_RECONCILE
                + f" (missing '{_SECTION_GAIN_ROW_LABEL[section]}' row in a Summary sheet)",
            ))
            continue
        c_total = (eq_row.total or 0.0) + (neq_row.total or 0.0)

        if b_totals is None:
            results.append(ReconciliationResult(
                category=category, txn_details_total=a_totals[category],
                scheme_summary_total=None, summary_sheet_total=c_total,
                agree=None, note=CANNOT_RECONCILE + " (Scheme_Level_Summary had no rows)",
            ))
            continue

        a_val, b_val = a_totals[category], b_totals[category]
        max_diff = max(abs(a_val - b_val), abs(a_val - c_total), abs(b_val - c_total))
        agree = max_diff <= AMOUNT_RECONCILIATION_TOLERANCE
        note = None if agree else (
            f"variance -- Trasaction_Details sum={a_val:.2f}, "
            f"Scheme_Level_Summary total={b_val:.2f}, "
            f"Summary sheets total={c_total:.2f}"
        )
        results.append(ReconciliationResult(
            category=category, txn_details_total=a_val, scheme_summary_total=b_val,
            summary_sheet_total=c_total, agree=agree, note=note,
        ))

    return results
