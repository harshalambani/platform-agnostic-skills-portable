"""
presentation.py -- the deliverable/presentation layer over the calculation
engine (2026-07-19 "presentable workbook" prompt).

The workbook write_workbook.py produces is a *calculation engine*: 17 sheets,
every figure traceable, none of it printable. This module ADDS presentable
sheets in front of those 17, in this order -- `Statement of Income`, `BS`,
`IS`, `PL for Business` (present only for an entity with a configured
`business_subtree`, 2026-07-19 "PL for Business" prompt), `CG` -- and hides
the four raw working sheets. It changes no computation, no rule, no rate and
no tax logic, and it overwrites no existing sheet's values.

**Every money cell written here is an Excel formula pointing back at the
existing sheets** (`Computation`, `CapitalGains`, `OtherSources`,
`IS_Transcript`, `BS_Transcript`, `TaxesPaid`, `Entity`). Nothing is
recomputed and nothing is hardcoded -- that formula link is the audit trail
this tool has and a hand-built workbook does not. The only literals this
module writes are labels, headings and captions.

Two items are PARKED by Harshal's explicit direction (2026-07-19) and render
as a label plus an empty, visibly-styled value cell so the layout is final and
they can be filled later without a re-layout: Father's Name and Aadhaar No.
A third, residential status, is an *assumed* constant `R/OR` -- it renders a
value a reader would take as computed, so it carries a footnote marker and an
Assumptions note (see `_STATUS_FOOTNOTE`).

Brought-forward-loss set-off was PARKED through 2026-07-19; as of the
2026-07-20 on-page-totals change it is a REAL, editable input cell
(`_INPUT_FILL`, defaulting to 0) instead -- see `write_statement_of_income`.
That same change moves the income-ladder totals (Gross Total Income, Total
Income, and the normal-income/special-rate-CG split feeding `Computation`'s
slab tax) onto this sheet as on-page formulas, so an override anywhere in the
ladder -- a leaf, Chapter VI-A, or the b/f-loss cell -- now flows end-to-end
through tax, cess and refund. See
`docs/2026-07-20-itr-onpage-totals-plan.md`.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.properties import PageSetupProperties

INR_FORMAT = "#,##,##0.00"
DATE_FORMAT = "DD-MM-YYYY"

FONT_NAME = "Arial"
INDENT = " " * 6          # CA file indents ~6 spaces per hierarchy level

#: Canonical order for the deliverable sheets. This is the ONLY place order is
#: expressed: `move_presentation_sheets_first` positions whatever subset of
#: these actually got created, so a sheet may be conditionally omitted (`CG`
#: and `PL for Business` already are) and a further sheet may later be
#: inserted at any position by adding its name here -- nothing downstream
#: assumes a fixed count or that any particular sheet is present.
PRESENTATION_SHEETS = ("Statement of Income", "BS", "IS", "PL for Business", "CG")

#: Raw working sheets hidden (never deleted) once the four above exist.
HIDDEN_SHEETS = ("Rules", "Mapping Review", "IS_Transcript", "BS_Transcript")

_STATUS_FOOTNOTE = "*"
_ASSUMPTION_NOTE = (
    "* Residential status is ASSUMED to be R/OR (Resident and Ordinarily Resident). "
    "It is not determined by this tool -- no day-count test or RNOR analysis is "
    "performed. Confirm before filing: R/OR vs RNOR vs NR changes what income is "
    "taxable at all."
)

#: Fail-loud (2026-07-19 CG gain-split-vs-action fix), "banner, no abort":
#: schedules.py's build_capital_gains already computes reconciliation_ok /
#: reconciliation_diff correctly -- this is only about making a mismatch
#: IMPOSSIBLE TO MISS on the deliverable sheets, never about refusing to
#: produce the workbook. `CG_RECONCILIATION_ERROR_MARKER` is the substring
#: agent.py greps its own run() summary for to decide the process's exit
#: code, so the wording here and the summary line it feeds must both
#: contain it verbatim.
CG_RECONCILIATION_ERROR_MARKER = "ERROR: Capital Gains do not reconcile to books"
_CG_ERROR_FILL = PatternFill(start_color="FFC7CE", end_color="FFC7CE", fill_type="solid")
_CG_ERROR_FONT_COLOR = "9C0006"


def cg_mismatch_banner_text(cg_schedule) -> str:
    return (
        f"*** {CG_RECONCILIATION_ERROR_MARKER} "
        f"(diff {cg_schedule.reconciliation_diff:,.2f}) -- DO NOT FILE ***"
    )


def _write_cg_error_banner(ws, row: int, cg_schedule, ncols: int) -> int:
    """Top-of-sheet ERROR banner, present only when the CG lot-sum fails to
    reconcile to the books' control total (schedules.py build_capital_gains,
    diff magnitude > 0.01). Modeled on the CapitalGains sheet's existing
    'Unresolved FMV scrips (fail-loud, review)' row, but rendered as a
    prominent merged banner at the very top of a presentation sheet instead
    of a label/value cell buried in a working sheet -- this is what a CA or
    a bank actually opens. The workbook is ALWAYS still produced ("banner,
    no abort"); omitted entirely (returns `row` unchanged) when reconciled."""
    if cg_schedule.reconciliation_ok:
        return row
    c = ws.cell(row=row, column=1, value=cg_mismatch_banner_text(cg_schedule))
    c.font = Font(name=FONT_NAME, size=11, bold=True, color=_CG_ERROR_FONT_COLOR)
    c.fill = _CG_ERROR_FILL
    c.alignment = Alignment(horizontal="center")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=max(ncols, 1))
    return row + 2

_PARKED_NOTE = "(to be filled)"

#: Human labels for rules.resolve_age_class()'s return values. 'general'
#: deliberately renders no suffix (prompt section 2b).
_AGE_LABELS = {"general": "", "senior": "Senior Citizen", "super_senior": "Super Senior Citizen"}

