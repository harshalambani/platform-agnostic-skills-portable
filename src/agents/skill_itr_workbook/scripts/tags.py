"""
tags.py -- the ITR Workbook mapping-tag vocabulary (plan section 3).

Every account leaf in a book/HTML tree resolves to exactly one tag (plan
section 3.1: nearest-ancestor wins). A tag carries metadata used by the
workbook writer in a later batch: which output sheet it feeds, and a
one-line treatment note (how the value is used once it lands there).

Two sheet categories matter for the mapping invariant (mapping.py):
  - "RE"  -- Retained Earnings (income/expense) leaves must resolve to one
    of the RE_TAGS.
  - "BS"  -- Balance Sheet (asset/liability) leaves must resolve to one of
    the BS_TAGS (all AL_* schedule buckets).
  - "EITHER" -- PERSONAL can legitimately sit on either side (a drawings
    leaf under Equity, or a personal expense misbooked under Expense) --
    explicitly non-deductible, non-taxable wherever it appears.

AL_FOREIGN is not a tag in its own right: it's a boolean flag (plan section
3, "plus boolean flag AL_FOREIGN on any tag") recorded in a mapping entry's
`flags` list alongside an AL_* (or any) tag, e.g. a foreign brokerage
account tagged AL_SECURITIES with flags: [AL_FOREIGN].
"""
from __future__ import annotations

from dataclasses import dataclass

RE = "RE"
BS = "BS"
EITHER = "EITHER"


@dataclass(frozen=True)
class TagMeta:
    sheet: str          # one of RE, BS, EITHER
    target: str          # the output workbook sheet this tag feeds
    treatment: str       # one-line note on how the value is used there


TAGS: dict[str, TagMeta] = {
    # --- Salary -----------------------------------------------------------
    "SALARY_GROSS": TagMeta(RE, "Salary", "Gross salary; cross-checked against Form 16 17(1) when supplied."),

    # --- Business P&L -------------------------------------------------------
    "BUS_REMUNERATION": TagMeta(RE, "BusinessPL", "Partner/proprietor remuneration; summed into BusinessPL.remuneration."),
    "BUS_EXPENSE": TagMeta(RE, "BusinessPL", "Business expense subtree; children summed, net of remuneration."),

    # --- Other Sources ------------------------------------------------------
    "OS_INTEREST_SB": TagMeta(RE, "OtherSources", "Savings-bank interest; summed, eligible for 80TTA/80TTB candidate."),
    "OS_INTEREST_BANK": TagMeta(RE, "OtherSources", "Bank FD/RD interest; summed into taxable other-sources interest."),
    "OS_INTEREST_NBFC": TagMeta(RE, "OtherSources", "NBFC/HFC deposit interest; summed into taxable other-sources interest."),
    "OS_INTEREST_EPF_TAXABLE": TagMeta(RE, "OtherSources", "Taxable EPF interest portion (post FY21-22 contribution cap); copied."),
    "OS_REFUND_INTEREST": TagMeta(RE, "OtherSources", "Interest on IT refund; taxable -- RULE-1."),
    "OS_DIVIDEND": TagMeta(RE, "OtherSources", "Dividend income; summed, quarterly-bucketed via quarters.py for 234C."),
    "OS_SLBS": TagMeta(RE, "OtherSources", "Securities lending and borrowing scheme income; copied."),

    # --- Non-taxable ---------------------------------------------------------
    "NONTAX_REFUND_PRINCIPAL": TagMeta(RE, "OtherSources", "IT refund principal; shown, excluded from GTI -- RULE-1."),

    # --- Capital gains control totals ----------------------------------------
    "CG_LT_CONTROL": TagMeta(RE, "CapitalGains", "Books' LTCG control total; lot rows must reconcile to this (+/-0.01)."),
    "CG_ST_CONTROL": TagMeta(RE, "CapitalGains", "Books' STCG control total; lot rows must reconcile to this (+/-0.01)."),

    # --- Exempt income ---------------------------------------------------------
    "EXEMPT_PPF_INTEREST": TagMeta(RE, "ExemptIncome", "PPF interest; exempt, shown for completeness."),
    "EXEMPT_10_2A": TagMeta(RE, "ExemptIncome", "Share of firm profit exempt under s.10(2A); copied."),

    # --- House property ------------------------------------------------------
    "HP_RENT": TagMeta(RE, "HouseProperty", "Gross rent received; feeds HP annual value computation."),
    "HP_MUNICIPAL_TAX": TagMeta(RE, "HouseProperty", "Municipal taxes paid; deducted from HP gross annual value."),
    "HP_INTEREST": TagMeta(RE, "HouseProperty", "Interest paid on housing loan; deducted u/s 24(b)."),

    # --- Taxes paid ------------------------------------------------------------
    "TAXPAID_ADV": TagMeta(RE, "TaxesPaid", "Advance tax paid; sign-flipped, credited against total tax."),
    "TAXPAID_SAT": TagMeta(RE, "TaxesPaid", "Self-assessment tax paid; sign-flipped, credited against total tax."),
    "TAXPAID_TDS_SALARY": TagMeta(RE, "TaxesPaid", "TDS on salary; sign-flipped, cross-checked against Form 16."),
    "TAXPAID_TDS_INTEREST": TagMeta(RE, "TaxesPaid", "TDS on interest; sign-flipped, cross-checked against 26AS when supplied."),
    "TAXPAID_TDS_DIVIDEND": TagMeta(RE, "TaxesPaid", "TDS on dividend; sign-flipped, cross-checked against 26AS when supplied."),
    "TAXPAID_TCS": TagMeta(RE, "TaxesPaid", "Tax collected at source; sign-flipped, credited against total tax."),

    # --- Personal (either side) -------------------------------------------------
    "PERSONAL": TagMeta(EITHER, "Reconciliation", "Drawings/gifts/personal withdrawals; explicitly non-deductible, non-taxable."),

    # --- Deduction candidates ----------------------------------------------------
    "DED_80C_CANDIDATE": TagMeta(RE, "Deductions", "80C-eligible outflow candidate; summed, capped via Rules, flagged for review."),
    "DED_80D_CANDIDATE": TagMeta(RE, "Deductions", "80D-eligible premium candidate; summed, capped via Rules, flagged for review."),
    "DED_80G_CANDIDATE": TagMeta(RE, "Deductions", "80G-eligible donation candidate; summed, capped at 10% of AGTI via Rules, flagged for review."),

    # --- Schedule AL (Balance Sheet) buckets --------------------------------------
    "AL_IMMOVABLE": TagMeta(BS, "ScheduleAL", "Immovable property, rolled up at cost."),
    "AL_JEWELLERY": TagMeta(BS, "ScheduleAL", "Jewellery/bullion/artwork, rolled up at cost."),
    "AL_VEHICLE": TagMeta(BS, "ScheduleAL", "Vehicles/yachts/aircraft, rolled up at cost."),
    "AL_SECURITIES": TagMeta(BS, "ScheduleAL", "Listed/unlisted securities and mutual funds, rolled up at cost."),
    "AL_INSURANCE_POLICY": TagMeta(BS, "ScheduleAL", "Insurance policies (cash value), rolled up at cost."),
    "AL_LOANS_GIVEN": TagMeta(BS, "ScheduleAL", "Loans and advances given, rolled up at cost."),
    "AL_CASH_BANK": TagMeta(BS, "ScheduleAL", "Cash and bank balances, rolled up at cost."),
    "AL_LIABILITY": TagMeta(BS, "ScheduleAL", "Liabilities, netted against the AL asset total."),

    # --- Balancing figures (BS-side, excluded from all Schedule-AL roll-ups) -----
    "EQUITY_CAPITAL": TagMeta(BS, "Reconciliation", "Equity/capital account; the balancing figure -- never an AL item."),
    "TRADING": TagMeta(BS, "Reconciliation", "Trading account balance; never an AL item."),
}

