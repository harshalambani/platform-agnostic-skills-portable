"""
llp_statement.py -- Stage 2 placeholder for the LLP's own annual partner
current/capital account statement (an internal accounting-system export
showing the partner's capital account movements independent of the
Compensation Advisory's projection).

Free-form export, no published layout, no public schema. Writing a layout
parser against an invented fixture would produce code that is confidently
wrong on the real document. Stage 1 (this PR) accepts the same closing
capital figures as structured YAML/JSON input instead (see skill.yaml and
engine.build_report()'s `external.return_closing_capital` /
`advisory.stated_closing_capital` fields). This function is a guarded
placeholder pending a real, de-identified specimen.
"""
from __future__ import annotations


def parse(path: str, password: str | None = None) -> dict:
    """Raises NotImplementedError -- no real LLP partner account statement
    specimen has been supplied yet. Do not implement this against an
    invented fixture; get a real (de-identified, if needed) specimen first.
    """
    raise NotImplementedError(
        "llp_statement.parse() is a Stage 2 placeholder: parsing the LLP's "
        "own partner current/capital account statement needs a real "
        "specimen of that document before any layout/regex logic can be "
        "written against it. Until then, supply the closing capital "
        "figures as structured YAML/JSON input (see skill.yaml and "
        "AGENT.md)."
    )
