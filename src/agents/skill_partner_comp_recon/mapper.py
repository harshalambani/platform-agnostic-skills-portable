"""
mapper.py -- Partner Compensation Reconciliation: pure assembly of parsed
L1 (payout advice) / L3 (compensation advisory) / L4 (payment schedule) /
L5 (LLP statement of account) document records into the plain `data` dict
shape `engine.build_report()` consumes.

Pure module: no filesystem, no pdfplumber, no network. `agent.py` is the
only caller, and the only place that reads/writes anything.

Governing rule, same as engine.py's: NEVER invent a figure. A source
field that is absent from a parsed record stays absent from the returned
dict (or, for engine.build_report()'s per-month HARD required keys --
`month`, `total_paid`, `remuneration`, `share_of_profit_gross` -- a
monthly advice record missing any one of them is dropped from `monthly`
entirely, with a diagnostic explaining why, rather than padding it with a
0.0 that build_report()'s KeyError-based contract was never designed to
receive as a real figure). Nothing here reads a rate, percentage, or
period from a constant -- `drivers` is accepted exactly as supplied by
the caller (agent.py sources it from entity/tax-rules config; a value
missing there flows straight through as a missing driver, which
engine.build_report() already reports as an explicit CANNOT RECONCILE
naming the missing rate -- this module adds no defaults of its own).

Two payout-advice document classes (L1 parser, parsers/payout_advice.py):

  * Class A (older, pre-s.194T rail): no "doc_class" key at all. Its
    `month` field is already the canonical "YYYY-MM" and its
    `share_of_profit_gross` is already gross.
  * Class B (current, s.194T rail): always carries `"doc_class": "B"`.
    Its month is given as a name ("April") plus a separate four-digit
    `year`, and its `share_of_profit` is NET of the firm's tax on that
    share of profit -- NEVER the same figure as Class A's
    `share_of_profit_gross`, and never mapped to that field. Class B also
    carries a `tds_label` (verbatim spelling of whichever TDS row label
    was printed) which distinguishes its `additional_share_of_profit`
    figure's real nature (a prior-year PLMI instalment vs year-end
    interest on capital) -- this module retains `tds_label` unchanged and
    never infers what `additional_share_of_profit` is from its amount.

Both classes are normalised to the same "YYYY-MM" month key and merged
into one chronologically sorted `monthly` list -- see _normalise_advice_
record()/_build_monthly() below. Branching is always on the *presence*
of "doc_class" in the record, never on a literal `doc_class == "A"`
check (Class A never carries the key at all).

The L4 payment schedule (parsers/payment_schedule.py, `schedule_record`
below) is a per-FY, per-partner month-by-month grid and is this module's
PRECEDENCE source for three figures no other document supplies reliably:

  * `gross_share_of_profit` -- Class B's own `share_of_profit` is NET, so
    the schedule is the only place a Class B month's GROSS figure comes
    from. For a Class A month (whose `share_of_profit_gross` already IS
    gross), the schedule is still preferred when present; a disagreement
    with the advice's own figure is a loud, non-blocking diagnostic --
    the schedule figure is what is used.
  * `firm_tax_on_sop` / `firm_tax_others` -- these are two DISTINCT
    figures (see engine.py's MonthlyLine.firms_tax_sop /
    firms_tax_other), never read from any other source. `firm_tax_on_sop`
    nets the current year's share of profit; `firm_tax_others` nets a
    prior-year PLMI drawdown, not the current year's share of profit --
    see jv_emitter.py for how the two legs differ.

When a schedule figure and a document figure disagree, the schedule
always wins (this module never blocks a run over a disagreement) but the
disagreement is always reported via `_diagnostics` -- never silently
resolved.

The schedule's `ctc_structuring` block is carried straight through to the
returned data dict, verbatim, as a RECON-ONLY block: it is never merged
into `monthly`, never contributes a figure to any hard-required key, and
(see jv_emitter.py) is never read by build_journals() -- no CSV row is
ever emitted from it. These are recon items only against CTC, not
taxable income: opting into them makes that part of CTC non-taxable in
the first place, paid out as a reimbursement or a direct lease payment,
so they never touch the journal or the taxable-income reconciliation.

Interest on capital is NOT a monthly figure in this skill's source
documents -- the L1 payout certificate has no separate line for it (see
parsers/payout_advice.py), and the L3 Advisory's own `interest_on_capital`
field is a different figure (that document's Part-1 component build-up,
not a cash movement). The only source this module uses for
`monthly[...]["interest_on_capital"]` is the L5 LLP statement's
`capital_interest_on_capital` field -- a single annual figure, which is
placed on exactly one monthly line (the last month present, in date
order -- March if the year's certificates run to March, else whichever
month is latest) and never spread or duplicated across the year. Absent
an L5 record, `interest_on_capital` is absent from every monthly line --
never synthesised from the advices.
"""
from __future__ import annotations

