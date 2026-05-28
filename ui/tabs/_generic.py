"""
ui/tabs/_generic.py — registry-driven generic skill tab.

Builds a Gradio tab for any skill discovered by agents.registry, using
the metadata in skill.yaml to determine input fields, output type, and
execution flow. Replaces the hand-coded per-skill tab files.

The run handler follows the same accumulating-log + yield-from pattern
established in skill_26as.py / skill_bob.py / skill_hsbc.py.

Supported input types (declared in skill.yaml):
  - "file"      → single file upload (gr.File)
  - "files"     → multi-file upload (gr.File with file_count="multiple").
                   Uploaded files are staged into a temp directory; the
                   input value passed to the skill is that directory path.
  - "directory"  → paste a folder path (gr.Textbox)
  - "text"       → free-text input (gr.Textbox)
"""
from __future__ import annotations

import shutil
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import gradio as gr

from .. import _config
from .. import _health
from .. import _runner

if TYPE_CHECKING:
    from agents.registry import SkillInfo


# ---------------------------------------------------------------------------
# Shared helpers (same as the old hand-coded tabs).
# ---------------------------------------------------------------------------

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


def _check_native_binaries(skill: SkillInfo) -> str | None:
    """Return an error string if required native binaries are missing, else None."""
    needed = skill.requires.native_binaries
    if not needed:
        return None

    from .. import _native
    status = _native.ensure_native_path()
    if status.ok:
        return None

    missing = []
    if "tesseract" in needed and status.tesseract_exe is None:
        missing.append("Tesseract OCR")
    if "poppler" in needed and status.pdftoppm_exe is None:
        missing.append("Poppler (pdftoppm)")
    if missing:
        return (
            f"Error: this build is missing native binaries — {', '.join(missing)}. "
            "Run: python bundling\\refresh_binaries.py and rebuild."
        )
    return None


def _check_external_tools(skill: SkillInfo) -> str | None:
    """Return an error string if required external tools are missing, else None."""
    import shutil
    missing = [t for t in skill.requires.external_tools if shutil.which(t) is None]
    if missing:
        return (
            f"Error: required external tool(s) not found on PATH — {', '.join(missing)}. "
            "Install them and ensure they are accessible."
        )
    return None


# ---------------------------------------------------------------------------
# Generic run handler (generator — yields (markdown, download_update) tuples).
# ---------------------------------------------------------------------------

