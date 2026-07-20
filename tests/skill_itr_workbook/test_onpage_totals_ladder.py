"""
tests/skill_itr_workbook/test_onpage_totals_ladder.py -- regression coverage
for the 2026-07-20 on-page-totals change (docs/2026-07-20-itr-onpage-totals-plan.md).

Three things this file exists to prove, none of them covered elsewhere:

1. FORMULA-STRING WIRING -- the `Statement of Income` page's income ladder
   (Gross Total Income, Chapter VI-A, b/f-loss, Total Income, the
   normal-income/special-rate-CG split) is built from on-page cells, and
   `Computation`'s re-anchored slab-tax formula reads the PAGE's
   normal-income-base cell, not a locally recomputed figure.
2. PYTHON SHADOW-CALC RECONCILIATION -- for the DEFAULT (no b/f override)
   case, the arithmetic the new on-page formula graph implies must equal
   `schedules.py`'s own (untouched) tax engine output to the paisa -- that
   engine is "today's" ground truth, unaffected by this presentation-layer
   change. A synthetic b/f-loss override is also hand-simulated and checked
   against `schedules.compute_tax` (the same pure-Python slab/rebate/
   surcharge/cess implementation `Computation`'s Excel formulas mirror).
3. THE CORRECTNESS TRAP (design doc section 5) -- special-rate capital gains
   (111A/112A) must never enter the slab-tax base. A dedicated regression
   proves the on-page normal-income-base formula excludes special-rate CG,
   that `Computation`'s special-rate-CG tax cell never references the page
   at all (independent of any override), and that naively including CG in
   the slab base would change the numbers -- i.e. the carve-out is not a
   no-op.

openpyxl never evaluates formulas, so (1) is proven via formula-string
assertions and (2)/(3) via a Python re-derivation compared against
`schedules.py`'s own engine -- the same approach used throughout this test
suite (see test_write_workbook.py's module docstring).

Fixtures: reuses the existing SYN-IND synthetic fixture set already used by
test_write_workbook.py and test_presentation.py (Data/itr/entities.example.yaml
+ tests/skill_itr_workbook/fixtures/syn_ind.*) -- fabricated identity/financial
data, no real PII, PAN, account numbers or amounts anywhere in this file.
"""
from __future__ import annotations

import re
import sys
from datetime import date
from pathlib import Path

import openpyxl
import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULES_DIR = ROOT / "Data" / "itr" / "rules"

for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import parse_eguile as pe  # noqa: E402
import parse_gnucash as pg  # noqa: E402
import parse_form16  # noqa: E402
import configs  # noqa: E402
import mapping as mapping_engine  # noqa: E402
import rules as rules_engine  # noqa: E402
import schedules as sch  # noqa: E402
import write_workbook as ww  # noqa: E402
import fixture_gen  # noqa: E402

YEAR_KEY = "2024-25"
REGIME = "new"


@pytest.fixture(scope="module")
def built():
    """Same construction as test_write_workbook.py's `built_workbook` --
    the SYN-IND synthetic fixture, which conveniently has BOTH normal income
    (salary) and special-rate capital gains (LT loss + ST gain), making it
    the right fixture for the correctness-trap regression without inventing
    a second one."""
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    book = pg.parse_book(FIXTURES / "syn_ind.gnucash")
    loaded = configs.load_mapping(FIXTURES / "syn_ind.mapping.yaml")
    result = mapping_engine.resolve_tree(tree, loaded)
    form16 = parse_form16.parse_form16(FIXTURES / "syn_ind_form16.pdf")
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    entities = configs.load_entities(ROOT / "Data" / "itr" / "entities.example.yaml")
    entity = entities["SYN-IND"]
    scrips = configs.load_scrips(ROOT / "Data" / "itr" / "scrips.example.yaml")
    fmv_tables = sch.load_fmv_tables()
    user_rules = rules_engine.load_user_rules(RULES_DIR / "user_rules.yaml")

    model = sch.build_all_schedules(
        tree, result.resolved, book, form16, YEAR_KEY, rules, REGIME,
        entity.status, entity.dob, scrips, fmv_tables,
    )
    return model, rules, entity


