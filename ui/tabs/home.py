"""
ui/tabs/home.py — Home tab.

Renders a short description, the status of each configured LLM endpoint
(green / amber / red dot per spec §8.2), and a quick-link button per
skill that's wired in the current build. In Phase 1 the only wired skill
is 26AS; the BoB / HSBC entries are placeholders.
"""
from __future__ import annotations

import gradio as gr

from .. import _config
from .. import _health
from .. import _buildinfo


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


def render() -> None:
    """Render the Home tab; must be called inside a gr.Tab context."""
    gr.Markdown(
        f"""
        # PA Skills Portable

        LLM-powered PDF and statement processing skills, packaged portably.
        Three skills ship with this build; **Phase 1 wires the 26AS skill only**.
        BoB and HSBC become available in Phase 2.

        _Version `{_buildinfo.VERSION}` · commit `{_buildinfo.COMMIT_SHA[:7] if _buildinfo.COMMIT_SHA else 'dev'}`_
        """
    )

    with gr.Row():
        with gr.Column(scale=2):
            gr.Markdown("## LLM endpoint status")
            status_md = gr.Markdown(value=_build_status_markdown())
            refresh_btn = gr.Button("Refresh status", variant="secondary")
            refresh_btn.click(fn=_build_status_markdown, outputs=status_md)

        with gr.Column(scale=1):
            gr.Markdown(
                """
                ## Quick links

                - **26AS** — extract Form 26AS PDF → Excel with one sheet per Part.

                _(BoB, HSBC: coming in Phase 2.)_
                """
            )