def _make_run_handler(skill: SkillInfo):
    """
    Return a Gradio-compatible generator function that runs the skill.

    The returned function's signature matches the generic tab's input
    components: (file_or_dir, *text_inputs, model_choice).
    """

    def _run(*args):
        # Last arg is always model_choice; everything before maps to skill.inputs.
        *input_values, model_choice = args
        log: list[str] = []

        def add(line: str) -> str:
            log.append(line)
            return "\n\n".join(log)

        def tick(line: str) -> str:
            if log and log[-1].startswith("**Running** —"):
                log[-1] = line
            else:
                log.append(line)
            return "\n\n".join(log)

        # -- Step 1: validate inputs --
        yield add("**Validating inputs…**"), gr.update(visible=False)

        # Map positional args back to skill input names.
        input_map: dict[str, str] = {}
        for i, inp_def in enumerate(skill.inputs):
            val = input_values[i] if i < len(input_values) else None
            if inp_def.type == "file":
                if val is None:
                    yield add(f"Warning: please provide: {inp_def.label}"), gr.update(visible=False)
                    return
                fpath = Path(val.name if hasattr(val, "name") else val)
                if not fpath.is_file():
                    yield add(f"Warning: file not found at {fpath}"), gr.update(visible=False)
                    return
                input_map[inp_def.name] = str(fpath)
            elif inp_def.type == "files":
                # Multi-file upload: Gradio gives a list of file paths.
                # Stage them into a temp directory so the skill receives
                # a single directory path containing all uploaded files.
                if val is None or (isinstance(val, list) and len(val) == 0):
                    if inp_def.required:
                        yield add(f"Warning: please upload at least one file for: {inp_def.label}"), gr.update(visible=False)
                        return
                    input_map[inp_def.name] = ""
                else:
                    file_list = val if isinstance(val, list) else [val]
                    stage_dir = Path(tempfile.mkdtemp(
                        prefix=f"pa-skills-{skill.name.lower().replace(' ', '-')}-uploads-",
                    ))
                    for fp in file_list:
                        src = Path(fp.name if hasattr(fp, "name") else fp)
                        if src.is_file():
                            shutil.copy2(src, stage_dir / src.name)
                    staged_count = len(list(stage_dir.iterdir()))
                    if staged_count == 0:
                        yield add(f"Warning: no valid files found for: {inp_def.label}"), gr.update(visible=False)
                        return
                    yield add(f"Staged **{staged_count}** file(s) into temp directory."), gr.update(visible=False)
                    input_map[inp_def.name] = str(stage_dir)
            elif inp_def.type == "directory":
                if val is None or str(val).strip() == "":
                    if inp_def.required:
                        yield add(f"Warning: please provide: {inp_def.label}"), gr.update(visible=False)
                        return
                    input_map[inp_def.name] = ""
                else:
                    input_map[inp_def.name] = str(val).strip()
            else:  # text
                input_map[inp_def.name] = str(val or "").strip()

        # -- Step 2: check dependencies --
        native_err = _check_native_binaries(skill)
        if native_err:
            yield add(native_err), gr.update(visible=False)
            return

        tool_err = _check_external_tools(skill)
        if tool_err:
            yield add(tool_err), gr.update(visible=False)
            return

        # -- Step 3: check LLM endpoint --
        yield add("**Checking LLM endpoint…**"), gr.update(visible=False)

        cfg = _config.load_portable_config()
        endpoints = cfg.get("endpoints") or {}
        active = cfg.get("active_endpoint", "")
        ep = endpoints.get(active) or {}
        health = _health.check(ep)
        if not health.ok:
            yield add(
                f"Error: endpoint '{active}' is {health.status}: {health.detail}. "
                "Fix in Data\\settings\\config.yaml and Refresh on the Home tab."
            ), gr.update(visible=False)
            return

        yield add(
            f"**Running** — endpoint OK ({ep.get('base_url', '?')}, model: {model_choice}). "
            "First call may take 30–60s while the model loads."
        ), gr.update(visible=False)

        # -- Build output path --
        out_dir = _config.output_dir()
        stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")

        if skill.output.type == "directory":
            out_path = out_dir / f"{stamp}-{skill.output.suffix}"
            out_path.mkdir(parents=True, exist_ok=True)
        else:
            primary_input = next(
                (v for k, v in input_map.items() if v),
                "output",
            )
            # Use .stem for files (strips extension), .name for dirs/paths
            # (takes last component only — avoids embedding full paths in filename).
            p = Path(primary_input)
            stem = p.stem if p.suffix else p.name
            out_path = out_dir / f"{stamp}-{stem}-{skill.output.suffix}{skill.output.extension}"

        # -- Materialise legacy config --
        try:
            legacy_cfg = _config.materialize_legacy_config(active)
        except Exception as e:
            yield add(f"Error: config error: {e}"), gr.update(visible=False)
            return

        # -- Import the run function --
        try:
            from agents.registry import load_run_function
            run_fn = load_run_function(skill)
        except Exception as e:
            yield add(f"Error: failed to import {skill.entry_point} — {e}"), gr.update(visible=False)
            return

        # -- Build kwargs from skill.run_args template --
        work_dir = tempfile.mkdtemp(prefix=f"pa-skills-{skill.name.lower().replace(' ', '-')}-")
        kwargs: dict[str, str] = {}
        for param, template in skill.run_args.items():
            val = template
            # Replace tokens.
            for inp_name, inp_val in input_map.items():
                val = val.replace(f"{{inputs.{inp_name}}}", inp_val)
            val = val.replace("{output_path}", str(out_path))
            val = val.replace("{output_path_dir}", str(out_path))
            val = val.replace("{config_path}", str(legacy_cfg))
            val = val.replace("{model_override}", model_choice or "")
            val = val.replace("{work_dir}", work_dir)
            # Don't pass empty model_override — let the skill default.
            if param == "model_override" and val == "":
                val = None
            kwargs[param] = val

        # -- Execute --
        def work():
            return run_fn(**kwargs)

        def tick_factory(elapsed: int):
            return (
                tick(f"**Running** — still working ({elapsed}s elapsed)"),
                gr.update(visible=False),
            )

        try:
            agent_reply = yield from _runner.run_with_progress(work, tick_factory)
        except Exception as e:
            tb = "".join(traceback.format_exception(e))
            yield add(
                f"Error: run failed: {e}\n\n"
                f"<details><summary>Traceback</summary>\n\n```\n{tb}\n```\n</details>"
            ), gr.update(visible=False)
            return

        # -- Verify output --
        yield add("**Verifying output…**"), gr.update(visible=False)

        if skill.output.type == "directory":
            if not out_path.is_dir() or not any(out_path.iterdir()):
                yield add(
                    f"Warning: no output produced at {out_path}.\n\n"
                    f"**Agent reply:**\n\n{agent_reply}"
                ), gr.update(visible=False)
                return
            out_abs = str(out_path.resolve())
            msg = add(
                f"### Done\n\n"
                f"**Output folder:** {out_abs}\n\n"
                f"---\n\n**Agent reply:**\n\n{agent_reply}"
            )
            yield msg, gr.update(visible=False)
        else:
            if not out_path.is_file():
                yield add(
                    f"Warning: no output file at {out_path}.\n\n"
                    f"**Agent reply:**\n\n{agent_reply}"
                ), gr.update(visible=False)
                return
            out_abs = str(out_path.resolve())
            msg = add(
                f"### Done\n\n"
                f"**File:** {out_path.name}\n\n"
                f"**Saved to:** {out_abs}\n\n"
                f"Click **{skill.output.download_label}** below.\n\n"
                f"---\n\n**Agent reply:**\n\n{agent_reply}"
            )
            yield msg, gr.update(value=out_abs, visible=True)

    return _run


