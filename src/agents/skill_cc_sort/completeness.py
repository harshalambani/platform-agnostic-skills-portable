"""CC-01: completeness check for sorted credit-card statement PDFs.

After the sort, page 1 of each sorted PDF is read, a statement period (or,
failing that, a statement date) is extracted, and every card folder is checked
for gaps in the chosen financial year. Pure logic plus one thin PDF reader
(`read_page1_text`), so tests can supply the text directly.
"""
from __future__ import annotations

import calendar
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

M = r'(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*'

# (name, regex, strptime format). Tried in order. Two groups = (start, end).
PERIOD_PATTERNS = [
    ("Axis", r'(\d\d/\d\d/\d{4}) - (\d\d/\d\d/\d{4}) \d\d/\d\d/\d{4} \d\d/\d\d/\d{4}', "%d/%m/%Y"),
    ("HDFC", r'Billing Period (\d{1,2} ' + M + r', \d{4}) - (\d{1,2} ' + M + r', \d{4})', "%d %b, %Y"),
    ("HSBC", r'(\d\d [A-Z]{3} \d{4}) To (\d\d [A-Z]{3} \d{4})', "%d %b %Y"),
    ("ICICI", r'Statement period : (' + M + r' \d{1,2}, \d{4}) to (' + M + r' \d{1,2}, \d{4})', "%B %d, %Y"),
    ("YES", r'(\d\d/\d\d/\d{4}) To (\d\d/\d\d/\d{4})', "%d/%m/%Y"),
]
# One group = the statement date.
DATE_PATTERNS = [
    ("HDFC-old", r'Statement Date:(\d\d/\d\d/\d{4})', "%d/%m/%Y"),
    ("SBM", r'Statement Date : (\d{1,2}-' + M + r'-\d{4})', "%d-%b-%Y"),
]

TOLERANCE_DAYS = 3  # next period must start 1..3 days after the previous end
UNKNOWN_FOLDER = "Unknown-Unknown"


def parse_date(text: str, fmt: str) -> date | None:
    """Parse with the stated format, always trying both %b and %B."""
    alts = [fmt]
    if "%b" in fmt:
        alts.append(fmt.replace("%b", "%B"))
    elif "%B" in fmt:
        alts.append(fmt.replace("%B", "%b"))
    for f in alts:
        try:
            return datetime.strptime(text.strip(), f).date()
        except ValueError:
            continue
    return None


@dataclass(frozen=True)
class StatementInfo:
    kind: str            # "period" or "date"
    start: date | None   # period start (None for date-only)
    end: date            # period end, or the statement date
    pattern: str = ""


def classify_text(text: str | None) -> StatementInfo | None:
    """Period first (patterns a-e in order), then statement-date-only (f-g).
    None means "not a statement"."""
    if not text:
        return None
    for name, rx, fmt in PERIOD_PATTERNS:
        for m in re.finditer(rx, text):
            s, e = parse_date(m.group(1), fmt), parse_date(m.group(2), fmt)
            if s and e and s <= e:
                return StatementInfo("period", s, e, name)
    for name, rx, fmt in DATE_PATTERNS:
        for m in re.finditer(rx, text):
            d = parse_date(m.group(1), fmt)
            if d:
                return StatementInfo("date", None, d, name)
    return None


def read_page1_text(pdf_path) -> str | None:
    """Page 1 text via pdfplumber; None when it cannot be read."""
    try:
        import pdfplumber
        with pdfplumber.open(str(pdf_path)) as pdf:
            if not pdf.pages:
                return None
            return pdf.pages[0].extract_text() or ""
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Financial year
# ---------------------------------------------------------------------------

def default_financial_year(today: date | None = None) -> str:
    """The last COMPLETED FY, e.g. "2025-26" for any day from 1 Apr 2026."""
    today = today or date.today()
    start = today.year - 1 if today >= date(today.year, 4, 1) else today.year - 2
    return f"{start}-{(start + 1) % 100:02d}"


def fy_bounds(fy: str) -> tuple[date, date]:
    m = re.fullmatch(r"\s*(\d{4})-(\d{2})\s*", fy or "")
    if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
        raise ValueError(f"financial year {fy!r} is not in 'YYYY-YY' form (e.g. 2025-26)")
    y = int(m.group(1))
    return date(y, 4, 1), date(y + 1, 3, 31)