@pytest.fixture(scope="module")
def built_workbook(tmp_path_factory, built):
    model, rules, entity = built
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    book = pg.parse_book(FIXTURES / "syn_ind.gnucash")
    loaded = configs.load_mapping(FIXTURES / "syn_ind.mapping.yaml")
    result = mapping_engine.resolve_tree(tree, loaded)
    form16 = parse_form16.parse_form16(FIXTURES / "syn_ind_form16.pdf")
    user_rules = rules_engine.load_user_rules(RULES_DIR / "user_rules.yaml")
    out_path = tmp_path_factory.mktemp("wb") / "syn_ind_ladder.xlsx"
    ww.write_workbook(
        str(out_path), tree, model, rules, user_rules, entity, REGIME, YEAR_KEY,
        form16.opted_out_115bac, [], [], [], result.unmapped, "v1", "2026-01-01T00:00:00", {},
        result.resolved, loaded.entries,
    )
    return openpyxl.load_workbook(str(out_path))


# ---------------------------------------------------------------------------
# Helpers -- exact-label cell lookup (avoids the substring ambiguity of
# scanning for e.g. "Total" which also matches "Total prepaid taxes").
# ---------------------------------------------------------------------------

def _find_exact(ws, label: str, col: int | None = None):
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and c.value == label:
                if col is None or c.column == col:
                    return c
    raise AssertionError(f"exact label {label!r} not found (col={col})")


def _find_containing(ws, needle: str, col: int | None = None):
    for row in ws.iter_rows():
        for c in row:
            if isinstance(c.value, str) and needle in c.value:
                if col is None or c.column == col:
                    return c
    raise AssertionError(f"label containing {needle!r} not found (col={col})")


LBL, INNER, SUB, OUTER = 2, 3, 4, 5


# ---------------------------------------------------------------------------
# 1. Formula-string wiring
# ---------------------------------------------------------------------------

def test_gti_is_an_on_page_sum_formula(built_workbook):
    """Gross Total Income ("Total" row, right after the income-head sections)
    must be a live =SUM(...) over on-page subtotal cells -- not a mirror of a
    hidden Computation-sheet cell (the pre-2026-07-20 behaviour)."""
    ws = built_workbook["Statement of Income"]
    gti_cell = _find_exact(ws, "Total", col=LBL)
    gti_value = ws.cell(row=gti_cell.row, column=OUTER).value
    assert isinstance(gti_value, str)
    assert re.fullmatch(r"=SUM\((D\d+,?)+\)", gti_value), \
        f"GTI formula not an on-page SUM over column D subtotal cells: {gti_value!r}"
    assert "Computation" not in gti_value, \
        "GTI must no longer mirror the Computation sheet"


def test_chapter_via_is_a_leaf_reference_to_deductions_sheet(built_workbook):
    ws = built_workbook["Statement of Income"]
    via_cell = _find_exact(ws, "Less - Chapter VI-A deductions", col=LBL)
    via_value = ws.cell(row=via_cell.row, column=SUB).value
    assert via_value.startswith("='Deductions'!")


def test_bf_loss_cell_is_input_and_total_income_is_gti_minus_via_minus_bf(built_workbook):
    ws = built_workbook["Statement of Income"]
    gti_cell = _find_exact(ws, "Total", col=LBL)
    gti_ref = f"{ws.cell(row=gti_cell.row, column=OUTER).coordinate}"

    via_cell = _find_exact(ws, "Less - Chapter VI-A deductions", col=LBL)
    via_ref = ws.cell(row=via_cell.row, column=SUB).coordinate

    bf_label = _find_containing(ws, "Brought forward losses set off", col=LBL)
    bf_cell = ws.cell(row=bf_label.row, column=SUB)
    assert bf_cell.value == 0, "b/f-loss input cell must default to literal 0"
    bf_ref = bf_cell.coordinate

    ti_cell = _find_exact(ws, "Total Income", col=LBL)
    ti_formula = ws.cell(row=ti_cell.row, column=OUTER).value
    assert isinstance(ti_formula, str) and ti_formula.startswith("=MROUND(")
    assert gti_ref in ti_formula, "Total Income must reference the GTI cell"
    assert via_ref in ti_formula, "Total Income must reference the Chapter VI-A cell"
    assert bf_ref in ti_formula, "Total Income must reference the b/f-loss input cell"
    assert "'Rules'!" in ti_formula, "Total Income must keep the s.288A MROUND rounding"