import calendar

from .engine import fy_of_date, fy_start_year

# Hard-required keys on every assembled monthly line -- engine.build_report()
# reads these via `m["..."]` (KeyError, not `.get()`), so a record missing
# any of them cannot be represented as a monthly line at all.
_HARD_REQUIRED_MONTHLY_KEYS = ("total_paid", "remuneration", "share_of_profit_gross")

# Optional monthly keys copied through when present (from either class, or
# derived during assembly).
_OPTIONAL_MONTHLY_KEYS = (
    "additional_share_of_profit", "tds", "tds_label",
    "share_of_profit_net_reported", "firms_tax_sop", "firms_tax_other",
)

_MONTH_NAME_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

# Tolerance (rupees) for comparing two sources' figures for the same month
# -- same order of magnitude as engine.py's CAPITAL_TOLERANCE.
_AMOUNT_TOLERANCE = 1.0


class FinancialYearMismatchError(ValueError):
    """Raised when two or more supplied documents disagree on the
    financial year they belong to. Never silently trusted/picked -- the
    caller must resolve the disagreement (wrong document, wrong folder)
    before a report can be assembled."""


def _fy_from_month(month: str) -> str | None:
    """"2026-01" -> "2025-26", via engine.fy_of_date() (never a
    reimplementation of the FY-boundary rule)."""
    if not month:
        return None
    try:
        return fy_of_date(f"{month}-01")
    except (ValueError, TypeError):
        return None


def _resolve_financial_year(
    financial_year: str | None,
    advisory_record: dict | None,
    advice_records: list[dict],
    llp_record: dict | None,
    schedule_record: dict | None = None,
) -> str:
    """Every document that carries its own financial year is compared;
    any disagreement fails loud naming every source and its year (never
    picks one silently). Returns the agreed year, or raises
    FinancialYearMismatchError / ValueError if none/disagreeing."""
    candidates: dict[str, str] = {}
    if financial_year:
        candidates["explicit financial_year"] = financial_year
    if advisory_record and advisory_record.get("financial_year"):
        candidates["advisory (L3)"] = advisory_record["financial_year"]
    if llp_record and llp_record.get("financial_year"):
        candidates["LLP statement (L5)"] = llp_record["financial_year"]
    if schedule_record and schedule_record.get("financial_year"):
        candidates["payment schedule (L4)"] = schedule_record["financial_year"]

    advice_years: dict[str, str] = {}
    for rec in advice_records:
        norm = _normalise_advice_record(rec)
        month = norm.get("month")
        fy = _fy_from_month(month) if month else None
        if fy:
            advice_years[rec.get("source_name") or month] = fy
    if advice_years:
        distinct_advice_years = set(advice_years.values())
        if len(distinct_advice_years) > 1:
            named = ", ".join(f"{k}={v}" for k, v in sorted(advice_years.items()))
            raise FinancialYearMismatchError(
                f"Payout advices disagree on financial year among themselves: {named}."
            )
        candidates["payout advices (L1)"] = distinct_advice_years.pop()

    distinct = set(candidates.values())
    if len(distinct) > 1:
        named = ", ".join(f"{k}={v}" for k, v in sorted(candidates.items()))
        raise FinancialYearMismatchError(
            f"Documents disagree on financial year: {named}."
        )
    if not distinct:
        raise ValueError(
            "No financial year could be determined from any supplied document "
            "(financial_year was not supplied, and no document carried one)."
        )
    return distinct.pop()