_QUARTERS = {"Q1": (4, 6), "Q2": (7, 9), "Q3": (10, 12), "Q4": (1, 3)}


def resolve_period(spec: str | None, today: date | None = None) -> tuple[date, date, str]:
    """Turn the user's period choice into (start_date, end_date, label).

    ""                         -> last completed FY
    "2025-26"                  -> the FY
    "2025-26 Q3"               -> a quarter of that FY (Q1 Apr-Jun ... Q4 Jan-Mar)
    "Oct 2025" / "October 2025"-> a single month
    "2025-07-15 to 2025-09-20" -> custom range (ISO dates, from <= to)
    """
    s = (spec or "").strip()
    if not s:
        s = default_financial_year(today)
    m = re.fullmatch(r"(\d{4}-\d\d)\s+(Q[1-4])", s, flags=re.I)
    if m:
        fs, _fe = fy_bounds(m.group(1))
        q = m.group(2).upper()
        a, b = _QUARTERS[q]
        ya = fs.year if a >= 4 else fs.year + 1
        yb = fs.year if b >= 4 else fs.year + 1
        return (date(ya, a, 1), date(yb, b, calendar.monthrange(yb, b)[1]),
                f"FY{m.group(1)} {q}")
    m = re.fullmatch(r"(\d{4}-\d\d-\d\d)\s+to\s+(\d{4}-\d\d-\d\d)", s, flags=re.I)
    if m:
        try:
            a = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            b = datetime.strptime(m.group(2), "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"custom range {s!r} has an impossible date") from None
        if a > b:
            raise ValueError(f"custom range is back to front: from {a.isoformat()} is after "
                             f"to {b.isoformat()}. Swap them yourself; they are not swapped for you.")
        return a, b, f"{_fmt(a)} - {_fmt(b)}"
    mo = parse_date("1 " + s, "%d %b %Y")
    if mo:
        return mo, date(mo.year, mo.month, calendar.monthrange(mo.year, mo.month)[1]), mo.strftime("%b %Y")
    if re.fullmatch(r"\d{4}-\d\d", s):
        a, b = fy_bounds(s)
        return a, b, f"FY{s}"
    raise ValueError(f"period {s!r} not understood. Use a financial year (2025-26), a quarter "
                     "(2025-26 Q3), a month (Oct 2025) or a range (2025-07-15 to 2025-09-20).")


def _month_back(d: date) -> date:
    y, mo = (d.year, d.month - 1) if d.month > 1 else (d.year - 1, 12)
    return date(y, mo, min(d.day, calendar.monthrange(y, mo)[1]))


def _fmt(d: date) -> str:
    return f"{d.day} {d.strftime('%b %Y')}"


# ---------------------------------------------------------------------------
# Per-card coverage
# ---------------------------------------------------------------------------

@dataclass
class CardCoverage:
    card: str
    covered: tuple[date, date] | None = None
    gaps: list[str] = field(default_factory=list)
    statements: int = 0
    outside_fy: int = 0
    duplicates: list[str] = field(default_factory=list)      # "B duplicates A"
    not_statements: list[str] = field(default_factory=list)  # file names

    def line(self) -> str:
        span = f"{_fmt(self.covered[0])} - {_fmt(self.covered[1])}" if self.covered else "nothing in this FY"
        gaps = "; ".join(self.gaps) if self.gaps else "none"
        return f"{self.card}: covered {span}; gaps: {gaps}"


def _interval(info: StatementInfo) -> tuple[date, date]:
    if info.kind == "period":
        return info.start, info.end
    # statement date only: it covers the month ending on that date
    return _month_back(info.end) + timedelta(days=1), info.end


def check_card(card: str, entries: list[tuple[str, StatementInfo | None]],
               fy_start: date, fy_end: date) -> CardCoverage:
    """entries: (file name, StatementInfo or None for "not a statement")."""
    cov = CardCoverage(card=card)
    seen: dict[tuple, str] = {}
    intervals: list[tuple[date, date]] = []
    last_date_only: date | None = None  # latest date-only statement date in the FY
    for name, info in sorted(entries, key=lambda e: e[0]):
        if info is None:
            cov.not_statements.append(name)
            continue
        key = (info.kind, info.start, info.end)
        if key in seen:
            cov.duplicates.append(f"{name} duplicates {seen[key]}")
            continue
        seen[key] = name
        s, e = _interval(info)
        if e < fy_start or s > fy_end:
            cov.outside_fy += 1
            continue
        intervals.append((s, e))
        if info.kind == "date":
            last_date_only = max(last_date_only or e, e)
        cov.statements += 1
    intervals.sort()
    if not intervals:
        cov.gaps.append(f"no statement covers {_fmt(fy_start)} - {_fmt(fy_end)} "
                        "- card opened/closed, or statement missing?")
        return cov
    cov.covered = (max(intervals[0][0], fy_start), min(max(e for _s, e in intervals), fy_end))

    # FY boundaries behave like a neighbouring statement under the same rule.
    if (intervals[0][0] - fy_start).days > TOLERANCE_DAYS - 1:
        cov.gaps.append(f"no statement covers {_fmt(fy_start)} - "
                        f"{_fmt(intervals[0][0] - timedelta(days=1))} "
                        "- card opened/closed, or statement missing?")
    reach = intervals[0][1]
    for s, e in intervals[1:]:
        if (s - reach).days > TOLERANCE_DAYS:
            cov.gaps.append(f"no statement covers {_fmt(reach + timedelta(days=1))} - "
                            f"{_fmt(s - timedelta(days=1))}")
        reach = max(reach, e)
    # A date-only statement dated in the FY's last month is the last one the
    # FY can contain: the rest of that month is billed on the next statement.
    if last_date_only is not None and reach == last_date_only:
        reach = max(reach, date(last_date_only.year, last_date_only.month,
                                calendar.monthrange(last_date_only.year, last_date_only.month)[1]))
    if (fy_end - reach).days > TOLERANCE_DAYS - 1:
        cov.gaps.append(f"no statement covers {_fmt(reach + timedelta(days=1))} - "
                        f"{_fmt(fy_end)} - card opened/closed, or statement missing?")
    return cov


# ---------------------------------------------------------------------------
# Whole run
# ---------------------------------------------------------------------------

@dataclass
class CompletenessReport:
    financial_year: str
    cards: list[CardCoverage]
    unknown_files: list[str]
    failed_decryption: list[str]

    @property
    def issues(self) -> list[str]:
        out = [f"could not decrypt: {n}" for n in self.failed_decryption]
        out += [f"sorted to {UNKNOWN_FOLDER} (bank/card not recognised): {n}"
                for n in self.unknown_files]
        for c in self.cards:
            out += [f"{c.card}: {g}" for g in c.gaps]
        return out

    def text(self) -> str:
        lines = [f"Completeness check, {self.financial_year}:"]
        for c in self.cards:
            lines.append("  " + c.line())
        nots = [(c.card, n) for c in self.cards for n in c.not_statements]
        if nots:
            lines.append("Not a statement (no period or statement date found; never counted):")
            lines += [f"  {card}: {n}" for card, n in nots]
        dups = [(c.card, d) for c in self.cards for d in c.duplicates]
        if dups:
            lines.append("Duplicates (same period; counted once):")
            lines += [f"  {card}: {d}" for card, d in dups]
        return "\n".join(lines)


def check_folder(decrypted_dir, fy: str, failed_decryption=(), reader=None) -> CompletenessReport:
    """Read every PDF under <decrypted_dir>/<card folder>/ and check coverage."""
    reader = reader or read_page1_text
    fy_start, fy_end, label = resolve_period(fy)
    root = Path(decrypted_dir)
    cards: list[CardCoverage] = []
    unknown: list[str] = []
    if root.is_dir():
        for folder in sorted(p for p in root.iterdir() if p.is_dir()):
            pdfs = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".pdf")
            if folder.name == UNKNOWN_FOLDER:
                unknown += [p.name for p in pdfs]   # never counts toward any card
                continue
            entries = [(p.name, classify_text(reader(p))) for p in pdfs]
            cards.append(check_card(folder.name, entries, fy_start, fy_end))
    return CompletenessReport(label, cards, unknown, list(failed_decryption))
