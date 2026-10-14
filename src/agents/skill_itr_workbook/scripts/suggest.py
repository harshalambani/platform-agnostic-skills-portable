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
Degrades gracefully to "no suggestions" when no endpoint is configured or a
call fails -- the run must still complete; the unmapped list itself is
unaffected either way. ITR-01: the pass is CAPPED (MAX_SUGGESTIONS_PER_RUN)
and STOPS ON THE FIRST FAILURE, recording a plain-words reason (never the
key) so the run can say how many accounts were left without a suggestion.
"""
from __future__ import annotations

import logging
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

import tags as tag_vocab

_SRC_ROOT = Path(__file__).resolve().parents[3]  # .../src

log = logging.getLogger(__name__)

# Hard ceiling on AI calls in one run. Accounts beyond it are left without a
# suggestion and reported; a bigger book is tagged in the Review Mapping tab.
MAX_SUGGESTIONS_PER_RUN = 25

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


def _config_secrets(config_path: str) -> tuple:
    """Best-effort: the api_key values in the legacy config, so they can be
    scrubbed from any error text. Never raises, never returned to callers."""
    try:
        import yaml
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001
        return ()
    found = []
    for v in cfg.values():
        if isinstance(v, dict) and v.get("api_key"):
            found.append(str(v["api_key"]))
    return tuple(found)


def _ask(account_path, fy_total, sibling_tags, config_path, model_override):
    """One AI call. RAISES on any transport/provider failure; returns the
    parsed Suggestion, or None when the reply was unusable."""
    if str(_SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_ROOT))
    from agents.base_agent import run_direct

    user_message = (
        f"Account path: {account_path}\n"
        f"FY total: {fy_total if fy_total is not None else 'unknown'}\n"
        f"Sibling tags already used nearby: {', '.join(sibling_tags) if sibling_tags else 'none'}\n"
    )
    reply = run_direct(user_message, _SYSTEM_PROMPT, config_path, model_override)
    tag_match = _TAG_LINE_RE.search(reply or "")
    if not tag_match:
        return None
    tag = tag_match.group(1).strip()
    if not tag_vocab.is_valid_tag(tag):
        return None
    why_match = _WHY_LINE_RE.search(reply)
    return Suggestion(tag=tag, rationale=why_match.group(1).strip() if why_match else "")


def suggest_tag(
    account_path: str,
    fy_total: float | None,
    sibling_tags: list,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> Suggestion | None:
    """Ask the configured LLM to suggest a tag for one unmapped account.
    Returns None (never raises) if no endpoint is configured, the call
    fails, the reply can't be parsed, or the tag isn't in the vocabulary."""
    try:
        return _ask(account_path, fy_total, sibling_tags, config_path, model_override)
    except Exception:  # noqa: BLE001
        log.info("suggest_tag: no LLM suggestion available for %s (endpoint unavailable)", account_path)
        return None


@dataclass
class SuggestionRun:
    """Outcome of one suggestion pass."""
    suggestions: dict = field(default_factory=dict)   # guid -> Suggestion
    total: int = 0                # unmapped accounts the pass was asked about
    attempted: int = 0            # AI calls actually made
    stopped_reason: str | None = None   # plain words, never contains a key
    failure_kind: str | None = None     # llm_errors kind, or "cap" / "cancelled"
    cancelled: bool = False

    @property
    def left_without(self) -> int:
        return self.total - len(self.suggestions)

    @property
    def not_attempted(self) -> int:
        return self.total - self.attempted


def _is_cancelled() -> bool:
    try:
        from ui._runner import is_cancelled
        return bool(is_cancelled())
    except Exception:  # noqa: BLE001
        return False


def _emit(message: str) -> None:
    try:
        if str(_SRC_ROOT) not in sys.path:
            sys.path.insert(0, str(_SRC_ROOT))
        from agents.base_agent import get_progress_queue
        q = get_progress_queue()
    except Exception:  # noqa: BLE001
        q = None
    if q is not None:
        q.put({"step": 3, "type": "pipeline", "snippet": message})


def run_suggestions(
    unmapped: list,
    resolved: dict,
    config_path: str = "config.yaml",
    model_override: str | None = None,
    max_calls: int = MAX_SUGGESTIONS_PER_RUN,
    ask=None,
    cancelled=None,
) -> SuggestionRun:
    """The AI pass over unmapped leaves. Makes NO call when nothing is
    unmapped; never more than ``max_calls``; stops on the FIRST failure
    (the same endpoint will fail the same way for every other account) and
    records why. Never raises."""
    ask = ask or _ask
    cancelled = cancelled or _is_cancelled
    run = SuggestionRun(total=len(unmapped))
    if not unmapped:
        return run
    sibling_tags = sorted({leaf.tag for leaf in resolved.values()})
    secrets = _config_secrets(config_path)
    if str(_SRC_ROOT) not in sys.path:
        sys.path.insert(0, str(_SRC_ROOT))
    from agents import llm_errors

    for leaf in unmapped:
        if cancelled():
            run.cancelled, run.failure_kind = True, "cancelled"
            run.stopped_reason = "stopped by the user"
            break
        if run.attempted >= max_calls:
            run.failure_kind = "cap"
            run.stopped_reason = f"the per-run limit of {max_calls} AI suggestion calls was reached"
            break
        _emit(f"Suggesting tags: {run.attempted + 1} of {min(len(unmapped), max_calls)}")
        run.attempted += 1
        try:
            suggestion = ask(leaf.path, leaf.total, sibling_tags, config_path, model_override)
        except Exception as e:  # noqa: BLE001
            failure = llm_errors.classify(e, model=model_override, secrets=secrets)
            run.stopped_reason, run.failure_kind = failure.reason, failure.kind
            log.info("suggest pass stopped after %d call(s): %s", run.attempted, failure.reason)
            break
        if suggestion is not None:
            run.suggestions[leaf.guid] = suggestion
    return run


def suggest_for_unmapped(
    unmapped: list,
    resolved: dict,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> dict:
    """Back-compat wrapper: {guid: Suggestion}. Same cap and stop-on-first-
    failure behaviour as run_suggestions(). Never raises."""
    return run_suggestions(unmapped, resolved, config_path, model_override).suggestions
