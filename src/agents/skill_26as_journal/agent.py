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


class EntityResolutionError(Exception):
    """Raised when `entity` is blank or does not resolve to a profile in
    entities.yaml. Both must fail loud, before any CSV is written -- never
    fall back to silently journalling Category C for an entity we could not
    actually check for double-booking."""


def _resolve_partner_comp_configured(entity: str, entities_path: str) -> bool:
    """True only when `entity` resolves to an EntityProfile in entities_path
    AND that profile's partner_comp_accounts is non-empty (configs.py: empty
    means "the [partner comp] skill cannot emit a journal for this entity at
    all").

    `entity` is now a required input (skill.yaml), so a blank value or one
    that is not found in entities.yaml is a user-input problem and raises
    EntityResolutionError -- the caller must refuse the run before writing
    any CSV, per the s.194T double-booking check this value drives.

    entities_path itself being missing, unreadable, or unparsable is ALSO
    a hard failure, not a silent fallback: if this file cannot be read, we
    cannot know whether `entity`'s partner_comp_accounts is configured,
    and cannot tell whether Category C (s.194T) would double-book TDS
    that skill_partner_comp_recon's monthly journal already booked.
    Falling back to False here would be exactly the silent fallback the
    double-booking ruling closed elsewhere -- so this raises
    EntityResolutionError naming the path, and no CSV is written. The
    ONLY path that still journals Category C is an entity that IS found
    in entities.yaml and has no partner_comp_accounts configured."""
    if not entity:
        raise EntityResolutionError(
            "No entity selected. Pick the entity this 26AS workbook belongs "
            "to before running -- this is required so the skill can check "
            "whether s.194T partner-comp TDS is already booked elsewhere "
            "and avoid double-booking it."
        )
    try:
        import configs  # type: ignore
        entities = configs.load_entities(entities_path)
    except Exception as e:
        raise EntityResolutionError(
            f"entities.yaml could not be read or parsed at '{entities_path}' "
            f"({type(e).__name__}: {e}). Fix or restore this file before "
            f"running -- this is required so the skill can check whether "
            f"s.194T partner-comp TDS is already booked elsewhere and avoid "
            f"double-booking it; this run has been refused and no CSV was "
            f"written."
        ) from e
    profile = entities.get(entity)
    if profile is None:
        raise EntityResolutionError(
            f"Entity '{entity}' was not found in entities.yaml. Fix the "
            f"entity selection before running -- this is required so the "
            f"skill can check whether s.194T partner-comp TDS is already "
            f"booked elsewhere and avoid double-booking it."
        )
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

    entity/entities_path: `entity` is required (skill.yaml). A blank entity,
    one not found in entities.yaml, or an entities.yaml that is itself
    missing/unreadable/unparsable, all fail loud here -- before build_agent
    or any tool runs, so no CSV is ever written -- rather than silently
    falling back to journalling Category C. When `entity` resolves and has
    partner_comp_accounts configured in entities.yaml
    (skill_partner_comp_recon/jv_emitter.py's _monthly_journal() already
    books its s.194T TDS month-by-month), this skill's own Category C
    (s.194T) postings are left out of the importable CSV instead of
    double-booking the same TDS -- see build_tds_journals.py's
    build_journals(partner_comp_configured=...). When `entity` resolves but
    has no partner_comp_accounts configured, the skill behaves as before,
    with a double-booking warning kept on Category C rows.
    """
    try:
        partner_comp_configured = _resolve_partner_comp_configured(entity, entities_path)
    except EntityResolutionError as e:
        return f"ERROR: {e}"
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