def _normalise_advice_record(rec: dict) -> dict:
    """Normalises one L1 advice record (Class A or Class B) to a common
    shape keyed by canonical "YYYY-MM" month -- never drops a
    class-specific figure, never conflates Class B's NET share of profit
    with Class A's GROSS one.

    Dispatch is on the *presence* of "doc_class" -- Class A records never
    carry that key at all (see parsers/payout_advice.py's module
    docstring); this is never a literal `doc_class == "A"` check.
    """
    source_name = rec.get("source_name") or "<unknown>"
    if "doc_class" not in rec:
        # Class A -- month is already canonical "YYYY-MM"; share_of_profit
        # is already gross.
        return {
            "month": rec.get("month"),
            "source_name": source_name,
            "is_class_b": False,
            "total_paid": rec.get("total_paid"),
            "remuneration": rec.get("remuneration"),
            "share_of_profit_gross": rec.get("share_of_profit_gross"),
            "share_of_profit_net": None,
            "additional_share_of_profit": rec.get("additional_share_of_profit"),
            "tds": rec.get("tds"),
            "tds_label": None,
        }

    # Class B -- month name + year given separately; share_of_profit is NET.
    month_name = rec.get("month")
    year = rec.get("year")
    num = _MONTH_NAME_TO_NUM.get(str(month_name).lower()) if month_name else None
    month = f"{int(year):04d}-{num:02d}" if (num is not None and year is not None) else None
    return {
        "month": month,
        "source_name": source_name,
        "is_class_b": True,
        "total_paid": rec.get("total"),
        "remuneration": rec.get("remuneration"),
        "share_of_profit_gross": None,
        "share_of_profit_net": rec.get("share_of_profit"),
        "additional_share_of_profit": rec.get("additional_share_of_profit"),
        "tds": rec.get("tds"),
        "tds_label": rec.get("tds_label"),
    }


def _schedule_month_map(schedule_record: dict | None) -> dict[str, dict]:
    """Reshapes the L4 payment-schedule record's month-name-keyed grid
    (parsers/payment_schedule.py's `months`/`rows` shape) into a plain
    dict keyed by canonical "YYYY-MM", each value a dict of whichever
    per-row fields the schedule carries for that month (a field absent
    from the schedule for a given month is simply absent here -- never
    defaulted). Returns {} if no usable schedule was supplied."""
    if not schedule_record:
        return {}
    fy = schedule_record.get("financial_year")
    months = schedule_record.get("months") or []
    rows = schedule_record.get("rows") or {}
    try:
        start_year = int(str(fy).split("-")[0])
    except (ValueError, IndexError, TypeError):
        start_year = None
    if start_year is None:
        return {}

    out: dict[str, dict] = {}
    for name in months:
        num = _MONTH_NAME_TO_NUM.get(str(name).lower())
        if num is None:
            continue
        year = start_year if num >= 4 else start_year + 1
        key = f"{year:04d}-{num:02d}"
        month_fields: dict = {}
        for field_name, row in rows.items():
            if not isinstance(row, dict):
                continue
            value = (row.get("months") or {}).get(name)
            if value is not None:
                month_fields[field_name] = value
        out[key] = month_fields
    return out