_THIN = Side(style="thin")
_TOP_RULE = Border(top=_THIN)
_BOTTOM_RULE = Border(bottom=_THIN)
#: The empty-but-visible cell used for every PARKED value (prompt 2a/2d).
_PARKED_BORDER = Border(bottom=Side(style="dotted"))
_PARKED_FILL = PatternFill(start_color="FFF2CC", end_color="FFF2CC", fill_type="solid")

# A real, editable INPUT cell (2026-07-20 on-page-totals change) -- distinct
# from a PARKED cell: it always carries a literal value (never blank) and
# feeds live downstream formulas. Styled with a solid border (not dotted) and
# a different fill so a reader -- and a test -- can tell "type here, it
# means something" apart from "not filled in yet". See b/f-loss cell below.
_INPUT_BORDER = Border(bottom=Side(style="thin"))
_INPUT_FILL = PatternFill(start_color="DDEBF7", end_color="DDEBF7", fill_type="solid")


def _input_cell(ws, row: int, col: int, default_value=0):
    """A real, editable input cell: a literal value (default 0), visibly
    styled so it reads as "preparer-editable", never a formula. Unlike
    `_parked_cell` this is wired into downstream formulas -- overriding it
    must move every total below it."""
    c = ws.cell(row=row, column=col, value=default_value)
    c.number_format = INR_FORMAT
    c.border = _INPUT_BORDER
    c.fill = _INPUT_FILL
    c.font = _font()
    return c


def _font(size: int = 10, *, bold: bool = False, underline: str | None = None,
          italic: bool = False) -> Font:
    return Font(name=FONT_NAME, size=size, bold=bold, underline=underline, italic=italic)


def status_line(age_class: str, residency: str = "R/OR", *, declared: bool = False) -> str:
    """The header block's residential-status line. `residency` is the
    resolved value (R/OR / RNOR / NR) from `rules.resolve_residency()`; the
    age half is whatever `rules.resolve_age_class()` returned. 'general'
    renders no suffix. The footnote marker is dropped once `declared` is
    True -- a reader must be able to tell "someone asserted this" from "the
    tool fell back" (2026-07-19 residency prompt, section 2)."""
    label = _AGE_LABELS.get(age_class, "")
    base = f"{residency} - {label}" if label else residency
    return base if declared else base + " " + _STATUS_FOOTNOTE


# ---------------------------------------------------------------------------
# Hierarchy rebuild -- IS_Transcript / BS_Transcript `Path` column -> tree
# ---------------------------------------------------------------------------

@dataclass
class HNode:
    """One node of a GnuCash account tree rebuilt from transcript `Path`
    strings. Leaves carry `source` (the A1 ref of the transcript cell holding
    their amount); groups carry children and get their own subtotal row."""
    name: str
    source: str | None = None
    children: dict = field(default_factory=dict)   # name -> HNode, insertion-ordered

    @property
    def is_leaf(self) -> bool:
        return not self.children


def build_hierarchy(entries) -> HNode:
    """`entries` is [(path, source_cell), ...] where `path` is a GnuCash
    account path like 'Assets/Current Assets/Cash and Bank/BOB - <acct>'.
    Splits on '/' to rebuild the full tree -- depth is driven by the data, so
    a deeper or shallower book renders correctly with no hardcoded level
    count."""
    root = HNode("")
    for path, source in entries:
        parts = [p for p in str(path).split("/") if p != ""]
        if not parts:
            continue
        node = root
        for part in parts[:-1]:
            node = node.children.setdefault(part, HNode(part))
        leaf = node.children.setdefault(parts[-1], HNode(parts[-1]))
        leaf.source = source
    return root


def max_group_level(root: HNode) -> int:
    """Deepest *group* (non-leaf) level below `root`, 0-based. Drives how many
    tiered subtotal columns the sheet needs -- never a hardcoded count."""
    best = -1

    def walk(node: HNode, level: int) -> None:
        nonlocal best
        for child in node.children.values():
            if not child.is_leaf:
                best = max(best, level)
                walk(child, level + 1)

    walk(root, 0)
    return best


def render_hierarchy(ws, start_row: int, root: HNode, source_sheet: str,
                     label_col: int = 1, money_col: int = 2) -> tuple:
    """Render `root`'s children as an indented, tiered-subtotal block.

    Leaf amounts sit in `money_col`; a group's subtotal sits one column
    further out per level of nesting, so a leaf, its group total and a
    higher-level total occupy three different columns -- exactly how the CA
    reference builds `C14 = SUM(B10:B13)` and tiers above it.

    Every amount cell is a formula: leaves point at `source_sheet`, group
    subtotals SUM their own children's cells on this sheet.

    Returns (next_row, [(row, col) for each top-level group/leaf]).
    """
    depth = max_group_level(root)
    row = start_row

    def col_for_group(level: int) -> int:
        return money_col + (depth - level) + 1

    def emit(node: HNode, level: int) -> tuple:
        nonlocal row
        label = INDENT * level + node.name
        if node.is_leaf:
            ws.cell(row=row, column=label_col, value=label).font = _font()
            c = ws.cell(row=row, column=money_col, value=f"='{source_sheet}'!{node.source}")
            c.number_format = INR_FORMAT
            c.font = _font()
            here = (row, money_col)
            row += 1
            return here

        head = ws.cell(row=row, column=label_col, value=label)
        head.font = _font(bold=True)
        row += 1

        child_cells = [emit(child, level + 1) for child in node.children.values()]

        total_col = col_for_group(level)
        tc = ws.cell(row=row, column=label_col, value=INDENT * level + f"Total {node.name}")
        tc.font = _font(bold=True)
        if child_cells and all(c[1] == money_col for c in child_cells):
            first, last = child_cells[0][0], child_cells[-1][0]
            letter = get_column_letter(money_col)
            formula = f"=SUM({letter}{first}:{letter}{last})"
        else:
            formula = "=" + "+".join(f"{get_column_letter(c)}{r}" for r, c in child_cells) if child_cells else "=0"
        t = ws.cell(row=row, column=total_col, value=formula)
        t.number_format = INR_FORMAT
        t.font = _font(bold=True)
        t.border = _TOP_RULE
        here = (row, total_col)
        row += 1
        return here

    tops = [emit(child, 0) for child in root.children.values()]
    return row, tops


