"""
suggest.py -- optional LLM tag suggestions for unmapped accounts (plan
sections 4.1, 4.4).

Deterministic core stays deterministic: this module is only ever consulted
for leaves that mapping.py already found UNMAPPED. It never writes to the
mapping file -- suggestions only ever land in the generated
proposed-mappings snippet (mapping.py's proposed_mapping_snippet) and the
run summary, annotated `suggested_by_llm: <date>` until a human reviews
and pastes them in.

Uses the same endpoint config as every other agent-mode skill
(agents.base_agent.run_direct -> config.yaml's provider/default_model).
Degrades gracefully to "no suggestions" (empty dict) when no endpoint is
configured or the call fails for any reason -- the run must still
complete; the unmapped list itself is unaffected either way.
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass
from pathlib import Path

import tags as tag_vocab

_SRC_ROOT = Path(__file__).resolve().parents[3]  # .../src

log = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are tagging GnuCash accounts for an Indian ITR workbook. Given an "
    "account's full path, its FY total, and the tags already used by its "
    "sibling accounts, reply with EXACTLY two lines:\n"
    "TAG: <one tag from the allowed list>\n"
    "WHY: <one short sentence>\n"
    "Allowed tags: " + ", ".join(sorted(tag_vocab.all_tags()))
)

_TAG_LINE_RE = re.compile(r"^TAG:\s*([A-Z0-9_]+)\s*$", re.MULTILINE)
_WHY_LINE_RE = re.compile(r"^WHY:\s*(.+)$", re.MULTILINE)


@dataclass
class Suggestion:
    tag: str
    rationale: str


def suggest_tag(
    account_path: str,
    fy_total: float | None,
    sibling_tags: list,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> Suggestion | None:
    """Ask the configured LLM to suggest a tag for one unmapped account.
    Returns None (never raises) if no endpoint is configured, the reply
    can't be parsed, or the suggested tag isn't in the vocabulary -- the
    caller treats that exactly like "no suggestion available"."""
    try:
        if str(_SRC_ROOT) not in sys.path:
            sys.path.insert(0, str(_SRC_ROOT))
        from agents.base_agent import run_direct
    except ImportError:
        return None

    user_message = (
        f"Account path: {account_path}\n"
        f"FY total: {fy_total if fy_total is not None else 'unknown'}\n"
        f"Sibling tags already used nearby: {', '.join(sibling_tags) if sibling_tags else 'none'}\n"
    )

    try:
        reply = run_direct(user_message, _SYSTEM_PROMPT, config_path, model_override)
    except Exception:
        log.info("suggest_tag: no LLM suggestion available for %s (endpoint unavailable)", account_path)
        return None

    tag_match = _TAG_LINE_RE.search(reply)
    if not tag_match:
        return None
    tag = tag_match.group(1).strip()
    if not tag_vocab.is_valid_tag(tag):
        return None

    why_match = _WHY_LINE_RE.search(reply)
    rationale = why_match.group(1).strip() if why_match else ""
    return Suggestion(tag=tag, rationale=rationale)


def suggest_for_unmapped(
    unmapped: list,
    resolved: dict,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> dict:
    """Suggest a tag for every unmapped leaf. Returns {guid: Suggestion},
    omitting any leaf where no suggestion was produced. Never raises --
    a broken endpoint yields an empty dict and the run still completes."""
    sibling_tags = sorted({leaf.tag for leaf in resolved.values()})
    suggestions: dict[str, Suggestion] = {}
    for leaf in unmapped:
        suggestion = suggest_tag(leaf.path, leaf.total, sibling_tags, config_path, model_override)
        if suggestion is not None:
            suggestions[leaf.guid] = suggestion
    return suggestions
