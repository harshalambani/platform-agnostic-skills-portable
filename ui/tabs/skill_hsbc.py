"""
ui/tabs/skill_hsbc.py — HSBC skill tab.

Mirrors the 26AS/BoB shape but adds a native-binaries preflight because
HSBC's OCR path subprocess-shells out to `pdftoppm` (Poppler) and
`tesseract` (Tesseract). _native.ensure_native_path() is called once at
import time so the binaries are on PATH before the skill spins.

The Run handler is a generator: yields immediate progress so the user
sees feedback during the long OCR + LLM run.
"""
from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

import gradio as gr

from .. import _config
from .. import _health
from .. import _native

_NATIVE = _native.ensure_native_path()


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


def _native_warning_or_none() -> str | None:
    if _NATIVE.ok:
        return None
    missing = []
    if _NATIVE.tesseract_exe is None:
        missing.append("Tesseract OCR")
    if _NATIVE.pdftoppm_exe is None:
        missing.append("Poppler (pdftoppm)")
    return (
        f"Error: this build is missing native binaries — {', '.join(missing)}.\n\n"
        "Phase 2a expects them under `vendor/` (source mode) or alongside "
        "`pa_skills.exe` (frozen mode). Run "
        "`python bundling\\refresh_binaries.py --target all` from the project root "
        "and rebuild."
    )


def _run_hsbc(pdf_file, model_choice):
    yield "Running — please wait. HSBC OCR plus LLM can take 60–120s on first invocation.", gr.update(value=None, visible=False)

    if pdf_file is None:
        yield "Warning: upload an HSBC statement PDF first.", gr.update(value=None, visible=False)
        return

    native_err = _native_warning_or_none()
    if native_err is not None:
        yield native_err, gr.update(value=None, visible=False)
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
    out_path = out_dir / f"{stamp}-{pdf_path.stem}-HSBC.xlsx"

    try:
        legacy_cfg = _config.materialize_legacy_config(active)
    except Exception as e:
        yield f"Error: config error: {e}", gr.update(value=None, visible=False)
        return

    try:
        from agents.skill_hsbc.agent import run as run_hsbc
    except Exception as e:
        yield "Error: failed to import `agents.skill_hsbc.agent`:\n" + f"```\n{e}\n```", gr.update(value=None, visible=False)
        return

    yield f"Running HSBC OCR + extraction against `{ep.get('base_url', '?')}` with model `{model_choice}`…", gr.update(value=None, visible=False)

    try:
        agent_reply = run_hsbc(
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
    if _NATIVE.ok:
        banner = f"_Native OCR binaries detected ({_NATIVE.mode} mode)._"
    else:
        banner = "_Native binaries missing — see Run button error for details._"

    gr.Markdown(
        f"""
        ## HSBC — bank statement PDFs to Excel

        Consolidates one or more HSBC bank-statement PDFs into a clean Excel
        workbook with extracted transaction IDs, separate transaction dates,
        scrubbed descriptions, and an Extra Information column. Scanned
        pages flow through Tesseract OCR; text-extractable pages skip the
        OCR step automatically.

        {banner}
        """
    )

    with gr.Row():
        with gr.Column(scale=1):
            pdf_upload = gr.File(
                label="HSBC statement PDF",
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
        fn=_run_hsbc,
        inputs=[pdf_upload, model_dd],
        outputs=[result_md, download],
    )