# ---------------------------------------------------------------------------
# Shared formatting / print setup
# ---------------------------------------------------------------------------

def apply_sheet_chrome(ws, widths: dict, last_row: int, last_col: int, *,
                       freeze: str | None = None, landscape: bool = False,
                       print_title: str = "") -> None:
    """Column widths, gridlines off, freeze panes, print area and A4
    fit-to-one-page-wide print setup. No sheet ships with default widths."""
    for col, width in widths.items():
        ws.column_dimensions[col].width = width
    ws.sheet_view.showGridLines = False
    if freeze:
        ws.freeze_panes = freeze

    ws.print_area = f"A1:{get_column_letter(last_col)}{max(last_row, 1)}"
    ws.page_setup.paperSize = ws.PAPERSIZE_A4
    ws.page_setup.orientation = "landscape" if landscape else "portrait"
    ws.sheet_properties.pageSetUpPr = PageSetupProperties(fitToPage=True)
    ws.page_setup.fitToWidth = 1
    ws.page_setup.fitToHeight = 0
    ws.page_margins.left = ws.page_margins.right = 0.5
    ws.page_margins.top = ws.page_margins.bottom = 0.6
    if print_title:
        ws.oddHeader.center.text = print_title
        ws.oddFooter.right.text = "Page &P of &N"


def fit_label_width(ws, col: int, minimum: float, cap: float = 90.0) -> float:
    """Label-column width: wide enough for the longest label actually present,
    never narrower than the CA reference's own width, never absurdly wide.

    The CA file's fixed A=38.1/47.8 were tuned to that one entity's account
    names; a book with deeper nesting or longer names would truncate at those
    widths, and a truncated label is the single most visible defect this whole
    exercise exists to fix. Formulas are skipped -- their rendered text is not
    the formula string.
    """
    longest = 0
    for row in ws.iter_rows(min_col=col, max_col=col):
        for c in row:
            if isinstance(c.value, str) and not c.value.startswith("="):
                longest = max(longest, len(c.value))
    return max(minimum, min(cap, longest + 2))


def _aadhaar_formula(ref: str) -> str:
    """CA-file space-grouped Aadhaar (`NNNN NNNN NNNN`), built as a formula
    over the raw digits stored on the Entity sheet -- never a second literal
    copy of the number (2026-07-19 residency prompt, section 1)."""
    return f'=LEFT({ref},4)&" "&MID({ref},5,4)&" "&MID({ref},9,4)'


def _parked_cell(ws, row: int, col: int):
    """A PARKED value: empty, but visibly styled -- dotted rule plus a light
    fill -- so the reader can see the field exists and is unfilled (prompt
    2a/2d). Never carries a value, and the row is never dropped. The
    "(to be filled)" wording goes in the LABEL, so no stray text ever lands in
    a money column."""
    c = ws.cell(row=row, column=col, value=None)
    c.border = _PARKED_BORDER
    c.fill = _PARKED_FILL
    c.font = _font(italic=True)
    return c


# ---------------------------------------------------------------------------
# Sheet 1 -- Statement of Income
# ---------------------------------------------------------------------------

