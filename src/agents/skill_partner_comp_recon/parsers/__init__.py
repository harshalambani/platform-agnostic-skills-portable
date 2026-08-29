"""
agents.skill_partner_comp_recon.parsers -- Stage 2 placeholders.

Every module in this package exposes a single function:

    parse(path: str, password: str | None = None) -> dict

Every one of them raises NotImplementedError naming the real specimen this
skill needs before that parser can be written -- see each module's
docstring and AGENT.md's "Stage 2" section. This is deliberate: writing
regex/layout logic against an invented, non-PII fixture would produce code
that is confidently wrong against the real document, since these are
free-form PDFs with no public schema. Stage 1 (this PR) accepts the same
data as a structured YAML/JSON input instead -- see skill.yaml and
engine.build_report().
"""
