"""
agents/bank_common/normalize.py — shared amount/date normalization.

Moved verbatim from ``skill_hdfc/agent.py`` (``_clean_amount`` /
``_normalise_date``), which remains the reference implementation. Extended
for BoB (P2): a trailing Cr/Dr balance suffix (e.g. "1,57,950.00Cr") and a
"-"-separated DD-MM-YY(YY) date form, both common on BoB statements but
absent from HDFC's.
"""
from __future__ import annotations

import re

_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')
_CR_DR_SUFFIX_RE = re.compile(r'(Cr|Dr)$', re.IGNORECASE)

# ICICI-specific: month abbreviation -> number, for the "DD,Mon,YYYY" date shape
# (e.g. "01,Apr,2024") used throughout ICICI's XLS statement downloads.
MONTH_ABBR = {
    'jan': '01', 'feb': '02', 'mar': '03', 'apr': '04',
    'may': '05', 'jun': '06', 'jul': '07', 'aug': '08',
    'sep': '09', 'oct': '10', 'nov': '11', 'dec': '12',
}


def clean_amount(s, blank_zero: bool = True) -> str:
    """Strip thousands separators and a trailing Cr/Dr suffix.

    ``blank_zero`` (default True, HDFC's original convention) renders an
    exact-zero amount as "" (blank cell) -- the canonical schema's
    convention for "no amount". Pass ``blank_zero=False`` to keep the
    zero-amount string as-is instead (BoB's canonical mapper applies its own
    blank/zero convention on top of this primitive).
    """
    s = str(s).replace(",", "").strip()
    s = _CR_DR_SUFFIX_RE.sub("", s).strip()
    try:
        return "" if (blank_zero and float(s) == 0.0) else s
    except ValueError:
        return s


def normalise_date(d) -> str:
    """Normalize a DD/MM/YY(YY) or DD-MM-YY(YY) date string to ISO
    YYYY-MM-DD; pass through values already in ISO form or that don't match
    either shape."""
    d = str(d).strip()
    if _ISO_DATE_RE.match(d):
        return d  # already canonical ISO
    sep = "/" if "/" in d else "-" if "-" in d else None
    if sep is None:
        return d
    parts = d.split(sep)
    if len(parts) != 3:
        return d
    dd, mm, yy = parts[0], parts[1], parts[2]
    if len(yy) == 2:
        yy = "20" + yy
    return yy + "-" + mm + "-" + dd


def parse_comma_month_date(d) -> str | None:
    """Parse ICICI's "DD,Mon,YYYY" date shape (e.g. "01,Apr,2024") to ISO
    YYYY-MM-DD. Returns None if the shape or month abbreviation doesn't
    match -- callers decide how to log/report that."""
    if not d or not isinstance(d, str):
        return None
    d = d.strip().strip('"')
    if not d:
        return None
    parts = d.split(',')
    if len(parts) != 3:
        return None
    day, month_abbr, year = parts[0].strip(), parts[1].strip().lower(), parts[2].strip()
    month = MONTH_ABBR.get(month_abbr)
    if not month:
        return None
    return f"{year}-{month}-{day.zfill(2)}"
