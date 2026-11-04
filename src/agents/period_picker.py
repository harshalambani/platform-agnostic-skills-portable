"""Shared period picker for skills that report over a date range.

One resolver (`resolve_period`) turns the form's three inputs -- a `period`
select plus `custom_start` / `custom_end` text boxes -- into a single
(start_date, end_date, label). The options come from `period_options`, which
the UI registers as `options_from: "report_periods"`, so the list moves on by
itself every April instead of going stale.

Indian financial year: 1 Apr to 31 Mar. Quarters: Q1 Apr-Jun, Q2 Jul-Sep,
Q3 Oct-Dec, Q4 Jan-Mar.
"""
from __future__ import annotations

import calendar
import re
from datetime import date, datetime

CUSTOM_OPTION = "Custom date range (use dates below)"

_QUARTERS = {"Q1": (4, 6), "Q2": (7, 9), "Q3": (10, 12), "Q4": (1, 3)}


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


def _fy_name(start_year: int) -> str:
    return f"{start_year}-{(start_year + 1) % 100:02d}"


def period_options(today: date | None = None) -> list[str]:
    """Picker choices. The last completed FY comes first (the default), then
    its quarters, then the current FY and the one before, each with quarters,
    and finally the custom range. A single month is a custom range."""
    last = int(default_financial_year(today)[:4])
    out: list[str] = []
    for y in (last, last + 1, last - 1):
        out.append(f"FY {_fy_name(y)}")
        out += [f"FY {_fy_name(y)} {q}" for q in _QUARTERS]
    out.append(CUSTOM_OPTION)
    return out


def _parse_iso(text: str, what: str) -> date:
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"{what} {text!r} is not a real date. Enter it as YYYY-MM-DD.") from None


def resolve_period(period: str | None, custom_start: str = "", custom_end: str = "",
                   today: date | None = None) -> tuple[date, date, str]:
    """Return (start_date, end_date, label) for the chosen period.

    A blank period means the last completed FY. Custom dates are used only when
    the custom option is picked, and then both are required; from after to is
    an error (it is never swapped).
    """
    p = (period or "").strip()
    start, end = (custom_start or "").strip(), (custom_end or "").strip()
    if "custom" in p.lower():
        if not (start and end):
            raise ValueError("'Custom date range' selected but Start/End dates are missing. "
                             "Enter both as YYYY-MM-DD, or pick a FY option.")
        a, b = _parse_iso(start, "Start date"), _parse_iso(end, "End date")
        if a > b:
            raise ValueError(f"Custom range is back to front: start {a.isoformat()} is after end "
                             f"{b.isoformat()}. Fix the dates; they are not swapped for you.")
        return a, b, f"{a.isoformat()} to {b.isoformat()}"
    if not p:
        p = f"FY {default_financial_year(today)}"
    m = re.fullmatch(r"(?:FY\s*)?(\d{4}-\d\d)(?:\s+(Q[1-4]))?", p, flags=re.I)
    if not m:
        raise ValueError(f"Period {p!r} not understood. Pick a financial year or quarter "
                         "from the list, or choose the custom date range.")
    fs, fe = fy_bounds(m.group(1))
    if not m.group(2):
        return fs, fe, f"FY {m.group(1)}"
    q = m.group(2).upper()
    a, b = _QUARTERS[q]
    ya = fs.year if a >= 4 else fs.year + 1
    yb = fs.year if b >= 4 else fs.year + 1
    return date(ya, a, 1), date(yb, b, calendar.monthrange(yb, b)[1]), f"FY {m.group(1)} {q}"


def period_slug(label: str) -> str:
    """Filesystem-safe form of a period label, for file names:
    "FY 2025-26 Q1" -> "FY2025-26-Q1"; "2025-04-01 to 2025-06-30" -> "2025-04-01_to_2025-06-30"."""
    t = re.sub(r"[^A-Za-z0-9._-]+", "-", label.strip().replace(" to ", "_to_"))
    return re.sub(r"-{2,}", "-", t).strip("-").replace("FY-", "FY")
