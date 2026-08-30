"""
payment_schedule.py -- Stage 2 placeholder for the firm's incentive
payment-schedule PDF (the document that lists each cohort's instalment
dates and gross amounts, independent of the payout advices that later pay
them).

Free-form PDF, no published layout, no public schema. Writing a layout
parser against an invented fixture would produce code that is confidently
wrong on the real document. Stage 1 (this PR) accepts the same instalment
data as structured YAML/JSON input instead (see skill.yaml and
engine.build_report()'s `cohorts` block). This function is a guarded
placeholder pending a real, de-identified specimen.
"""
from __future__ import annotations


def parse(path: str, password: str | None = None) -> dict:
    """Raises NotImplementedError -- no real incentive payment-schedule PDF
    specimen has been supplied yet. Do not implement this against an
    invented fixture; get a real (de-identified, if needed) specimen first.
    """
    raise NotImplementedError(
        "payment_schedule.parse() is a Stage 2 placeholder: parsing the "
        "firm's incentive payment-schedule PDF needs a real specimen of "
        "that document before any layout/regex logic can be written "
        "against it. Until then, supply the cohort instalment data as "
        "structured YAML/JSON input (see skill.yaml and AGENT.md)."
    )
