"""
agent.py -- MF CAS (CAMS/KFintech Consolidated Account Statement) skill.
DIRECT mode, no LLM, no network.

Architecture is split hard at the PDF boundary, per this skill's design
brief: `_extract_pdf_text` below is the ONLY function in this package that
touches pdfplumber, a password, or the filesystem. Everything downstream --
`parser.parse_cas_text` and `lots.derive_all` -- is pure, takes plain text
lines or already-parsed dataclasses, and is exercised in tests with
synthetic CAS-shaped text only (see tests/test_mf_cas.py). No encrypted PDF
fixture is ever generated or committed.

N1 assumption (stated here, in AGENT.md, and in the PR body): this skill's
output lands as STANDALONE ARTIFACTS ONLY. It does not write into any
.gnucash book, does not extend skill_gnucash_import, and does not
auto-inject anything into the ITR workbook. GnuCash books in this codebase
hold mutual funds only as cash transfers to the AMC (see AGENT.md) --
reconciling that cash-transfer view against this skill's per-lot realised
gains is left as a manual step for v1.
"""
from __future__ import annotations

from pathlib import Path

from agents.bank_common import password as _password

from .excel_writer import write_report_workbook
from .lots import derive_all
from .parser import parse_cas_text


def _extract_pdf_text(pdf_path: str, password: str | None) -> list[str]:
    """The ONLY function in this package that touches pdfplumber or a
    password. Returns plain text lines. Raises ValueError (never leaking the
    password) on a wrong/missing password, and ValueError if no text could
    be extracted at all -- v1 does not fall back to OCR (see AGENT.md
    non-goals); fail loud rather than silently returning an empty report.
    """
    import pdfplumber

    try:
        pdf = pdfplumber.open(pdf_path, password=password or "")
    except Exception as e:
        if _password.is_password_error(e):
            raise ValueError(
                _password.password_error_message(
                    "the CAS delivery password -- for CAMS/KFintech this is usually "
                    "your PAN (upper-case) or date of birth (DDMMYYYY)"
                )
            ) from e
        raise

    lines: list[str] = []
    try:
        for page in pdf.pages:
            text = page.extract_text(x_tolerance=1) or ""
            for line in text.split("\n"):
                stripped = line.strip()
                if stripped:
                    lines.append(stripped)
    finally:
        pdf.close()

    if not lines:
        raise ValueError(
            "No text could be extracted from this PDF. It may be a scanned/image-only "
            "statement -- OCR is not supported in this skill's v1; a text-layer CAS PDF is required."
        )
    return lines


def run(
    cas_path: str,
    output_path: str,
    config_path: str = None,
    model_override: str = None,
    cas_password: str = None,
) -> str:
    """Parse a CAMS/KFintech Consolidated Account Statement PDF, derive FIFO
    realised capital gains per folio+scheme, and write a report workbook
    (Transactions/Holdings/RealisedGains/Exceptions) plus CSV siblings for
    Transactions and RealisedGains to output_path. Returns a text summary.
    """
    input_path = Path(cas_path)
    if not input_path.is_file():
        return f"ERROR: file not found: {cas_path}"

    try:
        lines = _extract_pdf_text(str(input_path), cas_password)
    except ValueError as e:
        return f"ERROR: {e}"

    schemes = parse_cas_text(lines)
    if not schemes:
        return (
            "ERROR: no folio/scheme blocks were recognised in this statement's text. "
            "The PDF extracted text but did not match this skill's expected CAS layout -- "
            "see AGENT.md for the recognised line shapes."
        )

    recons = derive_all(schemes)
    write_report_workbook(schemes, recons, output_path)

    total_txns = sum(len(s.transactions) for s in schemes)
    total_disposal_lots = sum(len(r.disposal_lots) for r in recons)
    breaches = [r for r in recons if not r.units_ok or not r.matched_vs_disposed_ok]
    flagged_lots = sum(1 for r in recons for dl in r.disposal_lots if dl.flags)
    variances = [r for r in recons if r.rta_gain_variance not in (None, 0)
                 and abs(r.rta_gain_variance) > 0.01]

    lines_out = [
        f"MF CAS parsed -- {len(schemes)} folio/scheme block(s), {total_txns} transaction(s).",
        f"  Realised-gain lots derived: {total_disposal_lots} "
        f"({flagged_lots} flagged for review).",
    ]
    if breaches:
        lines_out.append(
            f"  WARNING: units reconciliation breach in {len(breaches)} scheme(s) -- see Exceptions sheet."
        )
    if variances:
        lines_out.append(
            f"  NOTE: RTA-reported realised gain differs from FIFO-derived gain in "
            f"{len(variances)} scheme(s) -- see Exceptions sheet."
        )
    lines_out.append(f"  Workbook: {output_path}")
    return "\n".join(lines_out)