# ---------------------------------------------------------------------------
# Public: render a tab for a given skill.
# ---------------------------------------------------------------------------

def render(skill: SkillInfo) -> None:
    """
    Render a complete Gradio tab body for the given skill.

    Must be called inside a `with gr.Tab(...)` context.
    """
    # Banner: description + native binary status.
    desc = skill.description.strip()
    banner_parts = [f"## {skill.display_name}\n\n{desc}"]
    if skill.requires.native_binaries:
        native_err = _check_native_binaries(skill)
        if native_err is None:
            banner_parts.append("\n\n_Native OCR binaries detected._")
        else:
            banner_parts.append("\n\n_Native binaries missing — see Run button error for details._")

    gr.Markdown("\n".join(banner_parts))

    with gr.Row():
        with gr.Column(scale=1):
            # Build input components from skill.inputs.
            input_components = []
            for inp in skill.inputs:
                if inp.type == "file":
                    comp = gr.File(
                        label=inp.label,
                        file_types=list(inp.file_types) if inp.file_types else None,
                        type="filepath",
                    )
                elif inp.type == "files":
                    comp = gr.File(
                        label=inp.label,
                        file_types=list(inp.file_types) if inp.file_types else None,
                        file_count="multiple",
                        type="filepath",
                    )
                elif inp.type == "directory":
                    comp = gr.Textbox(
                        label=inp.label,
                        placeholder="Paste full folder path here",
                    )
                else:  # text
                    comp = gr.Textbox(label=inp.label)
                input_components.append(comp)

            # Model dropdown (always present).
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
            result_md = gr.Markdown("_Awaiting input._", min_height=200)
            download = gr.DownloadButton(
                label=skill.output.download_label,
                visible=False,
                variant="primary",
            )

    refresh_models_btn.click(
        fn=lambda: gr.update(choices=_refresh_models()),
        outputs=model_dd,
    )

    handler = _make_run_handler(skill)
    run_btn.click(
        fn=handler,
        inputs=input_components + [model_dd],
        outputs=[result_md, download],
    )
