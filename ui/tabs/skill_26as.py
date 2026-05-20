"""
ui/tabs/skill_26as.py — 26AS skill tab.

Shape per spec §9.2: file upload, model dropdown, Run button, output preview,
download link. Runs the upstream `agents.skill_26as.agent.run` function with
a transient legacy-shaped config materialised by ui._config.materialize_legacy_config.
"""
from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

import gradio as gr

from .. import _config
from .. import _health


def _refresh_models() -> list[str]:
    """Pull the model list from the active endpoint, falling back to the
    endpoint's default_model if the health probe fails."""
    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    active = cfg.get("active_endpoint", "")
    ep = endpoints.get(active) or {}
    res = _health.check(ep)
    if res.ok and res.models:
        return list(res.models)
    fallback = ep.get("default_model")
    return [fallback] if fallback else []


def _run_26as(pdf_file, model_choice):
    """Invoked when the user clicks Run."""
    if pdf_file is None:
        return "Warning: upload a Form 26AS PDF first.", gr.update(value=None, visible=False)

    pdf_path = Path(pdf_file.name if hasattr(pdf_file, "name") else pdf_file)
    if not pdf_path.is_file():
        return f"Warning: PDF not found at `{pdf_path}`.", gr.update(value=None, visible=False)

    # Pre-flight: make sure the active endpoint is reachable.
    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    active = cfg.get("active_endpoint", "")
    ep = endpoints.get(active) or {}
    health = _health.check(ep)
    if not health.ok:
        msg = (
            f"Error: active endpoint `{active}` is {health.status}: {health.detail}\n\n"
            "Fix it in `Data\\settings\\config.yaml` and click Refresh status on the Home tab."
        )
        return msg, gr.update(value=None, visible=False)

    # Resolve the output path inside the project's outputs dir.
    out_dir = _config.output_dir()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_path = out_dir / f"{stamp}-{pdf_path.stem}-26AS.xlsx"

    # Materialise the legacy config the skill expects.
    try:
        legacy_cfg = _config.materialize_legacy_config(active)
    except Exception as e:
        return f"Error: config error: {e}", gr.update(value=None, visible=False)

    # Lazy import the skill module (pulls heavy LangChain deps).
    try:
        from agents.skill_26as.agent import run as run_26as
    except Exception as e:
        msg = "Error: failed to import `agents.skill_26as.agent`:\n" + f"```\n{e}\n```"
        return msg, gr.update(value=None, visible=False)

    try:
        agent_reply = run_26as(
            pdf_path=str(pdf_path),
            output_path=str(out_path),
            config_path=str(legacy_cfg),
            model_override=model_choice or None,
        )
    except Exception as e:
        tb = "".join(traceback.format_exception(e))
        details = (
            f"Error: run failed: {e}\n\n"
            f"<details><summary>Traceback</summary>\n\n```\n{tb}\n```\n</details>"
        )
        return details, gr.update(value=None, visible=False)

    if not out_path.is_file():
        msg = (
            f"Warning: skill returned but no output file was produced at `{out_path}`.\n\n"
            f"Agent reply:\n\n```\n{agent_reply}\n```"
        )
        return msg, gr.update(value=None, visible=False)

    msg = (
        f"Extraction complete.\n\n"
        f"**Output:** `{out_path.name}`\n\n"
        f"**Agent reply:**\n\n```\n{agent_reply}\n```"
    )
    return msg, gr.update(value=str(out_path), visible=True)


def render() -> None:
    """Render the 26AS tab; must be called inside a gr.Tab context."""
    gr.Markdown(
        """
        ## 26AS — Form 26AS PDF to Excel

        Convert an Indian Income Tax Form 26AS (TRACES Annual Tax Statement) into
        a single Excel workbook with one sheet per Part. Per-deductor sub-totals
        are inserted inline; a Grand Total row closes Part I.
        """
    )

    with gr.Row():
        with gr.Column(scale=1):
            pdf_upload = gr.File(
                label="Form 26AS PDF",
                file_types=[".pdf"],
                type="filepath",
            )
            initial_models = _refresh_models()
            model_dd = gr.Dropdown(
                label="Model",
                choices=initial_models,
                value=initial_models[0] if initial_models else None,
                allow_custom_value=True,
                interactive=True,
            )
            refresh_models_btn = gr.Button("Refresh model list", variant="secondary")
            run_btn = gr.Button("Run", variant="primary")

        with gr.Column(scale=2):
            result_md = gr.Markdown("_Awaiting input._")
            download = gr.File(label="Download Excel output", visible=False, interactive=False)

    refresh_models_btn.click(
        fn=lambda: gr.update(choices=_refresh_models()),
        outputs=model_dd,
    )
    run_btn.click(
        fn=_run_26as,
        inputs=[pdf_upload, model_dd],
        outputs=[result_md, download],
    )