def _build_monthly(
    advice_records: list[dict], schedule_map: dict[str, dict]
) -> tuple[list[dict], list[str]]:
    """Normalises both advice classes to "YYYY-MM", merges in the L4
    schedule's precedence figures, and builds `monthly` sorted
    chronologically. A record missing any hard-required key (after the
    schedule has had a chance to supply gross_share_of_profit) is dropped,
    with a diagnostic naming the source and the missing key(s) -- never
    padded with a 0.0.
    """
    diagnostics: list[str] = []
    normalised: list[dict] = []
    for rec in advice_records:
        norm = _normalise_advice_record(rec)
        if not norm.get("month"):
            diagnostics.append(
                f"NOTE: payout advice {rec.get('source_name') or '<unknown>'} has no "
                "recognisable month -- dropped from monthly reconciliation."
            )
            continue
        normalised.append(norm)

    normalised.sort(key=lambda r: r["month"])

    monthly: list[dict] = []
    for rec in normalised:
        month = rec["month"]
        sched = schedule_map.get(month, {})

        # gross_share_of_profit -- schedule is precedence source; Class A's
        # own figure is used only as a fallback when the schedule doesn't
        # cover this month, and otherwise only cross-checked.
        gross = sched.get("gross_share_of_profit")
        advice_gross = None if rec["is_class_b"] else rec.get("share_of_profit_gross")
        if gross is None:
            gross = advice_gross
        elif advice_gross is not None and abs(gross - advice_gross) > _AMOUNT_TOLERANCE:
            diagnostics.append(
                f"NOTE: {month} -- payment schedule's gross share of profit "
                f"({gross:,.2f}) disagrees with the payout advice's "
                f"({advice_gross:,.2f}) -- the schedule figure is used "
                "(precedence source); not blocking."
            )

        # firm_tax_on_sop / firm_tax_others -- schedule-only, distinct legs.
        firm_tax_on_sop = sched.get("firm_tax_on_sop")
        firm_tax_others = sched.get("firm_tax_others")

        # Class B net-vs-gross identity check: gross - net == -firm_tax_on_sop.
        if (
            rec["is_class_b"]
            and gross is not None
            and rec.get("share_of_profit_net") is not None
            and firm_tax_on_sop is not None
        ):
            implied_tax = gross - rec["share_of_profit_net"]
            if abs(implied_tax + firm_tax_on_sop) > _AMOUNT_TOLERANCE:
                diagnostics.append(
                    f"NOTE: {month} -- Class B net share of profit "
                    f"({rec['share_of_profit_net']:,.2f}) does not reconcile "
                    f"against gross ({gross:,.2f}) less firm's tax on SoP "
                    f"({firm_tax_on_sop:,.2f}); implied tax was "
                    f"{implied_tax:,.2f} -- reported, not blocking."
                )

        missing = []
        if rec.get("total_paid") is None:
            missing.append("total_paid")
        if rec.get("remuneration") is None:
            missing.append("remuneration")
        if gross is None:
            missing.append("share_of_profit_gross")
        if missing:
            diagnostics.append(
                f"NOTE: payout advice for {month} ({rec['source_name']}) is missing "
                f"{', '.join(missing)} -- dropped from monthly reconciliation "
                "rather than defaulted to zero."
            )
            continue

        line: dict = {
            "month": month,
            "total_paid": rec["total_paid"],
            "remuneration": rec["remuneration"],
            "share_of_profit_gross": gross,
        }

        # 2.1 -- three schedule rows carried straight through, sign as
        # parsed (never negated/abs()'d), absent when the schedule has no
        # figure for the month (never padded with 0.0). Note the name
        # change: the schedule's "transferred_to_capital" row becomes the
        # line's "capital_transferred" key -- see engine.MonthlyLine.
        if sched.get("medical_topup") is not None:
            line["medical_topup"] = sched["medical_topup"]
        if sched.get("transferred_to_capital") is not None:
            line["capital_transferred"] = sched["transferred_to_capital"]
        if sched.get("interest_on_capital") is not None:
            line["interest_on_capital"] = sched["interest_on_capital"]

        # 2.2 -- prior_cohort_drawdown is the prior-year PLMI instalment
        # NET of the firm's tax on it (firm_tax_others is negative as
        # parsed, so this is an addition and the result is positive). Left
        # absent when previous_year_plmis is itself absent or zero for the
        # month -- never computed off a synthesised 0.0.
        prev_plmis = sched.get("previous_year_plmis")
        if prev_plmis:
            line["prior_cohort_drawdown"] = prev_plmis + (firm_tax_others or 0.0)

        # 2.3 -- tds is sourced from the schedule's tds_on_rem_ioc
        # (precedence source; the payslip under-states some months). Falls
        # back to the payslip's own figure only when the schedule has none
        # for this month. A disagreement between the two is a loud,
        # non-blocking diagnostic naming both figures -- the month is
        # never dropped and nothing raises.
        sched_tds = sched.get("tds_on_rem_ioc")
        payslip_tds = rec.get("tds")
        if sched_tds is not None and payslip_tds is not None and abs(sched_tds - payslip_tds) > _AMOUNT_TOLERANCE:
            diagnostics.append(
                f"NOTE: {month} -- payslip's TDS figure ({payslip_tds:,.2f}) "
                f"disagrees with the payment schedule's tds_on_rem_ioc "
                f"({sched_tds:,.2f}) -- the schedule figure is used "
                "(precedence source); not blocking."
            )
        tds = sched_tds if sched_tds is not None else payslip_tds
        if tds is not None:
            line["tds"] = tds
        if rec.get("tds_label") is not None:
            line["tds_label"] = rec["tds_label"]

        # 2.4 -- additional_share_of_profit is booked from the schedule's
        # arrears_share_of_profit ONLY. The payslip's own
        # additional_share_of_profit field is polymorphic across the year
        # (a prior-year PLMI drawdown in some months, net interest on
        # capital in others, genuine arrears in others) and cannot be a
        # booking source; it is used here purely as a reconciliation
        # check. A disagreement is reported as a loud, non-blocking
        # diagnostic -- never a dropped month, never a changed booked
        # amount.
        sched_arrears = sched.get("arrears_share_of_profit")
        if sched_arrears is not None:
            line["additional_share_of_profit"] = sched_arrears
        payslip_arrears = rec.get("additional_share_of_profit")
        if payslip_arrears is not None:
            effective_sched_arrears = sched_arrears if sched_arrears is not None else 0.0
            if abs(payslip_arrears - effective_sched_arrears) > _AMOUNT_TOLERANCE:
                diagnostics.append(
                    f"NOTE: {month} -- payslip's additional_share_of_profit "
                    f"figure ({payslip_arrears:,.2f}) disagrees with the payment "
                    f"schedule's arrears_share_of_profit "
                    f"({effective_sched_arrears:,.2f}); this payslip field is "
                    "polymorphic across the year (a prior-year PLMI drawdown, "
                    "net interest on capital, or genuine share-of-profit "
                    "arrears, depending on the month) and is used here only as "
                    "a reconciliation check, not a booking source -- the "
                    "schedule's arrears_share_of_profit is what is booked; "
                    "not blocking."
                )

        if rec["is_class_b"] and rec.get("share_of_profit_net") is not None:
            # Distinctly named -- NEVER share_of_profit_gross. Carried for
            # traceability/diagnostics; engine.build_report() does not read
            # this key.
            line["share_of_profit_net_reported"] = rec["share_of_profit_net"]
        if firm_tax_on_sop is not None:
            line["firms_tax_sop"] = firm_tax_on_sop
        if firm_tax_others is not None:
            line["firms_tax_other"] = firm_tax_others
        monthly.append(line)
    return monthly, diagnostics


