"""
ui/tabs/skill_hsbc.py — HSBC skill tab.

Same accumulating-log + yield-from pattern as BoB/26AS, plus a
native-binaries preflight because HSBC's OCR path subprocess-shells
out to pdftoppm + tesseract.
"""
from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

import gradio as gr

from .. import _config
from .. import _health
from .. import _native
from .. import _runner

_NATIVE = _native.ensure_native_path()


def _refresh_models() -> list[tuple[str, str]]:
    """Return (display_label, raw_name) pairs with capability badges."""
    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    active = cfg.get("active_endpoint", "")
    ep = endpoints.get(active) or {}
    choices = _health.get_model_choices(ep)
    if choices:
        return choices
    fallback = ep.get("default_model")
    return [(fallback, fallback)] if fallback else []


def _native_warning_or_none() -> str | None:
    if _NATIVE.ok:
        return None
    missing = []
    if _NATIVE.tesseract_exe is None:
        missing.append("Tesseract OCR")
    if _NATIVE.pdftoppm_exe is None:
        missing.append("Poppler (pdftoppm)")
    return (
        f"Error: this build is missing native binaries — {', '.join(missing)}. "
        "Run: python bundling\\refresh_binaries.py --from-tesseract \"<install>\" and rebuild."
    )


def _run_hsbc(pdf_file, model_choice):
    log: list[str] = []

    def add(line: str) -> str:
        log.append(line)
        return "\n\n".join(log)

    def tick(line: str) -> str:
        if log and log[-1].startswith("**Step 4/5** — OCR + agent running"):
            log[-1] = line
        else:
            log.append(line)
        return "\n\n".join(log)

    yield add("**Step 1/5** — Validating inputs."), gr.update(visible=False)

    if pdf_file is None:
        yield add("Warning: upload an HSBC statement PDF first."), gr.update(visible=False)
        return

    native_err = _native_warning_or_none()
    if native_err is not None:
        yield add(native_err), gr.update(visible=False)
        return

    pdf_path = Path(pdf_file.name if hasattr(pdf_file, "name") else pdf_file)
    if not pdf_path.is_file():
        yield add(f"Warning: PDF not found at {pdf_path}."), gr.update(visible=False)
        return

    yield add("**Step 2/5** — Native OCR binaries detected."), gr.update(visible=False)
    yield add("**Step 3/5** — Checking the LLM endpoint."), gr.update(visible=False)

    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    active = cfg.get("active_endpoint", "")
    ep = endpoints.get(active) or {}
    health = _health.check(ep)
    if not health.ok:
        yield add(
            f"Error: active endpoint '{active}' is {health.status}: {health.detail}. "
            "Fix it in Data\\settings\\config.yaml and click Refresh status on the Home tab."
        ), gr.update(visible=False)
        return

    yield add(
        f"**Step 4/5** — Endpoint OK. Running OCR + agent loop against {ep.get('base_url', '?')} "
        f"with model {model_choice}. Scanned pages flow through Tesseract — this is the slow part."
    ), gr.update(visible=False)

    out_dir = _config.output_dir()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_path = out_dir / f"{stamp}-{pdf_path.stem}-HSBC.xlsx"

    try:
        legacy_cfg = _config.materialize_legacy_config(active)
    except Exception as e:
        yield add(f"Error: config error: {e}"), gr.update(visible=False)
        return

    try:
        from agents.skill_hsbc.agent import run as run_hsbc
    except Exception as e:
        yield add(f"Error: failed to import agents.skill_hsbc.agent — {e}"), gr.update(visible=False)
        return

    def work():
        return run_hsbc(
            pdf_path=str(pdf_path),
            output_path=str(out_path),
            config_path=str(legacy_cfg),
            model_override=model_choice or None,
        )

    def tick_factory(elapsed: int):
        return tick(f"**Step 4/5** — OCR + agent running… still working ({elapsed}s elapsed)"), gr.update(visible=False)

    try:
        agent_reply = yield from _runner.run_with_progress(work, tick_factory)
    except Exception as e:
        tb = "".join(traceback.format_exception(e))
        yield add(
            f"Error: run failed: {e}\n\n<details><summary>Traceback</summary>\n\n```\n{tb}\n```\n</details>"
        ), gr.update(visible=False)
        return

    yield add("**Step 5/5** — Verifying output."), gr.update(visible=False)

    if not out_path.is_file():
        yield add(
            f"Warning: skill returned but no output file was produced at {out_path}.\n\n"
            f"**Agent reply:**\n\n{agent_reply}"
        ), gr.update(visible=False)
        return

    out_abs = str(out_path.resolve())
    msg = add(
        f"### ✓ Extraction complete\n\n"
        f"**File:** {out_path.name}\n\n"
        f"**Saved to:** {out_abs}\n\n"
        f"Click the **Download Excel** button below to save it locally.\n\n"
        f"---\n\n"
        f"**Agent reply:**\n\n{agent_reply}"
    )
    yield msg, gr.update(value=out_abs, visible=True)


def render() -> None:
    if _NATIVE.ok:
        banner = "_Native OCR binaries detected._"
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
            pdf_upload = gr.File(label="HSBC statement PDF", file_types=[".pdf"], type="filepath")
            initial_choices = _refresh_models()
            model_dd = gr.Dropdown(
                label="Model", choices=initial_choices,
                value=initial_choices[0][1] if initial_choices else None,
                allow_custom_value=True, interactive=True,
            )
            refresh_models_btn = gr.Button("Refresh model list", variant="secondary")
            with gr.Row():
                run_btn = gr.Button("Run", variant="primary")
                stop_btn = gr.Button("Stop", variant="stop")

        with gr.Column(scale=2):
            result_md = gr.Markdown("_Awaiting input._", min_height=200)
            download = gr.DownloadButton(label="Download Excel", visible=False, variant="primary")

    refresh_models_btn.click(fn=lambda: gr.update(choices=_refresh_models()), outputs=model_dd)

    def _handle_stop():
        _runner.request_cancel()
        return "**Cancelled** — stopping after current step."

    stop_btn.click(fn=_handle_stop, outputs=result_md)
    run_btn.click(fn=_run_hsbc, inputs=[pdf_upload, model_dd], outputs=[result_md, download])
