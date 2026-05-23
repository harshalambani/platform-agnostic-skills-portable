"""
ui/tabs/skill_26as.py — 26AS skill tab.

Shape per spec §9.2: file upload, model dropdown, Run button, output preview,
download link. The Run handler is a generator that yields a "Running..."
status immediately, then the final result.
"""
from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

import gradio as gr

from .. import _config
from .. import _health


def _refresh_models() -> list[str]:
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
    yield "Running — please wait. The LLM call alone can take 30–60s on first invocation.", gr.update(value=None, visible=False)

    if pdf_file is None:
        yield "Warning: upload a Form 26AS PDF first.", gr.update(value=None, visible=False)
        return

    pdf_path = Path(pdf_file.name if hasattr(pdf_file, "name") else pdf_file)
    if not pdf_path.is_file():
        yield f"Warning: PDF not found at `{pdf_path}`.", gr.update(value=None, visible=False)
        return

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
        yield msg, gr.update(value=None, visible=False)
        return

    out_dir = _config.output_dir()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_path = out_dir / f"{stamp}-{pdf_path.stem}-26AS.xlsx"

    try:
        legacy_cfg = _config.materialize_legacy_config(active)
    except Exception as e:
        yield f"Error: config error: {e}", gr.update(value=None, visible=False)
        return

    try:
        from agents.skill_26as.agent import run as run_26as
    except Exception as e:
        yield "Error: failed to import `agents.skill_26as.agent`:\n" + f"```\n{e}\n```", gr.update(value=None, visible=False)
        return

    yield f"Running 26AS extraction against `{ep.get('base_url', '?')}` with model `{model_choice}`…", gr.update(value=None, visible=False)

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
        yield details, gr.update(value=None, visible=False)
        return

    if not out_path.is_file():
        msg = (
            f"Warning: skill returned but no output file was produced at `{out_path}`.\n\n"
            f"Agent reply:\n\n```\n{agent_reply}\n```"
        )
        yield msg, gr.update(value=None, visible=False)
        return

    msg = (
        f"Extraction complete.\n\n"
        f"**Output:** `{out_path.name}`\n\n"
        f"**Agent reply:**\n\n```\n{agent_reply}\n```"
    )
    yield msg, gr.update(value=str(out_path.resolve()), visible=True)


def render() -> None:
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
            result_md = gr.Markdown("_Awaiting input._", min_height=120)
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