def _place_interest_on_capital(monthly: list[dict], llp_record: dict | None) -> list[str]:
    """Places the L5 record's `capital_interest_on_capital` (a single
    annual figure) on exactly the final monthly line present, never
    spread/duplicated. Returns diagnostics explaining what happened (or
    why nothing happened)."""
    diagnostics: list[str] = []
    if llp_record is None:
        return diagnostics
    ioc = llp_record.get("capital_interest_on_capital")
    if ioc is None:
        return diagnostics
    if not monthly:
        diagnostics.append(
            "NOTE: LLP statement's interest_on_capital "
            f"({ioc}) could not be placed -- no monthly payout advice lines were "
            "available to carry it."
        )
        return diagnostics
    target = monthly[-1]
    target["interest_on_capital"] = ioc
    target["interest_on_capital_comment"] = (
        "Annual interest-on-capital figure from the LLP statement of account "
        f"(L5), placed on this month ({target['month']}, the final month present) "
        "rather than spread across the year -- it is a single year-end credit, "
        "not a per-month cash movement."
    )
    return diagnostics


def _cohort_month_end(year: int, month: int) -> str:
    """(year, month) -> ISO date of that month's last day. Mirrors
    jv_emitter._month_end's convention exactly (duplicated here rather
    than imported, to avoid a new mapper -> jv_emitter dependency)."""
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-{last_day:02d}"


