"""
feedback.py — Phase D: advisory AIS portal-feedback SUGGESTIONS derived from
the deltas surfaced by reconcile.py / reconcile_26as.py / reconcile_books.py.

*** ADVISORY ONLY. NOTHING IN THIS MODULE SUBMITS ANYTHING. ***

This module never talks to the AIS portal, never files anything, and never
claims a discrepancy is definitively wrong. It produces a list of
FeedbackSuggestion objects for a human (a CA) to read, judge, and act on --
or not -- themselves, directly on the income-tax portal. Where the right
call is genuinely ambiguous (which is most of the time -- a mismatch could
be an AIS rollup quirk, a timing difference, a real error, or a portal
artifact), the suggestion is the conservative "Review - no automated
suggestion" fallback rather than a guess at one of the six standard portal
options. Confidence is capped at "medium" everywhere in this module --
"high" is never emitted, because no purely mechanical delta check earns
that level of certainty about a taxpayer's return.

Pure functions, no I/O: suggest_feedback() takes already-built reconciliation
report objects and returns a list of FeedbackSuggestion. The tools/agent
layer decides what (if anything) a human does with the output.
"""
from __future__ import annotations

from dataclasses import dataclass

from agents.skill_ais_reconcile.reconcile import AisRecoReport, ElementReco
from agents.skill_ais_reconcile.reconcile_26as import Ais26asReport
from agents.skill_ais_reconcile.reconcile_books import AisBooksReport

# The exact standard AIS portal feedback vocabulary. Any suggested_action
# this module emits is either one of these six strings verbatim, or the
# literal conservative fallback below -- never a paraphrase, never a new
# string.
FEEDBACK_INFORMATION_CORRECT = "Information is correct"
FEEDBACK_NOT_FULLY_CORRECT = "Information is not fully correct"
FEEDBACK_OTHER_PAN_YEAR = "Information relates to other PAN/year"
FEEDBACK_NOT_TAXABLE = "Income is not taxable"
FEEDBACK_DUPLICATE = "Information is duplicate / included in other information"
FEEDBACK_DENIED = "Information is denied"
FEEDBACK_REVIEW = "Review - no automated suggestion"

_CONFIDENCE_ORDER = {"medium": 0, "low": 1}


@dataclass
class FeedbackSuggestion:
    section_key: str
    category: str
    info_src_id: str | None
    delta_context: str
    suggested_action: str
    rationale: str
    confidence: str  # "low" / "medium" -- never "high"


def _sort_key(s: FeedbackSuggestion):
    return (_CONFIDENCE_ORDER.get(s.confidence, 99), s.section_key, s.category,
            s.info_src_id or "", s.suggested_action)


def _dedupe_key(s: FeedbackSuggestion):
    return (s.section_key, s.category, s.info_src_id, s.suggested_action)


def _internal_suggestions(report: AisRecoReport) -> list[FeedbackSuggestion]:
    out: list[FeedbackSuggestion] = []
    for el in report.elements:
        if el.flag_derivation:
            out.append(FeedbackSuggestion(
                section_key=el.section_key, category=el.category,
                info_src_id=el.info_src_id,
                delta_context="AIS applied a derivation adjustment to the reported amount",
                suggested_action=FEEDBACK_NOT_FULLY_CORRECT,
                rationale=(
                    "AIS has already applied feedback-driven derivation to this "
                    "element (the Derived amount differs from the originally "
                    "Reported amount) -- verify the derived figure against your "
                    "own source documents before relying on it."
                ),
                confidence="low",
            ))
        if el.flag_detail_mismatch:
            out.append(FeedbackSuggestion(
                section_key=el.section_key, category=el.category,
                info_src_id=el.info_src_id,
                delta_context="l1 detail sum disagrees with the l2 reported amount",
                suggested_action=FEEDBACK_REVIEW,
                rationale=(
                    "The underlying transaction-level detail total does not match "
                    "AIS's own reported (l2) figure for this element. This can be "
                    "an AIS rollup quirk (e.g. a coarser reporting grain) or a "
                    "genuine discrepancy -- manual review needed before choosing a "
                    "portal action."
                ),
                confidence="low",
            ))
    return out


def _books_suggestions(report: AisRecoReport, books: AisBooksReport | None) -> list[FeedbackSuggestion]:
    if books is None:
        return []
    out: list[FeedbackSuggestion] = []

    derivation_flagged_categories = {
        el.category for el in report.elements
        if el.section_key == "tdsTcs" and el.flag_derivation
    }

    for category in books.ais_income_not_in_books:
        out.append(FeedbackSuggestion(
            section_key="tdsTcs", category=category, info_src_id=None,
            delta_context=f"AIS reports {category} income with no matching books posting",
            suggested_action=FEEDBACK_REVIEW,
            rationale=(
                f"AIS reports {category} income with no matching books entry -- "
                "verify whether a posting is missing or the AIS entry belongs to "
                "another PAN/year."
            ),
            confidence="medium",
        ))
        if category in derivation_flagged_categories:
            out.append(FeedbackSuggestion(
                section_key="tdsTcs", category=category, info_src_id=None,
                delta_context=f"{category} income unbooked AND independently flagged for derivation",
                suggested_action=FEEDBACK_DUPLICATE,
                rationale=(
                    f"{category} income is both absent from the books and already "
                    "flagged for an AIS-internal derivation mismatch -- this MAY "
                    "indicate the same income is being double-reported elsewhere "
                    "in AIS; confirm before selecting this option."
                ),
                confidence="low",
            ))

    for category in books.books_income_not_in_ais:
        out.append(FeedbackSuggestion(
            section_key="tdsTcs", category=category, info_src_id=None,
            delta_context=f"Books show {category} income not present in AIS",
            suggested_action=FEEDBACK_REVIEW,
            rationale=(
                "income booked but absent from AIS -- ensure it is declared in "
                "the return."
            ),
            confidence="medium",
        ))

    return out


def _as26_suggestions(as26: Ais26asReport | None) -> list[FeedbackSuggestion]:
    if as26 is None:
        return []
    if not (as26.flag_aggregate_mismatch or as26.flagged_categories or as26.flagged_quarters):
        return []

    out: list[FeedbackSuggestion] = []
    categories = list(as26.flagged_categories)
    if not categories:
        # Aggregate and/or quarter-level mismatch with no category
        # granularity available (tds_sections wasn't supplied) -- still
        # surface ONE suggestion so the discrepancy isn't silently dropped.
        categories = ["aggregate"]

    for category in categories:
        out.append(FeedbackSuggestion(
            section_key="tdsTcs", category=category, info_src_id=None,
            delta_context=f"AIS TDS credit differs from 26AS for {category}",
            suggested_action=FEEDBACK_REVIEW,
            rationale=(
                f"AIS TDS credit differs from 26AS for {category} -- reconcile "
                "before claiming credit."
            ),
            confidence="medium",
        ))
    return out


def suggest_feedback(internal: AisRecoReport, books: AisBooksReport | None = None,
                      as26: Ais26asReport | None = None) -> list[FeedbackSuggestion]:
    """Build advisory (never auto-submitted) portal-feedback suggestions from
    the internal AIS reconciliation, and optionally the books and/or 26AS
    tie-outs. Pure function -- no I/O, no portal interaction of any kind."""
    all_suggestions = (
        _internal_suggestions(internal)
        + _books_suggestions(internal, books)
        + _as26_suggestions(as26)
    )

    deduped: dict[tuple, FeedbackSuggestion] = {}
    for s in all_suggestions:
        deduped.setdefault(_dedupe_key(s), s)

    return sorted(deduped.values(), key=_sort_key)