def write_statement_of_income(wb, model, entity_layout: dict, comp_layout: dict,
                              os_layout: dict, tp_layout: dict, ded_layout: dict,
                              rules_layout: dict,
                              age_class: str, period_label: str, print_title: str,
                              computation_tail_fn, *,
                              father_name: str | None = None, aadhaar: str | None = None,
                              residency_value: str = "R/OR", residency_declared: bool = False):
    """Mirrors the CA reference's `ITWorking`: a letterhead header block, then
    three money columns (line items / sub-totals / running total) showing ONLY
    the selected regime.

    2026-07-20 on-page-totals change: the income ladder (GTI -> Chapter VI-A
    -> b/f loss -> Total Income -> normal/special-CG split) is now computed
    ON THIS PAGE from on-page cells, rather than mirroring a hidden
    `Computation` sheet. `comp_layout` here is the LEAF layout only (salary,
    hp, business, os, cg_lt, cg_st -- see `write_computation_leaf_cells`).
    Once this page has written its own `normal_income_base` /
    `total_income` coordinates, it calls `computation_tail_fn(page_layout)`
    to have `write_computation_tail` write the re-anchored slab-tax
    machinery on `Computation`, reading THIS page's cells. That callback
    returns a comp_layout-shaped dict (`tail_layout`) for the tax/cess/
    liability/refund lines below. See `docs/2026-07-20-itr-onpage-totals-plan.md`.
    """
    ws = wb.create_sheet("Statement of Income")
    ent = lambda key: f"='Entity'!{entity_layout[key].coordinate}"  # noqa: E731
    # comp_layout values are already sheet-qualified expressions
    # (e.g. "'Computation'!B12" or "'Computation'!B45+'Computation'!B46").
    comp = lambda key: f"={comp_layout[key]}"                        # noqa: E731

    regime_ref = f"'Entity'!{entity_layout['regime'].coordinate}"

    def sel(new_expr: str, old_expr: str) -> str:
        """Selected-regime picker -- the both-regimes comparison stays on the
        Computation working sheet; it is not deliverable content."""
        return f"=IF({regime_ref}=\"old\",{old_expr},{new_expr})"

    LBL, INNER, SUB, OUTER = 2, 3, 4, 5

    # -- header block -------------------------------------------------------
    row = 1
    row = _write_cg_error_banner(ws, row, model.capital_gains, OUTER)
    title = ws.cell(row=row, column=1, value="STATEMENT OF INCOME")
    title.font = _font(14, bold=True)
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=OUTER)
    title.alignment = Alignment(horizontal="center")
    row += 2

    def left(label: str, formula: str | None):
        ws.cell(row=row, column=1, value=label).font = _font(bold=True)
        if formula is None:
            _parked_cell(ws, row, 2)
        else:
            ws.cell(row=row, column=2, value=formula).font = _font()

    def right(label: str, value, *, parked: bool = False):
        ws.cell(row=row, column=4, value=label).font = _font(bold=True)
        if parked:
            _parked_cell(ws, row, 5)
        else:
            ws.cell(row=row, column=5, value=value).font = _font()

    left("Name", ent("name"))
    right("Previous Year", period_label)
    row += 1
    # Father's Name / Aadhaar are OPTIONAL entity fields (2026-07-19 residency
    # prompt, section 1 -- previously PARKED, Harshal 2026-07-19). Per field:
    # present -> a real formula + label with "(to be filled)" dropped; absent
    # -> label keeps "(to be filled)" and the cell stays empty-but-styled.
    if father_name:
        left("Father's Name", ent("father_name"))
    else:
        left(f"Father's Name {_PARKED_NOTE}", None)
    right("PAN", ent("pan"))
    row += 1
    left("Address", ent("address"))
    if aadhaar:
        right("Aadhaar No.", _aadhaar_formula(f"'Entity'!{entity_layout['aadhaar'].coordinate}"))
    else:
        right(f"Aadhaar No. {_PARKED_NOTE}", None, parked=True)
    row += 1
    left("", "");                             right("Date of Birth", ent("dob"));          row += 1
    ws.cell(row=row, column=4, value="Status").font = _font(bold=True)
    ws.cell(row=row, column=5, value=ent("status")).font = _font()
    row += 1
    ws.cell(row=row, column=4, value="Residential Status").font = _font(bold=True)
    ws.cell(row=row, column=5,
            value=status_line(age_class, residency_value, declared=residency_declared)).font = _font()
    row += 1
    ws.cell(row=row, column=4, value="Regime").font = _font(bold=True)
    ws.cell(row=row, column=5,
            value=f"=IF({regime_ref}=\"old\",\"Old Regime\",\"Tax u/s 115BAC\")").font = _font()
    row += 2

    header_end = row
    for col in (INNER, SUB, OUTER):
        c = ws.cell(row=row, column=col, value="Rs.")
        c.font = _font(bold=True)
        c.alignment = Alignment(horizontal="center")
        c.border = _BOTTOM_RULE
    row += 1

    # -- body ---------------------------------------------------------------
    def section(name: str):
        c = ws.cell(row=row, column=1, value="•")
        c.font = _font(bold=True)
        h = ws.cell(row=row, column=LBL, value=name)
        h.font = _font(bold=True, underline="single")

    def money(col: int, formula: str, *, bold: bool = False, rule: Border | None = None):
        c = ws.cell(row=row, column=col, value=formula)
        c.number_format = INR_FORMAT
        c.font = _font(bold=bold)
        if rule is not None:
            c.border = rule
        return c

    def item(label: str, formula: str):
        ws.cell(row=row, column=LBL, value=INDENT + label).font = _font()
        money(INNER, formula)

    def line(label: str, col: int, formula: str, *, bold: bool = False,
             rule: Border | None = None):
        ws.cell(row=row, column=LBL, value=label).font = _font(bold=bold)
        money(col, formula, bold=bold, rule=rule)

    subtotal_cells: list[str] = []

    def close_section(first_item_row: int):
        """Section items live in the inner column; the section total lands one
        column out."""
        nonlocal row
        letter = get_column_letter(INNER)
        c = ws.cell(row=row, column=SUB, value=f"=SUM({letter}{first_item_row}:{letter}{row - 1})")
        c.number_format = INR_FORMAT
        c.font = _font(bold=True)
        c.border = _TOP_RULE
        subtotal_cells.append(f"{get_column_letter(SUB)}{row}")
        row += 1

    # Heads of income -- rendered only when the underlying figure is non-zero.
    if model.salary.income_chargeable:
        section("Income from Salary"); row += 1
        first = row
        item("Income chargeable under Salaries", comp("salary")); row += 1
        close_section(first)

    if model.house_property.income:
        section("Income from House Property"); row += 1
        first = row
        item("Income from house property", comp("hp")); row += 1
        close_section(first)

    if model.business.remuneration or model.business.expenses_total:
        section("Income from Business or Profession"); row += 1
        first = row
        item("Net business income", comp("business")); row += 1
        close_section(first)

    cg = model.capital_gains
    if cg.lt_taxable_gross or cg.st_taxable_gross:
        section("Capital Gains"); row += 1
        first = row
        if cg.lt_taxable_gross:
            item("Long Term Capital Gain / (Loss)", comp("cg_lt")); row += 1
        if cg.st_taxable_gross:
            item("Short Term Capital Gain / (Loss)", comp("cg_st")); row += 1
        close_section(first)

    os_ = model.other_sources
    os_items = (
        ("Savings bank interest", "sb", os_.interest_sb),
        ("Bank FD interest", "bank", os_.interest_bank),
        ("NBFC/HFC interest", "nbfc", os_.interest_nbfc),
        ("EPF taxable interest", "epf", os_.interest_epf_taxable),
        ("Interest on Income Tax refund", "refund_interest", os_.refund_interest),
        ("Dividend income (gross)", "dividend", os_.dividend_gross),
        ("SLBS income", "slbs", os_.slbs),
    )
    if any(v for _, _, v in os_items):
        section("Income from other sources"); row += 1
        first = row
        for label, key, value in os_items:
            if value:
                item(label, f"='OtherSources'!{os_layout[key].coordinate}")
                row += 1
        close_section(first)

    row += 1
    # Gross Total Income -- ON-PAGE, summed from this page's own section
    # subtotal cells (2026-07-20 on-page-totals change). Previously this
    # mirrored a hidden Computation-sheet total; now it IS the total, so an
    # override to any leaf item flows through automatically.
    # Guard against the (theoretical) case of every head-of-income section
    # being zero and therefore unrendered -- "=SUM()" is not a valid Excel
    # formula, so fall back to a literal 0 range rather than an empty SUM().
    gti_formula = f"=SUM({','.join(subtotal_cells)})" if subtotal_cells else "=0"
    line("Total", OUTER, gti_formula, bold=True, rule=_TOP_RULE)
    gti_row = row
    gti_ref = f"{get_column_letter(OUTER)}{gti_row}"
    row += 1

    # Chapter VI-A deductions -- a leaf reference to the Deductions sheet
    # (individual items still come from the source schedule; only the total
    # is aggregated here).
    line("Less - Chapter VI-A deductions", SUB, f"='Deductions'!{ded_layout['total']}")
    via_row = row
    via_ref = f"{get_column_letter(SUB)}{via_row}"
    row += 1

    # Brought-forward losses set off -- REAL, editable input cell defaulting
    # to 0 (was PARKED through 2026-07-19; see module docstring and
    # docs/2026-07-20-itr-onpage-totals-plan.md section 6). Reduces the
    # NORMAL-income base first -- a documented default, not a full head-wise
    # set-off engine (out of scope for v1).
    ws.cell(row=row, column=LBL,
            value="Less - Brought forward losses set off (editable; reduces normal income first)"
            ).font = _font()
    _input_cell(ws, row, SUB, default_value=0)
    bf_row = row
    bf_ref = f"{get_column_letter(SUB)}{bf_row}"
    row += 1

    # s.288A rounding (nearest, from Rules) is applied here, exactly as the
    # pre-change Computation-sheet formula did (MROUND(ti_raw, round_ti_nearest))
    # -- dropping it would silently break paisa-exact parity with today's
    # workbook for the default (no-override) case.
    round_ti_ref = f"'Rules'!{rules_layout['round_ti_nearest'].coordinate}"
    line("Total Income", OUTER, f"=MROUND({gti_ref}-{via_ref}-{bf_ref},{round_ti_ref})",
         bold=True, rule=_TOP_RULE)
    ti_row = row
    ti_ref = f"{get_column_letter(OUTER)}{ti_row}"
    row += 1

    # Special-rate CG base and normal-income base -- the correctness-trap
    # carve-out (design doc section 5): Total Income (above) INCLUDES
    # special-rate CG (112A/111A LTCG/STCG); the slab-tax BASE that
    # `Computation` reads must exclude it. cg_lt/cg_st are always present in
    # comp_layout (the leaf cells are written unconditionally), so this
    # reads safely even when the CG section is not rendered on-page (both
    # leaves are 0 in that case).
    line("  (of which) Special-rate Capital Gains", SUB,
         f"={comp_layout['cg_lt']}+{comp_layout['cg_st']}")
    special_cg_row = row
    special_cg_ref = f"{get_column_letter(SUB)}{special_cg_row}"
    row += 1
    line("  (of which) Normal income (slab-tax base)", SUB,
         f"=MAX(0,{ti_ref}-{special_cg_ref})")
    normal_income_row = row
    normal_income_ref = f"{get_column_letter(SUB)}{normal_income_row}"
    row += 2

    # Re-anchor point: hand the page's own coordinates to `Computation` so
    # its slab/rebate/surcharge/cess formulas read THIS page instead of
    # recomputing GTI/TI from source leaves (design doc section 7 step 2).
    page_layout = {
        "gti": gti_ref,
        "total_income": ti_ref,
        "normal_income_base": normal_income_ref,
        "special_cg_base": special_cg_ref,
    }
    tail_layout = computation_tail_fn(page_layout)
    tail = lambda key: f"={tail_layout[key]}"  # noqa: E731

    line("Tax on total income", SUB,
         sel(tail_layout["new_tax_before_cess"], tail_layout["old_tax_before_cess"]))
    row += 1
    line("Add - Health & Education Cess", SUB,
         sel(tail_layout["new_cess"], tail_layout["old_cess"]))
    row += 1
    line("Tax with cess", OUTER, tail("selected_liability"), bold=True, rule=_TOP_RULE)
    row += 2

    tp = model.taxes_paid
    prepaid = (
        ("TDS on salary", "tds_salary", tp.tds_salary),
        ("TDS on interest", "tds_interest", tp.tds_interest),
        ("TDS on dividend", "tds_dividend", tp.tds_dividend),
        ("TCS", "tcs", tp.tcs),
        ("Advance Tax", "advance", tp.advance_tax),
        ("Self-assessment Tax", "sat", tp.self_assessment_tax),
    )
    section("Less - Prepaid Taxes"); row += 1
    first = row
    for label, key, value in prepaid:
        if value:
            item(label, f"='TaxesPaid'!{tp_layout[key].coordinate}")
            row += 1
    if row == first:                      # nothing prepaid -- still show the head
        item("Prepaid taxes", f"='TaxesPaid'!{tp_layout['total']}")
        row += 1
    close_section(first)

    line("Total prepaid taxes", OUTER, f"='TaxesPaid'!{tp_layout['total']}", bold=True)
    row += 1
    line("Refund Due / (Tax Payable)", OUTER, tail("refund"), bold=True, rule=_TOP_RULE)
    row += 3

    # Assumptions note only renders while residency is DEFAULTED -- a reader
    # must be able to tell "someone asserted this" from "the tool fell back"
    # (2026-07-19 residency prompt, section 2). Dropped entirely once the
    # entity declares R/OR / RNOR / NR.
    if not residency_declared:
        ws.cell(row=row, column=1, value="Assumptions").font = _font(bold=True, underline="single")
        row += 1
        note = ws.cell(row=row, column=1, value=_ASSUMPTION_NOTE)
        note.font = _font(8, italic=True)
        note.alignment = Alignment(wrap_text=True, vertical="top")
        ws.merge_cells(start_row=row, start_column=1, end_row=row + 1, end_column=OUTER)
        row += 2

    apply_sheet_chrome(
        ws,
        {"A": 3.5, "B": fit_label_width(ws, LBL, 46), "C": 15, "D": 15, "E": 16, "F": 12},
        last_row=row, last_col=OUTER,
        freeze=f"A{header_end + 1}", print_title=print_title,
    )
    return ws


