"""
ui/tabs/skill_26as.py — 26AS skill tab.

Same accumulating-log + yield-from pattern as BoB.
"""
from __future__ import annotations

import traceback
from datetime import datetime
from pathlib import Path

import gradio as gr

from .. import _config
from .. import _health
from .. import _runner


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


def _run_26as(pdf_file, model_choice):
    log: list[str] = []

    def add(line: str) -> str:
        log.append(line)
        return "\n\n".join(log)

    def tick(line: str) -> str:
        if log and log[-1].startswith("**Step 3/4** — Agent running"):
            log[-1] = line
        else:
            log.append(line)
        return "\n\n".join(log)

    yield add("**Step 1/4** — Validating inputs."), gr.update(visible=False)

    if pdf_file is None:
        yield add("Warning: upload a Form 26AS PDF first."), gr.update(visible=False)
        return

    pdf_path = Path(pdf_file.name if hasattr(pdf_file, "name") else pdf_file)
    if not pdf_path.is_file():
        yield add(f"Warning: PDF not found at {pdf_path}."), gr.update(visible=False)
        return

    yield add("**Step 2/4** — Checking the LLM endpoint."), gr.update(visible=False)

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
        f"**Step 3/4** — Endpoint OK. Calling agent loop against {ep.get('base_url', '?')} "
        f"with model {model_choice}. First call can take 30–60s while the model warms up."
    ), gr.update(visible=False)

    out_dir = _config.output_dir()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    out_path = out_dir / f"{stamp}-{pdf_path.stem}-26AS.xlsx"

    try:
        legacy_cfg = _config.materialize_legacy_config(active)
    except Exception as e:
        yield add(f"Error: config error: {e}"), gr.update(visible=False)
        return

    try:
        from agents.skill_26as.agent import run as run_26as
    except Exception as e:
        yield add(f"Error: failed to import agents.skill_26as.agent — {e}"), gr.update(visible=False)
        return

    def work():
        return run_26as(
            pdf_path=str(pdf_path),
            output_path=str(out_path),
            config_path=str(legacy_cfg),
            model_override=model_choice or None,
        )

    def tick_factory(elapsed: int):
        return tick(f"**Step 3/4** — Agent running… still working ({elapsed}s elapsed)"), gr.update(visible=False)

    try:
        agent_reply = yield from _runner.run_with_progress(work, tick_factory)
    except Exception as e:
        tb = "".join(traceback.format_exception(e))
        yield add(
            f"Error: run failed: {e}\n\n<details><summary>Traceback</summary>\n\n```\n{tb}\n```\n</details>"
        ), gr.update(visible=False)
        return

    yield add("**Step 4/4** — Verifying output."), gr.update(visible=False)

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
            pdf_upload = gr.File(label="Form 26AS PDF", file_types=[".pdf"], type="filepath")
            initial_choices = _refresh_models()
            model_dd = gr.Dropdown(
                label="Model", choices=initial_choices,
                value=initial_choices[0][1] if initial_choices else None,
                allow_custom_value=True, interactive=True,
            )
            refresh_models_btn = gr.Button("Refresh model list", variant="secondary")
            run_btn = gr.Button("Run", variant="primary")

        with gr.Column(scale=2):
            result_md = gr.Markdown("_Awaiting input._", min_height=200)
            download = gr.DownloadButton(label="Download Excel", visible=False, variant="primary")

    refresh_models_btn.click(fn=lambda: gr.update(choices=_refresh_models()), outputs=model_dd)
    run_btn.click(fn=_run_26as, inputs=[pdf_upload, model_dd], outputs=[result_md, download])