def _prior_fy(fy: str) -> str:
    """"2025-26" -> "2024-25", via engine.fy_start_year (never a
    hardcoded year)."""
    start = fy_start_year(fy) - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _build_cohorts(schedule_record: dict | None, fy: str) -> list[dict]:
    """Assembles the `cohorts` list engine.classify_cohort_instalments()
    consumes from the L4 payment schedule's "Previous Year PLMIs" row --
    the only source in this build that carries payment *dates* for these
    instalments (see build brief DX4 s.1). One instalment is emitted for
    every month where previous_year_plmis is present and non-zero; all of
    them are grouped into a single cohort whose award_fy is the FY
    immediately preceding the reporting FY (the row is literally labelled
    "Previous Year PLMIs", so the award year is definitionally the prior
    FY). Returns [] -- never an empty list nested under a "cohorts" key --
    when no such month is found.
    """
    if not schedule_record:
        return []
    fy_field = schedule_record.get("financial_year")
    months = schedule_record.get("months") or []
    rows = schedule_record.get("rows") or {}
    try:
        start_year = int(str(fy_field).split("-")[0])
    except (ValueError, IndexError, TypeError):
        return []

    plmi_row = rows.get("previous_year_plmis")
    if not isinstance(plmi_row, dict):
        return []
    plmi_months = plmi_row.get("months") or {}

    tax_row = rows.get("firm_tax_others")
    tax_months = (tax_row.get("months") or {}) if isinstance(tax_row, dict) else {}
    capital_row = rows.get("transferred_to_capital")
    capital_months = (capital_row.get("months") or {}) if isinstance(capital_row, dict) else {}

    instalments: list[dict] = []
    for name in months:
        num = _MONTH_NAME_TO_NUM.get(str(name).lower())
        if num is None:
            continue
        gross_raw = plmi_months.get(name)
        if not gross_raw:
            # Absent or exactly zero for this month -- no instalment.
            continue
        year = start_year if num >= 4 else start_year + 1
        date = _cohort_month_end(year, num)
        gross = abs(float(gross_raw))

        firms_tax = tax_months.get(name)
        firms_tax = firms_tax if firms_tax else None
        capital = capital_months.get(name)
        capital = capital if capital else None

        if firms_tax is None and capital is None:
            net = None
        else:
            net = gross + (firms_tax or 0.0) + (capital or 0.0)

        instalments.append({
            "date": date,
            "gross": gross,
            "firms_tax": firms_tax,
            "capital": capital,
            "net": net,
        })

    if not instalments:
        return []
    return [{"award_fy": _prior_fy(fy), "instalments": instalments}]