# ---------------------------------------------------------------------------
# Sheets 2 and 3 -- IS and BS
# ---------------------------------------------------------------------------

def _write_hierarchy_sheet(wb, sheet_name: str, title_formula: str, entries,
                           source_sheet: str, label_width: float, print_title: str, *,
                           extra_row_fn=None):
    """`extra_row_fn(ws, next_row, tops, last_col) -> new_next_row`, if given,
    runs after the hierarchy body and before sheet chrome is applied -- e.g.
    `PL for Business`'s net-income row, which is not part of the generic
    hierarchy shape but still needs `last_row`/widths sized to include it."""
    ws = wb.create_sheet(sheet_name)
    t = ws.cell(row=1, column=1, value=title_formula)
    t.font = _font(12, bold=True)
    root = build_hierarchy(entries)
    depth = max_group_level(root)
    last_col = max(2 + depth + 1, 3)
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=last_col)
    t.alignment = Alignment(horizontal="center")

    for col in range(2, last_col + 1):
        c = ws.cell(row=3, column=col, value="Rs.")
        c.font = _font(bold=True)
        c.alignment = Alignment(horizontal="center")
        c.border = _BOTTOM_RULE

    next_row, tops = render_hierarchy(ws, 4, root, source_sheet, label_col=1, money_col=2)
    if extra_row_fn is not None:
        next_row = extra_row_fn(ws, next_row, tops, last_col)

    widths = {"A": fit_label_width(ws, 1, label_width)}
    for col in range(2, last_col + 1):
        widths[get_column_letter(col)] = 15
    apply_sheet_chrome(ws, widths, last_row=next_row, last_col=last_col,
                       freeze="A4", print_title=print_title)
    return ws


