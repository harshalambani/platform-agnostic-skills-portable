"""
agent.py — 26AS TDS Journal agent.

The file paths are bound here, into closure tools, so the LLM never has to
pass a path (small local models garble long Windows paths). The model only:
  1. calls build_journals()  — no arguments,
  2. optionally calls apply_overrides(overrides={...}) — account picks only.
The deterministic builder always writes a valid, balanced CSV; the override
step is pure polish.
"""
import sys
from pathlib import Path

from langchain_core.tools import tool

from agents.base_agent import build_agent
from agents.skill_26as_journal import tools as T

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")

# skill_itr_workbook/scripts is not on the package path by default (it runs as
# a stand-alone scripts/ dir, same reason build_tds_journals.py can't import
# `agents` — see that file's SPECIAL_BOOL_FLAG_KEYS comment). Mirrors
# skill_partner_comp_recon/agent.py's and skill_ais_reconcile/agent.py's own
# sys.path insert to reach configs.load_entities()/EntityProfile.
_ITR_SCRIPTS_DIR = str(Path(__file__).parent.parent / "skill_itr_workbook" / "scripts")
if _ITR_SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _ITR_SCRIPTS_DIR)


def _resolve_partner_comp_configured(entity: str, entities_path: str) -> bool:
    """True only when `entity` resolves to an EntityProfile in entities_path
    AND that profile's partner_comp_accounts is non-empty (configs.py: empty
    means "the [partner comp] skill cannot emit a journal for this entity at
    all"). Any failure mode (no entity picked, no entities_path, the file
    missing/unparsable, the entity not found) resolves to False -- "not
    configured, or cannot be determined" -- which keeps this skill's existing
    behaviour (Category C journalled as before, now with a loud double-
    booking warning) rather than silently excluding a TDS entry no other
    journal is actually booking."""
    if not entity or not entities_path:
        return False
    try:
        import configs  # type: ignore
        entities = configs.load_entities(entities_path)
    except Exception:
        return False
    profile = entities.get(entity)
    if profile is None:
        return False
    return bool(getattr(profile, "partner_comp_accounts", None))


def _make_tools(xlsx_path: str, gnucash_path: str, output_path: str,
                partner_comp_configured: bool = False) -> list:
    """Build the closure tools that bind the file paths, so the LLM never has to
    pass a path (small local models garble long Windows paths) — it only chooses
    accounts. Defined at module scope (not nested in ``run``) so tests can
    construct and inspect the tool schemas without invoking an LLM."""

    @tool
    def build_journals() -> str:
        """Build the GnuCash TDS journal CSV. Takes NO arguments — the workbook,
        GnuCash file and output path are already configured. Writes and verifies
        the CSV and lists any NEEDS REVIEW deductors with their candidate
        accounts. Call this first."""
        return T.run_build(xlsx_path, gnucash_path, output_path,
                           partner_comp_configured=partner_comp_configured)

    @tool
    def apply_overrides(overrides: dict | None = None) -> str:
        """Optional. Resolve the NEEDS REVIEW deductors. `overrides` is an OBJECT
        mapping the deductor Sr.No (as a string) to a chosen full account path,
        e.g. {"2": "Income:Interest Income:Interest on HDFC - FD"}.
        Use only Sr numbers flagged NEEDS REVIEW and account paths from their
        candidate lists; omit any you are unsure about (leaving them on Suspense
        is correct). Do not pass any file paths. `overrides` is optional and
        defaults to none — calling with no overrides is a harmless no-op that
        leaves the already-verified CSV unchanged."""
        return T.run_apply(xlsx_path, gnucash_path, output_path, overrides,
                           partner_comp_configured=partner_comp_configured)

    return [build_journals, apply_overrides]


def run(
    xlsx_path: str,
    gnucash_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
    entity: str = "",
    entities_path: str = "Data/itr/entities.yaml",
) -> str:
    """Build GnuCash TDS journals from a 26AS Convert workbook + a .gnucash file.

    Paths are captured in the closure tools below, so the model cannot mistype
    them — it only chooses accounts for the NEEDS REVIEW deductors.

    entity/entities_path: optional. When `entity` has partner_comp_accounts
    configured in entities.yaml (skill_partner_comp_recon/jv_emitter.py's
    _monthly_journal() already books its s.194T TDS month-by-month), this
    skill's own Category C (s.194T) postings are left out of the importable
    CSV instead of double-booking the same TDS -- see build_tds_journals.py's
    build_journals(partner_comp_configured=...). Left unset (the UI-only
    "entity" field with no book_from consumer changed here otherwise), the
    skill behaves exactly as before, apart from a new double-booking warning
    on Category C rows.
    """
    partner_comp_configured = _resolve_partner_comp_configured(entity, entities_path)
    tools = _make_tools(xlsx_path, gnucash_path, output_path, partner_comp_configured)
    agent = build_agent(tools, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            "Create the GnuCash TDS journals. Call build_journals() first. If it "
            "reports NEEDS REVIEW deductors and a candidate clearly fits, call "
            "apply_overrides with your picks; otherwise you are done. Then report "
            "the saved CSV, how many matched vs. went to Suspense, and any "
            "accounts to create."
        )]
    })
    # Return a SINGLE deterministic summary computed from the output files, not
    # the model's free-text narration — a small local model mislabels the counts
    # (and mistypes the filename), which contradicts the real numbers. The LLM's
    # only job was to make the apply_overrides tool calls; its prose is dropped.
    summary = T.final_summary(output_path, gnucash_path)
    if summary:
        return summary
    return result["messages"][-1].content
