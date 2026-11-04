"""IMP-08: read the statement's own account number (page-1 header text).

Pure text logic, shared by the OCR parse stage (``scripts/parse_tsv.py``,
which runs as a subprocess) and ``HSBCSkill.parse``. It only accepts a number
that is LABELLED ("Account Number", "Account No", "A/C No") on the line; a
bare digit run is never guessed. When page 1 carries two different labelled
numbers the result is ambiguous and None is returned -- the pipeline then
decides by evidence or stops and asks, it never guesses.

The label wording is unvalidated against a real statement specimen; a miss
is safe (None -> IMP-08's evidence / stop-and-ask path).
"""
from __future__ import annotations

import re

MIN_DIGITS = 6

_LABELLED = re.compile(
    r"(?:a\s*/\s*c|acct\.?|account)\s*(?:no\.?|number|num\.?|#)\s*[:.\-]?\s*"
    r"([0-9][0-9 \-]{4,28}[0-9])",
    re.IGNORECASE,
)


def digits_only(s: str | None) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def find_account_number(lines) -> str | None:
    """Digits-only account number from header text lines, or None."""
    found: list[str] = []
    for line in lines or []:
        for m in _LABELLED.finditer(str(line)):
            d = digits_only(m.group(1))
            if len(d) >= MIN_DIGITS and d not in found:
                found.append(d)
    return found[0] if len(found) == 1 else None