def write_is_sheet(wb, entries, entity_layout: dict, period_text: str, print_title: str):
    """`IS` -- clustered as closely to GnuCash as the transcript's own `Path`
    column allows. `IS` is naturally shallow; it is not forced to look
    symmetrical with `BS`."""
    title = (f"='Entity'!{entity_layout['name'].coordinate}"
             f"&\" Income Statement For Period Covering {period_text}\"")
    return _write_hierarchy_sheet(wb, "IS", title, entries, "IS_Transcript", 38.1, print_title)


def write_bs_sheet(wb, entries, entity_layout: dict, as_at_text: str, print_title: str):
    """`BS` -- a GnuCash view. Every intermediate group keeps its own row and
    its own subtotal; sibling groups are never merged. In particular
    `Fixed Deposits` stays a sibling of `Cash and Bank` -- Schedule AL's
    statutory buckets do combine them, but that is a different sheet with a
    different purpose and does not license combining them here."""
    title = (f"='Entity'!{entity_layout['name'].coordinate}"
             f"&\" Balance Sheet as at {as_at_text}\"")
    return _write_hierarchy_sheet(wb, "BS", title, entries, "BS_Transcript", 47.8, print_title)


# ---------------------------------------------------------------------------
# PL for Business -- nets one entity's business income against its expenses
# ---------------------------------------------------------------------------

class BusinessSubtreeError(Exception):
    """Raised when an entity's `business_subtree` config (Data/itr/entities.yaml)
    is set but this FY's `IS` entries contain nothing under it.

    A configured `business_subtree` is a structural fact about that entity's
    GnuCash chart of accounts, not a per-year on/off signal -- once set, it is
    expected to keep matching every year. A zero-match year is therefore far
    more likely a GnuCash rename or a typo in the config than a genuine
    zero-business year, and gate 4 (2026-07-19 PL for Business prompt) forbids
    treating the two the same way: silently rendering an empty/omitted sheet
    here would hide exactly the failure this check exists to catch.
    """


def resolve_business_entries(is_entries, business_subtree: str | None):
    """Filter `is_entries` -- the same `[(path, cell), ...]` list `write_is_sheet`
    already consumes -- down to the leaves under `business_subtree` (a GnuCash
    account path prefix, e.g. `"Income/xBusiness Income"`), via a plain
    path-prefix subtree walk. Never a keyword/name match: an out-of-subtree
    account that merely *sounds* business-related (e.g. `Expense/Professional
    Tax`) must never leak in just because its name contains "business"-ish
    words.

    Returns None when `business_subtree` is not configured for this entity --
    the ordinary, per-FY "this entity has no business" case, evaluated fresh
    from config every run (never an entity-level flag baked in elsewhere,
    never cross-year-persisted, never inferred from a prior year or from
    whether a CA reference workbook happened to include the sheet). The
    `PL for Business` sheet is simply omitted; no error.

    Raises BusinessSubtreeError when `business_subtree` IS configured but this
    FY's `is_entries` matches nothing under it (see BusinessSubtreeError).
    """
    if not business_subtree:
        return None
    prefix = business_subtree.rstrip("/") + "/"
    matches = [(path, cell) for path, cell in is_entries if str(path).startswith(prefix)]
    if not matches:
        raise BusinessSubtreeError(
            f"business_subtree {business_subtree!r} is configured for this entity "
            f"but no IS entries under it were found for this FY -- check for a "
            f"GnuCash rename before assuming a zero-business year"
        )
    return matches


