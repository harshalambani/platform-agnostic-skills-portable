"""
precheck.py -- UI-17: WARN-only checks on the document set, run before the
reconciliation.

Every function here only READS the parsed records and returns note strings.
Nothing is dropped, edited or reordered: a warning tells the user what looks
wrong so they can fix the document set and run again; it never changes what
the reconciliation consumes. An in-year set with one document per month and
the right kind of advisory returns no notes at all.
"""
from __future__ import annotations

import email
import email.policy
import re
import tempfile
from pathlib import Path

_MONTH_NAMES = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
                "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_MONTH_NAME_TO_NUM = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11,
    "december": 12,
}

PREFIX = "WARNING (pre-run check): "


def fy_months(fy: str | None) -> list[str]:
    """['2025-04', ..., '2026-03'] for a 'YYYY-YY' financial year, else []."""
    try:
        start = int(str(fy).split("-")[0])
    except (ValueError, IndexError, TypeError):
        return []
    return [f"{start + (1 if m < 4 else 0):04d}-{m:02d}" for m in
            (4, 5, 6, 7, 8, 9, 10, 11, 12, 1, 2, 3)]


def _label(month: str) -> str:
    y, m = month.split("-")
    return f"{_MONTH_NAMES[int(m) - 1]} {y}"


def record_month(rec: dict) -> str | None:
    """Canonical 'YYYY-MM' of one raw payout record (Class A or B), or None.
    Read-only twin of mapper._normalise_advice_record's month logic."""
    if "doc_class" not in rec:
        m = rec.get("month")
        return str(m) if m else None
    name, year = rec.get("month"), rec.get("year")
    num = _MONTH_NAME_TO_NUM.get(str(name).lower()) if name else None
    return f"{int(year):04d}-{num:02d}" if (num and year is not None) else None


def _source(rec: dict) -> str:
    return rec.get("source_name") or "<unknown file>"


def _kind(rec: dict) -> str:
    return "payout statement" if "doc_class" in rec else "monthly certificate"


def check_payout_set(advice_records: list[dict], fy: str | None,
                     schedule_months: set[str] | None = None) -> list[str]:
    """Out-of-year months, the same month given twice, and months missing from
    the year (with whether the payment schedule covers them)."""
    notes: list[str] = []
    in_year = fy_months(fy)
    seen: dict[str, list[dict]] = {}
    for rec in advice_records:
        month = record_month(rec)
        if month:
            seen.setdefault(month, []).append(rec)

    if in_year:
        for month in sorted(seen):
            if month not in in_year:
                names = ", ".join(_source(r) for r in seen[month])
                notes.append(
                    f"{PREFIX}{_label(month)} is outside FY {fy} "
                    f"(Apr {in_year[0][:4]} - Mar {in_year[-1][:4]}): {names}. "
                    "Nothing was removed; check that this is the right year's document.")

    for month in sorted(seen):
        recs = seen[month]
        if len(recs) > 1:
            what = "; ".join(f"{_source(r)} ({_kind(r)})" for r in recs)
            notes.append(
                f"{PREFIX}{_label(month)} was given {len(recs)} times: {what}. "
                "A month must come from ONE document, so this run will be refused "
                "(nothing is written) until the extra one is removed.")

    if in_year and seen:
        missing = [m for m in in_year if m not in seen]
        if missing:
            txt = ", ".join(_label(m) for m in missing)
            if schedule_months is None:
                cover = "no payment schedule was supplied to cover them"
            else:
                covered = [m for m in missing if m in schedule_months]
                if len(covered) == len(missing):
                    cover = "the payment schedule covers all of them"
                elif covered:
                    cover = ("the payment schedule covers only "
                             + ", ".join(_label(m) for m in covered))
                else:
                    cover = "the payment schedule does not cover them either"
            notes.append(f"{PREFIX}no payout document for {txt} of FY {fy}; {cover}.")
    return notes


# ---------------------------------------------------------------------------
# Advisory slot: Compensation summary vs target-compensation letter
# ---------------------------------------------------------------------------
# TODO(UI-17, needs a real specimen): the two titles below are quoted from the
# printed documents as described in the build brief, but NO fixture of a real
# target-compensation letter exists in this repo, so the exact text, case and
# page it prints its title on are unverified. The check is therefore
# conservative -- it fires only on a positive title match and stays silent on
# anything else -- and tests/test_partner_form_checks.py guards the marker.
ADVISORY_TITLE_UNVERIFIED = True

_SUMMARY_TITLE_RE = re.compile(r"compensation\s+summary\s*:?\s*year\s+ended\s+31\s+march", re.I)
_TARGET_TITLE_RE = re.compile(r"target\s+compensation\s+advice", re.I)


def classify_advisory_text(text: str) -> str:
    """'compensation_summary', 'target_comp_letter' or 'unknown'."""
    if _SUMMARY_TITLE_RE.search(text or ""):
        return "compensation_summary"
    if _TARGET_TITLE_RE.search(text or ""):
        return "target_comp_letter"
    return "unknown"


def check_advisory_slot(text: str | None) -> list[str]:
    if text and classify_advisory_text(text) == "target_comp_letter":
        return [
            f"{PREFIX}the advisory slot holds what looks like a \"Target "
            "compensation advice\" letter. This slot wants the \"Compensation "
            "summary : Year ended 31 March\" document; the figures may not match."
        ]
    return []


def read_first_page_text(path: str, password: str | None) -> str | None:
    """Text of the first page, or None on any problem (the real parser will
    report an unreadable file by itself)."""
    try:
        import pdfplumber  # noqa: PLC0415
        with pdfplumber.open(path, password=password or None) as pdf:
            return (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    except Exception:
        return None


# ---------------------------------------------------------------------------
# LLP statement arriving as the .eml it came in
# ---------------------------------------------------------------------------

def extract_pdf_from_eml(eml_path: str) -> str:
    """Write the first PDF attachment of a .eml to a temp file; return its path.
    Raises ValueError when the message has no PDF attachment."""
    with open(eml_path, "rb") as fh:
        msg = email.message_from_binary_file(fh, policy=email.policy.default)
    for part in msg.walk():
        name = (part.get_filename() or "").lower()
        if part.get_content_type() == "application/pdf" or name.endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                tmp = Path(tempfile.mkdtemp(prefix="pask-eml-")) / (
                    Path(part.get_filename() or "attachment.pdf").name)
                tmp.write_bytes(payload)
                return str(tmp)
    raise ValueError(f"no PDF attachment found in {eml_path}")


# ---------------------------------------------------------------------------
# Default journal CSV paths
# ---------------------------------------------------------------------------

def default_journal_paths(entity: str, fy: str, output_path: str) -> tuple[str, str]:
    """(monthly journal, accrual journal) CSV paths named for entity and FY,
    beside the workbook."""
    def slug(s: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]+", "-", str(s)).strip("-") or "entity"
    base = Path(output_path).parent
    stem = f"{slug(entity)}-FY{slug(fy)}"
    return (str(base / f"{stem}-partner-journal.csv"),
            str(base / f"{stem}-partner-accrual-journal.csv"))
