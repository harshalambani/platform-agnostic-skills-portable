"""IMP-08: read the statement's own account number.

Pure text logic, shared by the OCR parse stage (``scripts/parse_tsv.py``,
which runs as a subprocess) and ``HSBCSkill.parse``.

Two rules, both safe on a miss (None -> the pipeline decides by evidence or
stops and asks, it never guesses):

* the labelled rule ("Account Number", "Account No", "A/C No" on the line),
  now in ``agents.bank_common.account_number`` and re-exported here;
* BNK-01: the SAVINGS heading rule. A real HSBC statement has, on page 2 and
  again on some later pages, a heading line that STARTS with
  ``SAVINGS <WORD>-<CCY> NNN-NNNNNN-NNN`` (twelve digits, dashed 3-6-3) under a
  "Details of Your ..." heading. Only that line is read, on every page. The
  "Customer Number" of the address block, the Summary table row (OCR garbles
  it) and the dashed numbers inside transaction narrations are never taken.
  Exactly one distinct number gives that number; none, or two different ones,
  gives None. A heading form not seen on a real statement (a current account)
  gives None.
"""
from __future__ import annotations

import re

from agents.bank_common.account_number import (  # noqa: F401 - re-exported
    MIN_DIGITS,
    digits_only,
    find_account_number,
)

_SAVINGS_HEADING = re.compile(
    r"^\s*SAVINGS\s+[A-Za-z]+-[A-Za-z]{3}\s+(\d{3})-(\d{6})-(\d{3})(?!\d)")


def find_savings_heading_number(pages) -> str | None:
    """Digits-only account number from the SAVINGS heading lines of every page.

    ``pages`` is a list (one entry per page) of text lines, or a flat list of
    lines. Returns the number when exactly one distinct number is found.
    """
    found: list[str] = []
    for page in pages or []:
        lines = [page] if isinstance(page, str) else (page or [])
        for line in lines:
            m = _SAVINGS_HEADING.match(str(line))
            if m:
                d = "".join(m.groups())
                if d not in found:
                    found.append(d)
    return found[0] if len(found) == 1 else None
