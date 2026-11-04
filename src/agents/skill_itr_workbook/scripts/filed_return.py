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


def parse_filed_json(path: str | Path, entity_key: str) -> FiledReturn:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    root = data["ITR"]
    form = next(k for k in root if k.startswith("ITR"))
    itr = root[form]

    regime, regime_note = _extract_regime(itr)
    fr = FiledReturn(
        entity_key=entity_key, source_format="json", source_path=str(path),
        itr_form=form, regime=regime, regime_note=regime_note,
        heads=_extract_heads(itr),
        schedule_os=_extract_schedule_os(itr),
        schedule_cg=_extract_schedule_cg(itr),
        via_claimed=_extract_via(itr),
        schedule_al=_extract_al(itr),
        bf_losses=_extract_bf_losses(itr),
        interest_234=_extract_234(itr),
        taxes_paid=_extract_taxes_paid(itr),
        tax_liability=_extract_tax_liability(itr),
    )
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
