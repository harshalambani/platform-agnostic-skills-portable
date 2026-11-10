"""
tests/test_help_coverage.py — help documentation coverage + freshness.

Guards the single-source-of-truth help system:
  1. Every UI skill (one discovered by the registry) has a non-empty `help:`
     block in its skill.yaml.
  2. Help inputs and manifest inputs match BOTH ways: no help entry for an input
     that does not exist, and no manifest input left undocumented.
  2a. Help text names no tab, field, status or message that the app does not
      contain (a made-up name fails the guard test).
  2b. "Works offline" and "Uses the LLM" are worded one way, and agree with the
      manifest's requires.llm.
  2c. Internal-step manifests (no display_name) have no page in the user guide.
  3. `scripts/gen_docs.py --check` reports no stale generated docs — i.e. a
     `help:` block was edited but the docs were not regenerated.

These run in source mode and need no LLM, no native binaries, and no gradio.
"""
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents import registry  # noqa: E402


@pytest.fixture(scope="module")
def skills():
    return registry.discover(refresh=True)


def test_every_ui_skill_has_help(skills):
    missing = [s.display_name for s in skills if not s.help or s.help.is_empty()]
    assert not missing, f"UI skills missing a help: block: {missing}"


def _input_diff(inputs, help_inputs):
    """(undocumented, unknown): manifest inputs with no help entry, and help
    entries naming no manifest input."""
    manifest = {i.name for i in inputs}
    helped = {h.name for h in help_inputs}
    return sorted(manifest - helped), sorted(helped - manifest)


def test_help_inputs_match_manifest_inputs_both_ways(skills):
    problems = []
    for s in skills:
        if not s.help:
            continue
        undocumented, unknown = _input_diff(s.inputs, s.help.inputs)
        if undocumented:
            problems.append(f"{s.display_name}: manifest input(s) with no help entry: {undocumented}")
        if unknown:
            problems.append(f"{s.display_name}: help input(s) that are not declared inputs: {unknown}")
    assert not problems, "\n".join(problems)


def test_input_diff_catches_both_directions():
    """Negative proof: each direction of drift is reported, a match is not."""
    class _N:
        def __init__(self, name):
            self.name = name
    assert _input_diff([_N("a"), _N("b")], [_N("a")]) == (["b"], [])
    assert _input_diff([_N("a")], [_N("a"), _N("ghost")]) == ([], ["ghost"])
    assert _input_diff([_N("a")], [_N("a")]) == ([], [])


def test_help_has_outputs_and_steps(skills):
    thin = []
    for s in skills:
        h = s.help
        if not h:
            continue
        if not h.overview:
            thin.append(f"{s.display_name}: no overview")
        if not h.steps:
            thin.append(f"{s.display_name}: no steps")
        if not h.output_files:
            thin.append(f"{s.display_name}: no output files described")
    assert not thin, "\n".join(thin)


def test_no_skill_ships_a_scaffold_marker(skills):
    """No REGISTERED skill's help block may still carry the Skill Scaffolder's
    placeholder marker -- that means someone shipped a scaffolded skill
    (agents.skill_scaffold) without filling in the generated placeholders.
    The marker legitimately appears only under src/agents/skill_scaffold/templates/
    and as a constant in test files (never in a registered skill.yaml)."""
    marker = "TODO(scaffold)"
    offenders = []
    for s in skills:
        h = s.help
        if not h:
            continue
        haystack = " ".join([
            s.description or "",
            h.overview, h.when_to_use, h.tips,
            " ".join(h.steps),
            " ".join(f"{i.tooltip} {i.accepts} {i.gotchas}" for i in h.inputs),
            " ".join(f.tooltip for f in h.output_files),
            " ".join(f"{t.problem} {t.fix}" for t in h.troubleshooting),
        ])
        if marker in haystack:
            offenders.append(s.display_name)
    assert not offenders, (
        f"skill(s) still ship the Skill Scaffolder's placeholder marker: {offenders} "
        "-- fill in the generated placeholders before this skill ships."
    )


