"""
agent.py -- Skill Scaffolder (DEV-TIME).

DETERMINISTIC, no LLM, no network: renders the templates under templates/
via plain {{TOKEN}} string substitution (see scaffold.py) to create a
brand-new skill package under src/agents/<skill_dir>/, plus a starter test
module under tests/. Exposed as the top-level "Skill Scaffolder" UI tab
(category "dev" is not in webui.py's GROUP_ORDER / _known_cats, so it
renders as a flat top-level tab -- no webui.py change needed) and from the
dev CLI:

    python -m agents.skill_scaffold --help

Both entry points below (run_ui and __main__.main) check the frozen-build
guard before touching the filesystem: the shipped app's agents/ tree lives
inside the frozen bundle and is not writable, so this tool only makes sense
run from a source checkout.
"""
from __future__ import annotations

from pathlib import Path

from agents import registry
from agents.skill_scaffold import scaffold as _engine


def _bool_from_choice(value) -> bool:
    return str(value).strip().lower() in ("yes", "true", "1")


def run_ui(
    skill_dir: str = "",
    display_name: str = "",
    category: str = "dev",
    mode: str = "direct",
    output_type: str = "file",
    needs_llm: str = "no",
    output_path: str = "",
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    UI entry point (skill.yaml entry_point: agent:run_ui).

    Scaffolds a new skill package under src/agents/ from the tab's inputs,
    writes the run report into output_path, and returns the report string
    for the generic tab to display. See module docstring for the
    frozen-build guard.
    """
    if _engine.is_frozen():
        return _engine.FROZEN_MESSAGE

    result = _engine.scaffold(
        skill_dir=(skill_dir or "").strip(),
        display_name=(display_name or "").strip(),
        category=(category or "dev").strip(),
        mode=(mode or "direct").strip(),
        output_type=(output_type or "file").strip(),
        needs_llm=_bool_from_choice(needs_llm),
        agents_root=registry._AGENTS_ROOT,
        tests_root=_engine.DEFAULT_TESTS_ROOT,
    )

    lines = ["# Skill Scaffolder report", "", result.message, ""]
    if result.ok:
        lines.append("## Files created")
        for f in result.created_files:
            lines.append(f"- {f}")
        lines.append("")
        gen_docs_rc = _engine.run_gen_docs(_engine.PROJECT_ROOT)
        lines.append(f"## gen_docs.py exit code: {gen_docs_rc}")
        if gen_docs_rc != 0:
            lines.append(
                "gen_docs.py did not exit 0 -- inspect the new skill.yaml's "
                "help: block and re-run `python scripts/gen_docs.py`."
            )
        lines.append("")
        lines.append(
            "**The new tab will only appear after the app is restarted** -- "
            "the skill registry (agents.registry.discover()) caches its scan "
            "for the life of the process."
        )
    else:
        lines.append("No files were written.")

    report = "\n".join(lines)
    if output_path:
        out_dir = Path(output_path)
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "skill-scaffold-report.md").write_text(report, encoding="utf-8")
    return report


def run(**kwargs) -> str:
    """Alias for direct/programmatic callers -- identical behaviour to run_ui()."""
    return run_ui(**kwargs)
