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
  - "select"    → dropdown with predefined choices (gr.Dropdown).
                   Requires "options: [...]" in skill.yaml, OR
                   "options_from: <key>" to resolve choices dynamically at
                   render time (with a refresh button) — see
                   _OPTIONS_FROM_RESOLVERS below. Allows custom values typed
                   by the user either way.
  - "directory"  → paste a folder path (gr.Textbox)
  - "text"       → free-text input (gr.Textbox)
  - "password"   → masked free-text input (gr.Textbox, type="password").
                   Shoulder-surfing protection only — the value is passed as
                   a run arg, not stored at rest. Use for secrets that aren't
                   already covered by the Settings-tab API key.
"""
from __future__ import annotations

import re
import shutil
import tempfile
import traceback
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

import gradio as gr

from .. import _config
from .. import _filedialog
from .. import _health
from .. import _help
from .. import _review_csv
from .. import _runner
from .. import _runlog

if TYPE_CHECKING:
    from agents.registry import SkillInfo


# ---------------------------------------------------------------------------
# Upload safety limits (security: finding #7)
#
# Gradio's file-type filter in gr.File is a browser-side hint only — it does
# not prevent a user from uploading an oversized or unexpected file via the
# API.  These constants are enforced server-side before any file is copied
# into the staging directory and fed to a native parser (Poppler, Tesseract,
# qpdf, pypdf).
#
# PATCH CADENCE NOTE: the native binaries bundled in vendor/ (Tesseract, Poppler,
# qpdf) should be updated on each release cycle.  Parser vulnerabilities in these
# binaries are the primary attack surface for untrusted document inputs.  Track
# their CVE feeds and update refresh_binaries.py SHA pins when new versions ship.
# ---------------------------------------------------------------------------

_MAX_UPLOAD_SIZE_BYTES: int = 100 * 1024 * 1024  # 100 MB per file
_MAX_FILE_COUNT: int = 20                          # max files per run


# ---------------------------------------------------------------------------
# Shared helpers (same as the old hand-coded tabs).
# ---------------------------------------------------------------------------

_choices_cache: list[tuple[str, str]] | None = None


def _scan_output_files(match: str, file_types: tuple[str, ...]) -> list[tuple[str, str]]:
    """List files in the output dir for an 'output_file' picker, newest first.

    Returns (label, value) pairs where the label is the file NAME (so the
    dropdown shows the name, not a long truncated path) and the value is the
    full path passed to the skill. Uses the input's `match` glob (e.g.
    '*-26AS.xlsx') when given, otherwise '*<ext>' for each declared file type.
    """
    try:
        out_dir = _config.output_dir()
    except Exception:
        return []
    patterns = [match] if match else ([f"*{ext}" for ext in file_types] or ["*"])
    found: set = set()
    for pat in patterns:
        found.update(out_dir.glob(pat))
    files = sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)
    return [(p.name, str(p)) for p in files[:30]]


def _options_from_itr_entities() -> list[tuple[str, str]]:
    """(label, entity_key) pairs from Data/itr/entities.yaml, for the ITR
    Workbook skill's `entity` dropdown (options_from: itr_entities). Reads
    fresh on every call so entities.yaml edits show up on refresh without a
    restart; gracefully empty when the file is absent (first run) or
    malformed (caller keeps the dropdown usable via allow_custom_value)."""
    path = _config.data_root_dir() / "itr" / "entities.yaml"
    if not path.is_file():
        return []
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return []
        return sorted(
            (f"{key} ({fields_.get('status', '?')})" if isinstance(fields_, dict) else key, key)
            for key, fields_ in raw.items()
        )
    except Exception:
        return []


def _options_from_itr_ay_years() -> list[tuple[str, str]]:
    """(year_label, year_key) pairs from Data/itr/rules/tax_rules_*.yaml, for
    the ITR Workbook skill's `ay` dropdown (options_from: itr_ay_years).
    year_key is the canonical income-year key (e.g. "2025-26") used by
    rules.load_rules() and the hard-fail year-mismatch check in agent.py."""
    rules_dir = _config.data_root_dir() / "itr" / "rules"
    if not rules_dir.is_dir():
        return []
    try:
        import yaml
        pairs = []
        for p in sorted(rules_dir.glob("tax_rules_*.yaml")):
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            meta = raw.get("meta", {}) if isinstance(raw, dict) else {}
            fy = meta.get("fy")
            if fy:
                pairs.append((meta.get("year_label", fy), fy))
        # Newest year first (matches the plan's "default first option = live
        # filing year" convention).
        return sorted(pairs, key=lambda pair: pair[1], reverse=True)
    except Exception:
        return []


# Named dynamic option sources for SkillInput.options_from. Keyed by the
# string used in skill.yaml (`options_from: <key>`); each resolver returns
# (label, value) pairs. Add an entry here whenever a new skill needs a
# dropdown driven by a data file rather than a static `options:` list.
_OPTIONS_FROM_RESOLVERS = {
    "itr_entities": _options_from_itr_entities,
    "itr_ay_years": _options_from_itr_ay_years,
}


def _resolve_options_from(key: str) -> list[tuple[str, str]]:
    resolver = _OPTIONS_FROM_RESOLVERS.get(key)
    if resolver is None:
        return []
    return resolver()


def _scan_parser_files() -> list[tuple[str, str]]:
    """(label, path) pairs of the project's embedded parsers for a 'parser_file'
    picker. Label is 'skill_dir / filename'; value is the full path. Used by the
    Parser Generator tab so 'Fix' can pick a known parser instead of typing it.
    """
    try:
        from agents.registry import discover_parser_scripts
    except Exception:
        return []
    pairs = []
    for p in discover_parser_scripts():
        skill_dir = p.parent.parent.name
        pairs.append((f"{skill_dir} / {p.name}", str(p)))
    return pairs


def _refresh_models(*, use_cache: bool = False) -> list[tuple[str, str]]:
    """Return (display_label, raw_name) pairs with capability badges.

    Display labels look like 'gemma4:12b (tools)' or 'llama3.2:3b (text-only)'.
    The *value* sent to the runner is the plain model name.
    """
    global _choices_cache
    if use_cache and _choices_cache is not None:
        return list(_choices_cache)
    cfg = _config.load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    active = cfg.get("active_endpoint", "")
    ep = endpoints.get(active) or {}
    choices = _health.get_model_choices(ep)
    if choices:
        _choices_cache = choices
        return list(_choices_cache)
    fallback = _config.default_model_for(ep, cfg)
    _choices_cache = [(fallback, fallback)] if fallback else []
    return list(_choices_cache)


def _default_model_value(choices: list[tuple[str, str]]):
    """Pre-select the configured default model when it is among the available
    models, so a fresh tab defaults to the config.yaml `default_model` knob
    rather than whatever model happens to be listed first."""
    if not choices:
        return None
    cfg = _config.load_portable_config()
    ep = (cfg.get("endpoints") or {}).get(cfg.get("active_endpoint", "")) or {}
    want = _config.default_model_for(ep, cfg)
    values = [v for _, v in choices]
    return want if want in values else choices[0][1]


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
# RAG colouring for run-status output.
#
# Wraps status lines in coloured <div> blocks so the result panel makes
# success / warning / error states obvious at a glance (green / amber / red).
# Only lines that clearly signal a state are recoloured; ordinary progress
# lines and the agent reply keep the default colour. Applied to every skill
# tab via add()/tick() in the run handler.
# ---------------------------------------------------------------------------

def _colorize_status(md: str) -> str:
    out: list[str] = []
    for ln in md.split("\n"):
        s = ln.strip()
        low = s.lower()
        if not s:
            out.append(ln)
            continue
        if low.startswith("error:") or low.startswith("security error:"):
            out.append(f'<div class="rag-error">⛔ {s}</div>')
        elif low.startswith("warning:"):
            out.append(f'<div class="rag-warn">⚠️ {s[len("warning:"):].strip()}</div>')
        elif "**cancelled**" in low or low.startswith("cancelled"):
            out.append(f'<div class="rag-warn">{s.replace("**", "")}</div>')
        elif (s.startswith("### Done") or s.startswith("### ✓") or s.startswith("✓")
              or low.startswith("ok —") or low.startswith("ok -")
              or "all balanced" in low or "complete" in low):
            txt = s.lstrip("#").replace("**", "").strip()
            if txt.startswith("✓"):
                txt = txt[1:].strip()
            out.append(f'<div class="rag-ok">✅ {txt}</div>')
        else:
            out.append(ln)
    return "\n".join(out)


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
            return _colorize_status("\n\n".join(log))

        def tick(line: str) -> str:
            if log and log[-1].startswith("**Running** —"):
                log[-1] = line
            else:
                log.append(line)
            return _colorize_status("\n\n".join(log))

        # -- Step 1: validate inputs --
        # Clear any saved-path text left over from a prior run on this tab.
        yield add("**Validating inputs…**"), gr.update(interactive=False, value=None), gr.update(value="")

        # Map positional args back to skill input names.
        input_map: dict[str, str] = {}
        for i, inp_def in enumerate(skill.inputs):
            val = input_values[i] if i < len(input_values) else None
            if inp_def.type in ("file", "output_file"):
                if val is None or (isinstance(val, str) and not val.strip()):
                    if inp_def.required:
                        yield add(f"Warning: please provide: {inp_def.label}"), gr.update(interactive=False, value=None), gr.update()
                        return
                    input_map[inp_def.name] = ""
                else:
                    fpath = Path(val.name if hasattr(val, "name") else val)
                    if not fpath.is_file():
                        yield add(f"Warning: file not found at {fpath}"), gr.update(interactive=False, value=None), gr.update()
                        return
                    input_map[inp_def.name] = str(fpath)
            elif inp_def.type == "files":
                # Multi-file upload: Gradio gives a list of file paths.
                # Stage them into a temp directory so the skill receives
                # a single directory path containing all uploaded files.
                if val is None or (isinstance(val, list) and len(val) == 0):
                    if inp_def.required:
                        yield add(f"Warning: please upload at least one file for: {inp_def.label}"), gr.update(interactive=False, value=None), gr.update()
                        return
                    input_map[inp_def.name] = ""
                else:
                    file_list = val if isinstance(val, list) else [val]

                    # -- File count cap (security: finding #7) --
                    if len(file_list) > _MAX_FILE_COUNT:
                        yield add(
                            f"Error: too many files — received {len(file_list)}, "
                            f"maximum is {_MAX_FILE_COUNT} per run."
                        ), gr.update(interactive=False, value=None), gr.update()
                        return

                    stage_dir = Path(tempfile.mkdtemp(
                        prefix=f"pa-skills-{skill.name.lower().replace(' ', '-')}-uploads-",
                    ))
                    for fp in file_list:
                        src = Path(fp.name if hasattr(fp, "name") else fp)
                        if not src.is_file():
                            continue

                        # -- Per-file size cap (security: finding #7) --
                        try:
                            file_size = src.stat().st_size
                        except OSError:
                            file_size = 0
                        if file_size > _MAX_UPLOAD_SIZE_BYTES:
                            yield add(
                                f"Error: file **{src.name}** is too large "
                                f"({file_size // (1024 * 1024)} MB) — "
                                f"maximum is {_MAX_UPLOAD_SIZE_BYTES // (1024 * 1024)} MB per file."
                            ), gr.update(interactive=False, value=None), gr.update()
                            return

                        shutil.copy2(src, stage_dir / src.name)
                    staged_count = len(list(stage_dir.iterdir()))
                    if staged_count == 0:
                        yield add(f"Warning: no valid files found for: {inp_def.label}"), gr.update(interactive=False, value=None), gr.update()
                        return
                    yield add(f"Staged **{staged_count}** file(s) into temp directory."), gr.update(interactive=False, value=None), gr.update()
                    input_map[inp_def.name] = str(stage_dir)
            elif inp_def.type == "directory":
                if val is None or str(val).strip() == "":
                    if inp_def.required:
                        yield add(f"Warning: please provide: {inp_def.label}"), gr.update(interactive=False, value=None), gr.update()
                        return
                    input_map[inp_def.name] = ""
                else:
                    input_map[inp_def.name] = str(val).strip()
            elif inp_def.type in ("select", "parser_file"):
                input_map[inp_def.name] = str(val or "").strip()
            else:  # text
                input_map[inp_def.name] = str(val or "").strip()

        # -- Step 2: check dependencies --
        native_err = _check_native_binaries(skill)
        if native_err:
            yield add(native_err), gr.update(interactive=False, value=None), gr.update()
            return

        tool_err = _check_external_tools(skill)
        if tool_err:
            yield add(tool_err), gr.update(interactive=False, value=None), gr.update()
            return

        # -- Step 3: resolve endpoint config (always; needed later for
        #    materialize_legacy_config), then health-check it only for skills
        #    that actually use an LLM. --
        cfg = _config.load_portable_config()
        endpoints = cfg.get("endpoints") or {}
        active = cfg.get("active_endpoint", "")
        ep = endpoints.get(active) or {}

        if skill.requires.llm:
            yield add("**Checking LLM endpoint…**"), gr.update(interactive=False, value=None), gr.update()

            health = _health.check(ep)
            if not health.ok:
                yield add(
                    f"Error: endpoint '{active}' is {health.status}: {health.detail}. "
                    "Fix in Data\\settings\\config.yaml and Refresh on the Home tab."
                ), gr.update(interactive=False, value=None), gr.update()
                return

            yield add(
                f"**Running** — endpoint OK ({ep.get('base_url', '?')}, model: {model_choice}). "
                "First call may take 30–60s while the model loads."
            ), gr.update(interactive=False, value=None), gr.update()
        else:
            # Deterministic skill — no LLM endpoint required; run fully offline.
            yield add(
                "**Running** — deterministic skill (no LLM required)."
            ), gr.update(interactive=False, value=None), gr.update()

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
            # If the chosen input is itself a prior run output (output_file
            # picker), it already carries a "YYYY-MM-DD-HHMMSS-" stamp; strip it
            # so we don't double-stamp and bloat the path.
            stem = re.sub(r"^\d{4}-\d{2}-\d{2}-\d{6}-", "", stem)
            out_path = out_dir / f"{stamp}-{stem}-{skill.output.suffix}{skill.output.extension}"

        # -- Materialise legacy config --
        try:
            legacy_cfg = _config.materialize_legacy_config(active)
        except Exception as e:
            yield add(f"Error: config error: {e}"), gr.update(interactive=False, value=None), gr.update()
            return

        # -- Import the run function --
        try:
            from agents.registry import load_run_function
            run_fn = load_run_function(skill)
        except Exception as e:
            yield add(f"Error: failed to import {skill.entry_point} — {e}"), gr.update(interactive=False, value=None), gr.update()
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
            # Same Data\ anchor as data_root_dir() in both source and frozen
            # builds -- lets a skill.yaml `run:` token resolve config
            # subfolders (e.g. Data/itr/...) without baking in a CWD-relative
            # "Data/" prefix that doubles up when the frozen Launcher already
            # sets CWD to Data\ (see agent.py Batch 8 / defect A).
            val = val.replace("{data_root}", str(_config.data_root_dir()))
            # Don't pass empty model_override — let the skill default.
            if param == "model_override" and val == "":
                val = None
            kwargs[param] = val

        # -- Execute --
        def work():
            return run_fn(**kwargs)

        try:
            if skill.mode == "agent":
                # Agent-mode: use streaming runner for live progress.
                def make_tuple(md: str):
                    return (md, gr.update(interactive=False, value=None), gr.update())

                agent_reply = yield from _runner.run_with_streaming(
                    work, log, make_tuple,
                )
            else:
                # Direct-mode: elapsed-time ticks only.
                def tick_factory(elapsed: int):
                    return (
                        tick(f"**Running** — still working ({elapsed}s elapsed)"),
                        gr.update(interactive=False, value=None), gr.update(),
                    )

                agent_reply = yield from _runner.run_with_progress(work, tick_factory)
        except Exception as e:
            tb = "".join(traceback.format_exception(e))
            log_path = _runlog.new_log_path(skill.name)
            _runlog.write_run_log(
                log_path, skill_name=skill.name, run_log_lines=log, traceback_text=tb,
            )
            yield add(
                f"Error: run failed: {e}\n\n"
                f"Full log: `{log_path}`\n\n"
                f"<details><summary>Traceback</summary>\n\n```\n{tb}\n```\n</details>"
            ), gr.update(interactive=False, value=None), gr.update()
            return

        # -- Handle cancellation --
        if agent_reply == "__CANCELLED__":
            yield add("**Cancelled** — run was stopped by user."), gr.update(interactive=False, value=None), gr.update()
            return

        # -- Log this run (agent-mode: `log` also carries the tool-call
        #    transcript) regardless of outcome, so a silently-absorbed tool
        #    failure inside a successful-looking agent reply still leaves a
        #    trace on disk. --
        log_path = _runlog.new_log_path(skill.name)
        _runlog.write_run_log(log_path, skill_name=skill.name, run_log_lines=log)

        # -- Verify output --
        yield add("**Verifying output…**"), gr.update(interactive=False, value=None), gr.update()

        if skill.output.type == "directory":
            if not out_path.is_dir() or not any(out_path.iterdir()):
                yield add(
                    f"Error: the run did not finish successfully — no output "
                    f"was produced at {out_path}. Check the details below, fix "
                    f"the input, and run again.\n\n"
                    f"Full log: `{log_path}`\n\n"
                    f"**Agent reply:**\n\n{agent_reply}"
                ), gr.update(interactive=False, value=None), gr.update()
                return
            out_abs = str(out_path.resolve())

            # -- Surface any Review.csv the skill dropped in its output dir --
            # Generic across skills (not KRC-specific): any directory-output
            # skill that writes a "Review.csv" of rows it couldn't fully
            # process gets it rendered inline here instead of only a buried
            # agent-reply line. See ui/_review_csv.py.
            review_section = ""
            review_csv_path = _review_csv.find_review_csv(out_path)
            if review_csv_path is not None:
                try:
                    review_section = _review_csv.render_review_section_html(review_csv_path)
                except Exception:
                    review_section = ""

            msg = add(
                f"### Done\n\n"
                f"**Output folder:** {out_abs}\n\n"
                f"{review_section}"
                f"---\n\n**Agent reply:**\n\n{agent_reply}"
            )
            yield msg, gr.update(interactive=False, value=None), gr.update(value=out_abs)
        else:
            if not out_path.is_file():
                yield add(
                    f"Error: the run did not finish successfully — no output "
                    f"file was produced, so there is nothing to download. "
                    f"Check the details below, fix the input, and run again.\n\n"
                    f"Full log: `{log_path}`\n\n"
                    f"**Agent reply:**\n\n{agent_reply}"
                ), gr.update(interactive=False, value=None), gr.update()
                return

            # --- Path containment + download staging (security: finding #5) ---
            # 1. Assert the produced file resolves inside output_dir so a buggy
            #    or malicious run_fn can't point us at an arbitrary path.
            # 2. Copy only this file into the per-session download staging dir.
            #    Gradio's file server is allowed ONLY that dir (not all of
            #    outputs/), so other run outputs aren't reachable via the HTTP
            #    route. The durable copy in outputs/ is untouched.
            try:
                resolved = out_path.resolve()
                expected_root = _config.output_dir().resolve()
                if not resolved.is_relative_to(expected_root):
                    yield add(
                        f"Security error: output path {resolved} is outside "
                        f"the expected output directory ({expected_root}). "
                        "Download aborted."
                    ), gr.update(interactive=False, value=None), gr.update()
                    return
                staging = _config.download_staging_dir()
                served_path = staging / out_path.name
                shutil.copy2(out_path, served_path)
                out_abs = str(served_path.resolve())
            except Exception as e:
                yield add(
                    f"Error: could not stage download file: {e}"
                ), gr.update(interactive=False, value=None), gr.update()
                return
            # --- end security block ---

            msg = add(
                f"### Done\n\n"
                f"**File:** {out_path.name}\n\n"
                f"**Saved to:** {out_path.resolve()}\n\n"
                f"Click **{skill.output.download_label}** below.\n\n"
                f"---\n\n**Agent reply:**\n\n{agent_reply}"
            )
            yield msg, gr.update(value=out_abs, interactive=True), gr.update(value=str(out_path.resolve()))

    return _run


# ---------------------------------------------------------------------------
# Public: render a tab for a given skill.
# ---------------------------------------------------------------------------

def _open_output_folder(suffix: str, is_dir_output: bool):
    """
    Open the relevant output location in the OS file manager. Directory-output
    skills open their most recent result subfolder; file-output skills open the
    outputs root (where the dated output file is saved).
    """
    base = _config.output_dir()
    target = base
    if is_dir_output and base.is_dir():
        try:
            matches = sorted(
                (q for q in base.glob(f"*-{suffix}") if q.is_dir()),
                key=lambda q: q.stat().st_mtime,
            )
            if matches:
                target = matches[-1]
        except Exception:
            pass
    _config.open_in_file_manager(target)
    return None


def render(skill: SkillInfo, container_tab=None) -> None:
    """
    Render a complete Gradio tab body for the given skill.

    Must be called inside a `with gr.Tab(...)` context. Pass that gr.Tab as
    ``container_tab`` so output-file pickers re-scan and auto-select the newest
    matching file whenever the tab is opened (picks up a prior step's output).
    """
    # Banner: description + native binary status.
    desc = skill.description.strip()
    llm_badge = "🧠 AI-powered" if skill.requires.llm else "⚙️ Deterministic"
    badges = f"`{llm_badge}`"
    if skill.requires.network:
        # Distinct from the LLM badge: its own emoji + a coloured pill (not
        # just another backtick span) so an internet-calling skill is
        # impossible to mistake for a local-only one at a glance.
        badges += (
            ' <span style="background:#e0f2fe;color:#075985;padding:2px 8px;'
            'border-radius:6px;font-size:0.85em;">🌐 Network access</span>'
        )
    banner_parts = [f"## {skill.display_name}  {badges}\n\n{desc}"]
    if skill.requires.native_binaries:
        native_err = _check_native_binaries(skill)
        if native_err is None:
            banner_parts.append("\n\n_Native OCR binaries detected._")
        else:
            banner_parts.append("\n\n_Native binaries missing — see Run button error for details._")

    gr.Markdown("\n".join(banner_parts))

    # Inline help panel (collapsible) — reads the skill's help: block.
    _help.render_inline(skill)

    # Per-input helper text (Tier-1 tooltips) from the help: block.
    _info = _help.input_info_map(skill)

    with gr.Row():
        # Inputs get equal width with the results pane so long output-file
        # picker filenames (e.g. the Part I ledger) fit without truncation.
        with gr.Column(scale=2):
            # Build input components from skill.inputs.
            input_components = []
            output_pickers = []   # (dropdown, refresh_btn, match, file_types)
            parser_pickers = []   # (dropdown, refresh_btn) for type="parser_file"
            dynamic_pickers = []  # (dropdown, refresh_btn, options_from_key) for type="select" with options_from
            browse_buttons = []   # (button, file_comp, input_def, multiple) for native Browse…
            for inp in skill.inputs:
                if inp.type == "file":
                    with gr.Row():
                        comp = gr.File(
                            label=inp.label,
                            file_types=list(inp.file_types) if inp.file_types else None,
                            type="filepath",
                            scale=5,
                            **_help.maybe_info(gr.File, _info.get(inp.name)),
                        )
                        _brbtn = gr.Button("Browse…", scale=0, min_width=110)
                    browse_buttons.append((_brbtn, comp, inp, False))
                elif inp.type == "output_file":
                    # Pick a prior-step output from the outputs folder, with a
                    # refresh button — same UX as the Review-Mappings CSV picker.
                    _choices = _scan_output_files(inp.match, tuple(inp.file_types))
                    with gr.Row():
                        comp = gr.Dropdown(
                            label=inp.label,
                            choices=_choices,
                            value=_choices[0][1] if _choices else None,
                            allow_custom_value=True,
                            interactive=True,
                            scale=5,
                            **_help.maybe_info(gr.Dropdown, _info.get(inp.name)),
                        )
                        _rbtn = gr.Button("↻", scale=0, min_width=40)
                    output_pickers.append((comp, _rbtn, inp.match, tuple(inp.file_types)))
                elif inp.type == "files":
                    with gr.Row():
                        comp = gr.File(
                            label=inp.label,
                            file_types=list(inp.file_types) if inp.file_types else None,
                            file_count="multiple",
                            type="filepath",
                            scale=5,
                            **_help.maybe_info(gr.File, _info.get(inp.name)),
                        )
                        _brbtn = gr.Button("Browse…", scale=0, min_width=110)
                    browse_buttons.append((_brbtn, comp, inp, True))
                elif inp.type == "parser_file":
                    # Dropdown of the project's known parsers, with a refresh
                    # button. allow_custom_value=True so "Create a new parser"
                    # can still type a brand-new path that isn't on disk yet.
                    _pchoices = _scan_parser_files()
                    with gr.Row():
                        comp = gr.Dropdown(
                            label=inp.label,
                            choices=_pchoices,
                            value=None,
                            allow_custom_value=True,
                            interactive=True,
                            scale=5,
                            **_help.maybe_info(gr.Dropdown, _info.get(inp.name)),
                        )
                        _pbtn = gr.Button("↻", scale=0, min_width=40)
                    parser_pickers.append((comp, _pbtn))
                elif inp.type == "select":
                    if inp.options_from:
                        _dchoices = _resolve_options_from(inp.options_from)
                        with gr.Row():
                            comp = gr.Dropdown(
                                label=inp.label,
                                choices=_dchoices,
                                value=_dchoices[0][1] if _dchoices else None,
                                allow_custom_value=True,
                                interactive=True,
                                scale=5,
                                **_help.maybe_info(gr.Dropdown, _info.get(inp.name)),
                            )
                            _dbtn = gr.Button("↻", scale=0, min_width=40)
                        dynamic_pickers.append((comp, _dbtn, inp.options_from))
                    else:
                        comp = gr.Dropdown(
                            label=inp.label,
                            choices=list(inp.options),
                            value=inp.options[0] if inp.options else None,
                            allow_custom_value=True,
                            interactive=True,
                            **_help.maybe_info(gr.Dropdown, _info.get(inp.name)),
                        )
                elif inp.type == "directory":
                    comp = gr.Textbox(
                        label=inp.label,
                        placeholder="Paste full folder path here",
                        **_help.maybe_info(gr.Textbox, _info.get(inp.name)),
                    )
                elif inp.type == "password":
                    comp = gr.Textbox(
                        label=inp.label,
                        type="password",
                        **_help.maybe_info(gr.Textbox, _info.get(inp.name)),
                    )
                else:  # text
                    comp = gr.Textbox(
                        label=inp.label,
                        **_help.maybe_info(gr.Textbox, _info.get(inp.name)),
                    )
                input_components.append(comp)

            # Model dropdown — only meaningful for LLM-powered skills; deterministic
            # skills ignore model_override entirely, so hide it there.
            # Choices are (display_label, raw_name) tuples with capability badges.
            initial_choices = _refresh_models(use_cache=True)
            model_dd = gr.Dropdown(
                label="Model",
                choices=initial_choices,
                value=_default_model_value(initial_choices),
                allow_custom_value=True,
                interactive=True,
                visible=skill.requires.llm,
            )
            refresh_models_btn = gr.Button(
                "Refresh model list", variant="secondary", visible=skill.requires.llm,
            )
            with gr.Row():
                run_btn = gr.Button("Run", variant="primary")
                stop_btn = gr.Button("Stop", variant="stop", visible=True)
                reset_btn = gr.Button("Reset", variant="secondary")

        with gr.Column(scale=2):
            result_md = gr.Markdown("_Awaiting input._", min_height=200)
            # NOTE: created visible=True/interactive=False rather than
            # visible=False. Gradio 6's frontend does not reliably reveal a
            # DownloadButton that starts hidden and is later toggled to
            # visible=True via a streamed/generator update (confirmed: the
            # backend update carries the correct visible=True + value, but
            # the button never mounts). Toggling `interactive` instead keeps
            # the component always mounted, sidestepping that issue.
            download = gr.DownloadButton(
                label=skill.output.download_label,
                visible=True,
                interactive=False,
                variant="primary",
            )
            # WebView2 suppresses the native right-click Copy menu, so the
            # saved path is otherwise uncopyable in the native window.
            # Gradio's built-in copy button (show_copy_button) works there.
            # Created empty and always mounted, same rationale as `download`.
            path_tb = gr.Textbox(
                label="Saved file path",
                value="",
                interactive=False,
                buttons=["copy"],
            )
            # Every result tab gets a button to open the output location in
            # the file manager (directory skills -> their result folder;
            # file skills -> the outputs folder holding the dated file).
            open_folder_btn = gr.Button(
                "Open output folder",
                variant=("primary" if skill.output.type == "directory" else "secondary"),
            )

    refresh_models_btn.click(
        fn=lambda: gr.update(choices=_refresh_models()),
        outputs=model_dd,
    )

    # Wire each output-folder picker's refresh button to re-scan the outputs dir.
    for _comp, _rbtn, _match, _fts in output_pickers:
        _rbtn.click(
            fn=lambda m=_match, f=_fts: gr.update(choices=_scan_output_files(m, f)),
            outputs=_comp,
        )

    # Wire each parser picker's refresh button to re-scan the parser tree.
    for _comp, _pbtn in parser_pickers:
        _pbtn.click(
            fn=lambda: gr.update(choices=_scan_parser_files()),
            outputs=_comp,
        )

    # Wire each options_from dropdown's refresh button to re-resolve its source.
    for _comp, _dbtn, _key in dynamic_pickers:
        _dbtn.click(
            fn=lambda k=_key: gr.update(choices=_resolve_options_from(k)),
            outputs=_comp,
        )

    # Wire each "Browse…" button to the native OS file picker. It opens at the
    # box's remembered folder, validates the picks (extension + size, since the
    # browser filter and upload-staging caps are bypassed), sets the file box,
    # and remembers the folder for next time. Additive: drag-drop still works.
    for _brbtn, _fcomp, _inp, _multiple in browse_buttons:
        _box_key = f"{skill.name}.{_inp.name}"
        _fts = tuple(_inp.file_types) if _inp.file_types else ()

        def _browse(bk=_box_key, mult=_multiple, fts=_fts, label=_inp.label):
            valid, warnings = _filedialog.pick_files(
                bk,
                multiple=mult,
                file_types=fts,
                max_size_bytes=_MAX_UPLOAD_SIZE_BYTES,
                title=f"Select file{'s' if mult else ''} — {label}",
            )
            for w in warnings:
                gr.Warning(w)
            if not valid:
                # Cancelled, or every pick was rejected — keep the current value.
                return gr.update()
            return gr.update(value=(valid if mult else valid[0]))

        _brbtn.click(fn=_browse, inputs=[], outputs=[_fcomp])

    # When this tab is (re)opened, re-scan each output-file picker and
    # auto-select the newest match — picks up a prior step's fresh output.
    if container_tab is not None:
        for _comp, _rbtn, _match, _fts in output_pickers:
            def _rescan_newest(m=_match, f=_fts):
                choices = _scan_output_files(m, f)
                return gr.update(
                    choices=choices,
                    value=(choices[0][1] if choices else None),
                )
            container_tab.select(fn=_rescan_newest, inputs=[], outputs=[_comp])
        for _comp, _dbtn, _key in dynamic_pickers:
            def _rescan_options_from(k=_key):
                return gr.update(choices=_resolve_options_from(k))
            container_tab.select(fn=_rescan_options_from, inputs=[], outputs=[_comp])

    def _handle_stop():
        from .. import _runner
        _runner.request_cancel()
        return "**Cancelled** — stopping after current step."

    stop_btn.click(fn=_handle_stop, outputs=result_md)

    # ── Reset: clear on-screen state + logs, reset input pickers to defaults.
    # Does NOT touch output files on disk — only the current tab's UI state.
    def _reset_output_picker(m, f):
        choices = _scan_output_files(m, f)
        return gr.update(choices=choices, value=(choices[0][1] if choices else None))

    def _reset_options_from_picker(k):
        choices = _resolve_options_from(k)
        return gr.update(choices=choices, value=(choices[0][1] if choices else None))

    # One reset spec per input, in the same order as input_components, so the
    # click handler can return updates that line up with the outputs list.
    reset_specs: list = []  # (component, reset_update_callable)
    for _inp, _comp in zip(skill.inputs, input_components):
        if _inp.type in ("file", "files"):
            reset_specs.append((_comp, lambda: gr.update(value=None)))
        elif _inp.type == "output_file":
            reset_specs.append((
                _comp,
                lambda m=_inp.match, f=tuple(_inp.file_types): _reset_output_picker(m, f),
            ))
        elif _inp.type == "parser_file":
            reset_specs.append((_comp, lambda: gr.update(value=None)))
        elif _inp.type == "select" and _inp.options_from:
            reset_specs.append((
                _comp,
                lambda k=_inp.options_from: _reset_options_from_picker(k),
            ))
        elif _inp.type == "select":
            reset_specs.append((
                _comp,
                lambda o=(_inp.options[0] if _inp.options else None): gr.update(value=o),
            ))
        else:  # directory, text
            reset_specs.append((_comp, lambda: gr.update(value="")))

    def _handle_reset():
        from .. import _runner
        _runner.reset_cancel()
        updates = [
            gr.update(value="_Awaiting input._"),   # result_md
            gr.update(interactive=False, value=None),   # download
            gr.update(value=""),   # path_tb
        ]
        updates.extend(fn() for _c, fn in reset_specs)
        return tuple(updates)

    reset_btn.click(
        fn=_handle_reset,
        outputs=[result_md, download, path_tb] + [_c for _c, _fn in reset_specs],
    )

    handler = _make_run_handler(skill)
    run_btn.click(
        fn=handler,
        inputs=input_components + [model_dd],
        outputs=[result_md, download, path_tb],
    )

    open_folder_btn.click(
        fn=lambda _s=skill.output.suffix, _d=(skill.output.type == "directory"):
            _open_output_folder(_s, _d),
        inputs=None, outputs=None,
    )
