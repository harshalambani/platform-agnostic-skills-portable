"""
schedules.py -- pure-python schedule models built from the resolved mapping,
book, Form 16, and rules config (plan section 2.2 / 3). Every number here is
traceable to a tag/source (mapping tag -> leaf total, book split, Form16
field, or Rules config value) -- nothing is hardcoded.

This module is deliberately data-only: it builds dataclasses describing each
schedule's figures. write_workbook.py turns them into formula-linked cells;
this module supplies the VALUES the formulas must reproduce (used for golden
tests and for the values baked into Rules/Entity/transcript sheets, which
are not themselves formulas).
"""
from __future__ import annotations

import csv
import math
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import as26 as as26_engine
import lots as lots_engine
import parse_eguile as pe
import quarters as quarters_engine
import rules as rules_engine
from parse_gnucash import Book, fy_window

DATA_DIR = Path(__file__).parent.parent / "data"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _node_by_guid(tree: pe.ParsedBalanceSheet) -> dict:
    return {n.guid: n for n in tree.all_nodes() if n.guid}


def _sum_tag(resolved: dict, node_by_guid: dict, tag: str) -> float:
    return sum(
        (node_by_guid[leaf.guid].total or 0.0)
        for leaf in resolved.values()
        if leaf.tag == tag and leaf.guid in node_by_guid
    )


def _leaves_for_tag(resolved: dict, node_by_guid: dict, tag: str) -> list:
    return [
        (leaf, node_by_guid[leaf.guid])
        for leaf in resolved.values()
        if leaf.tag == tag and leaf.guid in node_by_guid
    ]


def round_288a(value: float, nearest: int) -> float:
    """s.288A: Total Income rounded to the nearest `nearest` (typically 10),
    standard round-half-up (rupee rounding) -- Python's builtin round() is
    banker's rounding (round-half-to-even) and would silently round some
    exact halves down, so this uses floor(x/nearest + 0.5) instead."""
    if nearest <= 0:
        return value
    return float(math.floor(value / nearest + 0.5) * nearest)


round_288b = round_288a  # same mechanics, different checkpoint (s.288B)


# ---------------------------------------------------------------------------
# Salary
# ---------------------------------------------------------------------------

@dataclass
class SalarySchedule:
    gross: float = 0.0
    s10_exempt_total: float = 0.0
    std_deduction: float = 0.0
    prof_tax: float = 0.0
    income_chargeable: float = 0.0
    source: str = "manual"     # "form16" | "manual" | "book-only"
    manual_flagged: bool = False


def build_salary(
    resolved: dict, node_by_guid: dict, form16, rules: rules_engine.RulesConfig, regime: str,
) -> SalarySchedule:
    gross = _sum_tag(resolved, node_by_guid, "SALARY_GROSS")
    if gross == 0.0 and not any(leaf.tag == "SALARY_GROSS" for leaf in resolved.values()):
        return SalarySchedule(source="manual", manual_flagged=True)

    if form16 is not None and form16.s17_1 is not None:
        std_deduction = form16.std_deduction_16a or 0.0
        return SalarySchedule(
            gross=gross,
            s10_exempt_total=form16.total_2i or 0.0,
            std_deduction=std_deduction,
            prof_tax=form16.prof_tax_16c or 0.0,
            income_chargeable=form16.income_chargeable_6 if form16.income_chargeable_6 is not None else gross,
            source="form16",
        )

    # Book-only path (CF3): no Form 16 to carry a std deduction figure, so
    # apply the Rules-driven std_deduction_salary for the selected regime --
    # still manual_flagged=True since perquisites/s.10 exemptions/prof tax
    # remain unknown without Form 16.
    std_deduction = rules.regime(regime)["std_deduction_salary"]
    income_chargeable = max(0.0, gross - std_deduction)
    return SalarySchedule(
        gross=gross, std_deduction=std_deduction, income_chargeable=income_chargeable,
        source="book-only", manual_flagged=True,
    )


# ---------------------------------------------------------------------------
# Business P&L (ITR-3 only)
# ---------------------------------------------------------------------------

@dataclass
class BusinessSchedule:
    remuneration: float = 0.0
    expenses_total: float = 0.0   # negative, HTML sign convention
    net: float = 0.0


def build_business(resolved: dict, node_by_guid: dict) -> BusinessSchedule:
    remuneration = _sum_tag(resolved, node_by_guid, "BUS_REMUNERATION")
    expenses = _sum_tag(resolved, node_by_guid, "BUS_EXPENSE")
    return BusinessSchedule(remuneration=remuneration, expenses_total=expenses, net=remuneration + expenses)


# ---------------------------------------------------------------------------
# House Property (OQ-1 order: GAV - municipal tax = NAV; -30% NAV; -interest)
# ---------------------------------------------------------------------------

@dataclass
class HousePropertySchedule:
    gav: float = 0.0
    municipal_tax: float = 0.0
    nav: float = 0.0
    std_deduction_24a: float = 0.0
    interest_24b: float = 0.0
    income: float = 0.0