def test_generated_docs_are_fresh():
    """gen_docs --check must pass; if this fails, run scripts/gen_docs.py."""
    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "scripts" / "gen_docs.py"), "--check"],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        "Generated docs are stale — run `python scripts/gen_docs.py`.\n"
        + result.stdout + result.stderr
    )


# ---------------------------------------------------------------------------
# Guard: help text may only name things the app actually contains.
# ---------------------------------------------------------------------------

# Illustrative examples inside help text: sample data or sample output, not names
# of anything in the app. Each entry says why it is exempt.
_ILLUSTRATIVE = {
    "which month had the most refunds?",   # CSV Data Analyzer: a sample question
    "jun 2025, jul 2025",                  # Coverage Gap Detector: a sample report line
    "no transactions since oct 2025",      # Coverage Gap Detector: a sample report line
    "<firm>-<fy>-rect",                    # Partner Compensation Reconciliation: a naming pattern
    "internal pipeline step",              # Skill Scaffolder: names an option that does NOT exist
}

_PHRASE_PATTERNS = (
    re.compile(r'"([^"]{4,80})"'),                      # "Quoted"
    re.compile(r"(?<![\w])'([^']{4,60})'(?!\w)"),       # 'Quoted'
    re.compile(r"\*\*([^*]{4,80}?)(?<![.:])\*\*"),      # **Bold** (a heading ending in . or : is prose)
)


def _norm(text: str) -> str:
    text = (text.replace("—", "-").replace("–", "-").replace("--", "-")
            .replace("’", "'").replace("‘", "'")
            .replace("“", '"').replace("”", '"'))
    return re.sub(r"\s+", " ", text).casefold()


def _help_texts(h):
    yield h.overview
    yield h.when_to_use
    yield h.tips
    yield from h.steps
    for i in h.inputs:
        yield from (i.tooltip, i.accepts, i.gotchas)
    for f in h.output_files:
        yield f.tooltip
    for t in h.troubleshooting:
        yield t.problem
        yield t.fix


def _corpus() -> str:
    """Everything the app can show: python source, AGENT.md files, and each
    manifest minus its help block (labels, options, descriptions)."""
    import yaml
    parts = []
    for pattern in ("src/agents/**/*.py", "ui/**/*.py", "src/agents/**/AGENT.md"):
        for f in PROJECT_ROOT.glob(pattern):
            parts.append(f.read_text(encoding="utf-8", errors="replace"))
    for f in (PROJECT_ROOT / "src" / "agents").glob("*/skill.yaml"):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        data.pop("help", None)
        parts.append(yaml.dump(data, allow_unicode=True))
    return _norm("\n".join(parts))


def _unknown_phrases(texts, corpus: str) -> list:
    """Quoted or bold phrases in `texts` that appear nowhere in `corpus`."""
    unknown = []
    for text in texts:
        for pattern in _PHRASE_PATTERNS:
            for m in pattern.finditer(text or ""):
                phrase = _norm(m.group(1))
                if "..." in phrase or "…" in phrase or phrase in _ILLUSTRATIVE:
                    continue
                if phrase not in corpus:
                    unknown.append(m.group(1))
    return unknown


def test_help_names_nothing_the_app_does_not_contain(skills):
    corpus = _corpus()
    problems = []
    for s in skills:
        if not s.help:
            continue
        for phrase in _unknown_phrases(_help_texts(s.help), corpus):
            problems.append(f"{s.display_name}: help names {phrase!r}, found nowhere in the app")
    assert not problems, "\n".join(problems)


