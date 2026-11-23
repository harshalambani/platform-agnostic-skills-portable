"""
ui/tabs/home.py -- Home tab (UI-54, part 3).

  top line      version + commit, the AI one-liner, "> Settings"
  NEEDS YOU     problems and coverage gaps, by entity then financial year; one global
                [Check coverage now]
  RECENT RUNS   the last 20 runs; "> History"
  SKILLS        a collapsed launch pad, one group per top-level tab

The endpoint-status blocks moved to the Settings tab. The data logic lives in ui/_home_data.py.

Nothing is read or probed when this tab is BUILT. Home's data (both stores, the registry and the
AI line) loads once after the window shows (app.load), again whenever the Home tab is selected,
and again after [Check coverage now]. Only that button writes anything, and only
coverage-last.json.
"""
from __future__ import annotations

import html
from typing import TYPE_CHECKING

import gradio as gr

from .. import _buildinfo, _home_data, _update

if TYPE_CHECKING:
    from agents.registry import SkillInfo

SETTINGS_TAB_ID = "settings"
HISTORY_TAB_ID = "history"

_LOADING = "<div style='opacity:.75'>Loading...</div>"

# Filled by render(); consumed by wire_deferred_home_loads(). Reset per build_app().
_deferred_home: dict = {}


def reset_deferred_home_panels() -> None:
    """Drop registrations from a previous build_app() in this process."""
    _deferred_home.clear()


def _version_line() -> str:
    sha = _buildinfo.COMMIT_SHA[:7] if _buildinfo.COMMIT_SHA else "dev"
    return f"**PA Skills Portable**  _v{_buildinfo.VERSION} - commit `{sha}`_"


def launch_pad_rows(groups: list) -> list[tuple[str, int, str]]:
    """[(group label, count, escaped html body)] for the collapsed launch pad."""
    out = []
    for label, items in groups:
        rows = "".join(
            f"<div style='display:flex;gap:.75rem'><span style='min-width:16rem'>{html.escape(name)}</span>"
            f"<span style='opacity:.7'>{html.escape(path)}</span></div>"
            for name, path in items)
        out.append((label, len(items), rows))
    return out


def render(skills: list[SkillInfo] | None = None, *, home_tab=None, main_tabs=None,
           skill_groups: list | None = None, display_names: dict | None = None) -> None:
    """Render the Home tab; must be called inside a gr.Tab context.

    home_tab / main_tabs let the buttons and the select event reach the other tabs; both are
    optional so the tab still renders on its own (tests)."""
    display_names = display_names or {s.name: s.display_name for s in (skills or [])}

    # -- top line --
    with gr.Row(equal_height=True):
        gr.Markdown(_version_line())
        ai = gr.HTML(_home_data.render_ai(_home_data.ai_placeholder()))
        to_settings = gr.Button("> Settings", size="sm", scale=0, min_width=110)

    banner = _update.format_banner()
    if banner:
        gr.Markdown(banner)

    # -- needs you --
    with gr.Row():
        gr.Markdown("### NEEDS YOU")
        check_btn = gr.Button("Check coverage now", size="sm", scale=0, min_width=170)
    check_status = gr.Markdown("")
    needs = gr.HTML(_LOADING)

    # -- recent runs --
    with gr.Row():
        gr.Markdown("### RECENT RUNS")
        to_history = gr.Button("> History", size="sm", scale=0, min_width=110)
    recent = gr.HTML(_LOADING)

    # -- skills launch pad --
    gr.Markdown("### SKILLS")
    if skill_groups:
        for label, count, body in launch_pad_rows(skill_groups):
            with gr.Accordion(f"{label} ({count})", open=False):
                gr.HTML(body)
    else:
        gr.Markdown("_No skills discovered. Check that agents/*/skill.yaml files exist._")

    # -- wiring (events only; nothing runs now) --
    if main_tabs is not None:
        to_settings.click(fn=lambda: gr.Tabs(selected=SETTINGS_TAB_ID), inputs=None, outputs=main_tabs)
        to_history.click(fn=lambda: gr.Tabs(selected=HISTORY_TAB_ID), inputs=None, outputs=main_tabs)

    def _fill():
        return _home_data.load_home(display_names)

    panels = [ai, needs, recent]
    _deferred_home.update(fill=_fill, panels=panels)

    if home_tab is not None:
        home_tab.select(fn=_fill, inputs=None, outputs=panels, show_progress="hidden")

    def _busy():
        return gr.Button(interactive=False), "_Checking the registered books..._"

    (check_btn.click(fn=_busy, inputs=None, outputs=[check_btn, check_status], show_progress="hidden")
     .then(fn=_home_data.run_check_now, inputs=None, outputs=check_status, show_progress="hidden")
     .then(fn=_fill, inputs=None, outputs=panels, show_progress="hidden")
     .then(fn=lambda: gr.Button(interactive=True), inputs=None, outputs=check_btn, show_progress="hidden"))


def wire_deferred_home_loads(app) -> None:
    """Attach the one load event that fills Home once the window is showing."""
    fill, panels = _deferred_home.get("fill"), _deferred_home.get("panels")
    if fill and panels:
        app.load(fn=fill, inputs=None, outputs=panels)
