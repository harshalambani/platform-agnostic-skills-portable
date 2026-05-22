"""
ui/tabs/skill_bob.py — BoB (Bank of Baroda) skill tab.

Shape mirrors skill_26as.py: file upload, model dropdown, Run button,
output preview, download. BoB uses pdfplumber only — no Tesseract/Poppler.
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


def _run_bob(pdf_file, model_choice):
    if pdf_file is None:
        return "Warning: upload a Bank of Baroda statement PDF first.", gr.update(value=None, visible=False)

    pdf_path = Path(pdf_file.name if hasattr(pdf_file, "name") else pdf_file)
    if not pdf_path.is_file():
        return f"Warning: PDF not found at `{pdf_path}`.", gr.update(value=None, visible=False)

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

    out_dir = _config.output_dir()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_path = out_dir / f"{stamp}-{pdf_path.stem}-BoB.csv"

    try:
        legacy_cfg = _config.materialize_legacy_config(active)
    except Exception as e:
        return f"Error: config error: {e}", gr.update(value=None, visible=False)

    try:
        from agents.skill_bob.agent import run as run_bob
    except Exception as e:
        msg = "Error: failed to import `agents.skill_bob.agent`:\n" + f"```\n{e}\n```"
        return msg, gr.update(value=None, visible=False)

    try:
        agent_reply = run_bob(
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
    gr.Markdown(
        """
        ## BoB — Bank of Baroda statement to CSV

        Parses a Bank of Baroda transaction-statement PDF into a flat CSV of
        rows. Handles the multi-page table overflow without column-header
        re-banner that BoB statements are known for.
        """
    )

    with gr.Row():
        with gr.Column(scale=1):
            pdf_upload = gr.File(
                label="Bank of Baroda statement PDF",
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
            download = gr.File(label="Download CSV output", visible=False, interactive=False)

    refresh_models_btn.click(
        fn=lambda: gr.update(choices=_refresh_models()),
        outputs=model_dd,
    )
    run_btn.click(
        fn=_run_bob,
        inputs=[pdf_upload, model_dd],
        outputs=[result_md, download],
    )