# Tags that a Retained-Earnings (income/expense) leaf may resolve to.
RE_TAGS = frozenset(name for name, meta in TAGS.items() if meta.sheet in (RE, EITHER))

# Tags that a Balance-Sheet (asset/liability) leaf may resolve to.
BS_TAGS = frozenset(name for name, meta in TAGS.items() if meta.sheet in (BS, EITHER))

# Schedule-AL asset buckets (Assets-section leaves only).
AL_ASSET_TAGS = frozenset({
    "AL_IMMOVABLE", "AL_JEWELLERY", "AL_VEHICLE", "AL_SECURITIES",
    "AL_INSURANCE_POLICY", "AL_LOANS_GIVEN", "AL_CASH_BANK",
})

# Schedule-AL liability bucket (Liability-section leaves only).
AL_LIABILITY_TAGS = frozenset({"AL_LIABILITY"})

# Every tag that actually rolls up into Schedule AL (excludes the balancing
# figures EQUITY_CAPITAL/TRADING and the non-AL PERSONAL bucket).
AL_TAGS = AL_ASSET_TAGS | AL_LIABILITY_TAGS

# Section-aware tag validation (B4 carry-forward): which tags a leaf under
# each eguile Balance-Sheet section may resolve to. RetainedEarnings-Income/
# -Expense sections are handled separately via RE_TAGS (see mapping.py's
# _is_re_section). PERSONAL (EITHER) is allowed on every BS section since a
# drawings/gift/personal-withdrawal leaf can legitimately sit under any of
# them (plan section 3).
SECTION_ALLOWED_TAGS: dict[str, frozenset[str]] = {
    "Assets": AL_ASSET_TAGS | {"PERSONAL"},
    "Liability": AL_LIABILITY_TAGS | {"PERSONAL"},
    "Equity": frozenset({"EQUITY_CAPITAL", "PERSONAL"}),
    "Trading": frozenset({"TRADING", "PERSONAL"}),
}

# Boolean flags a mapping entry may attach to any tag.
FLAGS = frozenset({"AL_FOREIGN"})


def is_valid_tag(tag: str) -> bool:
    return tag in TAGS


def is_valid_flag(flag: str) -> bool:
    return flag in FLAGS


def all_tags() -> frozenset[str]:
    return frozenset(TAGS.keys())
