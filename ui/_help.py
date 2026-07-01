"""
ui/_help.py — in-app help rendering.

Single source of truth is each skill's `help:` block (parsed by
agents.registry into SkillInfo.help). This module turns that block into:

  * `maybe_info(cls, text)`  — Tier-1 tooltips: an {"info": ...} kwarg for any
    Gradio component that supports it (feature-detected, version-safe).
  * `render_inline(skill)`   — the collapsible "How to use" panel shown on each
    skill tab (Markdown body + an Outputs block with native title= hover).
  * `render_help_tab(skills)`— the central, browsable Help tab.
  * `HELP_CSS`               — Tier-2 hover-tooltip styling (appended to APP_CSS).

Edit help text in the skill's skill.yaml `help:` block, not here.
"""
from __future__ import annotations

import html
import inspect
from typing import TYPE_CHECKING

import gradio as gr

if TYPE_CHECKING:
    from agents.registry import SkillInfo


# ---------------------------------------------------------------------------
# Tier 1 — feature-detected `info=` helper text.
# ---------------------------------------------------------------------------

_INFO_SUPPORT: dict[type, bool] = {}


def _supports_info(cls: type) -> bool:
    if cls not in _INFO_SUPPORT:
        try:
            _INFO_SUPPORT[cls] = "info" in inspect.signature(cls.__init__).parameters
        except (ValueError, TypeError):
            _INFO_SUPPORT[cls] = False
    return _INFO_SUPPORT[cls]


def maybe_info(cls: type, text: str | None) -> dict:
    """Return {'info': text} if `cls` accepts an info= kwarg, else {}."""
    if text and _supports_info(cls):
        return {"info": text}
    return {}


def input_info_map(skill: "SkillInfo") -> dict[str, str]:
    """Map input name -> short helper text for the component's info= slot."""
    out: dict[str, str] = {}
    if not skill.help:
        return out
    for hi in skill.help.inputs:
        text = hi.tooltip or hi.accepts
        if text:
            out[hi.name] = text
    return out


# ---------------------------------------------------------------------------
# Shared derivations.
# ---------------------------------------------------------------------------

def _output_pattern(skill: "SkillInfo") -> str:
    folder = (skill.help.output_folder if skill.help else "Data/outputs/").rstrip("/")
    if skill.output.type == "directory":
        return f"{folder}/YYYY-MM-DD-HHMMSS-{skill.output.suffix}/"
    return f"{folder}/YYYY-MM-DD-HHMMSS-<input>-{skill.output.suffix}{skill.output.extension}"


def _input_labels(skill: "SkillInfo") -> dict[str, str]:
    return {si.name: si.label for si in skill.inputs}


# ---------------------------------------------------------------------------
# Markdown body (overview, when-to-use, inputs, steps, tips, troubleshooting).
# ---------------------------------------------------------------------------

