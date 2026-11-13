"""
filed_return.py -- parses the ACTUALLY FILED ITR return (ground truth, per
the SESSION spec, better than the CA working Excels) into a comparable model
for diffing against our own workbook output (see outlier_report.py).

JSON path: reads the income-tax e-filing utility's own JSON schema directly
(ITR-2/ITR-3 style, confirmed against real AY2025-26 filed returns in
Data/ITRFiled/). Nothing here is a guess -- every field path was verified
against a real filed JSON before being wired in.

PDF fallback: pdfplumber, label-anchored text extraction. Any field not
confidently found is recorded as NOT_EXTRACTED rather than guessed -- per
spec: "flag any field not confidently extracted as NOT-EXTRACTED (never
guess)."
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

NOT_EXTRACTED = "NOT-EXTRACTED"


def _get(d, *path, default=None):
    cur = d
    for key in path:
        if cur is None:
            return default
        if isinstance(key, int):
            if not isinstance(cur, list) or key >= len(cur):
                return default
            cur = cur[key]
        else:
            if not isinstance(cur, dict) or key not in cur:
                return default
            cur = cur[key]
    return cur if cur is not None else default


@dataclass
class FiledReturn:
    entity_key: str
    source_format: str  # "json" | "pdf"
    source_path: str
    itr_form: str = NOT_EXTRACTED
    regime: str = NOT_EXTRACTED
    regime_note: str = ""
    heads: dict = field(default_factory=dict)
    schedule_os: dict = field(default_factory=dict)
    schedule_cg: dict = field(default_factory=dict)
    via_claimed: dict = field(default_factory=dict)
    schedule_al: dict = field(default_factory=dict)
    bf_losses: dict = field(default_factory=dict)
    interest_234: dict = field(default_factory=dict)
    taxes_paid: dict = field(default_factory=dict)
    tax_liability: dict = field(default_factory=dict)
    not_extracted_fields: list = field(default_factory=list)
    # ITR-21 additions (the fields above keep their meaning). Everything the
    # return tie-out reads that is not in the fields above lives here; anything
    # that could not be read goes in return_not_extracted, never guessed.
    pan: str = NOT_EXTRACTED
    assessment_year: str = NOT_EXTRACTED   # ITR[form][Form_<form>][AssessmentYear], a string e.g. "2026"
    salary: dict = field(default_factory=dict)
    tds_rows: list = field(default_factory=list)      # one dict per TDS row (see _tds_row)
    tds_totals: dict = field(default_factory=dict)
    tcs_rows: list = field(default_factory=list)
    tcs_total: object = NOT_EXTRACTED
    tds_rows_extracted: bool = False       # False for a PDF: rows come back NOT-EXTRACTED
    return_not_extracted: list = field(default_factory=list)


SUPPORTED_FORMS = ("ITR1", "ITR2", "ITR3")


class UnsupportedReturnForm(ValueError):
    """The return is not an ITR-1, ITR-2 or ITR-3 JSON."""


def _extract_regime(itr: dict) -> tuple[str, str]:
    """ITR-1/2 style filings carry a direct Y/N flag. ITR-3/4 style filings
    instead carry Form 10-IEA opt-out bookkeeping fields
    (OptOutNewTaxRegime_Method etc.) whose mere presence is NOT proof of an
    old-regime opt-out -- 10-IEA is also filed to opt back IN to the new
    regime after a prior opt-out. The one figure AY2025-26 pins
    unambiguously either way is the salary standard deduction u/s 16(ia):
    75000 is new-regime-only, 50000 is old-regime-only."""
    opt = _get(itr, "PartA_GEN1", "FilingStatus", "OptOutNewTaxRegime")
    if opt in ("Y", "N"):
        return ("old" if opt == "Y" else "new"), f"OptOutNewTaxRegime={opt}"

    std_ded = _get(itr, "ScheduleS", "DeductionUnderSection16ia")
    if std_ded == 75000:
        return "new", "no direct OptOutNewTaxRegime flag; ScheduleS.DeductionUnderSection16ia=75000 (new-regime-only amount for AY2025-26)"
    if std_ded == 50000:
        return "old", "no direct OptOutNewTaxRegime flag; ScheduleS.DeductionUnderSection16ia=50000 (old-regime-only amount for AY2025-26)"
    return NOT_EXTRACTED, "no OptOutNewTaxRegime flag and no salary standard deduction to cross-check"


def _extract_heads(itr: dict) -> dict:
    ti = _get(itr, "PartB-TI", default={})
    return {
        "Salaries": ti.get("Salaries", NOT_EXTRACTED),
        "IncomeFromHP": ti.get("IncomeFromHP", NOT_EXTRACTED),
        "CapitalGainsTotal": _get(ti, "CapGain", "TotalCapGains", default=NOT_EXTRACTED),
        "OtherSourcesTotal": _get(ti, "IncFromOS", "TotIncFromOS", default=NOT_EXTRACTED),
        "GrossTotalIncome": ti.get("GrossTotalIncome", NOT_EXTRACTED),
        "DeductionsVIA": ti.get("DeductionsUnderScheduleVIA", NOT_EXTRACTED),
        "TotalIncome": ti.get("TotalIncome", NOT_EXTRACTED),
    }


def _extract_schedule_os(itr: dict) -> dict:
    block = _get(itr, "ScheduleOS", "IncOthThanOwnRaceHorse")
    if block is None:
        return {}
    return {
        "interest_sb": block.get("IntrstFrmSavingBank", NOT_EXTRACTED),
        "interest_td": block.get("IntrstFrmTermDeposit", NOT_EXTRACTED),
        "interest_refund": block.get("IntrstFrmIncmTaxRefund", NOT_EXTRACTED),
        "interest_other": block.get("IntrstFrmOthers", NOT_EXTRACTED),
        "dividend_gross": block.get("DividendGross", NOT_EXTRACTED),
        **{out_key: block[in_key] for in_key, out_key in (
            ("DividendOthThan22e", "dividend_oth_than_22e"), ("Dividend22e", "dividend_22e"))
           if in_key in block},
    }


def _extract_schedule_cg(itr: dict) -> dict:
    cg = _get(itr, "PartB-TI", "CapGain")
    if cg is None:
        return {}
    return {
        "short_term_total": _get(cg, "ShortTerm", "TotalShortTerm", default=NOT_EXTRACTED),
        "long_term_total": _get(cg, "LongTerm", "TotalLongTerm", default=NOT_EXTRACTED),
        "total": cg.get("TotalCapGains", NOT_EXTRACTED),
    }


def _extract_via(itr: dict) -> dict:
    via = _get(itr, "ScheduleVIA", "DeductUndChapVIA")
    return dict(via) if via is not None else {}


def _extract_al(itr: dict) -> dict:
    # Schedule AL is only required in the filed return above a total-income
    # threshold; absence here is expected for most entities, not a gap.
    al = itr.get("ScheduleAL")
    return dict(al) if al is not None else {"status": "not_present_in_filed_return"}


def _extract_bf_losses(itr: dict) -> dict:
    cfl = itr.get("ScheduleCFL")
    return dict(cfl) if cfl is not None else {}


def _extract_234(itr: dict) -> dict:
    ip = _get(itr, "PartB_TTI", "ComputationOfTaxLiability", "IntrstPay")
    return dict(ip) if ip is not None else {}


def _extract_taxes_paid(itr: dict) -> dict:
    tp = _get(itr, "PartB_TTI", "TaxPaid", "TaxesPaid")
    return dict(tp) if tp is not None else {}


def _extract_tax_liability(itr: dict) -> dict:
    ctl = _get(itr, "PartB_TTI", "ComputationOfTaxLiability")
    out = {} if ctl is None else {
        "gross_tax_liability": ctl.get("GrossTaxLiability", NOT_EXTRACTED),
        "rebate_87a": ctl.get("Rebate87A", NOT_EXTRACTED),
        "surcharge": ctl.get("TotalSurcharge", NOT_EXTRACTED),
        "cess": ctl.get("EducationCess", NOT_EXTRACTED),
        "net_tax_liability": ctl.get("NetTaxLiability", NOT_EXTRACTED),
    }
    refund = _get(itr, "PartB_TTI", "Refund", "RefundDue")
    bal_payable = _get(itr, "PartB_TTI", "TaxPaid", "BalTaxPayable")
    out["refund_due"] = refund if refund is not None else NOT_EXTRACTED
    out["balance_tax_payable"] = bal_payable if bal_payable is not None else NOT_EXTRACTED
    return out


def _collect_not_extracted(fr: FiledReturn) -> None:
    for section_name, section in (
        ("heads", fr.heads), ("schedule_os", fr.schedule_os),
        ("schedule_cg", fr.schedule_cg), ("tax_liability", fr.tax_liability),
    ):
        for k, v in section.items():
            if v == NOT_EXTRACTED:
                fr.not_extracted_fields.append(f"{section_name}.{k}")
    if fr.regime == NOT_EXTRACTED:
        fr.not_extracted_fields.append("regime")


# --- ITR-21: ITR-1 support, PAN, salary, TDS/TCS rows ---------------------
# Only the paths below are wired; each was supplied as verified. Any other
# path is NOT read -- the field comes back NOT-EXTRACTED and is listed in
# return_not_extracted, never guessed.

_ITR1_NATURE = {"SAV": "interest_sb", "IFD": "interest_td", "TAX": "interest_refund",
                "DIV": "dividend_gross", "OTH": "other_income"}


def _as_list(value) -> list:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _num_or_ne(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else NOT_EXTRACTED


def _extract_itr1_heads(itr: dict) -> dict:
    inc = _get(itr, "ITR1_IncomeDeductions", default={})
    # Salaries / HP / capital gains / deductions have no verified ITR-1 path:
    # NOT-EXTRACTED here (salary detail lives in FiledReturn.salary).
    return {
        "Salaries": NOT_EXTRACTED,
        "IncomeFromHP": NOT_EXTRACTED,
        "CapitalGainsTotal": NOT_EXTRACTED,
        "OtherSourcesTotal": inc.get("IncomeOthSrc", NOT_EXTRACTED),
        "GrossTotalIncome": inc.get("GrossTotIncome", NOT_EXTRACTED),
        "DeductionsVIA": NOT_EXTRACTED,
        "TotalIncome": inc.get("TotalIncome", NOT_EXTRACTED),
    }


def _extract_itr1_os(itr: dict, fr: FiledReturn) -> dict:
    rows = _get(itr, "ITR1_IncomeDeductions", "OthersInc", "OthersIncDtlsOthSrc")
    if rows is None:
        fr.return_not_extracted.append("ITR1_IncomeDeductions.OthersInc.OthersIncDtlsOthSrc")
        return {}
    out = {"interest_sb": 0, "interest_td": 0, "interest_refund": 0,
           "dividend_gross": 0, "other_income": 0}
    for row in _as_list(rows):
        code = row.get("OthSrcNatureDesc")
        amt = row.get("OthSrcOthAmount")
        key = _ITR1_NATURE.get(code)
        if key is None or not isinstance(amt, (int, float)):
            fr.return_not_extracted.append(
                f"ITR-1 other-source row with nature code {code!r} (amount {amt!r}) is not a "
                "recognised code (SAV, IFD, TAX, DIV, OTH): left out of every head")
            continue
        out[key] += amt
        if code == "DIV":
            rng = _get(row, "DividendInc", "DateRange")
            if isinstance(rng, dict):
                q = out.setdefault("dividend_by_period", {})
                for k, v in rng.items():
                    q[k] = q.get(k, 0) + (v if isinstance(v, (int, float)) else 0)
    return out


def _tds_row(kind, tan, name, section, head, credit_owner, gross, claimed, brought_fwd=None,
             carried_fwd=None, deducted=None, other_hands=None) -> dict:
    return {"kind": kind, "tan": tan if tan else NOT_EXTRACTED, "name": name or "",
            "section": section or "", "head": head or "", "credit_owner": credit_owner,
            "gross": gross, "claimed": claimed, "deducted": deducted,
            "brought_fwd": brought_fwd, "carried_fwd": carried_fwd, "other_hands": other_hands or {}}


def _extract_tds_itr1(itr: dict, fr: FiledReturn) -> None:
    for key in itr:
        if str(key).startswith("TDSonSalar"):
            fr.return_not_extracted.append(
                f"ITR-1 salary TDS ({key}): layout not verified, not read -- salary TDS is not compared")
    other = itr.get("TDSonOthThanSals")
    if other is None:
        fr.return_not_extracted.append("TDSonOthThanSals")
    else:
        for row in _as_list(other.get("TDSonOthThanSal")):
            ded = row.get("EmployerOrDeductorOrCollectDetl", {}) or {}
            claimed = _num_or_ne(row.get("ClaimOutOfTotTDSOnAmtPaid"))
            if claimed == NOT_EXTRACTED:
                fr.return_not_extracted.append(
                    f"ITR-1 TDS row {ded.get('TAN')}: ClaimOutOfTotTDSOnAmtPaid")
            fr.tds_rows.append(_tds_row(
                "other", ded.get("TAN"), ded.get("EmployerOrDeductorOrCollecterName"),
                row.get("TDSSection"), "OS", "S", row.get("AmtForTaxDeduct"), claimed,
                deducted=row.get("TotTDSOnAmtPaid")))
        fr.tds_totals["other"] = other.get("TotalTDSonOthThanSals", NOT_EXTRACTED)
    fr.tds_totals["tds3"] = _get(itr, "ScheduleTDS3Dtls", "TotalTDS3Details", default=NOT_EXTRACTED)
    fr.tcs_total = _get(itr, "ScheduleTCS", "TotalSchTCS", default=NOT_EXTRACTED)
    fr.tds_rows_extracted = True


def _extract_tds_itr23(itr: dict, fr: FiledReturn) -> None:
    s1 = itr.get("ScheduleTDS1")
    if s1 is not None:
        for row in _as_list(s1.get("TDSonSalary")):
            ded = row.get("EmployerOrDeductorOrCollectDetl", {}) or {}
            claimed = _num_or_ne(row.get("TotalTDSSal"))
            if claimed == NOT_EXTRACTED:
                fr.return_not_extracted.append(f"ScheduleTDS1 row {ded.get('TAN')}: TotalTDSSal")
            fr.tds_rows.append(_tds_row(
                "salary", ded.get("TAN"), ded.get("EmployerOrDeductorOrCollecterName"),
                "192", "salary", "S", row.get("IncChrgSal"), claimed))
        fr.tds_totals["salary"] = s1.get("TotalTDSonSalaries", NOT_EXTRACTED)
    s2 = itr.get("ScheduleTDS2")
    if s2 is not None:
        for row in _as_list(s2.get("TDSOthThanSalaryDtls")):
            cr = row.get("TaxDeductCreditDtls", {}) or {}
            tan = row.get("TANOfDeductor")
            claimed = _num_or_ne(cr.get("TaxClaimedOwnHands"))
            if claimed == NOT_EXTRACTED:
                fr.return_not_extracted.append(f"ScheduleTDS2 row {tan}: TaxClaimedOwnHands")
            fr.tds_rows.append(_tds_row(
                "other", tan, "", row.get("TDSSection"), row.get("HeadOfIncome"),
                row.get("TDSCreditName"), row.get("GrossAmount"), claimed,
                brought_fwd=row.get("BroughtFwdTDSAmt"), carried_fwd=row.get("AmtCarriedFwd"),
                deducted=cr.get("TaxDeductedOwnHands"),
                other_hands={k: cr[k] for k in ("TaxDeductedIncome", "TaxClaimedIncome",
                                                "TaxDeductedTDS", "TaxClaimedTDS") if k in cr}))
        fr.tds_totals["other"] = s2.get("TotalTDSonOthThanSals", NOT_EXTRACTED)
    fr.tds_totals["tds3"] = _get(itr, "ScheduleTDS3", "TotalTDS3OnOthThanSal", default=NOT_EXTRACTED)
    tcs = itr.get("ScheduleTCS")
    if tcs is not None:
        for row in _as_list(tcs.get("TCS")):
            fr.tcs_rows.append({"tan": row.get("EmployerOrDeductorOrCollectTAN", NOT_EXTRACTED),
                                "credit_owner": row.get("TCSCreditOwner"),
                                "carried_fwd": row.get("AmtCarriedFwd")})
        fr.tcs_total = tcs.get("TotalSchTCS", NOT_EXTRACTED)
    fr.tds_rows_extracted = True


def _extract_itr21(form: str, itr: dict, fr: FiledReturn) -> None:
    """Fill the ITR-21 fields of `fr` from the return (JSON only)."""
    if form == "ITR1":
        fr.pan = _get(itr, "PersonalInfo", "PAN", default=NOT_EXTRACTED)
        inc = _get(itr, "ITR1_IncomeDeductions", default={})
        fr.salary = {"gross_salary": inc.get("GrossSalary", NOT_EXTRACTED),
                     "salary": inc.get("Salary", NOT_EXTRACTED),
                     "net_salary": inc.get("NetSalary", NOT_EXTRACTED)}
        fr.schedule_os = _extract_itr1_os(itr, fr)
        fr.taxes_paid = _get(itr, "TaxPaid", "TaxesPaid", default={}) or {}
        fr.tax_liability = {"balance_tax_payable": _get(itr, "TaxPaid", "BalTaxPayable", default=NOT_EXTRACTED)}
        _extract_tds_itr1(itr, fr)
    else:
        fr.pan = _get(itr, "PartA_GEN1", "PersonalInfo", "PAN", default=NOT_EXTRACTED)
        sched_s = itr.get("ScheduleS")
        head = _get(itr, "PartB-TI", "Salaries", default=NOT_EXTRACTED)
        gross = _get(sched_s, "TotalGrossSalary", default=NOT_EXTRACTED)
        if gross == NOT_EXTRACTED and sched_s is None and head == 0:
            gross = 0          # no Schedule S and a nil salary head: no salary declared
        fr.salary = {
            "gross_salary": gross,
            "income_under_head": _get(sched_s, "TotIncUnderHeadSalaries", default=NOT_EXTRACTED),
            "salaries_head": head,
        }
        _extract_tds_itr23(itr, fr)
    if fr.pan == NOT_EXTRACTED:
        fr.return_not_extracted.append("PAN")
    if fr.salary.get("gross_salary", NOT_EXTRACTED) == NOT_EXTRACTED:
        fr.return_not_extracted.append("gross salary")
    ay = _get(itr, "Form_" + form, "AssessmentYear")
    if isinstance(ay, (str, int)) and not isinstance(ay, bool) and str(ay).strip():
        fr.assessment_year = str(ay).strip()
    else:
        fr.return_not_extracted.append("assessment year")


def parse_filed_json(path: str | Path, entity_key: str) -> FiledReturn:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    root = data["ITR"]
    forms = [k for k in root if str(k).startswith("ITR")]
    form = forms[0] if forms else ""
    if form not in SUPPORTED_FORMS:
        raise UnsupportedReturnForm(
            f"unsupported return form {form or '(none found)'!r}: only ITR1, ITR2 and ITR3 "
            "JSON returns are read")
    itr = root[form]

    regime, regime_note = _extract_regime(itr)
    fr = FiledReturn(
        entity_key=entity_key, source_format="json", source_path=str(path),
        itr_form=form, regime=regime, regime_note=regime_note,
        heads=_extract_itr1_heads(itr) if form == "ITR1" else _extract_heads(itr),
        schedule_os={} if form == "ITR1" else _extract_schedule_os(itr),
        schedule_cg=_extract_schedule_cg(itr),
        via_claimed=_extract_via(itr),
        schedule_al=_extract_al(itr),
        bf_losses=_extract_bf_losses(itr),
        interest_234=_extract_234(itr),
        taxes_paid=_extract_taxes_paid(itr),
        tax_liability=_extract_tax_liability(itr),
    )
    _extract_itr21(form, itr, fr)
    _collect_not_extracted(fr)
    return fr


# --- PDF fallback -----------------------------------------------------

_PDF_LABELS = {
    "heads.GrossTotalIncome": ["Gross Total Income", "Gross total income"],
    "heads.TotalIncome": ["Total Income", "Total income"],
    "tax_liability.net_tax_liability": ["Net tax liability", "Total Tax, Fee and Interest Payable"],
    "tax_liability.refund_due": ["Refund"],
}

_NUM_RE = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _find_amount(lines: list[str], labels: list[str]):
    # Anchor at line start (ignoring leading whitespace) so e.g. "Total
    # Income" doesn't match inside a "Gross Total Income" line.
    for line in lines:
        stripped = line.strip()
        for label in labels:
            if stripped.lower().startswith(label.lower()):
                nums = _NUM_RE.findall(line)
                if nums:
                    try:
                        return float(nums[-1].replace(",", ""))
                    except ValueError:
                        continue
    return None


def parse_filed_pdf(path: str | Path, entity_key: str) -> FiledReturn:
    lines: list[str] = []
    with pdfplumber.open(str(path)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            lines.extend(text.splitlines())

    heads = {}
    for key in ("GrossTotalIncome", "TotalIncome"):
        val = _find_amount(lines, _PDF_LABELS[f"heads.{key}"])
        heads[key] = val if val is not None else NOT_EXTRACTED

    tax_liability = {}
    for key in ("net_tax_liability", "refund_due"):
        val = _find_amount(lines, _PDF_LABELS[f"tax_liability.{key}"])
        tax_liability[key] = val if val is not None else NOT_EXTRACTED

    fr = FiledReturn(
        entity_key=entity_key, source_format="pdf", source_path=str(path),
        itr_form=NOT_EXTRACTED,
        regime=NOT_EXTRACTED,
        regime_note="PDF fallback -- regime election is not reliably label-anchored in the printed ITR-V/acknowledgement; left NOT-EXTRACTED rather than guessed",
        heads=heads, tax_liability=tax_liability,
    )
    _collect_not_extracted(fr)
    return fr


def parse_filed_return(path: str | Path, entity_key: str) -> FiledReturn:
    path = Path(path)
    if path.suffix.lower() == ".json":
        return parse_filed_json(path, entity_key)
    if path.suffix.lower() == ".pdf":
        return parse_filed_pdf(path, entity_key)
    raise ValueError(f"Unsupported filed-return file type: {path.suffix}")
