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

from agents.period_picker import (  # noqa: F401  (re-exported for callers and tests)
    default_financial_year, fy_bounds, resolve_period,
)

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
# Period (shared picker: agents/period_picker.py)
# ---------------------------------------------------------------------------

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
    same_month: list[str] = field(default_factory=list)      # listed, never a gap

    def line(self) -> str:
        span = f"{_fmt(self.covered[0])} - {_fmt(self.covered[1])}" if self.covered else "nothing in this FY"
        gaps = "; ".join(self.gaps) if self.gaps else "none"
        return f"{self.card}: covered {span}; gaps: {gaps}"


def _interval(info: StatementInfo) -> tuple[date, date]:
    if info.kind == "period":
        return info.start, info.end
    # statement date only: it covers the month ending on that date
    return _month_back(info.end) + timedelta(days=1), info.end


def _months(start: date, end: date) -> list[tuple[int, int]]:
    out, y, m = [], start.year, start.month
    while (y, m) <= (end.year, end.month):
        out.append((y, m))
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


def _ym(ym: tuple[int, int]) -> str:
    return date(ym[0], ym[1], 1).strftime("%b %Y")


def _check_by_month(cov: CardCoverage, kept, start: date, end: date) -> CardCoverage:
    """Cards that only print a statement date: one statement is expected in each
    calendar month of the range. The date may move within its month (10th, then
    15th) -- that is not a gap. Two in one month are listed, not a gap."""
    needed = _months(start, end)
    by_month: dict[tuple[int, int], list[tuple[str, date]]] = {}
    for name, info in kept:
        ym = (info.end.year, info.end.month)
        if ym in needed:
            by_month.setdefault(ym, []).append((name, info.end))
        else:
            cov.outside_fy += 1
    cov.statements = sum(len(v) for v in by_month.values())
    for ym, items in sorted(by_month.items()):
        if len(items) > 1:
            cov.same_month.append(f"{_ym(ym)}: " + ", ".join(n for n, _d in items))
    present = [ym for ym in needed if ym in by_month]
    if not present:
        cov.gaps.append(f"no statement covers {_fmt(start)} - {_fmt(end)} "
                        "- card opened/closed, or statement missing?")
        return cov
    cov.covered = (max(start, date(present[0][0], present[0][1], 1)),
                   min(end, date(present[-1][0], present[-1][1],
                                 calendar.monthrange(*present[-1])[1])))
    i = 0
    while i < len(needed):
        if needed[i] in by_month:
            i += 1
            continue
        j = i
        while j + 1 < len(needed) and needed[j + 1] not in by_month:
            j += 1
        span = _ym(needed[i]) if i == j else f"{_ym(needed[i])} - {_ym(needed[j])}"
        edge = i == 0 or j == len(needed) - 1
        cov.gaps.append(f"no statement dated in {span}"
                        + (" - card opened/closed, or statement missing?" if edge else ""))
        i = j + 1
    return cov


def check_card(card: str, entries: list[tuple[str, StatementInfo | None]],
               fy_start: date, fy_end: date) -> CardCoverage:
    """entries: (file name, StatementInfo or None for "not a statement")."""
    cov = CardCoverage(card=card)
    seen: dict[tuple, str] = {}
    intervals: list[tuple[date, date]] = []
    kept: list[tuple[str, StatementInfo]] = []
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
        kept.append((name, info))
    if kept and all(i.kind == "date" for _n, i in kept):
        return _check_by_month(cov, kept, fy_start, fy_end)
    for _n, info in kept:
        s, e = _interval(info)
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
    results_unreadable: bool = False
    card_folders_found: int = 0

    @property
    def issues(self) -> list[str]:
        out = []
        if self.results_unreadable:
            out.append("could not read the decrypt results - decrypt failures may be "
                       "missing from this report")
        out += [f"could not decrypt: {n}" for n in self.failed_decryption]
        if not self.cards:
            out.append("no card folders found in Decrypted_PDFs_Correct - nothing was checked")
        elif not any(c.statements or c.outside_fy for c in self.cards):
            out.append("no statements found in any card folder - nothing was checked")
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
        sm = [(c.card, d) for c in self.cards for d in c.same_month]
        if sm:
            lines.append("Several statements in one month (listed; not a gap):")
            lines += [f"  {card}: {d}" for card, d in sm]
        dups = [(c.card, d) for c in self.cards for d in c.duplicates]
        if dups:
            lines.append("Duplicates (same period; counted once):")
            lines += [f"  {card}: {d}" for card, d in dups]
        return "\n".join(lines)


def check_folder(decrypted_dir, period, failed_decryption=(), reader=None,
                 results_unreadable: bool = False) -> CompletenessReport:
    """Read every PDF under <decrypted_dir>/<card folder>/ and check coverage.
    period is (start, end, label) from period_picker.resolve_period."""
    reader = reader or read_page1_text
    fy_start, fy_end, label = period
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
    return CompletenessReport(label, cards, unknown, list(failed_decryption),
                              results_unreadable, len(cards))