def test_normal_income_base_excludes_special_rate_cg_on_page(built_workbook):
    """THE correctness-trap wiring check (design doc section 5): the on-page
    normal-income-base formula must be MAX(0, TotalIncome - SpecialRateCG),
    i.e. it structurally subtracts CG rather than including it."""
    ws = built_workbook["Statement of Income"]
    ti_cell = _find_exact(ws, "Total Income", col=LBL)
    ti_ref = ws.cell(row=ti_cell.row, column=OUTER).coordinate

    cg_base_label = _find_containing(ws, "Special-rate Capital Gains", col=LBL)
    cg_base_ref = ws.cell(row=cg_base_label.row, column=SUB).coordinate

    normal_label = _find_containing(ws, "Normal income (slab-tax base)", col=LBL)
    normal_formula = ws.cell(row=normal_label.row, column=SUB).value
    assert normal_formula == f"=MAX(0,{ti_ref}-{cg_base_ref})"


def test_computation_slab_base_reads_the_pages_normal_income_cell(built_workbook):
    """The re-anchor point: `Computation`'s "Tax on normal-rate income" cell
    (both regimes) must reference `'Statement of Income'!<normal_income_base>`
    -- not a locally recomputed GTI/TI. Proven for at least one regime block;
    both are built by the same `_regime_block` closure in write_workbook.py."""
    stmt = built_workbook["Statement of Income"]
    normal_label = _find_containing(stmt, "Normal income (slab-tax base)", col=LBL)
    normal_ref = stmt.cell(row=normal_label.row, column=SUB).coordinate
    expected_ref = f"'Statement of Income'!{normal_ref}"

    comp = built_workbook["Computation"]
    tax_normal_label = _find_containing(comp, "Tax on normal-rate income", col=1)
    tax_normal_formula = comp.cell(row=tax_normal_label.row, column=2).value
    assert expected_ref in tax_normal_formula, (
        f"Computation slab-tax formula does not reference the page's normal-income "
        f"cell {expected_ref!r}: {tax_normal_formula!r}"
    )


def test_special_rate_cg_tax_cell_never_references_the_page(built_workbook):
    """The other half of the correctness trap: 112A/111A tax on special-rate
    CG must stay wired to `CapitalGains` only -- an on-page override (b/f
    loss, VI-A, any leaf) must never change how special-rate CG itself is
    taxed."""
    comp = built_workbook["Computation"]
    for label_cell in comp.iter_rows():
        for c in label_cell:
            if c.value == "112A/111A tax on special-rate CG":
                formula = comp.cell(row=c.row, column=2).value
                assert formula.startswith("='CapitalGains'!")
                assert "Statement of Income" not in formula
                return
    raise AssertionError("112A/111A tax on special-rate CG cell not found")


# ---------------------------------------------------------------------------
# 2. Python shadow-calc reconciliation (default, no override)
# ---------------------------------------------------------------------------