def test_guard_flags_a_made_up_tab_field_or_status():
    """Negative proof: invented names are reported, real ones are not."""
    corpus = _corpus()
    assert _unknown_phrases(['Open the "Quarterly Rescue" tab.'], corpus) == ["Quarterly Rescue"]
    assert _unknown_phrases(["Set the **Magic Wand** field."], corpus) == ["Magic Wand"]
    assert _unknown_phrases(['The status reads "TOTALLY-FAKE-STATUS".'], corpus) == ["TOTALLY-FAKE-STATUS"]
    # Real names pass.
    assert _unknown_phrases(['Pick the "Entity" and "Assessment Year" dropdowns.'], corpus) == []
    assert _unknown_phrases(['The run ends "CANCELLED" or "Sort completed with" issues.'], corpus) == []


# ---------------------------------------------------------------------------
# One wording for "works offline" and for "uses the LLM".
# ---------------------------------------------------------------------------

OFFLINE_SENTENCE = "Works offline and does not use the LLM."
LLM_SENTENCE = "Uses the LLM."

# Any of these in help text is a second wording for the same fact.
_DRIFTED_WORDING = re.compile(
    r"(runs (fully )?offline|fully offline|processes offline|reconciles offline|"
    r"\(no LLM|no LLM needed|needs an LLM|uses an LLM|your configured LLM|"
    r"requires an LLM|\bAI-powered\b)",
    re.IGNORECASE,
)


def _all_help_text(s) -> str:
    return " ".join(t for t in _help_texts(s.help) if t)


def test_one_wording_for_offline_and_llm(skills):
    problems = []
    for s in skills:
        if not s.help:
            continue
        for m in _DRIFTED_WORDING.finditer(_all_help_text(s)):
            problems.append(f"{s.display_name}: second wording {m.group(0)!r} "
                            f"(use {OFFLINE_SENTENCE!r} or 'uses the LLM')")
    assert not problems, "\n".join(problems)


def test_offline_and_llm_claims_agree_with_the_manifest(skills):
    problems = []
    for s in skills:
        if not s.help:
            continue
        text = _all_help_text(s)
        says_offline = OFFLINE_SENTENCE in text
        says_llm = bool(re.search(r"uses the LLM", text, re.IGNORECASE))
        if says_offline and s.requires.llm:
            problems.append(f"{s.display_name}: help says it works offline but requires.llm is true")
        if says_llm and not s.requires.llm and not says_offline:
            problems.append(f"{s.display_name}: help says it uses the LLM but requires.llm is false")
        if s.requires.llm and not says_llm:
            problems.append(f"{s.display_name}: requires.llm is true but the help never says it uses the LLM")
    assert not problems, "\n".join(problems)


def test_wording_check_catches_a_second_wording():
    """Negative proof: variants are caught, the canonical wording is not."""
    assert _DRIFTED_WORDING.search("Runs fully offline (no LLM).")
    assert _DRIFTED_WORDING.search("This skill needs an LLM.")
    assert not _DRIFTED_WORDING.search(OFFLINE_SENTENCE)
    assert not _DRIFTED_WORDING.search("This skill uses the LLM. Start Ollama.")


# ---------------------------------------------------------------------------
# Internal-step manifests never reach the user guide.
# ---------------------------------------------------------------------------

def test_internal_steps_are_not_in_the_user_guide(skills):
    """Internal steps (no display_name) are not UI skills: no page in
    docs/user-guide/ and no section in USER-GUIDE.html."""
    import yaml
    internal = []
    for f in (PROJECT_ROOT / "src" / "agents").glob("*/skill.yaml"):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        if not data.get("display_name"):
            internal.append(f.parent.name)
    assert internal, "expected internal-step manifests to exist"
    guide_dir = PROJECT_ROOT / "docs" / "user-guide"
    guide_html = (PROJECT_ROOT / "docs" / "USER-GUIDE.html").read_text(encoding="utf-8")
    for folder in internal:
        assert not (guide_dir / f"{folder}.md").exists(), f"{folder} has a user-guide page"
        assert f'id="{folder}"' not in guide_html, f"{folder} is in USER-GUIDE.html"