def build_house_property(resolved: dict, node_by_guid: dict, rules: rules_engine.RulesConfig) -> HousePropertySchedule:
    gav = _sum_tag(resolved, node_by_guid, "HP_RENT")
    municipal_tax = _sum_tag(resolved, node_by_guid, "HP_MUNICIPAL_TAX")
    interest = _sum_tag(resolved, node_by_guid, "HP_INTEREST")
    if gav == 0.0 and municipal_tax == 0.0 and interest == 0.0:
        return HousePropertySchedule()

    # municipal_tax/interest are booked as negative expenses (HTML convention);
    # the Rules-driven order operates on their absolute (paid) amounts.
    municipal_tax_paid = abs(municipal_tax)
    interest_paid = abs(interest)
    nav = gav - municipal_tax_paid
    pct = rules.common["house_property"]["std_deduction_pct_of_nav"]
    std_ded = nav * pct
    income = nav - std_ded - interest_paid
    return HousePropertySchedule(
        gav=gav, municipal_tax=municipal_tax_paid, nav=nav,
        std_deduction_24a=std_ded, interest_24b=interest_paid, income=income,
    )


# ---------------------------------------------------------------------------
# Other Sources
# ---------------------------------------------------------------------------

@dataclass
class OtherSourcesSchedule:
    interest_sb: float = 0.0
    interest_bank: float = 0.0
    interest_nbfc: float = 0.0
    interest_epf_taxable: float = 0.0
    refund_interest: float = 0.0
    refund_principal_excluded: float = 0.0
    dividend_gross: float = 0.0
    dividend_quarters: list = field(default_factory=lambda: [0.0] * 5)
    dividend_gross_up_flags: list = field(default_factory=list)
    dividend_quarters_source: str = "book"   # "book" | "26AS" (CF5)
    interest_quarters: list = field(default_factory=lambda: [0.0] * 5)
    interest_gross_up_flags: list = field(default_factory=list)
    interest_quarters_source: str = "book"   # "book" | "26AS" (CF5)
    slbs: float = 0.0
    taxable_total: float = 0.0


def build_other_sources(
    resolved: dict, node_by_guid: dict, book: Book | None, year_key: str | None,
    rules: rules_engine.RulesConfig | None = None, as26_data=None,
) -> OtherSourcesSchedule:
    sb = _sum_tag(resolved, node_by_guid, "OS_INTEREST_SB")
    bank = _sum_tag(resolved, node_by_guid, "OS_INTEREST_BANK")
    nbfc = _sum_tag(resolved, node_by_guid, "OS_INTEREST_NBFC")
    epf = _sum_tag(resolved, node_by_guid, "OS_INTEREST_EPF_TAXABLE")
    refund_interest = _sum_tag(resolved, node_by_guid, "OS_REFUND_INTEREST")
    refund_principal = _sum_tag(resolved, node_by_guid, "NONTAX_REFUND_PRINCIPAL")
    dividend = _sum_tag(resolved, node_by_guid, "OS_DIVIDEND")
    slbs = _sum_tag(resolved, node_by_guid, "OS_SLBS")

    tds_sections = rules.common.get("tds_sections", {}) if rules is not None else {}

    div_quarters = [0.0] * 5
    div_gross_up_flags: list = []
    div_source = "book"
    int_quarters = [0.0] * 5
    int_gross_up_flags: list = []
    int_source = "book"

    if as26_data is not None and as26_data.transactions and year_key is not None and tds_sections:
        # CF5: 26AS transaction dates are the authoritative receipt dates for
        # 234C bucketing (D17) -- override the book-date buckets when a 26AS
        # workbook is supplied.
        div_buckets = quarters_engine.bucket_as26_transactions(
            as26_data.transactions, year_key, "dividend", tds_sections,
        )
        div_quarters, div_gross_up_flags, div_source = div_buckets.buckets, div_buckets.gross_up_flags, "26AS"
        int_buckets = quarters_engine.bucket_as26_transactions(
            as26_data.transactions, year_key, "interest", tds_sections,
        )
        int_quarters, int_gross_up_flags, int_source = int_buckets.buckets, int_buckets.gross_up_flags, "26AS"
    elif book is not None and year_key is not None:
        dividend_guids = {leaf.guid for leaf in resolved.values() if leaf.tag == "OS_DIVIDEND" and leaf.guid in book.accounts}
        if dividend_guids:
            buckets = quarters_engine.bucket_receipts(book, dividend_guids, year_key)
            div_quarters, div_gross_up_flags = buckets.buckets, buckets.gross_up_flags
        interest_guids = {
            leaf.guid for leaf in resolved.values()
            if leaf.tag in ("OS_INTEREST_SB", "OS_INTEREST_BANK", "OS_INTEREST_NBFC") and leaf.guid in book.accounts
        }
        if interest_guids:
            buckets = quarters_engine.bucket_receipts(book, interest_guids, year_key)
            int_quarters, int_gross_up_flags = buckets.buckets, buckets.gross_up_flags

    taxable_total = sb + bank + nbfc + epf + refund_interest + dividend + slbs
    return OtherSourcesSchedule(
        interest_sb=sb, interest_bank=bank, interest_nbfc=nbfc, interest_epf_taxable=epf,
        refund_interest=refund_interest, refund_principal_excluded=refund_principal,
        dividend_gross=dividend, dividend_quarters=div_quarters, dividend_gross_up_flags=div_gross_up_flags,
        dividend_quarters_source=div_source,
        interest_quarters=int_quarters, interest_gross_up_flags=int_gross_up_flags,
        interest_quarters_source=int_source,
        slbs=slbs, taxable_total=taxable_total,
    )