def test_default_case_ladder_matches_schedules_engine_to_the_paisa(built):
    """The strongest regression guarantee (design doc section 7, point 3):
    for the DEFAULT (b/f=0) case, the on-page ladder's implied arithmetic
    must equal `schedules.py`'s own (untouched) tax-engine output exactly.

    schedules.build_computation (unaffected by this change) already computes
    gti, via, total_income, and TaxBlock.normal_income/special_rate_income
    from the identical component leaves the page now sums on-page. This test
    re-derives GTI/TI/normal-base/special-CG-base the same way the new
    on-page formulas do, using the model's own leaf numbers, and checks the
    result matches the engine's ground truth -- i.e. moving the ladder
    on-page did not change what it computes."""
    model, rules, entity = built
    comp = model.computation

    # Re-derive the page's ladder the same way write_statement_of_income's
    # formulas do: GTI = SUM(section subtotals); special_cg_base = cg_lt + cg_st
    # (cg_lt already net of LT exemption, matching the leaf-cell formula);
    # normal_income_base = MAX(0, TotalIncome - special_cg_base).
    gti = (
        comp.salary_income + comp.house_property_income + comp.business_income
        + comp.other_sources_income + comp.capital_gains_lt + comp.capital_gains_st
    )
    assert gti == pytest.approx(comp.gti), "re-derived on-page GTI must match schedules.py's gti"

    total_income = sch.round_288a(gti - comp.via_deductions, rules.common["rounding"]["total_income"]["nearest"])
    assert total_income == pytest.approx(comp.total_income_rounded)

    special_cg_base = comp.capital_gains_lt + comp.capital_gains_st
    assert special_cg_base == pytest.approx(comp.tax_block.special_rate_income)

    normal_income_base = max(0.0, total_income - special_cg_base)
    assert normal_income_base == pytest.approx(comp.tax_block.normal_income), (
        "on-page normal-income-base (the slab-tax base Computation now reads) must match "
        "schedules.py's TaxBlock.normal_income exactly -- this is the paisa-exact default-"
        "case regression guarantee"
    )


def test_default_case_tax_liability_and_refund_match_schedules_engine(built):
    """Rounds out the previous test with the downstream figures a preparer
    actually reads: tax liability and refund, both computed by
    `schedules.compute_tax` (the same slab/rebate/surcharge/cess logic
    `Computation`'s re-anchored Excel formulas mirror) from the re-derived
    ladder, must equal the model's own numbers for the default b/f=0 case."""
    model, rules, entity = built
    comp = model.computation
    fy_end = date(int(YEAR_KEY[:4]) + 1, 3, 31)

    total_income = comp.total_income_rounded
    special_cg_base = comp.tax_block.special_rate_income
    normal_income_base = max(0.0, total_income - special_cg_base)

    shadow = sch.compute_tax(
        normal_income=normal_income_base,
        special_rate_tax=comp.tax_block.tax_on_special_rate_income,
        special_rate_income_amount=special_cg_base,
        rules=rules, regime=REGIME, status=entity.status, dob=entity.dob,
        fy_end=fy_end, residency=entity.residency,
    )
    assert shadow.tax_liability == pytest.approx(comp.tax_block.tax_liability)
    refund_shadow = sch.round_288b(
        comp.taxes_paid - shadow.tax_liability, rules.common["rounding"]["tax_payable_refund"]["nearest"],
    )
    assert refund_shadow == pytest.approx(comp.refund_or_payable)


def test_bf_loss_override_reduces_normal_income_and_total_income(built):
    """Simulates a preparer typing a b/f-loss amount into the (now real,
    editable) input cell: verifies the override propagates through Total
    Income and the slab base, and that tax liability moves accordingly --
    proving the wiring the whole change exists to deliver (design doc
    section 1: "even if manual override is done ... other totals come from
    underlying sheets" must no longer be true after this change)."""
    model, rules, entity = built
    comp = model.computation
    fy_end = date(int(YEAR_KEY[:4]) + 1, 3, 31)
    nearest = rules.common["rounding"]["total_income"]["nearest"]

    bf_amount = 50000.0  # synthetic override amount, not from any real return
    gti = comp.gti
    total_income_default = sch.round_288a(gti - comp.via_deductions, nearest)
    total_income_override = sch.round_288a(gti - comp.via_deductions - bf_amount, nearest)
    assert total_income_default - total_income_override == pytest.approx(bf_amount, abs=nearest)

    special_cg_base = comp.tax_block.special_rate_income
    normal_default = max(0.0, total_income_default - special_cg_base)
    normal_override = max(0.0, total_income_override - special_cg_base)
    # b/f reduces normal income first (design doc section 6's documented default).
    assert normal_default - normal_override == pytest.approx(bf_amount, abs=nearest)

    tax_default = sch.compute_tax(
        normal_income=normal_default, special_rate_tax=comp.tax_block.tax_on_special_rate_income,
        special_rate_income_amount=special_cg_base, rules=rules, regime=REGIME,
        status=entity.status, dob=entity.dob, fy_end=fy_end, residency=entity.residency,
    )
    tax_override = sch.compute_tax(
        normal_income=normal_override, special_rate_tax=comp.tax_block.tax_on_special_rate_income,
        special_rate_income_amount=special_cg_base, rules=rules, regime=REGIME,
        status=entity.status, dob=entity.dob, fy_end=fy_end, residency=entity.residency,
    )
    # Lower normal-income base can never increase tax liability (monotonic slabs).
    assert tax_override.tax_liability <= tax_default.tax_liability
    # And the special-rate CG tax component is completely unaffected by the override --
    # the carve-out means b/f loss never touches CG.
    assert tax_override.tax_on_special_rate_income == pytest.approx(tax_default.tax_on_special_rate_income)


