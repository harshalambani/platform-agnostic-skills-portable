"""
parse_form16.py -- TRACES Form 16 (Part B / Annexure-I) PDF -> Form16Data model
(plan section 6.2).

Both real samples inspected this batch are text-layer TRACES PDFs (Part A
quarterly TDS + Part B Annexure-I salary computation + Form 12BA). Hazards
observed and handled here:
  - one real PDF is password-protected: TRACES convention is password==PAN
    (never stored; passed in by the caller, e.g. from Data/itr/entities.yaml).
    Decryption uses pdfplumber's own `password=` (pdfminer under the hood) --
    not pypdf -- since pdfplumber is already a project dependency and this
    avoids adding a second PDF library just for decryption.
  - text-layer label/value separation: fields are extracted with label-anchored
    regexes tolerant of intervening whitespace/newlines/table cells, not by
    assuming the number sits on the same visual line as its label.
  - one real PDF contains pages from a SECOND certificate/TAN: pages are
    grouped by TAN, the group containing "PART B" + "Annexure - I" is treated
    as the certificate to parse, and any other TAN groups are recorded in
    `extra_certificates` (count + TANs) -- never silently dropped.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber


class Form16ParseError(Exception):
    pass


# TAN format: 4 letters, 5 digits, 1 letter (e.g. MUMC29822C).
_TAN_RE = re.compile(r"\b([A-Z]{4}[0-9]{5}[A-Z])\b")
_PART_B_RE = re.compile(r"PART\s*B", re.IGNORECASE)
_ANNEXURE_I_RE = re.compile(r"Annexure\s*-\s*I\b", re.IGNORECASE)

# Every currency figure on this form is printed with exactly 2 decimal
# places -- requiring the decimal part avoids accidentally matching a bare
# item/sub-item numeral (e.g. the "6." in "6. Income chargeable...") that
# happens to fall within the search window before the real value.
_NUM = r"(\d[\d,]*\.\d{2})"


def _to_float(raw: str) -> float:
    return float(raw.replace(",", ""))


def _find_number(text: str, label_pattern: str, window: int = 400) -> float | None:
    """Find the first number appearing within `window` characters after
    `label_pattern` -- tolerant of the label and value being separated by
    intervening text/whitespace/newlines (jumbled reading order), rather than
    assuming line adjacency."""
    m = re.search(label_pattern + r"[\s\S]{0,%d}?%s" % (window, _NUM), text)
    if not m:
        return None
    return _to_float(m.group(1))


def _find_text(text: str, label_pattern: str, choices: tuple[str, ...], window: int = 200) -> str | None:
    m = re.search(label_pattern + r"[\s\S]{0,%d}?\b(%s)\b" % (window, "|".join(choices)), text, re.IGNORECASE)
    if not m:
        return None
    return m.group(1)


@dataclass
class IdentityCheckResult:
    label: str
    expected: float
    actual: float

    @property
    def ok(self) -> bool:
        return abs(self.expected - self.actual) <= 0.01


@dataclass
class Form16Data:
    certificate_no: str | None = None
    tan: str | None = None
    employee_pan: str | None = None
    assessment_year: str | None = None
    period_from: str | None = None
    period_to: str | None = None
    employer_name: str | None = None
    opted_out_115bac: str | None = None   # "Yes" / "No" as printed on the form

    # 1: Gross salary
    s17_1: float | None = None
    s17_2: float | None = None
    s17_3: float | None = None
    total_1d: float | None = None
    other_employer_1e: float | None = None

    # 2: s.10 exemption breakup
    exempt_10_5: float | None = None
    exempt_10_10: float | None = None
    exempt_10_10aa: float | None = None
    exempt_10_13a: float | None = None
    exempt_10_14: float | None = None
    exempt_10_other: float | None = None
    total_2i: float | None = None

    # 3-6: salary from current employer, s.16, income chargeable
    salary_current_employer_3: float | None = None
    std_deduction_16a: float | None = None
    entertainment_16b: float | None = None
    prof_tax_16c: float | None = None
    total_s16_5: float | None = None
    income_chargeable_6: float | None = None

    # 7-9: other income, gross total income
    other_income_7a: float | None = None
    other_income_7b: float | None = None
    total_other_income_8: float | None = None
    gross_total_income_9: float | None = None

    # 10-11: VI-A
    via_aggregate_11: float | None = None

    # 12-21: tax computation
    taxable_income_12: float | None = None
    tax_on_total_income_13: float | None = None
    rebate_87a_14: float | None = None
    surcharge_15: float | None = None
    cess_16: float | None = None
    tax_payable_17: float | None = None
    relief_89_18: float | None = None
    net_tax_payable_21: float | None = None

    identity_checks: list = field(default_factory=list)          # list[IdentityCheckResult]
    extra_certificates: list = field(default_factory=list)        # list[(cert_no|None, tan)]

    @property
    def identity_ok(self) -> bool:
        return all(c.ok for c in self.identity_checks)


def _page_tan(text: str) -> str | None:
    m = _TAN_RE.search(text)
    return m.group(1) if m else None


def _group_pages_by_certificate(pages_text: list[str]) -> dict[str, list[int]]:
    """Group page indices by TAN. Pages with no TAN of their own (rare) are
    attached to the most recently seen TAN, since annexure/continuation pages
    within one certificate usually still repeat the TAN header."""
    groups: dict[str, list[int]] = {}
    last_tan: str | None = None
    for i, text in enumerate(pages_text):
        tan = _page_tan(text) or last_tan
        if tan is None:
            continue
        last_tan = tan
        groups.setdefault(tan, []).append(i)
    return groups


def _select_part_b_group(groups: dict[str, list[int]], pages_text: list[str]) -> tuple[str, list[int]]:
    for tan, indices in groups.items():
        combined = "\n".join(pages_text[i] for i in indices)
        if _PART_B_RE.search(combined) and _ANNEXURE_I_RE.search(combined):
            return tan, indices
    raise Form16ParseError("no certificate in this PDF contains a PART B / Annexure-I salary computation")


def _extract_certificate_no(text: str) -> str | None:
    m = re.search(r"Certificate\s*No\.?:?\s*([A-Z0-9]{5,15})", text)
    return m.group(1) if m else None


_PAN_RE = r"[A-Z]{5}[0-9]{4}[A-Z]"


def _extract_employee_pan(text: str) -> str | None:
    # The three-column header row prints "PAN of the Deductor / TAN of the
    # Deductor / PAN of the Employee..." as labels, then the three matching
    # values below in the same column order -- match all three positionally
    # rather than nearest-token-after-label (the employer's PAN is a nearer,
    # wrong, match for a naive "value after label" search).
    m = re.search(
        r"PAN of the Deductor\s+TAN of the Deductor\s+PAN of the Employee[^\n]*"
        r"[\s\S]{0,80}?(%s)\s+([A-Z]{4}[0-9]{5}[A-Z])\s+(%s)" % (_PAN_RE, _PAN_RE),
        text,
    )
    if m:
        return m.group(3)
    # Form 12BA fallback: "... Permanent Account Number ... of employee: Name, PAN"
    m = re.search(r"Number of employee:[\s\S]{0,120}?,\s*(%s)" % _PAN_RE, text)
    return m.group(1) if m else None


def _extract_ay(text: str) -> str | None:
    m = re.search(r"Assessment Year[:\s]{0,10}(\d{4}-\d{2})", text)
    return m.group(1) if m else None


def _extract_period(text: str) -> tuple[str | None, str | None]:
    # "Period with the Employer" is a column header; the two dd-Mon-yyyy
    # values that actually fill it can be several lines further down (the
    # intervening CIT(TDS) address wraps across lines) -- match the first
    # two date-shaped tokens after the header rather than assuming adjacency.
    m = re.search(r"Period with the Employer[\s\S]{0,400}?"
                  r"(\d{2}-\w{3}-\d{4})\s+(\d{2}-\w{3}-\d{4})", text)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def _extract_employer_name(text: str) -> str | None:
    m = re.search(r"Name and address of the Employer[^\n]*\n([^\n]+)", text)
    return m.group(1).strip() if m else None


def _identity(label: str, expected: float | None, actual: float | None) -> IdentityCheckResult | None:
    if expected is None or actual is None:
        return None
    return IdentityCheckResult(label=label, expected=expected, actual=actual)


def _parse_part_b(text: str, extra_certificates: list) -> Form16Data:
    data = Form16Data(
        certificate_no=_extract_certificate_no(text),
        tan=_page_tan(text),
        employee_pan=_extract_employee_pan(text),
        assessment_year=_extract_ay(text),
        employer_name=_extract_employer_name(text),
        extra_certificates=extra_certificates,
    )
    data.period_from, data.period_to = _extract_period(text)
    data.opted_out_115bac = _find_text(
        text, r"Whether opting out of taxation u/s 115BAC\(1A\)\?", ("Yes", "No"),
    )

    data.s17_1 = _find_number(text, r"section 17\(1\)")
    data.s17_2 = _find_number(text, r"section 17\(2\)")
    data.s17_3 = _find_number(text, r"section 17\(3\)")
    data.total_1d = _find_number(text, r"\(d\)\s*Total")
    data.other_employer_1e = _find_number(text, r"salary received from other employer")

    # Section 2 (s.10 exemption breakup): item (d) "10(10AA)" wraps its label
    # across lines in a way pdfplumber re-orders (the "(10AA)" continuation
    # can land AFTER the item's own value in reading order -- exactly the
    # "jumbled reading order" hazard flagged in the plan). Anchor on each
    # item's own bullet letter within the section-2 block instead of the
    # full descriptive label text, which is robust to that reordering.
    s2_block_m = re.search(
        r"exempt under section 10([\s\S]*?)Total amount of salary received from current employer", text,
    )
    s2 = s2_block_m.group(1) if s2_block_m else ""
    data.exempt_10_5 = _find_number(s2, r"\(a\)")
    data.exempt_10_10 = _find_number(s2, r"\(b\)")
    data.exempt_10_10aa = _find_number(s2, r"\(d\)")
    data.exempt_10_13a = _find_number(s2, r"\(e\)")
    data.exempt_10_14 = _find_number(s2, r"\(f\)")
    data.exempt_10_other = _find_number(text, r"other exemption under section 10")
    data.total_2i = _find_number(text, r"Total amount of exemption claimed under section 10")

    data.salary_current_employer_3 = _find_number(
        text, r"Total amount of salary received from current employer"
    )
    data.std_deduction_16a = _find_number(text, r"Standard deduction under section 16")
    data.entertainment_16b = _find_number(text, r"Entertainment allowance under section 16")
    data.prof_tax_16c = _find_number(text, r"Tax on employment under section 16")
    data.total_s16_5 = _find_number(text, r"Total amount of deductions under section 16")
    data.income_chargeable_6 = _find_number(text, r'Income chargeable under the head\s*"?Salaries"?')

    data.other_income_7a = _find_number(text, r"house property reported by\s*\n?\(a\)")
    data.other_income_7b = _find_number(text, r"Income under the head Other Sources")
    data.total_other_income_8 = _find_number(text, r"Total amount of other income reported by the employee")
    data.gross_total_income_9 = _find_number(text, r"Gross total income")

    data.via_aggregate_11 = _find_number(text, r"Aggregate of deductible amount under Chapter VI-A")

    data.taxable_income_12 = _find_number(text, r"Total taxable income")
    data.tax_on_total_income_13 = _find_number(text, r"Tax on total income")
    data.rebate_87a_14 = _find_number(text, r"Rebate under section 87A")
    data.surcharge_15 = _find_number(text, r"Surcharge, wherever applicable")
    data.cess_16 = _find_number(text, r"Health and education cess")
    data.tax_payable_17 = _find_number(text, r"Tax payable\s*\(13")
    data.relief_89_18 = _find_number(text, r"Relief under section 89")
    data.net_tax_payable_21 = _find_number(text, r"Net tax payable")

    checks = [
        _identity("1(d) = 17(1)+17(2)+17(3)",
                  (data.s17_1 or 0.0) + (data.s17_2 or 0.0) + (data.s17_3 or 0.0), data.total_1d),
        _identity("3 = 1(d)-2(i)",
                  (data.total_1d or 0.0) - (data.total_2i or 0.0), data.salary_current_employer_3),
        _identity("6 = 3+1(e)-5",
                  (data.salary_current_employer_3 or 0.0) + (data.other_employer_1e or 0.0)
                  - (data.total_s16_5 or 0.0), data.income_chargeable_6),
        _identity("8 = 7(a)+7(b)",
                  (data.other_income_7a or 0.0) + (data.other_income_7b or 0.0), data.total_other_income_8),
        _identity("9 = 6+8", (data.income_chargeable_6 or 0.0) + (data.total_other_income_8 or 0.0),
                  data.gross_total_income_9),
        _identity("17 = 13+15+16-14",
                  (data.tax_on_total_income_13 or 0.0) + (data.surcharge_15 or 0.0)
                  + (data.cess_16 or 0.0) - (data.rebate_87a_14 or 0.0), data.tax_payable_17),
        _identity("21 = 17-18",
                  (data.tax_payable_17 or 0.0) - (data.relief_89_18 or 0.0), data.net_tax_payable_21),
    ]
    data.identity_checks = [c for c in checks if c is not None]
    return data


def parse_form16(pdf_path: str | Path, pan: str | None = None) -> Form16Data:
    """Parse a TRACES Form 16 PDF. `pan` is the decryption password when the
    PDF is encrypted (TRACES convention: password == employee PAN) -- never
    stored, only used for this one open() call. Raises Form16ParseError on a
    wrong password or when no certificate in the PDF contains a Part B."""
    p = Path(pdf_path)
    try:
        pdf = pdfplumber.open(str(p), password=pan or "")
    except Exception as e:  # pdfplumber wraps pdfminer's password error
        raise Form16ParseError(f"{p}: could not open (wrong password, or not a valid PDF): {e}") from e

    with pdf:
        pages_text = [pg.extract_text() or "" for pg in pdf.pages]

    groups = _group_pages_by_certificate(pages_text)
    selected_tan, selected_indices = _select_part_b_group(groups, pages_text)

    extras = [(_extract_certificate_no("\n".join(pages_text[i] for i in idxs)), tan)
              for tan, idxs in groups.items() if tan != selected_tan]

    combined = "\n".join(pages_text[i] for i in selected_indices)
    return _parse_part_b(combined, extras)