# ---------------------------------------------------------------------------
# FMV / scrips lookup (plan section 6.1, OQ-3R, D14a)
# ---------------------------------------------------------------------------

@dataclass
class FmvTables:
    nse: dict            # symbol -> {isin, fmv_31jan2018}
    mf: dict              # scheme_name -> fmv_31jan2018


def load_fmv_tables(data_dir: str | Path = DATA_DIR) -> FmvTables:
    data_dir = Path(data_dir)
    nse: dict = {}
    nse_path = data_dir / "nse_bhavcopy_31jan2018.csv"
    if nse_path.is_file():
        with open(nse_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                nse[row["symbol"]] = {
                    "isin": row["isin"],
                    "fmv_31jan2018": float(row["fmv_31jan2018"]) if row["fmv_31jan2018"] else None,
                }
    mf: dict = {}
    mf_path = data_dir / "mf_nav_31jan2018.csv"
    if mf_path.is_file():
        with open(mf_path, encoding="utf-8", newline="") as f:
            for row in csv.DictReader(f):
                mf[row["scheme_name"]] = float(row["fmv_31jan2018"]) if row["fmv_31jan2018"] else None
    return FmvTables(nse=nse, mf=mf)


class FmvNotFoundError(Exception):
    """Raised (fail-loud, plan D14a) when a scrip has no scrips.yaml override
    and no bundled NSE/MF row -- e.g. a BSE-only grandfathered scrip."""


def resolve_fmv(symbol: str, scrips: dict, fmv_tables: FmvTables) -> float:
    """Resolve FMV as at 31-01-2018 for `symbol` (a GnuCash commodity id, e.g.
    'SYNCORP.NS'): scrips.yaml override (fmv_31jan2018 or table_ref) first,
    else direct NSE bhavcopy match, else MF NAV match by scheme_name alias.
    Fails loud (FmvNotFoundError) when nothing matches -- never fuzzy-matched
    into a tax figure (plan D14a)."""
    ref = scrips.get(symbol)
    if ref is not None and ref.fmv_31jan2018 is not None:
        return ref.fmv_31jan2018
    table_ref = ref.table_ref if ref is not None else None

    nse_key = symbol
    if table_ref and table_ref.startswith("bundled-fmv-table:"):
        nse_key = table_ref.split(":", 1)[1]
    nse_row = fmv_tables.nse.get(nse_key) or fmv_tables.nse.get(symbol)
    if nse_row is not None and nse_row["fmv_31jan2018"] is not None:
        return nse_row["fmv_31jan2018"]

    mf_key = table_ref.split(":", 1)[1] if table_ref else symbol
    if mf_key in fmv_tables.mf and fmv_tables.mf[mf_key] is not None:
        return fmv_tables.mf[mf_key]

    raise FmvNotFoundError(
        f"no FMV-31-01-2018 found for {symbol!r} -- add an override to scrips.yaml "
        f"(NSE-only bundled coverage; BSE-only scrips need manual FMV)"
    )


# ---------------------------------------------------------------------------
# Capital Gains
# ---------------------------------------------------------------------------

@dataclass
class CGLotRow:
    scrip: str
    sale_date: date
    buy_date: date | None
    term: str              # "LT" | "ST"
    qty: float
    cost: float
    proceeds: float
    booked_gain: float
    grandfathered: bool
    fmv_used: float | None
    taxable_gain: float
    attribution: str


@dataclass
class CapitalGainsSchedule:
    lt_control: float = 0.0
    st_control: float = 0.0
    lot_rows: list = field(default_factory=list)          # list[CGLotRow]
    lt_taxable_gross: float = 0.0
    st_taxable_gross: float = 0.0
    lt_exemption_used: float = 0.0
    lt_taxable_before_split: float = 0.0
    lt_taxable_on_after_split: float = 0.0
    st_taxable_before_split: float = 0.0
    st_taxable_on_after_split: float = 0.0
    reconciliation_ok: bool = True
    reconciliation_diff: float = 0.0
    unresolved_scrips: list = field(default_factory=list)  # symbols with no FMV
    split_year_exemption_prorated: bool = False   # CF4: True when the 112A
                                                    # exemption was allocated
                                                    # pro-rata across the
                                                    # before/after split-date
                                                    # buckets (regression-year
                                                    # runs with a split_date
                                                    # AND a partial exemption)
    split_year_exemption_ratio: float = 1.0        # taxable-after-exemption /
                                                    # taxable-before-exemption,
                                                    # applied to both buckets


def _cg_split_date(rules: rules_engine.RulesConfig) -> date | None:
    sd = rules.common["capital_gains"].get("split_date")
    return date.fromisoformat(sd) if sd else None


def _apply_split_year_exemption_prorata(
    lt_gross_total: float, exempt_used: float, lt_before_split: float, lt_on_after_split: float,
    split_date: date | None,
) -> tuple:
    """CF4: when the 112A exemption only partially offsets LT taxable gains,
    allocate the unabsorbed exemption pro-rata across the before/after
    split-date buckets (by taxable-gain share) -- a documented simplification
    since the law doesn't mandate an ordering between the two in-year rate
    buckets. Returns (lt_before_split, lt_on_after_split, prorated, ratio);
    `prorated` is only ever True for regression-year runs (split_date set)."""
    taxable_after_exemption = lt_gross_total - exempt_used
    if lt_gross_total > 0 and taxable_after_exemption != lt_gross_total:
        ratio = taxable_after_exemption / lt_gross_total if lt_gross_total else 0.0
        prorated = split_date is not None
        return lt_before_split * ratio, lt_on_after_split * ratio, prorated, ratio
    return lt_before_split, lt_on_after_split, False, 1.0


def build_capital_gains(
    resolved: dict, node_by_guid: dict, book: Book | None, year_key: str | None,
    rules: rules_engine.RulesConfig, scrips: dict, fmv_tables: FmvTables,
) -> CapitalGainsSchedule:
    lt_control = _sum_tag(resolved, node_by_guid, "CG_LT_CONTROL")
    st_control = _sum_tag(resolved, node_by_guid, "CG_ST_CONTROL")

    sched = CapitalGainsSchedule(lt_control=lt_control, st_control=st_control)
    if book is None or year_key is None:
        return sched

    cg = rules.common["capital_gains"]
    cutoff = date.fromisoformat(cg["s112a_ltcg_equity_stt"]["grandfathering"]["cutoff"])
    split_date = _cg_split_date(rules)

    reconciliations = lots_engine.reconstruct_lots(book, year_key)
    unresolved: list = []
    lt_gross_total = 0.0
    st_gross_total = 0.0
    lt_before_split = 0.0
    lt_on_after_split = 0.0

    for recon in reconciliations:
        for lot in lots_engine.all_lots([recon]):
            term = _term(lot, book)
            grandfathered = False
            fmv_used = None
            taxable = lot.gain

            if term == "LT" and lot.buy_date is not None and lot.buy_date <= cutoff:
                grandfathered = True
                symbol = book.accounts[lot.account_guid].commodity_id or lot.scrip
                try:
                    fmv = resolve_fmv(symbol, scrips, fmv_tables)
                except FmvNotFoundError:
                    unresolved.append(symbol)
                    fmv = None
                if fmv is not None:
                    fmv_used = fmv
                    fmv_value = fmv * lot.qty
                    taxable = lot.proceeds - max(lot.cost, min(lot.proceeds, fmv_value))

            sched.lot_rows.append(CGLotRow(
                scrip=lot.scrip, sale_date=lot.sale_date, buy_date=lot.buy_date, term=term,
                qty=lot.qty, cost=lot.cost, proceeds=lot.proceeds, booked_gain=lot.gain,
                grandfathered=grandfathered, fmv_used=fmv_used, taxable_gain=taxable,
                attribution=lot.attribution,
            ))

            if term == "LT":
                lt_gross_total += taxable
                if split_date is not None and lot.sale_date <= split_date:
                    lt_before_split += taxable
                elif split_date is not None:
                    lt_on_after_split += taxable
                else:
                    lt_on_after_split += taxable
            else:
                st_gross_total += taxable
                if split_date is not None and lot.sale_date <= split_date:
                    sched.st_taxable_before_split += taxable
                elif split_date is not None:
                    sched.st_taxable_on_after_split += taxable
                else:
                    sched.st_taxable_on_after_split += taxable

    exemption = cg["s112a_ltcg_equity_stt"].get("exemption_limit", 0) or 0
    exempt_used = min(exemption, max(lt_gross_total, 0.0))
    lt_before_split, lt_on_after_split, sched.split_year_exemption_prorated, sched.split_year_exemption_ratio = (
        _apply_split_year_exemption_prorata(lt_gross_total, exempt_used, lt_before_split, lt_on_after_split, split_date)
    )

    sched.lt_taxable_gross = lt_gross_total
    sched.st_taxable_gross = st_gross_total
    sched.lt_exemption_used = exempt_used
    sched.lt_taxable_before_split = lt_before_split
    sched.lt_taxable_on_after_split = lt_on_after_split
    sched.unresolved_scrips = unresolved

    booked_lt_sum = sum(r.booked_gain for r in sched.lot_rows if r.term == "LT")
    booked_st_sum = sum(r.booked_gain for r in sched.lot_rows if r.term == "ST")
    diff = (booked_lt_sum - lt_control) + (booked_st_sum - st_control) if (lt_control or st_control) else 0.0
    sched.reconciliation_diff = diff
    sched.reconciliation_ok = abs(diff) <= 0.01
    return sched


def _term(lot, book: Book) -> str:
    if lot.buy_date is None:
        return "ST"  # unattributed -- conservative default, always flagged for review anyway
    return "LT" if (lot.sale_date - lot.buy_date).days >= 365 else "ST"


# ---------------------------------------------------------------------------
# Exempt Income
# ---------------------------------------------------------------------------

@dataclass
class ExemptIncomeSchedule:
    ppf_interest: float = 0.0
    share_of_firm_profit: float = 0.0


def build_exempt_income(resolved: dict, node_by_guid: dict) -> ExemptIncomeSchedule:
    return ExemptIncomeSchedule(
        ppf_interest=_sum_tag(resolved, node_by_guid, "EXEMPT_PPF_INTEREST"),
        share_of_firm_profit=_sum_tag(resolved, node_by_guid, "EXEMPT_10_2A"),
    )


# ---------------------------------------------------------------------------
# Taxes Paid
# ---------------------------------------------------------------------------

@dataclass
class TaxesPaidSchedule:
    advance_tax: float = 0.0
    self_assessment_tax: float = 0.0
    tds_salary: float = 0.0
    tds_interest: float = 0.0
    tds_dividend: float = 0.0
    tcs: float = 0.0
    total: float = 0.0
    as26_available: bool = False               # CF5
    as26_tds_interest: float = 0.0
    as26_tds_dividend: float = 0.0
    tie_out_ok: bool = True
    tie_out_conflicts: list = field(default_factory=list)   # list[dict]


_TIE_OUT_TOLERANCE = 1.0   # rupee-rounding tolerance between book and 26AS TDS totals


def build_taxes_paid(
    resolved: dict, node_by_guid: dict,
    rules: rules_engine.RulesConfig | None = None, as26_data=None,
) -> TaxesPaidSchedule:
    adv = abs(_sum_tag(resolved, node_by_guid, "TAXPAID_ADV"))
    sat = abs(_sum_tag(resolved, node_by_guid, "TAXPAID_SAT"))
    tds_sal = abs(_sum_tag(resolved, node_by_guid, "TAXPAID_TDS_SALARY"))
    tds_int = abs(_sum_tag(resolved, node_by_guid, "TAXPAID_TDS_INTEREST"))
    tds_div = abs(_sum_tag(resolved, node_by_guid, "TAXPAID_TDS_DIVIDEND"))
    tcs = abs(_sum_tag(resolved, node_by_guid, "TAXPAID_TCS"))
    total = adv + sat + tds_sal + tds_int + tds_div + tcs

    as26_available = False
    as26_tds_int = 0.0
    as26_tds_div = 0.0
    conflicts: list = []
    if as26_data is not None and as26_data.transactions and rules is not None:
        tds_sections = rules.common.get("tds_sections", {})
        as26_available = True
        for txn in as26_data.transactions:
            category = as26_engine.classify_section(txn.section, tds_sections)
            if category == "interest":
                as26_tds_int += txn.tax_deducted
            elif category == "dividend":
                as26_tds_div += txn.tax_deducted

        if abs(as26_tds_int - tds_int) > _TIE_OUT_TOLERANCE:
            conflicts.append({
                "category": "TDS on interest", "book": tds_int, "as26": as26_tds_int,
                "diff": tds_int - as26_tds_int,
            })
        if abs(as26_tds_div - tds_div) > _TIE_OUT_TOLERANCE:
            conflicts.append({
                "category": "TDS on dividend", "book": tds_div, "as26": as26_tds_div,
                "diff": tds_div - as26_tds_div,
            })

    return TaxesPaidSchedule(
        advance_tax=adv, self_assessment_tax=sat, tds_salary=tds_sal,
        tds_interest=tds_int, tds_dividend=tds_div, tcs=tcs, total=total,
        as26_available=as26_available, as26_tds_interest=as26_tds_int, as26_tds_dividend=as26_tds_div,
        tie_out_ok=not conflicts, tie_out_conflicts=conflicts,
    )


# ---------------------------------------------------------------------------
# Deductions (Chapter VI-A candidates)
# ---------------------------------------------------------------------------

@dataclass
class DeductionCandidate:
    path: str
    amount: float


@dataclass
class DeductionsSchedule:
    candidates_80c: list = field(default_factory=list)
    candidates_80d: list = field(default_factory=list)
    candidates_80g: list = field(default_factory=list)
    total_80c_claimed: float = 0.0
    total_80d_claimed: float = 0.0
    total_80tta_ttb_claimed: float = 0.0
    total_80g_claimed: float = 0.0
    total: float = 0.0
    regime_na: bool = False
    regime_na_note: str = ""


def build_deductions(
    resolved: dict, node_by_guid: dict, other_sources: OtherSourcesSchedule,
    rules: rules_engine.RulesConfig, regime: str, status: str, agti: float,
    age_cls: str = "general",
) -> DeductionsSchedule:
    def _candidates(tag: str) -> list:
        return [
            DeductionCandidate(path=leaf.path, amount=abs(node.total or 0.0))
            for leaf, node in _leaves_for_tag(resolved, node_by_guid, tag)
        ]

    c80c = _candidates("DED_80C_CANDIDATE")
    c80d = _candidates("DED_80D_CANDIDATE")
    c80g = _candidates("DED_80G_CANDIDATE")
    sched = DeductionsSchedule(candidates_80c=c80c, candidates_80d=c80d, candidates_80g=c80g)

    if regime == "new":
        sched.regime_na = True
        sched.regime_na_note = rules.regime("new").get(
            "deductions_na_note", "N/A under New regime -- candidates shown for comparison only."
        )
        return sched

    caps = rules.regime("old")["vi_a_caps"]
    sched.total_80c_claimed = min(sum(c.amount for c in c80c), caps.get("80C_combined", 0))
    sched.total_80d_claimed = min(sum(c.amount for c in c80d), caps.get("80D_self", 0))

    # 80TTB (senior/super-senior, resolved from status+dob -- CF2/CF6) covers
    # all deposit interest (savings + bank/NBFC FDs); 80TTA (general) covers
    # savings-account interest only.
    if age_cls in ("senior", "super_senior"):
        ded_base = other_sources.interest_sb + other_sources.interest_bank + other_sources.interest_nbfc
        tta_ttb_cap = caps.get("80TTB", 0)
    else:
        ded_base = other_sources.interest_sb
        tta_ttb_cap = caps.get("80TTA", 0)
    sched.total_80tta_ttb_claimed = min(ded_base, tta_ttb_cap)

    g_cap = agti * caps.get("80G_qualifying_cap_pct_of_agti", 0)
    sched.total_80g_claimed = min(sum(c.amount for c in c80g), g_cap)

    sched.total = (
        sched.total_80c_claimed + sched.total_80d_claimed
        + sched.total_80tta_ttb_claimed + sched.total_80g_claimed
    )
    return sched


# ---------------------------------------------------------------------------
# Schedule AL
# ---------------------------------------------------------------------------

@dataclass
class ScheduleALSchedule:
    buckets: dict = field(default_factory=dict)   # tag -> total (at cost)
    total_assets: float = 0.0
    total_liabilities: float = 0.0
    net: float = 0.0
    threshold: float = 0.0
    required: bool = False


def build_schedule_al(resolved: dict, node_by_guid: dict, rules: rules_engine.RulesConfig, total_income: float) -> ScheduleALSchedule:
    import tags as tag_vocab

    buckets = {tag: _sum_tag(resolved, node_by_guid, tag) for tag in tag_vocab.AL_ASSET_TAGS}
    liabilities = _sum_tag(resolved, node_by_guid, "AL_LIABILITY")
    total_assets = sum(buckets.values())
    threshold = rules.common["schedule_al"]["total_income_threshold"]
    return ScheduleALSchedule(
        buckets=buckets, total_assets=total_assets, total_liabilities=liabilities,
        net=total_assets - liabilities, threshold=threshold, required=total_income > threshold,
    )


# ---------------------------------------------------------------------------
# Schedule FA
# ---------------------------------------------------------------------------

@dataclass
class ScheduleFARow:
    path: str
    amount: float


@dataclass
class ScheduleFASchedule:
    rows: list = field(default_factory=list)


def build_schedule_fa(resolved: dict, node_by_guid: dict) -> ScheduleFASchedule:
    rows = [
        ScheduleFARow(path=leaf.path, amount=node.total or 0.0)
        for leaf in resolved.values()
        for node in [node_by_guid.get(leaf.guid)]
        if node is not None and "AL_FOREIGN" in leaf.flags
    ]
    return ScheduleFASchedule(rows=rows)


# ---------------------------------------------------------------------------
# Computation backbone (plan section 2.1 / 3.3)
# ---------------------------------------------------------------------------

@dataclass
class TaxBlock:
    normal_income: float
    special_rate_income: float
    tax_on_normal_income: float
    tax_on_special_rate_income: float
    rebate_87a: float
    tax_after_rebate: float
    surcharge: float
    cess: float
    tax_before_relief: float
    relief_89: float
    tax_liability: float


def _slab_tax(income: float, slabs: list) -> float:
    tax = 0.0
    prev_upto = 0.0
    for band in slabs:
        upto = band["upto"]
        rate = band["rate"]
        top = upto if upto is not None else income
        if income <= prev_upto:
            break
        taxable_in_band = min(income, top) - prev_upto
        if taxable_in_band > 0:
            tax += taxable_in_band * rate
        prev_upto = top
        if upto is not None and income <= upto:
            break
    return tax


def _surcharge_rate_and_threshold(income: float, bands: list) -> tuple:
    """(rate, threshold) for the highest surcharge band `income` exceeds;
    (0.0, 0.0) if income is below every band's 'above' value. `threshold`
    is that band's 'above' figure -- the boundary CF1's marginal relief
    check operates against."""
    rate, threshold = 0.0, 0.0
    for band in bands:
        if income > band["above"]:
            rate, threshold = band["rate"], band["above"]
    return rate, threshold


def _surcharge(
    income_for_surcharge: float, normal_income: float, special_rate_income_amount: float,
    tax_normal_after_rebate: float, tax_special: float, slabs: list, surcharge_cfg: dict,
) -> float:
    """Surcharge on (tax_normal_after_rebate + tax_special), CF1-compliant:
      (b) tax_special (111A/112A/112 CG tax -- already flat-rate, so linear
          in income) is surcharged at min(band_rate, cap_on_cg_dividend);
          tax_normal_after_rebate is surcharged at the full band rate.
          NOTE (known deviation, flag on Reconciliation): dividend income is
          not yet split out of normal_income, so the 15% cap does not yet
          reach dividend-attributable tax -- CG (111A/112A/112) only.
      (a) marginal relief at every band boundary income_for_surcharge
          crosses: total tax+surcharge is capped at (tax-at-threshold +
          income excess over threshold). 'Tax at threshold' recomputes slab
          tax on normal_income scaled down to the threshold total and scales
          the (linear) special-rate tax by the same factor -- a documented
          simplification for mixed normal/special-rate income; the exact
          apportionment is not settled market practice either.
    Driven entirely from `surcharge_cfg` (Rules config) -- no hardcoded
    bands/caps/rates.
    """
    bands = surcharge_cfg["bands"]
    rate, threshold = _surcharge_rate_and_threshold(income_for_surcharge, bands)
    if rate == 0.0:
        return 0.0

    cap = surcharge_cfg.get("cap_on_cg_dividend")
    special_rate = min(rate, cap) if cap is not None else rate
    raw_surcharge = tax_normal_after_rebate * rate + tax_special * special_rate
    total_tax = tax_normal_after_rebate + tax_special

    if threshold <= 0 or income_for_surcharge <= 0:
        return raw_surcharge

    scale = threshold / income_for_surcharge
    normal_income_at_t = normal_income * scale
    tax_normal_at_t = _slab_tax(normal_income_at_t, slabs)
    tax_special_at_t = tax_special * scale   # linear in income (flat rate)

    rate_at_t, _ = _surcharge_rate_and_threshold(threshold, bands)
    special_rate_at_t = min(rate_at_t, cap) if cap is not None else rate_at_t
    surcharge_at_t = tax_normal_at_t * rate_at_t + tax_special_at_t * special_rate_at_t
    tax_at_t = tax_normal_at_t + tax_special_at_t

    max_allowed = tax_at_t + surcharge_at_t + (income_for_surcharge - threshold)
    if total_tax + raw_surcharge > max_allowed:
        return max(0.0, max_allowed - total_tax)
    return raw_surcharge


def compute_tax(
    normal_income: float, special_rate_tax: float, special_rate_income_amount: float,
    rules: rules_engine.RulesConfig, regime: str, status: str, dob: str | None, fy_end: date,
    relief_89: float = 0.0,
) -> TaxBlock:
    slabs = rules_engine.resolve_slabs(rules, regime, status, dob, fy_end)
    block = rules.regime(regime)
    tax_normal = _slab_tax(normal_income, slabs)

    # Special-rate income (111A/112A/112) is taxed at its own flat rate(s) --
    # schedules.py's CapitalGains schedule already applies the correct
    # before/after-split rate per lot; special_rate_tax is that pre-computed
    # tax, and special_rate_income_amount is the underlying INCOME (needed
    # separately for surcharge-threshold classification -- CF1 fix: these
    # were previously conflated, understating income_for_surcharge for any
    # entity with material CG since CG tax << CG income).
    tax_special = special_rate_tax

    rebate_cfg = block["rebate_87a"]
    total_income_for_rebate = (
        normal_income if rebate_cfg.get("excludes_special_rate_income")
        else normal_income + special_rate_income_amount
    )
    rebate = 0.0
    if rebate_cfg["eligibility"] == "resident-individual" and status == "Individual":
        if total_income_for_rebate <= rebate_cfg["max_total_income"]:
            rebate = min(rebate_cfg["max_rebate"], tax_normal)
        elif rebate_cfg.get("marginal_relief"):
            excess = total_income_for_rebate - rebate_cfg["max_total_income"]
            if tax_normal - excess > 0:
                rebate = max(0.0, tax_normal - excess)

    tax_normal_after_rebate = tax_normal - rebate
    income_for_surcharge = normal_income + special_rate_income_amount
    surcharge = _surcharge(
        income_for_surcharge, normal_income, special_rate_income_amount,
        tax_normal_after_rebate, tax_special, slabs, block["surcharge"],
    )
    tax_after_rebate = tax_normal_after_rebate + tax_special
    cess_rate = rules.common["cess_rate"]
    cess = (tax_after_rebate + surcharge) * cess_rate
    tax_before_relief = tax_after_rebate + surcharge + cess
    tax_liability = max(0.0, tax_before_relief - relief_89)

    return TaxBlock(
        normal_income=normal_income, special_rate_income=special_rate_income_amount,
        tax_on_normal_income=tax_normal, tax_on_special_rate_income=tax_special,
        rebate_87a=rebate, tax_after_rebate=tax_after_rebate, surcharge=surcharge,
        cess=cess, tax_before_relief=tax_before_relief, relief_89=relief_89,
        tax_liability=tax_liability,
    )


@dataclass
class ComputationSchedule:
    salary_income: float
    house_property_income: float
    business_income: float
    capital_gains_lt: float
    capital_gains_st: float
    other_sources_income: float
    gti: float
    via_deductions: float
    total_income_raw: float
    total_income_rounded: float
    tax_block: TaxBlock
    taxes_paid: float
    refund_or_payable: float   # positive == refund, negative == payable


def build_computation(
    salary: SalarySchedule, business: BusinessSchedule, house_property: HousePropertySchedule,
    other_sources: OtherSourcesSchedule, capital_gains: CapitalGainsSchedule,
    deductions: DeductionsSchedule, taxes_paid: TaxesPaidSchedule,
    rules: rules_engine.RulesConfig, regime: str, status: str, dob: str | None, fy_end: date,
) -> ComputationSchedule:
    cg_cfg = rules.common["capital_gains"]

    def _rate(block: dict, key: str) -> tuple:
        """Returns (before_split_rate, after_split_rate) whether the block is
        split-shaped ({before_split, on_after_split}) or flat ({rate: x})."""
        sub = block[key]
        if "rate" in sub:
            return sub["rate"], sub["rate"]
        return sub["before_split"], sub["on_after_split"]

    ltcg_before_rate, ltcg_after_rate = _rate(cg_cfg, "s112a_ltcg_equity_stt")
    stcg_before_rate, stcg_after_rate = _rate(cg_cfg, "s111a_stcg_equity_stt")

    tax_special = (
        capital_gains.lt_taxable_before_split * ltcg_before_rate
        + capital_gains.lt_taxable_on_after_split * ltcg_after_rate
        + capital_gains.st_taxable_before_split * stcg_before_rate
        + capital_gains.st_taxable_on_after_split * stcg_after_rate
    )
    cg_total = capital_gains.lt_taxable_gross + capital_gains.st_taxable_gross - capital_gains.lt_exemption_used

    normal_income = (
        salary.income_chargeable + house_property.income + business.net + other_sources.taxable_total
    )
    gti = normal_income + cg_total
    via = deductions.total if not deductions.regime_na else 0.0
    total_income_raw = gti - via
    nearest = rules.common["rounding"]["total_income"]["nearest"]
    total_income_rounded = round_288a(total_income_raw, nearest)

    tax_block = compute_tax(
        normal_income=max(0.0, total_income_rounded - cg_total),
        special_rate_tax=tax_special, special_rate_income_amount=cg_total,
        rules=rules, regime=regime, status=status, dob=dob, fy_end=fy_end,
    )

    refund_or_payable_raw = taxes_paid.total - tax_block.tax_liability
    nearest_tax = rules.common["rounding"]["tax_payable_refund"]["nearest"]
    refund_or_payable = round_288b(refund_or_payable_raw, nearest_tax)

    return ComputationSchedule(
        salary_income=salary.income_chargeable, house_property_income=house_property.income,
        business_income=business.net, capital_gains_lt=capital_gains.lt_taxable_gross,
        capital_gains_st=capital_gains.st_taxable_gross, other_sources_income=other_sources.taxable_total,
        gti=gti, via_deductions=via, total_income_raw=total_income_raw,
        total_income_rounded=total_income_rounded, tax_block=tax_block,
        taxes_paid=taxes_paid.total, refund_or_payable=refund_or_payable,
    )


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@dataclass
class ITRModel:
    salary: SalarySchedule
    business: BusinessSchedule
    house_property: HousePropertySchedule
    other_sources: OtherSourcesSchedule
    capital_gains: CapitalGainsSchedule
    exempt_income: ExemptIncomeSchedule
    taxes_paid: TaxesPaidSchedule
    deductions: DeductionsSchedule
    schedule_al: ScheduleALSchedule
    schedule_fa: ScheduleFASchedule
    computation: ComputationSchedule


def build_all_schedules(
    tree: pe.ParsedBalanceSheet, resolved: dict, book: Book | None, form16, year_key: str | None,
    rules: rules_engine.RulesConfig, regime: str, status: str, dob: str | None,
    scrips: dict, fmv_tables: FmvTables, as26_data=None,
) -> ITRModel:
    node_by_guid = _node_by_guid(tree)
    fy_end = fy_window(year_key)[1] if year_key else date.today()
    age_cls = rules_engine.resolve_age_class(status, dob, fy_end)

    salary = build_salary(resolved, node_by_guid, form16, rules, regime)
    business = build_business(resolved, node_by_guid)
    house_property = build_house_property(resolved, node_by_guid, rules)
    other_sources = build_other_sources(resolved, node_by_guid, book, year_key, rules, as26_data)
    capital_gains = build_capital_gains(resolved, node_by_guid, book, year_key, rules, scrips, fmv_tables)
    exempt_income = build_exempt_income(resolved, node_by_guid)
    taxes_paid = build_taxes_paid(resolved, node_by_guid, rules, as26_data)

    agti_estimate = (
        salary.income_chargeable + house_property.income + business.net + other_sources.taxable_total
        + capital_gains.lt_taxable_gross + capital_gains.st_taxable_gross
    )
    deductions = build_deductions(
        resolved, node_by_guid, other_sources, rules, regime, status, agti_estimate, age_cls,
    )
    computation = build_computation(
        salary, business, house_property, other_sources, capital_gains, deductions, taxes_paid,
        rules, regime, status, dob, fy_end,
    )
    schedule_al = build_schedule_al(resolved, node_by_guid, rules, computation.total_income_rounded)
    schedule_fa = build_schedule_fa(resolved, node_by_guid)

    return ITRModel(
        salary=salary, business=business, house_property=house_property, other_sources=other_sources,
        capital_gains=capital_gains, exempt_income=exempt_income, taxes_paid=taxes_paid,
        deductions=deductions, schedule_al=schedule_al, schedule_fa=schedule_fa, computation=computation,
    )
