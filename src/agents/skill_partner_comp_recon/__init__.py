"""
agents.skill_partner_comp_recon -- Partner Compensation Reconciliation.

Reconciles an LLP partner's remuneration, share of profit, incentive
cohorts, capital contribution, and (where applicable) payroll stream for
one financial year against the Advisory, the bank, Form 26AS, and the
filed return -- reporting agree / variance / cannot-reconcile per
category, never silently treating a missing source as a match.

See AGENT.md for the full design, non-goals, and the Stage 1/Stage 2 split
(this build ships the computation engine and workbook writer against a
structured YAML/JSON input; the PDF parsers under parsers/ are guarded
placeholders pending real specimens).
"""
