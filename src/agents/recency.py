"""MAP-31: one recency model shared by saved-rule scoring and history matching.

Recent bookings outweigh old ones. Age is always measured from the STATEMENT
being imported (its last row date), never from today, so importing an old
statement weighs the history of its own time fully. History dated after the
statement is clamped to weight 1.0, never more.

All constants live here, in one place.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Optional

HALF_LIFE_DAYS = 365          # a booking this old counts half (see note: 4 recent must beat 30 old)
RULE_HIGH_MAX_YEARS = 2       # a rule needs a last_date this recent to be 'high'
RULE_MEDIUM_MAX_YEARS = 5     # up to here a rule caps at 'medium'; older is 'low'
HISTORY_OLD_YEARS = 5         # newest supporting booking older than this (before the reference) is "old"

_DATE_FORMATS = ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y")


def parse_date(text) -> Optional[date]:
    """Parse the date shapes this codebase carries; None if it does not parse."""
    if isinstance(text, datetime):
        return text.date()
    if isinstance(text, date):
        return text
    t = str(text or "").strip()
    if not t:
        return None
    t = t[:10] if len(t) >= 10 and t[4:5] in "-/" else t
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(t, fmt).date()
        except ValueError:
            continue
    return None


def decay_weight(age_days: float) -> float:
    """0.5 ** (age / HALF_LIFE_DAYS); anything not older than the reference is 1.0."""
    if age_days <= 0:
        return 1.0
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def weight_at(when, reference: date) -> float:
    """Weight of an event on ``when`` seen from ``reference``; unparseable -> 1.0
    (no date means no decay: the legacy frequency-only behaviour)."""
    d = parse_date(when)
    if d is None:
        return 1.0
    return decay_weight((reference - d).days)


def years_before(ref: date, years: int) -> date:
    try:
        return ref.replace(year=ref.year - years)
    except ValueError:                       # 29 Feb
        return ref.replace(year=ref.year - years, day=28)


def rule_level(score: float) -> str:
    """Score -> label, the generator's long-standing cut-offs."""
    if score > 0.8:
        return "high"
    if score > 0.5:
        return "medium"
    return "low"


_LEVEL_RANK = {"high": 0, "medium": 1, "low": 2, "none": 3}


def rule_confidence(frequency, last_date, reference: date):
    """MAP-31(d): (label, decayed score, old_note) for a saved rule, measured from
    ``reference`` (the statement date). score = frequency x decay(age). The label
    comes from the score, then is capped by age: only a last_date within
    RULE_HIGH_MAX_YEARS can be 'high'; within RULE_MEDIUM_MAX_YEARS at most
    'medium'; older is 'low'. ``old_note`` is the "old rule, last seen YYYY" text
    when the AGE (not the count) is what lowered it, else None. A last_date that
    does not parse returns (None, None, None): the caller leaves the rule alone."""
    last = parse_date(last_date)
    if last is None:
        return None, None, None
    try:
        freq = float(frequency or 0)
    except (TypeError, ValueError):
        freq = 0.0
    score = freq * decay_weight((reference - last).days)
    level = rule_level(score)
    note = None
    if last < years_before(reference, RULE_MEDIUM_MAX_YEARS):
        capped = "low"
    elif last < years_before(reference, RULE_HIGH_MAX_YEARS):
        capped = "medium"
    else:
        capped = "high"
    if _LEVEL_RANK[capped] > _LEVEL_RANK[level]:
        level = capped
    if capped == "low":                       # the AGE alone makes it low
        note = f"old rule, last seen {last.year}"
    return level, score, note


def min_level(a: str, b: str) -> str:
    """The weaker of two labels (never promotes)."""
    return a if _LEVEL_RANK.get(a, 99) >= _LEVEL_RANK.get(b, 99) else b


def newest_history_date(extractor_output) -> Optional[date]:
    """The newest transaction date in the book history being used (extractor
    output: {"mappings": {bank: [ {last_date, dates}, ...]}}). This is the
    fallback reference when the statement has no parseable date. It is NEVER
    today: with no dates at all the answer is None and callers apply no decay."""
    best = None
    for rows in ((extractor_output or {}).get("mappings") or {}).values():
        for m in rows or []:
            for raw in list(m.get("dates") or []) + [m.get("last_date")]:
                d = parse_date(raw)
                if d is not None and (best is None or d > best):
                    best = d
    return best
