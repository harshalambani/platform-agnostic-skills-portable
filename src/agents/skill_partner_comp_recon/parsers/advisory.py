"""
advisory.py -- Stage 2 placeholder for the firm's annual Compensation
Advisory letter (the document that states the partner's remuneration,
share-of-profit, and capital-contribution terms for a financial year, and
projects a closing capital balance).

This is a free-form PDF with no published layout, no public schema, and no
two firms format it alike -- writing a layout parser against an invented
fixture would produce code that is confidently wrong on the real letter.
Stage 1 (this PR) accepts the same figures as structured YAML/JSON input
instead (see skill.yaml's `input_path` and engine.build_report()'s
`advisory` block). This function is a guarded placeholder pending a real,
de-identified specimen.
"""
from __future__ import annotations


def parse(path: str, password: str | None = None) -> dict:
    """Raises NotImplementedError -- no real Compensation Advisory letter
    specimen has been supplied yet. Do not implement this against an
    invented fixture; get a real (de-identified, if needed) specimen first.
    """
    raise NotImplementedError(
        "advisory.parse() is a Stage 2 placeholder: parsing the firm's "
        "Compensation Advisory letter needs a real specimen of that "
        "document before any layout/regex logic can be written against it. "
        "Until then, supply the Advisory's figures as structured YAML/JSON "
        "input (see skill.yaml and AGENT.md)."
    )