def write_pl_for_business_sheet(wb, entries, entity_layout: dict, period_text: str,
                                print_title: str):
    """`PL for Business` -- nets one entity's business income against its
    business expenses via a plain subtree walk (see `resolve_business_entries`).
    Reuses `_write_hierarchy_sheet` exactly like `IS`/`BS`: the income leaf and
    the nested expense group render as an ordinary hierarchy, sourced from
    `IS_Transcript` since `entries` is a filtered slice of the same
    `is_entries` list `IS` renders from. Expense leaves already carry the
    book's negative sign (HTML convention), so the net row is a plain SUM/`+`
    of the top-level cells -- never restated as a subtraction.
    """
    title = (f"='Entity'!{entity_layout['name'].coordinate}"
             f"&\" Profit and Loss for Business For Period Covering {period_text}\"")

    def net_row(ws, row, tops, last_col):
        label = ws.cell(row=row, column=1, value="Net Business Income / (Loss)")
        label.font = _font(bold=True)
        formula = ("=" + "+".join(f"{get_column_letter(c)}{r}" for r, c in tops)) if tops else "=0"
        cell = ws.cell(row=row, column=last_col, value=formula)
        cell.number_format = INR_FORMAT
        cell.font = _font(bold=True)
        cell.border = _TOP_RULE
        return row + 1

    return _write_hierarchy_sheet(wb, "PL for Business", title, entries, "IS_Transcript",
                                  38.1, print_title, extra_row_fn=net_row)


# ---------------------------------------------------------------------------
# Sheet 4 -- CG
# ---------------------------------------------------------------------------

_CG_HEADERS = [
    ("Sr.", "No.", ""),
    ("", "Name", ""),
    ("No. of", "Shares /", "Units"),
    ("Date of", "Purchase", ""),
    ("Cost", "Price", ""),
    ("Valuation", "as of", "31-01-2018"),
    ("Date of", "Sale", ""),
    ("Selling", "Price", ""),
    ("Capital", "Gain /", "(Loss)"),
    ("Taxable", "Capital Gain", "/ (Loss)"),
]

#: CapitalGains 'Lot detail' column letters -- this sheet is a VIEW over that
#: sheet, never a reimplementation. The CA reference hardcodes 31-Jan-2018 FMV
#: prices as literals inside each row's formula and its grandfathering
#: arithmetic is inconsistent between rows (K9 = I9-MAX(F9,G9) but
#: K10 = J10-G10, which subtracts FMV in addition to cost). Neither trait is
#: copied: every figure below is a direct reference to the CapitalGains sheet,
#: which already applies grandfathering consistently via the real fmv_tables
#: lookups.
_CG_SRC = {
    "scrip": "A", "sale_date": "B", "buy_date": "C", "term": "D", "qty": "E",
    "cost": "F", "proceeds": "G", "booked_gain": "H", "fmv_used": "J",
    "taxable_gain": "K",
}


def _cg_block(ws, row: int, banner: str, lot_rows, lot_start_row: int, term: str) -> int:
    b = ws.cell(row=row, column=1, value=banner)
    b.font = _font(11, bold=True)
    b.alignment = Alignment(horizontal="center")
    ws.merge_cells(start_row=row, start_column=1, end_row=row, end_column=len(_CG_HEADERS))
    row += 1

    header_top = row
    for col, parts in enumerate(_CG_HEADERS, start=1):
        for offset, word in enumerate(parts):
            c = ws.cell(row=header_top + offset, column=col, value=word or None)
            c.font = _font(bold=True)
            c.alignment = Alignment(horizontal="center", wrap_text=True)
    for col in range(1, len(_CG_HEADERS) + 1):
        ws.cell(row=header_top + 2, column=col).border = _BOTTOM_RULE
    row = header_top + 3

    first_data_row = row
    sr = 0
    for offset, lot in enumerate(lot_rows):
        if lot.term != term:
            continue
        sr += 1
        src = lot_start_row + offset

        def ref(key: str) -> str:
            return f"'CapitalGains'!{_CG_SRC[key]}{src}"

        # Sr. No. is a formula too, so "no numeric literal on this sheet"
        # stays a clean, assertable invariant.
        ws.cell(row=row, column=1, value=f"=ROW()-{first_data_row - 1}").font = _font()
        ws.cell(row=row, column=2, value=f"={ref('scrip')}").font = _font()
        c = ws.cell(row=row, column=3, value=f"={ref('qty')}")
        c.number_format = "#,##0.###"
        for col, key in ((4, "buy_date"), (7, "sale_date")):
            d = ws.cell(row=row, column=col,
                        value=f"=IF({ref(key)}=\"\",\"\",DATEVALUE({ref(key)}))")
            d.number_format = DATE_FORMAT
            d.font = _font()
            d.alignment = Alignment(horizontal="center")
        for col, key in ((5, "cost"), (8, "proceeds"), (9, "booked_gain"), (10, "taxable_gain")):
            m = ws.cell(row=row, column=col, value=f"={ref(key)}")
            m.number_format = INR_FORMAT
            m.font = _font()
        # FMV is READ from CapitalGains (which resolved it from fmv_tables),
        # never restated as a literal price in this formula.
        f = ws.cell(row=row, column=6, value=f"=IF({ref('fmv_used')}=\"\",\"\",{ref('fmv_used')})")
        f.number_format = INR_FORMAT
        f.font = _font()
        row += 1

    if sr == 0:
        ws.cell(row=row, column=2, value="(none)").font = _font(italic=True)
        row += 1

    ws.cell(row=row, column=2, value="SUM").font = _font(bold=True)
    for col in (5, 8, 9, 10):
        letter = get_column_letter(col)
        t = ws.cell(row=row, column=col, value=f"=SUM({letter}{first_data_row}:{letter}{row - 1})")
        t.number_format = INR_FORMAT
        t.font = _font(bold=True)
        t.border = _TOP_RULE
    return row + 2


