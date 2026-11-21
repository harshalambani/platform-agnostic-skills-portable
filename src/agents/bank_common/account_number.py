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

_LABEL = re.compile(
    r"(?:a\s*/\s*c|acct\.?|account)\s*(?:no\.?|number|num\.?|#)\s*[:.\-]?\s*",
    re.IGNORECASE,
)
_FIRST = re.compile(r"\d[\d\-]*")
_SHORT = re.compile(r"\d{1,4}")
_SEP = re.compile(r" (\d[\d\-]*)")


def digits_only(s: str | None) -> str:
    return "".join(c for c in (s or "") if c.isdigit())


def _number_after(rest: str) -> str:
    """The labelled number at the start of ``rest``.

    Dashes inside the number are part of it. A single space joins a short group
    (1-4 digits) to the next short group, as in "013 065 123 456" or
    "0130 6500 1234". A separate group after a long number (a date, a cheque
    number, a 3-6-3 narration number) is never joined, and neither is a group
    that is the start of a date or a time.
    """
    m = _FIRST.match(rest)
    if not m:
        return ""
    token = m.group(0).rstrip("-")
    parts = [token]
    pos = m.end()
    while _SHORT.fullmatch(parts[-1]):
        n = _SEP.match(rest, pos)
        if not n or not _SHORT.fullmatch(n.group(1)):
            break
        after = rest[n.end():n.end() + 1]
        if after in ("/", ":", ".", "-"):
            break
        parts.append(n.group(1))
        pos = n.end()
    return digits_only("".join(parts))


def find_account_number(lines) -> str | None:
    """Digits-only account number from header text lines, or None."""
    found: list[str] = []
    for line in lines or []:
        text = str(line)
        for m in _LABEL.finditer(text):
            d = _number_after(text[m.end():])
            if len(d) >= MIN_DIGITS and d not in found:
                found.append(d)
    return found[0] if len(found) == 1 else None