def _markdown_body(skill: "SkillInfo") -> str:
    h = skill.help
    labels = _input_labels(skill)
    badge = "Runs offline (no LLM)." if not skill.requires.llm else "Needs an LLM endpoint."
    deps = ""
    if skill.requires.native_binaries or skill.requires.external_tools:
        deps = " Requires: " + ", ".join(
            list(skill.requires.native_binaries) + list(skill.requires.external_tools)
        ) + "."
    parts: list[str] = [f"_{badge}{deps}_"]
    if h.overview:
        parts.append(h.overview)
    if h.when_to_use:
        parts.append(f"**When to use it.** {h.when_to_use}")
    if h.inputs:
        parts.append("**Inputs**")
        for hi in h.inputs:
            lbl = labels.get(hi.name, hi.name)
            acc = f" — accepts: {hi.accepts}" if hi.accepts else ""
            parts.append(f"- **{lbl}**{acc}")
            if hi.gotchas:
                parts.append(f"    - ⚠️ {hi.gotchas}")
    steps = h.steps or ("Provide the input(s) and click **Run**.",)
    parts.append("**How to run**")
    parts.append("\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1)))
    if h.tips:
        parts.append(f"**Tips.** {h.tips}")
    if h.troubleshooting:
        parts.append("**Troubleshooting**")
        for t in h.troubleshooting:
            fix = f" → {t.fix}" if t.fix else ""
            parts.append(f"- _{t.problem}_{fix}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Outputs block — native title= hover for "interpreting outputs".
# ---------------------------------------------------------------------------

def _outputs_html(skill: "SkillInfo") -> str:
    h = skill.help
    folder = html.escape((h.output_folder if h else "Data/outputs/"))
    rows = []
    files = h.output_files if h else ()
    if not files:
        files_html = f'<li><code>{html.escape(_output_pattern(skill))}</code></li>'
    else:
        for f in files:
            tip = html.escape(f.tooltip)
            name = html.escape(f.name)
            trigger = (f'<abbr class="pa-tip" title="{tip}">&#9432;</abbr>'
                       if f.tooltip else "")
            desc = f' — {tip}' if f.tooltip else ""
            rows.append(f'<li><code>{name}</code> {trigger}{desc}</li>')
        files_html = "\n".join(rows)
    return (
        '<div class="pa-help-out">'
        f'<p><b>Output</b> → written to <code>{folder}</code>:</p>'
        f'<pre>{html.escape(_output_pattern(skill))}</pre>'
        f'<ul>{files_html}</ul>'
        '</div>'
    )


# ---------------------------------------------------------------------------
# Public: inline panel (per skill tab).
# ---------------------------------------------------------------------------

def render_inline(skill: "SkillInfo") -> None:
    """Render the collapsible help panel. Call inside an open gr context."""
    if not skill.help or skill.help.is_empty():
        return
    with gr.Accordion("ℹ️  How to use — formats & output", open=False):
        gr.Markdown(_markdown_body(skill))
        gr.HTML(_outputs_html(skill))


# ---------------------------------------------------------------------------
# Public: central Help tab.
# ---------------------------------------------------------------------------

# Shown at the bottom of the Help tab — how to edit the help content itself.
EDIT_HELP_MD = (
    "All of this help — overviews, input formats, steps, output explanations, "
    "tips and troubleshooting — is generated from a single **`help:`** block in "
    "each skill's manifest. There is nothing to edit in the app itself.\n\n"
    "**To change a skill's help:**\n\n"
    "1. Open `src/agents/skill_<name>/skill.yaml`.\n"
    "2. Edit its `help:` block (fields: `overview`, `when_to_use`, `inputs` "
    "[`tooltip` / `accepts` / `gotchas`], `steps`, `outputs` [`folder`, `files` "
    "with `name` + `tooltip`], `tips`, `troubleshooting` [`problem` / `fix`]).\n"
    "3. Regenerate the guides:\n\n"
    "   ```\n   python scripts/gen_docs.py\n   ```\n\n"
    "This **Help** tab and the inline panel on each skill tab read the `help:` "
    "block live, so they refresh the next time the app starts — no regeneration "
    "needed for them. Running `gen_docs.py` refreshes the Markdown guides in "
    "`docs/user-guide/`, the bundled `docs/USER-GUIDE.html`, and "
    "`docs/dev/skills-reference.md`.\n\n"
    "Full details: `docs/dev/help-block-schema.md` and "
    "`docs/dev/editing-help.md`. The CI check `tests/test_help_coverage.py` "
    "fails if a skill ships without help or if the generated docs are stale."
)


def render_help_tab(skills: list["SkillInfo"]) -> None:
    """Render the central Help tab: pick a skill, read its full guide."""
    helped = [s for s in skills if s.help and not s.help.is_empty()]
    gr.Markdown(
        "## Help\n\nEvery skill writes to the **`Data/outputs/`** folder with a "
        "timestamped filename. Pick a skill below for its inputs, formats, steps "
        "and how to read each output. Hover the ⓘ icons for output details."
    )
    if not helped:
        gr.Markdown("_No skill help authored yet._")
        return

    choices = [s.display_name for s in helped]
    picker = gr.Dropdown(label="Skill", choices=choices, value=choices[0],
                         interactive=True)
    body = gr.Markdown(_markdown_body(helped[0]))
    outs = gr.HTML(_outputs_html(helped[0]))

    by_name = {s.display_name: s for s in helped}

    def _show(name: str):
        s = by_name.get(name, helped[0])
        return _markdown_body(s), _outputs_html(s)

    picker.change(_show, inputs=picker, outputs=[body, outs])

    # How to change the help content itself (for maintainers).
    with gr.Accordion("\U0001f6e0  How to change this help", open=False):
        gr.Markdown(EDIT_HELP_MD)


# ---------------------------------------------------------------------------
# Tier 2 — hover tooltip CSS (appended to APP_CSS in webui.py).
# ---------------------------------------------------------------------------

HELP_CSS = """
.pa-help-out ul { margin: 4px 0 0 0; padding-left: 20px; }
.pa-help-out li { margin: 4px 0; }
.pa-help-out pre {
    background: #262626; color: #F5F5F5; padding: 8px 10px; border-radius: 6px;
    overflow: auto; font-family: "JetBrains Mono", ui-monospace, monospace;
    font-size: 0.86em;
}
.pa-tip { cursor: help; color: #60A5FA; text-decoration: none; margin: 0 3px; }
.pa-skill h3 { margin-top: 18px; border-bottom: 1px solid #262626; padding-bottom: 4px; }
"""
