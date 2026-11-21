"""The statement's own account number, read from a LABELLED line (IMP-08).

Pure text logic shared by the HSBC OCR parse stage and the HDFC reader. It only
accepts a number that is LABELLED ("Account Number", "Account No", "A/C No") on
the line; a bare digit run is never guessed (a customer id, a card number or an
"A/C Open Date" is never taken). When the text carries two different labelled
numbers the result is ambiguous and None is returned -- the caller then falls
back to evidence or stops and asks, it never guesses. A miss is always safe.
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
