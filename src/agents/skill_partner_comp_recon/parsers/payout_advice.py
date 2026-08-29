"""
payout_advice.py -- Stage 2 placeholder for the firm's monthly payout
advice (the document a partner receives each month showing remuneration,
share of profit, firm's tax, TDS, capital transferred, and a printed
"Misc Adjustments" line whose sign is inconsistent between months -- see
engine.derive_misc(), which is why this skill always derives that figure
rather than ever reading it off the advice).

Free-form PDF, no published layout, no public schema, and the exact
column set varies month to month at some firms. Writing a layout parser
against an invented fixture would produce code that is confidently wrong
on the real document. Stage 1 (this PR) accepts the same monthly figures
as structured YAML/JSON input instead (see skill.yaml and
engine.build_report()'s `monthly` block). This function is a guarded
placeholder pending a real, de-identified specimen.
"""
from __future__ import annotations


def parse(path: str, password: str | None = None) -> dict:
    """Raises NotImplementedError -- no real monthly payout advice
    specimen has been supplied yet. Do not implement this against an
    invented fixture; get a real (de-identified, if needed) specimen first.
    """
    raise NotImplementedError(
        "payout_advice.parse() is a Stage 2 placeholder: parsing the "
        "firm's monthly payout advice needs a real specimen of that "
        "document before any layout/regex logic can be written against "
        "it. Until then, supply the monthly figures as structured "
        "YAML/JSON input (see skill.yaml and AGENT.md)."
    )