# ---------------------------------------------------------------------------
# 3. The correctness-trap regression -- special-rate CG must stay carved out
# ---------------------------------------------------------------------------

def test_including_special_rate_cg_in_slab_base_would_change_the_tax_the_carve_out_is_not_a_no_op(built):
    """Guards against the single most dangerous possible regression: silently
    dropping the CG carve-out and taxing special-rate CG at slab rates (or
    vice versa). Demonstrates that the correct (carved-out) computation and
    an intentionally WRONG one (CG folded into the slab base, no separate
    special-rate tax) diverge for this fixture -- so if a future change
    accidentally merges them back together, a paisa-exact test WILL notice."""
    model, rules, entity = built
    comp = model.computation
    fy_end = date(int(YEAR_KEY[:4]) + 1, 3, 31)

    total_income = comp.total_income_rounded
    special_cg_base = comp.tax_block.special_rate_income
    correct_normal_base = max(0.0, total_income - special_cg_base)

    correct = sch.compute_tax(
        normal_income=correct_normal_base, special_rate_tax=comp.tax_block.tax_on_special_rate_income,
        special_rate_income_amount=special_cg_base, rules=rules, regime=REGIME,
        status=entity.status, dob=entity.dob, fy_end=fy_end, residency=entity.residency,
    )

    # WRONG: fold special-rate CG into the slab base and drop its separate
    # flat-rate tax (special_rate_tax=0), as if the carve-out never happened.
    wrong_normal_base = total_income  # CG never subtracted out
    wrong = sch.compute_tax(
        normal_income=wrong_normal_base, special_rate_tax=0.0,
        special_rate_income_amount=0.0, rules=rules, regime=REGIME,
        status=entity.status, dob=entity.dob, fy_end=fy_end, residency=entity.residency,
    )

    # SYN-IND has non-trivial special-rate CG (LT loss + ST gain net non-zero
    # once exemption is applied) -- the two computations must diverge, proving
    # the carve-out has teeth for this fixture rather than accidentally being
    # a no-op that would let a regression slip through unnoticed.
    #
    # NOTE: compare `tax_before_relief` rather than the final `tax_liability`.
    # SYN-IND's income is small enough that s.87A rebate floors BOTH the
    # correct and the wrong computation to a 0 final liability, which would
    # make the two scenarios look identical at the tax_liability line despite
    # genuinely different intermediate tax -- exactly the false negative this
    # regression must not have. tax_before_relief (pre-floor) still exposes
    # the divergence.
    assert special_cg_base != 0.0, "fixture must carry non-zero special-rate CG for this regression to mean anything"
    assert wrong.tax_before_relief != pytest.approx(correct.tax_before_relief), (
        "special-rate CG carve-out must not be a no-op for this fixture -- if wrong == correct, "
        "a regression that silently drops the carve-out would go undetected"
    )
    # And the correct computation must match what schedules.py itself
    # (unaffected by this presentation-layer change) actually produced.
    assert correct.tax_liability == pytest.approx(comp.tax_block.tax_liability)
    assert correct.tax_before_relief == pytest.approx(comp.tax_block.tax_before_relief)