def build_input_data(
    *,
    financial_year: str | None = None,
    advisory_record: dict | None = None,
    advice_records: list[dict] | None = None,
    llp_record: dict | None = None,
    schedule_record: dict | None = None,
    drivers: dict | None = None,
    accounts: dict | None = None,
    firm_name: str = "",
) -> dict:
    """Pure assembly: converts parsed L1/L3/L4/L5 records into the `data`
    dict shape engine.build_report() consumes.

    Parameters
    ----------
    financial_year:
        An explicit FY label ("2025-26"), if the caller has one independent
        of the documents. Optional -- if omitted, the FY is derived
        entirely from the documents. Any disagreement between this value
        and a document-derived year raises FinancialYearMismatchError.
    advisory_record:
        The dict returned by parsers.advisory.parse_l3_text()/parse().
        Its `financial_year` and the gross total of its
        `schedule_instalments` (when present) are carried through into
        `data["advisory"]` as `financial_year` /
        `schedule_instalments_gross_total`, for engine.build_report()'s
        award-year-advisory-vs-payment-schedule reconciliation category.
    advice_records:
        A list of dicts, each returned by
        parsers.payout_advice.parse_l1_text()/parse() -- one per monthly
        certificate, Class A or Class B (see module docstring).
    llp_record:
        The dict returned by parsers.llp_statement.parse_l5_words()/parse(),
        or None if that optional leg was not supplied/could not be parsed.
    schedule_record:
        The dict returned by
        parsers.payment_schedule.parse_payment_schedule_pages(), or None
        if that optional leg was not supplied/could not be parsed. When
        present, it is the PRECEDENCE source for gross_share_of_profit,
        firm_tax_on_sop and firm_tax_others (see module docstring) and its
        ctc_structuring block is carried straight through, recon-only.
    drivers:
        The per-FY rates/periods block (firm's tax rate, capital
        contribution rate, capital accretion months, remuneration TDS
        section/rate/start-date, etc.) -- sourced by the caller from
        entity/tax-rules config, never invented here. Passed through
        unchanged; a missing rate is engine.build_report()'s job to report
        as CANNOT RECONCILE, naming the rate.
    accounts:
        The GnuCash account-path map (see jv_emitter.ACCOUNT_KEYS),
        resolved and validated by the caller. Passed through unchanged.
    firm_name:
        Cosmetic only -- appears on the workbook.

    Raises
    ------
    FinancialYearMismatchError, ValueError:
        If the supplied documents disagree on financial year, or if no
        financial year can be determined at all.

    Returns
    -------
    dict with keys: financial_year, firm_name, and (only when the source
    data supports them) drivers, advisory, monthly, cohorts, accounts,
    ctc_structuring. `cohorts` is assembled from the L4 payment schedule's
    "Previous Year PLMIs" row (the only source that carries payment dates
    for these instalments -- see _build_cohorts()) and is only ever
    emitted as a non-empty list with one cohort; it is never emitted as
    an empty list. `external` and `payroll` are never populated in this
    build -- no parser in this package produces them -- so the
    corresponding reconciliation categories in engine.build_report()
    legitimately report CANNOT RECONCILE. A `"_diagnostics"` key
    (list[str]) carries any NOTE-level messages produced while assembling
    this data (e.g. a dropped monthly record, a schedule/advice
    disagreement) -- engine.build_report() does not read this key; it
    exists purely for the caller (agent.py) to surface in its own summary.
    """
    advice_records = list(advice_records or [])

    fy = _resolve_financial_year(
        financial_year, advisory_record, advice_records, llp_record, schedule_record
    )

    schedule_map = _schedule_month_map(schedule_record)
    monthly, monthly_diagnostics = _build_monthly(advice_records, schedule_map)
    ioc_diagnostics = _place_interest_on_capital(monthly, llp_record)

    data: dict = {
        "financial_year": fy,
        "firm_name": firm_name,
    }
    if drivers is not None:
        data["drivers"] = drivers
    if advisory_record is not None:
        adv: dict = {}
        if advisory_record.get("schedule_projected_closing_balance") is not None:
            adv["stated_closing_capital"] = advisory_record["schedule_projected_closing_balance"]
        if advisory_record.get("financial_year"):
            adv["financial_year"] = advisory_record["financial_year"]
        adv_instalments = advisory_record.get("schedule_instalments") or []
        adv_grosses = [
            inst.get("gross")
            for inst in adv_instalments
            if isinstance(inst, dict) and inst.get("gross") is not None
        ]
        if adv_grosses:
            adv["schedule_instalments_gross_total"] = sum(adv_grosses)
        if adv:
            data["advisory"] = adv
    if monthly:
        data["monthly"] = monthly
    cohorts = _build_cohorts(schedule_record, fy)
    if cohorts:
        data["cohorts"] = cohorts
    if accounts:
        data["accounts"] = accounts
    if schedule_record is not None and schedule_record.get("ctc_structuring") is not None:
        # Recon-only -- never posted to any journal split (jv_emitter.py
        # never reads this key). See module docstring.
        data["ctc_structuring"] = schedule_record["ctc_structuring"]

    diagnostics = [*monthly_diagnostics, *ioc_diagnostics]
    if diagnostics:
        data["_diagnostics"] = diagnostics

    return data
