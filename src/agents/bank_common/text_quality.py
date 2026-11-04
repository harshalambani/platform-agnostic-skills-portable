"""
agents/bank_common/text_quality.py — garbled-PDF-text-layer detection shared
by bank statement PDF parsers.

Moved verbatim from ``skill_hdfc/agent.py`` (``_text_layer_usable``), which
remains the reference implementation. The structural "anchor" check is
parameterized so each bank can supply its own required-phrase regexes
(HDFC's originals were ``r'\\bdate\\b'`` and ``r'narration'``) while the
junk/printable-ratio heuristics stay identical for every bank.
"""
from __future__ import annotations

import re
from typing import Sequence

_CID_RE = re.compile(r'\(cid:\d+\)')


def text_layer_usable(full_text: str, anchor_patterns: Sequence[str] = ()) -> bool:
    """Heuristic: is this extracted PDF text layer usable for regex parsing?

    Unusable if: dense "(cid:NN)" junk (custom font encoding producing
    placeholders instead of real characters), a low ASCII-printable ratio, or
    any of ``anchor_patterns`` fails to match anywhere in the document — a
    caller passes the structural phrases (e.g. "date", "narration") whose
    absence indicates a font-encoding problem that would make its
    transaction-line regexes fail silently.
    """
    if not full_text or not full_text.strip():
        return False
    cid_hits = len(_CID_RE.findall(full_text))
    if cid_hits > 0 and cid_hits / max(len(full_text), 1) > 0.005:
        return False
    printable = sum(1 for c in full_text if c.isprintable() and ord(c) < 128)
    if printable / len(full_text) < 0.85:
        return False
    for pattern in anchor_patterns:
        if re.search(pattern, full_text, re.IGNORECASE) is None:
            return False
    return True
