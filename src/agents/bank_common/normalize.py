"""
agents/bank_common/normalize.py — shared amount/date normalization.

Moved verbatim from ``skill_hdfc/agent.py`` (``_clean_amount`` /
``_normalise_date``), which remains the reference implementation.
"""
from __future__ import annotations

import re

_ISO_DATE_RE = re.compile(r'^\d{4}-\d{2}-\d{2}$')


def clean_amount(s) -> str:
    """Strip thousands separators; render an exact-zero amount as "" (blank
    cell), matching the canonical schema's convention for "no amount"."""
    s = str(s).replace(",", "").strip()
    try:
        return "" if float(s) == 0.0 else s
    except ValueError:
        return s


def normalise_date(d) -> str:
    """Normalize a DD/MM/YY(YY) date string to ISO YYYY-MM-DD; pass through
    values already in ISO form or that don't match the DD/MM/YY(YY) shape."""
    d = str(d).strip()
    if _ISO_DATE_RE.match(d):
        return d  # already canonical ISO
    parts = d.split("/")
    if len(parts) != 3:
        return d
    dd, mm, yy = parts[0], parts[1], parts[2]
    if len(yy) == 2:
        yy = "20" + yy
    return yy + "-" + mm + "-" + dd
