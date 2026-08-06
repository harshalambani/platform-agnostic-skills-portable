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


def _format_endpoint_block(name: str, ep: dict, is_active: bool, *, fresh: bool) -> str:
    # `fresh` is what the Refresh button means: go and look again. Everything
    # else reads the shared cache the background prime fills (ui/_health.py),
    # because probing every configured endpoint here is what used to cost the
    # startup path ~6 seconds — an unreachable one pays a full timeout, and
    # none of it is needed to draw the panel.
    res = _health.check(ep) if fresh else _health.check_cached(ep)
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


def _no_endpoints_markdown() -> str:
    return (
        "_No endpoints configured. Edit_ "
        f"`{_config.PORTABLE_CONFIG_PATH}` _to add one._"
    )


def _build_status_markdown(*, fresh: bool = False) -> str:
    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    active = cfg.get("active_endpoint", "")
    if not endpoints:
        return _no_endpoints_markdown()
    return "\n\n".join(
        _format_endpoint_block(name, ep, name == active, fresh=fresh)
        for name, ep in endpoints.items()
    )


def _refresh_status_markdown() -> str:
    """Refresh button: re-probe every endpoint, ignoring the cache."""
    return _build_status_markdown(fresh=True)


# ---------------------------------------------------------------------------
# Deferred status fill
# ---------------------------------------------------------------------------
#
# Same arrangement as _generic's model dropdowns: the panel is registered here
# at construction and filled by one Blocks.load once the browser connects, so
# the probe overlaps the startup the user is already waiting through instead
# of extending it.

_PROBING_PLACEHOLDER = "_Checking…_"

_deferred_status_panels: list = []


def _build_status_placeholder() -> str:
    """The panel as it looks before anything has been probed.

    Deliberately NOT a one-line "Checking endpoints…" and deliberately not a
    spinner. The fill lands anywhere between 0.1s and ~2s (bounded by the
    _health socket timeout), and for most of that range a spinner is a worse
    experience than the answer arriving: it announces a wait that is already
    over. What was actually jarring is the panel changing SIZE — a single
    italic line growing into one heading plus three bullets per endpoint,
    shoving everything below it down the page just as the eye lands on it.

    So this emits the real block structure with the same line count and the
    same badge shape, reading from config only (no network, free at
    construction). When the load event lands, only the dot and the detail line
    change; nothing moves.
    """
    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    active = cfg.get("active_endpoint", "")
    if not endpoints:
        return _no_endpoints_markdown()
    return "\n\n".join(
        f"### ⚪ `{name}`{'  **(active)**' if name == active else ''}\n"
        f"- Provider: `{ep.get('provider', '?')}`\n"
        f"- URL: `{ep.get('base_url', '')}`\n"
        f"- {_PROBING_PLACEHOLDER}\n"
        for name, ep in endpoints.items()
    )


def reset_deferred_status_panels() -> None:
    """Drop registrations from a previous build_app() in this process."""
    _deferred_status_panels.clear()


def wire_deferred_status_loads(app) -> None:
    """Attach the load event that fills the endpoint-status panel."""
    panels = list(_deferred_status_panels)
    if not panels:
        return

    def _fill():
        md = _build_status_markdown()
        return md if len(panels) == 1 else [md] * len(panels)

    app.load(fn=_fill, inputs=None, outputs=panels)


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
            # Rendered empty and filled by a load event (registered below,
            # attached in webui.build_app). Note that Gradio's own callable-
            # value shortcut — gr.Markdown(value=some_fn) — would NOT do:
            # Component.get_load_fn_and_initial_value calls the function at
            # construction time as well, so the probe would still be paid
            # before the window appeared, which is the whole thing being fixed.
            status_md = gr.Markdown(value=_build_status_placeholder())
            _deferred_status_panels.append(status_md)
            refresh_btn = gr.Button("Refresh status", variant="secondary")
            refresh_btn.click(fn=_refresh_status_markdown, outputs=status_md)

        with gr.Column(scale=1):
            gr.Markdown("## Available skills")
            gr.Markdown(_build_skills_markdown(skills or []))
