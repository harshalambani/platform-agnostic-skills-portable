"""
ui/tabs/home.py — Home tab.

Renders a short description, the status of each configured LLM endpoint
(green / amber / red dot per spec §8.2), and a dynamic listing of all
skills discovered by the registry.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import gradio as gr

from .. import _config
from .. import _health
from .. import _buildinfo
from .. import _update

if TYPE_CHECKING:
    from agents.registry import SkillInfo


_STATUS_DOT = {
    "ok":          "🟢",
    "slow":        "🟡",
    "unreachable": "🔴",
}


def _format_endpoint_block(name: str, ep: dict, is_active: bool) -> str:
    res = _health.check(ep)
    dot = _STATUS_DOT.get(res.status, "⚪")
    flag = "  **(active)**" if is_active else ""
    base = ep.get("base_url", "")
    provider = ep.get("provider", "?")
    return (
        f"### {dot} `{name}`{flag}\n"
        f"- Provider: `{provider}`\n"
        f"- URL: `{base}`\n"
        f"- {res.detail}\n"
    )


def _build_status_markdown() -> str:
    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    active = cfg.get("active_endpoint", "")
    if not endpoints:
        return (
            "_No endpoints configured. Edit_ "
            f"`{_config.PORTABLE_CONFIG_PATH}` _to add one._"
        )
    return "\n\n".join(
        _format_endpoint_block(name, ep, name == active)
        for name, ep in endpoints.items()
    )


def _build_skills_markdown(skills: list[SkillInfo]) -> str:
    """Build a markdown listing of all discovered skills."""
    if not skills:
        return "_No skills discovered. Check that agents/*/skill.yaml files exist._"
    lines = []
    for s in skills:
        desc = s.description.strip().split("\n")[0]  # first line only
        mode_badge = f"`{s.mode}`"
        llm_badge = "🧠 AI-powered" if s.requires.llm else "⚙️ Deterministic"
        lines.append(f"- **{s.name}** — {desc} {mode_badge} `{llm_badge}`")
    return "\n".join(lines)


def render(skills: list[SkillInfo] | None = None) -> None:
    """Render the Home tab; must be called inside a gr.Tab context."""
    skill_count = len(skills) if skills else 0

    gr.Markdown(
        f"""
        # PA Skills Portable

        LLM-powered document processing skills, packaged portably.
        Works with any LLM — local (Ollama), LAN, or cloud (OpenAI-compatible).

        **{skill_count} skill(s) loaded** · _Version `{_buildinfo.VERSION}` · commit `{_buildinfo.COMMIT_SHA[:7] if _buildinfo.COMMIT_SHA else 'dev'}`_
        """
    )

    # Update banner — shows only if a newer GitHub release exists.
    update_banner = _update.format_banner()
    if update_banner:
        gr.Markdown(update_banner)

    with gr.Row():
        with gr.Column(scale=2):
            gr.Markdown("## LLM endpoint status")
            status_md = gr.Markdown(value=_build_status_markdown())
            refresh_btn = gr.Button("Refresh status", variant="secondary")
            refresh_btn.click(fn=_build_status_markdown, outputs=status_md)

        with gr.Column(scale=1):
            gr.Markdown("## Available skills")
            gr.Markdown(_build_skills_markdown(skills or []))