def has_capital_gains_activity(cg_schedule) -> bool:
    """Does the financial year being generated have any capital-gains activity?

    Evaluated PER RUN, from this year's own data -- lots/disposals, book
    control totals, or taxable figures. Deliberately NOT an entity-level flag,
    config toggle, or anything else that persists one year's answer into the
    next: an entity with no gains this year may well have a disposal next
    year, and a cached "this entity doesn't do CG" would silently drop a real
    CG sheet the year it finally matters. It is also never inferred from a
    prior year's output or from whether a CA's reference workbook happened to
    include the sheet.
    """
    return bool(cg_schedule.lot_rows) or any((
        cg_schedule.lt_control, cg_schedule.st_control,
        cg_schedule.lt_taxable_gross, cg_schedule.st_taxable_gross,
    ))


def write_cg_sheet(wb, cg_schedule, entity_layout: dict, lot_start_row: int, print_title: str):
    ws = wb.create_sheet("CG")
    ncols = len(_CG_HEADERS)

    row = 1
    row = _write_cg_error_banner(ws, row, cg_schedule, ncols)
    title_row = row
    t = ws.cell(row=title_row, column=1, value=f"='Entity'!{entity_layout['name'].coordinate}")
    t.font = _font(12, bold=True)
    t.alignment = Alignment(horizontal="center")
    ws.merge_cells(start_row=title_row, start_column=1, end_row=title_row, end_column=ncols)

    row = title_row + 2
    row = _cg_block(
        ws, row,
        "Details of Long Term Capital Gain / (Loss) on Shares and MF during the year",
        cg_schedule.lot_rows, lot_start_row, "LT",
    )
    row = _cg_block(
        ws, row,
        "Details of Short Term Capital Gain / (Loss) on Shares and MF during the year",
        cg_schedule.lot_rows, lot_start_row, "ST",
    )

    # The Name column holds formulas, so its rendered width can't be measured
    # from the sheet -- size it from the source scrip names instead.
    name_width = max([30.0] + [len(str(r.scrip)) + 2 for r in cg_schedule.lot_rows])
    widths = {"A": 5, "B": min(name_width, 90.0), "C": 12, "D": 13, "E": 14, "F": 13,
              "G": 13, "H": 14, "I": 15, "J": 16}
    apply_sheet_chrome(ws, widths, last_row=row, last_col=ncols,
                       landscape=True, print_title=print_title)
    return ws


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def hide_working_sheets(wb) -> None:
    """Hidden, not gone -- the audit trail stays in the file."""
    for name in HIDDEN_SHEETS:
        if name in wb.sheetnames:
            wb[name].sheet_state = "hidden"


def move_presentation_sheets_first(wb) -> None:
    front = [wb[n] for n in PRESENTATION_SHEETS if n in wb.sheetnames]
    rest = [ws for ws in wb._sheets if ws not in front]
    wb._sheets = front + rest


def build_presentation_layer(wb, model, entity_layout: dict, comp_layout: dict,
                             os_layout: dict, tp_layout: dict, ded_layout: dict,
                             rules_layout: dict,
                             is_entries, bs_entries,
                             lot_start_row: int, age_class: str, year_key: str,
                             year_label: str, computation_tail_fn, *,
                             father_name: str | None = None,
                             aadhaar: str | None = None, residency_value: str = "R/OR",
                             residency_declared: bool = False,
                             business_subtree: str | None = None) -> None:
    """Add the four deliverable sheets in front of the calculation sheets and
    hide the raw working sheets. Adds only -- restyles and overwrites nothing.

    `comp_layout` is the LEAF-only Computation layout (2026-07-20 on-page-totals
    change); `computation_tail_fn` is called from inside `write_statement_of_income`
    once the page's own ladder coordinates exist, to write the re-anchored
    slab-tax machinery. `rules_layout` is needed on-page now too, for the
    s.288A Total Income rounding (MROUND) that used to live on `Computation`.
    See `write_statement_of_income`'s docstring."""
    start_year = int(year_key[:4]) if year_key else 0
    period_text = f"01-04-{start_year} to 31-03-{start_year + 1}"
    as_at_text = f"31-03-{start_year + 1}"
    print_title = f"&\"{FONT_NAME},Bold\"&A  --  {year_label}"

    write_statement_of_income(
        wb, model, entity_layout, comp_layout, os_layout, tp_layout, ded_layout,
        rules_layout, age_class, period_text, print_title, computation_tail_fn,
        father_name=father_name, aadhaar=aadhaar,
        residency_value=residency_value, residency_declared=residency_declared,
    )
    write_is_sheet(wb, is_entries, entity_layout, period_text, print_title)
    write_bs_sheet(wb, bs_entries, entity_layout, as_at_text, print_title)
    # PL for Business is omitted entirely when this entity has no
    # business_subtree configured (the ordinary case for every entity except
    # Harshal's) -- see resolve_business_entries. When configured but this
    # FY's data matches nothing under it, resolve_business_entries raises
    # rather than silently rendering a zero/omitted sheet.
    business_entries = resolve_business_entries(is_entries, business_subtree)
    if business_entries is not None:
        write_pl_for_business_sheet(wb, business_entries, entity_layout, period_text, print_title)
    # CG is omitted entirely when this FY has no capital-gains activity --
    # mirroring what the CA actually produced for such a year. A blank grid on
    # a document handed to a CA or a bank is worse than no sheet.
    if has_capital_gains_activity(model.capital_gains):
        write_cg_sheet(wb, model.capital_gains, entity_layout, lot_start_row, print_title)

    hide_working_sheets(wb)
    move_presentation_sheets_first(wb)
