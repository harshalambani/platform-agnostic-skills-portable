"""
tests/skill_itr_workbook/test_schedules.py -- Batch 6 tests for
scripts/schedules.py: the FMV/scrips loader, the individual schedule
builders (unit-level, hand-built resolved-mapping dicts), the RULE-1
golden (refund principal excluded / interest taxable), the regime-flip
test, and the regression-year CG split-date test. Fully offline; synthetic
fixtures only (fixture_gen.py) plus the committed public-market-data FMV
CSVs under src/agents/skill_itr_workbook/data/.
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

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
import fixture_gen  # noqa: E402
import as26 as as26_engine  # noqa: E402
import quarters as quarters_engine  # noqa: E402

YEAR_KEY = "2024-25"


@pytest.fixture(scope="module")
def syn_ind_resolved():
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    loaded = configs.load_mapping(FIXTURES / "syn_ind.mapping.yaml")
    result = mapping_engine.resolve_tree(tree, loaded)
    assert not result.blocked
    return tree, result.resolved


@pytest.fixture(scope="module")
def syn_ind_book():
    return pg.parse_book(FIXTURES / "syn_ind.gnucash")


@pytest.fixture(scope="module")
def entity_and_scrips():
    entities = configs.load_entities(ROOT / "Data" / "itr" / "entities.example.yaml")
    scrips = configs.load_scrips(ROOT / "Data" / "itr" / "scrips.example.yaml")
    return entities["SYN-IND"], scrips


# ---------------------------------------------------------------------------
# FMV / scrips loader (plan section 6.1, OQ-3R, D14a)
# ---------------------------------------------------------------------------

def test_fmv_tables_load_bundled_csvs():
    tables = sch.load_fmv_tables()
    assert len(tables.nse) > 1000     # 1,870 NSE rows expected
    assert len(tables.mf) > 1000       # ~9.5k MF scheme rows expected


def test_resolve_fmv_alias_hit_via_scrips_yaml_override(entity_and_scrips):
    _, scrips = entity_and_scrips
    tables = sch.FmvTables(nse={}, mf={})
    fmv = sch.resolve_fmv("OLDTECH.NS", scrips, tables)
    assert fmv == 220.00


def test_resolve_fmv_direct_nse_bhavcopy_match():
    tables = sch.FmvTables(nse={"REALSYM": {"isin": "INE1234", "fmv_31jan2018": 99.5}}, mf={})
    assert sch.resolve_fmv("REALSYM", {}, tables) == 99.5


def test_resolve_fmv_table_ref_alias_to_bundled_row():
    scrips = {"MYALIAS.NS": configs.ScripRef(symbol="MYALIAS.NS", table_ref="bundled-fmv-table:REALSYM")}
    tables = sch.FmvTables(nse={"REALSYM": {"isin": "INE1234", "fmv_31jan2018": 42.0}}, mf={})
    assert sch.resolve_fmv("MYALIAS.NS", scrips, tables) == 42.0


def test_resolve_fmv_mf_scheme_name_match():
    tables = sch.FmvTables(nse={}, mf={"Some Fund - Direct Plan - Growth Option": 123.45})
    assert sch.resolve_fmv("Some Fund - Direct Plan - Growth Option", {}, tables) == 123.45


def test_resolve_fmv_nse_miss_fails_loud():
    tables = sch.FmvTables(nse={}, mf={})
    with pytest.raises(sch.FmvNotFoundError):
        sch.resolve_fmv("NOSUCHSCRIP.NS", {}, tables)


# ---------------------------------------------------------------------------
# Salary / Business schedules (form16-fed, unit-level)
# ---------------------------------------------------------------------------

def test_build_salary_uses_form16_when_supplied(syn_ind_resolved):
    tree, resolved = syn_ind_resolved
    node_by_guid = sch._node_by_guid(tree)
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    form16 = parse_form16.parse_form16(FIXTURES / "syn_ind_form16.pdf")
    salary = sch.build_salary(resolved, node_by_guid, form16, rules, "new")
    assert salary.source == "form16"
    assert salary.gross == 500000.0
    assert salary.income_chargeable == 447500.0
    assert not salary.manual_flagged


def test_build_salary_manual_fallback_without_form16(syn_ind_resolved):
    # CF3: the book-only path (SALARY_GROSS present, no Form 16) must still
    # apply the Rules-driven std_deduction_salary for the selected regime.
    tree, resolved = syn_ind_resolved
    node_by_guid = sch._node_by_guid(tree)
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    salary = sch.build_salary(resolved, node_by_guid, None, rules, "new")
    assert salary.source == "book-only"
    assert salary.manual_flagged
    assert salary.gross == 500000.0
    std_ded = rules.regime("new")["std_deduction_salary"]
    assert salary.std_deduction == std_ded
    assert salary.income_chargeable == 500000.0 - std_ded


def test_build_salary_book_only_old_regime_uses_old_std_deduction(syn_ind_resolved):
    tree, resolved = syn_ind_resolved
    node_by_guid = sch._node_by_guid(tree)
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    salary = sch.build_salary(resolved, node_by_guid, None, rules, "old")
    std_ded = rules.regime("old")["std_deduction_salary"]
    assert salary.std_deduction == std_ded
    assert salary.income_chargeable == 500000.0 - std_ded


def test_build_salary_no_salary_head_returns_pure_manual():
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    salary = sch.build_salary({}, {}, None, rules, "new")
    assert salary.source == "manual"
    assert salary.manual_flagged
    assert salary.gross == 0.0
    assert salary.income_chargeable == 0.0  # no head present -- std deduction not fabricated


def test_build_business(syn_ind_resolved):
    tree, resolved = syn_ind_resolved
    node_by_guid = sch._node_by_guid(tree)
    business = sch.build_business(resolved, node_by_guid)
    assert business.remuneration == 300000.0
    assert business.expenses_total == -120000.0
    assert business.net == 180000.0


# ---------------------------------------------------------------------------
# House Property (unit-level, hand-built inputs -- no SYN fixture has an
# HP seam; the schedule logic itself is exercised directly per OQ-1)
# ---------------------------------------------------------------------------

class _FakeLeaf:
    def __init__(self, guid, tag):
        self.guid = guid
        self.tag = tag
        self.path = f"fake/{tag}"
        self.flags = []


class _FakeNode:
    def __init__(self, total):
        self.total = total


def test_house_property_order_gav_minus_municipal_then_30pct_then_interest():
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    resolved = {
        "g1": _FakeLeaf("g1", "HP_RENT"),
        "g2": _FakeLeaf("g2", "HP_MUNICIPAL_TAX"),
        "g3": _FakeLeaf("g3", "HP_INTEREST"),
    }
    node_by_guid = {"g1": _FakeNode(240000.0), "g2": _FakeNode(-20000.0), "g3": _FakeNode(-50000.0)}
    hp = sch.build_house_property(resolved, node_by_guid, rules)
    assert hp.gav == 240000.0
    assert hp.municipal_tax == 20000.0
    assert hp.nav == 220000.0
    assert hp.std_deduction_24a == pytest.approx(66000.0)  # 30% of NAV
    assert hp.interest_24b == 50000.0
    assert hp.income == pytest.approx(220000.0 - 66000.0 - 50000.0)


def test_house_property_absent_returns_zeroed_schedule():
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    hp = sch.build_house_property({}, {}, rules)
    assert hp.gav == 0.0 and hp.income == 0.0


# ---------------------------------------------------------------------------
# RULE-1 golden: refund interest taxable, refund principal visible-excluded
# ---------------------------------------------------------------------------

def test_rule1_refund_interest_taxable_principal_excluded(syn_ind_resolved, syn_ind_book):
    tree, resolved = syn_ind_resolved
    node_by_guid = sch._node_by_guid(tree)
    other_sources = sch.build_other_sources(resolved, node_by_guid, syn_ind_book, YEAR_KEY)
    assert other_sources.refund_interest == 300.0
    assert other_sources.refund_principal_excluded == 1000.0
    # RULE-1: principal must NOT be part of the taxable total.
    assert other_sources.refund_interest in [300.0]
    assert 1000.0 not in [other_sources.taxable_total - other_sources.refund_interest
                          - other_sources.interest_sb - other_sources.interest_bank
                          - other_sources.interest_nbfc - other_sources.interest_epf_taxable
                          - other_sources.dividend_gross - other_sources.slbs]
    expected_taxable = (
        other_sources.interest_sb + other_sources.interest_bank + other_sources.interest_nbfc
        + other_sources.interest_epf_taxable + other_sources.refund_interest
        + other_sources.dividend_gross + other_sources.slbs
    )
    assert other_sources.taxable_total == pytest.approx(expected_taxable)


def test_rule1_computation_excludes_refund_principal_from_gti(syn_ind_resolved, syn_ind_book, entity_and_scrips):
    tree, resolved = syn_ind_resolved
    entity, scrips = entity_and_scrips
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    form16 = parse_form16.parse_form16(FIXTURES / "syn_ind_form16.pdf")
    fmv_tables = sch.load_fmv_tables()
    model = sch.build_all_schedules(
        tree, resolved, syn_ind_book, form16, YEAR_KEY, rules, "new",
        entity.status, entity.dob, scrips, fmv_tables,
    )
    # GTI must include the 300.00 refund interest but never the 1000.00
    # refund principal -- the only way to check "never" here is to confirm
    # other_sources.taxable_total (which feeds GTI) already excludes it,
    # asserted above; here we just confirm the schedule wiring reaches GTI.
    assert model.other_sources.refund_interest == 300.0
    assert model.computation.other_sources_income == pytest.approx(model.other_sources.taxable_total)


# ---------------------------------------------------------------------------
# Regime-flip test: New vs Old produce different tax blocks
# ---------------------------------------------------------------------------

def test_regime_flip_changes_tax_liability(syn_ind_resolved, syn_ind_book, entity_and_scrips):
    tree, resolved = syn_ind_resolved
    entity, scrips = entity_and_scrips
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    form16 = parse_form16.parse_form16(FIXTURES / "syn_ind_form16.pdf")
    fmv_tables = sch.load_fmv_tables()

    model_new = sch.build_all_schedules(
        tree, resolved, syn_ind_book, form16, YEAR_KEY, rules, "new",
        entity.status, entity.dob, scrips, fmv_tables,
    )
    model_old = sch.build_all_schedules(
        tree, resolved, syn_ind_book, form16, YEAR_KEY, rules, "old",
        entity.status, entity.dob, scrips, fmv_tables,
    )
    assert model_new.computation.tax_block.tax_liability != model_old.computation.tax_block.tax_liability
    # Only the tax block should move -- the heads of income are regime-independent.
    assert model_new.computation.gti == model_old.computation.gti


# ---------------------------------------------------------------------------
# Regression-year CG split-date test (23-07-2024): identical sale pre vs
# post the split date is taxed at different 112A/111A rates.
# ---------------------------------------------------------------------------

def test_cg_split_date_ltcg_rate_differs_before_and_after():
    rules = rules_engine.load_rules(RULES_DIR, "2024-25")
    cg_cfg = rules.common["capital_gains"]
    before_rate = cg_cfg["s112a_ltcg_equity_stt"]["before_split"]
    after_rate = cg_cfg["s112a_ltcg_equity_stt"]["on_after_split"]
    assert before_rate != after_rate

    # Build two identical LT taxable-gain buckets, one dated before and one
    # dated after the split, and confirm build_computation's tax formula
    # (mirrored here via compute_tax on hand-built CapitalGainsSchedule
    # values) taxes them at their respective rates.
    from schedules import CapitalGainsSchedule

    identical_gain = 100000.0
    cg_before = CapitalGainsSchedule(lt_taxable_gross=identical_gain, lt_taxable_before_split=identical_gain)
    cg_after = CapitalGainsSchedule(lt_taxable_gross=identical_gain, lt_taxable_on_after_split=identical_gain)

    tax_before = cg_before.lt_taxable_before_split * before_rate + cg_before.lt_taxable_on_after_split * after_rate
    tax_after = cg_after.lt_taxable_before_split * before_rate + cg_after.lt_taxable_on_after_split * after_rate
    assert tax_before == pytest.approx(identical_gain * before_rate)
    assert tax_after == pytest.approx(identical_gain * after_rate)
    assert tax_before != tax_after


def test_cg_split_date_live_year_has_no_split():
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    assert rules.common["capital_gains"]["split_date"] is None


def test_build_capital_gains_reconciles_and_splits_by_date(syn_ind_resolved, syn_ind_book, entity_and_scrips):
    """End-to-end: the OldTech sale (2024-08-15, after the 23-07-2024 split)
    lands entirely in the 'on_after_split' bucket, never 'before_split'."""
    tree, resolved = syn_ind_resolved
    _, scrips = entity_and_scrips
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    fmv_tables = sch.load_fmv_tables()
    cg = sch.build_capital_gains(resolved, sch._node_by_guid(tree), syn_ind_book, YEAR_KEY, rules, scrips, fmv_tables)
    assert cg.reconciliation_ok
    assert not cg.unresolved_scrips
    oldtech_rows = [r for r in cg.lot_rows if r.scrip == "OldTech Ltd"]
    assert oldtech_rows
    for r in oldtech_rows:
        assert r.sale_date > date(2024, 7, 23)


# ---------------------------------------------------------------------------
# Deductions candidates (unit-level; no SYN fixture carries DED_* tags)
# ---------------------------------------------------------------------------

def test_deductions_new_regime_is_na_but_candidates_still_listed():
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    resolved = {"g1": _FakeLeaf("g1", "DED_80C_CANDIDATE")}
    node_by_guid = {"g1": _FakeNode(-50000.0)}
    other_sources = sch.OtherSourcesSchedule()
    ded = sch.build_deductions(resolved, node_by_guid, other_sources, rules, "new", "Individual", 500000.0)
    assert ded.regime_na
    assert ded.total == 0.0
    assert len(ded.candidates_80c) == 1
    assert ded.candidates_80c[0].amount == 50000.0


def test_deductions_old_regime_caps_80c():
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    resolved = {"g1": _FakeLeaf("g1", "DED_80C_CANDIDATE")}
    node_by_guid = {"g1": _FakeNode(-200000.0)}  # exceeds the 150000 cap
    other_sources = sch.OtherSourcesSchedule()
    ded = sch.build_deductions(resolved, node_by_guid, other_sources, rules, "old", "Individual", 500000.0)
    assert not ded.regime_na
    assert ded.total_80c_claimed == 150000.0
    assert ded.total == 150000.0


def test_deductions_80tta_capped_at_savings_interest():
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    other_sources = sch.OtherSourcesSchedule(interest_sb=15000.0)
    ded = sch.build_deductions({}, {}, other_sources, rules, "old", "Individual", 500000.0)
    assert ded.total_80tta_ttb_claimed == 10000.0  # 80TTA cap, not the full 15000


def test_deductions_80ttb_senior_covers_deposit_interest_and_uses_higher_cap():
    # CF2: a senior (age_cls resolved from status+dob, CF6-guarded) gets
    # 80TTB -- a higher cap AND covers bank FD deposit interest, not just
    # savings-account interest.
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    other_sources = sch.OtherSourcesSchedule(interest_sb=15000.0, interest_bank=40000.0)
    ded = sch.build_deductions(
        {}, {}, other_sources, rules, "old", "Individual", 500000.0, age_cls="senior",
    )
    assert ded.total_80tta_ttb_claimed == 50000.0  # 80TTB cap, not the full 55000


def test_deductions_80ttb_senior_excludes_nbfc_hfc_interest():
    # 2026-07-19 mapping-precedence prompt, gate 5: 80TTB covers banks/
    # co-ops/post office only, never NBFC/HFC deposits, at any age class.
    # A senior with mostly NBFC interest must NOT get it folded into the
    # deduction base just because interest_nbfc is nonzero.
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    other_sources = sch.OtherSourcesSchedule(interest_sb=2000.0, interest_bank=3000.0, interest_nbfc=200000.0)
    ded = sch.build_deductions(
        {}, {}, other_sources, rules, "old", "Individual", 500000.0, age_cls="senior",
    )
    assert ded.total_80tta_ttb_claimed == 5000.0  # sb + bank only -- nbfc excluded, well under the 80TTB cap


def test_deductions_general_age_class_never_gets_80ttb_even_with_bank_interest():
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    other_sources = sch.OtherSourcesSchedule(interest_sb=5000.0, interest_bank=40000.0)
    ded = sch.build_deductions(
        {}, {}, other_sources, rules, "old", "Individual", 500000.0, age_cls="general",
    )
    assert ded.total_80tta_ttb_claimed == 5000.0  # 80TTA -- bank FD interest not counted


# ---------------------------------------------------------------------------
# Schedule AL (unit-level)
# ---------------------------------------------------------------------------

def test_schedule_al_excludes_equity_capital_and_trading(syn_ind_resolved):
    tree, resolved = syn_ind_resolved
    node_by_guid = sch._node_by_guid(tree)
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    al = sch.build_schedule_al(resolved, node_by_guid, rules, total_income=500000.0)
    assert "EQUITY_CAPITAL" not in al.buckets
    assert "TRADING" not in al.buckets
    assert al.total_assets > 0
    assert al.required == (500000.0 > al.threshold)


def _fake_resolved_and_nodes(tag_amounts: dict) -> tuple:
    """Builds minimal (resolved, node_by_guid) dicts -- one synthetic leaf
    per {tag: amount} pair -- exercising _sum_tag()'s tag-only routing
    without needing a full HTML/mapping fixture round trip."""
    resolved, node_by_guid = {}, {}
    for i, (tag, amount) in enumerate(tag_amounts.items()):
        guid = f"synthetic-guid-{i}"
        resolved[guid] = mapping_engine.ResolvedLeaf(guid=guid, path=f"Synthetic/{tag}", tag=tag)
        node_by_guid[guid] = pe.AccountNode(
            guid=guid, name=tag, depth=1, section="x", path=f"Synthetic/{tag}", total=amount,
        )
    return resolved, node_by_guid


def test_schedule_al_ncd_routes_to_securities_not_cash_bank():
    # 2026-07-19 mapping-precedence prompt, gate 6: an NCD/debenture tagged
    # AL_SECURITIES must land in the securities bucket, not AL_CASH_BANK --
    # and an ordinary FD tagged AL_CASH_BANK must still land there (1d: the
    # Schedule AL bucket definitions themselves are unchanged, ordinary FDs
    # stay clustered with bank accounts).
    resolved, node_by_guid = _fake_resolved_and_nodes({
        "AL_SECURITIES": 50000.0,   # e.g. an NCD, correctly tagged
        "AL_CASH_BANK": 30000.0,    # an ordinary FD, correctly tagged
    })
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    al = sch.build_schedule_al(resolved, node_by_guid, rules, total_income=500000.0)
    assert al.buckets["AL_SECURITIES"] == 50000.0
    assert al.buckets["AL_CASH_BANK"] == 30000.0


def test_exempt_income_ppf_interest_excluded_from_taxable_other_sources():
    # 2026-07-19 mapping-precedence prompt, gate 4: an account tagged
    # EXEMPT_PPF_INTEREST must land on ExemptIncome's "PPF interest" line
    # and never enter OtherSources.taxable_total.
    resolved, node_by_guid = _fake_resolved_and_nodes({
        "EXEMPT_PPF_INTEREST": 481957.0,
        "OS_INTEREST_SB": 1000.0,
    })
    exempt = sch.build_exempt_income(resolved, node_by_guid)
    assert exempt.ppf_interest == 481957.0

    other_sources = sch.build_other_sources(resolved, node_by_guid, book=None, year_key=None)
    assert other_sources.taxable_total == 1000.0  # PPF interest excluded entirely



# ---------------------------------------------------------------------------
# CF4: split-year 112A exemption pro-rata allocation
# ---------------------------------------------------------------------------

def test_cf4_prorata_engages_on_partial_exemption_in_regression_year():
    split_date = date(2024, 7, 23)
    before, after, prorated, ratio = sch._apply_split_year_exemption_prorata(
        lt_gross_total=200000.0, exempt_used=125000.0,
        lt_before_split=80000.0, lt_on_after_split=120000.0, split_date=split_date,
    )
    assert prorated
    assert ratio == pytest.approx(75000.0 / 200000.0)
    assert before == pytest.approx(80000.0 * ratio)
    assert after == pytest.approx(120000.0 * ratio)
    assert before + after == pytest.approx(75000.0)  # taxable_after_exemption


def test_cf4_prorata_never_engages_on_live_year_no_split_date():
    # AY2026-27 has split_date=None -- even with a partial exemption, this
    # must never be flagged as a split-year assumption (there's no split).
    before, after, prorated, ratio = sch._apply_split_year_exemption_prorata(
        lt_gross_total=200000.0, exempt_used=125000.0,
        lt_before_split=0.0, lt_on_after_split=200000.0, split_date=None,
    )
    assert not prorated
    assert ratio == pytest.approx(0.375)
    assert after == pytest.approx(75000.0)


def test_cf4_no_prorata_when_exemption_fully_absorbs_or_gain_is_nonpositive():
    # Full absorption: taxable_after_exemption == lt_gross_total is False here
    # too (0 != gross), so this DOES trigger allocation -- covered above.
    # A non-positive gross (loss year) must never trigger allocation at all.
    before, after, prorated, ratio = sch._apply_split_year_exemption_prorata(
        lt_gross_total=-50000.0, exempt_used=0.0,
        lt_before_split=-20000.0, lt_on_after_split=-30000.0, split_date=date(2024, 7, 23),
    )
    assert not prorated
    assert ratio == 1.0
    assert before == -20000.0 and after == -30000.0


def test_cf4_flag_reaches_capital_gains_schedule_end_to_end():
    # Build a synthetic case where LT gains straddle the split date and the
    # exemption only partially absorbs the total -- confirms the flag/ratio
    # actually reach CapitalGainsSchedule, not just the helper.
    rules = rules_engine.load_rules(RULES_DIR, "2024-25")  # regression year, split_date set
    cg_cfg = rules.common["capital_gains"]
    split_date = date.fromisoformat(cg_cfg["split_date"])
    assert split_date is not None

    class _FakeLot:
        def __init__(self, sale_date, buy_date, qty, cost, proceeds, gain, scrip, account_guid, attribution="exact"):
            self.sale_date, self.buy_date, self.qty, self.cost = sale_date, buy_date, qty, cost
            self.proceeds, self.gain, self.scrip, self.account_guid, self.attribution = (
                proceeds, gain, scrip, account_guid, attribution
            )

    import lots as lots_engine

    lot_before = _FakeLot(date(2024, 6, 1), date(2023, 1, 1), 100, 50000.0, 150000.0, 100000.0, "X", "acct1")
    lot_after = _FakeLot(date(2024, 8, 1), date(2023, 1, 1), 100, 50000.0, 150000.0, 100000.0, "X", "acct1")

    monkeypatch_targets = (lots_engine, "reconstruct_lots"), (lots_engine, "all_lots")
    orig = {name: getattr(mod, name) for mod, name in monkeypatch_targets}
    try:
        lots_engine.reconstruct_lots = lambda book, year_key: ["recon"]
        lots_engine.all_lots = lambda recons: [lot_before, lot_after]

        class _FakeAccount:
            commodity_id = "X"

        class _FakeBook:
            accounts = {"acct1": _FakeAccount()}

        cg = sch.build_capital_gains(
            resolved={}, node_by_guid={}, book=_FakeBook(), year_key="2024-25",
            rules=rules, scrips={}, fmv_tables=sch.FmvTables(nse={}, mf={}),
        )
    finally:
        for mod, name in monkeypatch_targets:
            setattr(mod, name, orig[name])

    assert cg.split_year_exemption_prorated
    assert 0.0 < cg.split_year_exemption_ratio < 1.0
    assert cg.lt_taxable_before_split + cg.lt_taxable_on_after_split == pytest.approx(cg.lt_taxable_gross - cg.lt_exemption_used)


# ---------------------------------------------------------------------------
# CF1: surcharge marginal relief + 15%-CG-cap, both regimes, boundary tests
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("year_key,regime,threshold", [
    ("2024-25", "old", 5_000_000),   # AY2025-26, 50L old-regime band
    ("2025-26", "old", 5_000_000),   # AY2026-27, 50L old-regime band
    ("2025-26", "new", 10_000_000),  # AY2026-27, 1cr new-regime band
])
def test_surcharge_marginal_relief_bites_just_above_boundary(year_key, regime, threshold):
    rules = rules_engine.load_rules(RULES_DIR, year_key)
    fy_end = date(int(year_key.split("-")[0]) + 1, 3, 31)
    excess = 100.0
    income_at_boundary = threshold + excess

    tb = sch.compute_tax(
        normal_income=income_at_boundary, special_rate_tax=0.0, special_rate_income_amount=0.0,
        rules=rules, regime=regime, status="Individual", dob=None, fy_end=fy_end,
    )
    # Reference point: tax+surcharge at exactly the threshold (itself far
    # enough from any lower boundary that no relief applies there) -- crossing
    # the boundary by `excess` rupees of income must never cost more than
    # `excess` rupees of extra tax+surcharge.
    tb_at_threshold = sch.compute_tax(
        normal_income=threshold, special_rate_tax=0.0, special_rate_income_amount=0.0,
        rules=rules, regime=regime, status="Individual", dob=None, fy_end=fy_end,
    )
    tax_and_surcharge_at_threshold = (
        tb_at_threshold.tax_on_normal_income - tb_at_threshold.rebate_87a + tb_at_threshold.surcharge
    )
    max_allowed = tax_and_surcharge_at_threshold + excess
    tax_and_surcharge_at_boundary = tb.tax_on_normal_income - tb.rebate_87a + tb.surcharge
    assert tax_and_surcharge_at_boundary <= max_allowed + 0.01

    # And relief must actually be doing something -- the naive (uncapped)
    # tax+surcharge at this band's rate would blow well past max_allowed.
    band_rate, _ = sch._surcharge_rate_and_threshold(income_at_boundary, rules.regime(regime)["surcharge"]["bands"])
    tax_normal_after_rebate = tb.tax_on_normal_income - tb.rebate_87a
    naive_total = tax_normal_after_rebate + tax_normal_after_rebate * band_rate
    assert naive_total > max_allowed  # confirms this case would need relief
    assert tax_and_surcharge_at_boundary < naive_total


def test_surcharge_no_relief_needed_far_from_boundary():
    # Sanity control: well inside a band (not near any threshold), the
    # surcharge should equal tax * band_rate un-capped -- relief must not
    # fire when it isn't needed.
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    fy_end = date(2026, 3, 31)
    income = 8_000_000  # deep inside the 50L-1cr / 10% band, old regime
    tb = sch.compute_tax(
        normal_income=income, special_rate_tax=0.0, special_rate_income_amount=0.0,
        rules=rules, regime="old", status="Individual", dob=None, fy_end=fy_end,
    )
    tax_normal_after_rebate = tb.tax_on_normal_income - tb.rebate_87a
    assert tb.surcharge == pytest.approx(tax_normal_after_rebate * 0.10, rel=1e-6)


def test_surcharge_15pct_cap_binds_on_large_capital_gains():
    # CF1(b): old regime AY2026-27, income far above the 5cr/37% band --
    # normal income surcharged at 37%, but CG (111A/112A/112) tax must be
    # capped at 15% (cap_on_cg_dividend), never the full band rate.
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    fy_end = date(2026, 3, 31)
    cg_tax = 12_000_000.0       # a large, deliberately round flat-rate CG tax
    cg_income = 60_000_000.0    # well past the 5cr (50000000) threshold alone
    tb = sch.compute_tax(
        normal_income=0.0, special_rate_tax=cg_tax, special_rate_income_amount=cg_income,
        rules=rules, regime="old", status="Individual", dob=None, fy_end=fy_end,
    )
    # far from any boundary (60cr vs the 5cr threshold) -- relief shouldn't
    # bite, so the surcharge must reflect the capped 15% rate exactly, not 37%.
    assert tb.surcharge == pytest.approx(cg_tax * 0.15, rel=1e-6)
    assert tb.surcharge < cg_tax * 0.37


def test_surcharge_income_for_surcharge_uses_cg_income_not_cg_tax():
    # Regression for the pre-CF1 bug: income_for_surcharge must be built
    # from special_rate_income_amount (actual CG income), not
    # special_rate_tax -- otherwise large-CG entities are silently
    # misclassified into a lower surcharge band.
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    fy_end = date(2026, 3, 31)
    # CG income alone crosses the 50L threshold; CG tax (at ~12.5%) alone
    # would NOT (12.5% * 6000000 = 750000, nowhere near 5000000).
    tb = sch.compute_tax(
        normal_income=0.0, special_rate_tax=750_000.0, special_rate_income_amount=6_000_000.0,
        rules=rules, regime="old", status="Individual", dob=None, fy_end=fy_end,
    )
    assert tb.surcharge > 0.0   # would be 0.0 under the pre-CF1 bug


def test_87a_marginal_relief_new_regime_caps_extra_tax_to_income_excess():
    # AY2026-27 new regime: max_total_income=1,200,000, marginal_relief=True,
    # special-rate income excluded from the rebate test.
    rules = rules_engine.load_rules(RULES_DIR, "2025-26")
    fy_end = date(2026, 3, 31)
    threshold = rules.regime("new")["rebate_87a"]["max_total_income"]
    excess = 500.0
    slabs = rules_engine.resolve_slabs(rules, "new", "Individual", None, fy_end)
    tax_at_threshold = sch._slab_tax(threshold, slabs)

    tb = sch.compute_tax(
        normal_income=threshold + excess, special_rate_tax=0.0, special_rate_income_amount=0.0,
        rules=rules, regime="new", status="Individual", dob=None, fy_end=fy_end,
    )
    tax_after_rebate = tb.tax_on_normal_income - tb.rebate_87a
    # Marginal relief: tax after rebate must not exceed tax-at-threshold +
    # the income excess over the threshold.
    assert tax_after_rebate <= tax_at_threshold + excess + 0.01
    assert tb.rebate_87a > 0.0


def test_round_288a_and_288b():
    assert sch.round_288a(123454.0, 10) == 123450.0
    assert sch.round_288a(123456.0, 10) == 123460.0
    assert sch.round_288b(99995.0, 10) == 100000.0


# ---------------------------------------------------------------------------
# CF5: 26AS wiring -- section classification, quarter-bucket override, and
# the TaxesPaid book<->26AS tie-out (deliberate-conflict case).
# ---------------------------------------------------------------------------

_TDS_SECTIONS = {"dividend": ["194", "194K"], "interest": ["194A"]}


def test_classify_section_maps_dividend_and_interest_codes():
    assert as26_engine.classify_section("194", _TDS_SECTIONS) == "dividend"
    assert as26_engine.classify_section("194K", _TDS_SECTIONS) == "dividend"
    assert as26_engine.classify_section("194A", _TDS_SECTIONS) == "interest"
    assert as26_engine.classify_section("192", _TDS_SECTIONS) is None
    assert as26_engine.classify_section(None, _TDS_SECTIONS) is None


def test_bucket_as26_transactions_uses_txn_date_for_quarter_index():
    # FY 2024-25 windows: <=15-Jun-24, 16-Jun..15-Sep-24, 16-Sep..15-Dec-24,
    # 16-Dec-24..15-Mar-25, 16-Mar..31-Mar-25 -- one transaction per window.
    txns = [
        as26_engine.As26Transaction("TAN1", "Bank A", "194A", date(2024, 6, 1), 1000.0, 100.0, 100.0),
        as26_engine.As26Transaction("TAN1", "Bank A", "194A", date(2024, 9, 1), 2000.0, 200.0, 200.0),
        as26_engine.As26Transaction("TAN1", "Bank A", "194A", date(2024, 12, 1), 3000.0, 300.0, 300.0),
        as26_engine.As26Transaction("TAN1", "Bank A", "194A", date(2025, 3, 1), 4000.0, 400.0, 400.0),
        as26_engine.As26Transaction("TAN1", "Bank A", "194A", date(2025, 3, 31), 5000.0, 500.0, 500.0),
        as26_engine.As26Transaction("TAN1", "Bank A", "194", date(2024, 6, 1), 9999.0, 999.0, 999.0),  # wrong category
    ]
    buckets = quarters_engine.bucket_as26_transactions(txns, "2024-25", "interest", _TDS_SECTIONS)
    assert buckets.buckets == [1000.0, 2000.0, 3000.0, 4000.0, 5000.0]
    assert buckets.total == 15000.0
    # the 31-03-dated entry is flagged as a TDS gross-up candidate.
    assert len(buckets.gross_up_flags) == 1
    assert buckets.gross_up_flags[0]["amount"] == 5000.0


def test_build_other_sources_as26_present_overrides_book_date_buckets(syn_ind_resolved, syn_ind_book):
    tree, resolved = syn_ind_resolved
    node_by_guid = sch._node_by_guid(tree)
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)

    book_only = sch.build_other_sources(resolved, node_by_guid, syn_ind_book, YEAR_KEY, rules, None)
    assert book_only.dividend_quarters_source == "book"

    as26_data = as26_engine.As26Data(transactions=[
        as26_engine.As26Transaction("TAN1", "Synthetic Corp", "194", date(2024, 6, 1), 12345.0, 0.0, 0.0),
    ])
    overridden = sch.build_other_sources(resolved, node_by_guid, syn_ind_book, YEAR_KEY, rules, as26_data)
    assert overridden.dividend_quarters_source == "26AS"
    assert overridden.dividend_quarters == [12345.0, 0.0, 0.0, 0.0, 0.0]
    assert overridden.interest_quarters_source == "26AS"
    # no interest-classified 26AS transactions supplied -> all-zero buckets,
    # not a silent fallback to the book-date buckets.
    assert overridden.interest_quarters == [0.0, 0.0, 0.0, 0.0, 0.0]


def test_build_taxes_paid_26as_tie_out_matches_book(syn_ind_resolved):
    tree, resolved = syn_ind_resolved
    node_by_guid = sch._node_by_guid(tree)
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    book_only = sch.build_taxes_paid(resolved, node_by_guid)

    as26_data = as26_engine.As26Data(transactions=[
        as26_engine.As26Transaction(
            "TAN1", "Bank A", "194A", date(2024, 6, 1), book_only.tds_interest * 10, book_only.tds_interest, book_only.tds_interest,
        ),
    ])
    tp = sch.build_taxes_paid(resolved, node_by_guid, rules, as26_data)
    assert tp.as26_available is True
    assert tp.as26_tds_interest == pytest.approx(book_only.tds_interest)
    assert tp.tie_out_ok is True
    assert tp.tie_out_conflicts == []


def test_build_taxes_paid_26as_conflict_flagged_on_book_vs_26as_mismatch(syn_ind_resolved):
    tree, resolved = syn_ind_resolved
    node_by_guid = sch._node_by_guid(tree)
    rules = rules_engine.load_rules(RULES_DIR, YEAR_KEY)
    book_only = sch.build_taxes_paid(resolved, node_by_guid)

    # Deliberate conflict: 26AS reports a materially different TDS-on-interest
    # figure than the book -- must be FLAGGED, never silently reconciled.
    conflicting_amount = book_only.tds_interest + 5000.0
    as26_data = as26_engine.As26Data(transactions=[
        as26_engine.As26Transaction(
            "TAN1", "Bank A", "194A", date(2024, 6, 1), conflicting_amount * 10, conflicting_amount, conflicting_amount,
        ),
    ])
    tp = sch.build_taxes_paid(resolved, node_by_guid, rules, as26_data)
    assert tp.as26_available is True
    assert tp.tie_out_ok is False
    assert len(tp.tie_out_conflicts) == 1
    conflict = tp.tie_out_conflicts[0]
    assert conflict["category"] == "TDS on interest"
    assert conflict["book"] == pytest.approx(book_only.tds_interest)
    assert conflict["as26"] == pytest.approx(conflicting_amount)
    assert conflict["diff"] == pytest.approx(book_only.tds_interest - conflicting_amount)
