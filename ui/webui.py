"""
ui/webui.py — Gradio web UI entry point for PA Skills Portable.

Spec mapping:
    §9.1   — Gradio chosen as the UI framework.
    §9.2   — Sidebar of tabs; Home is the default on first launch.
    §9.3   — Bind 127.0.0.1, choose a free port, write the chosen port to a
             small JSON file the PortableApps Launcher reads to open the
             browser. In source mode we let Gradio open the browser directly.
    §9.4   — Custom black + electric-blue theme (see ui._theme).

Phase 4A: tabs are now auto-generated from agents/*/skill.yaml via the
skill registry (agents.registry) and the generic tab builder (ui.tabs._generic).
No more hand-coded per-skill tab files.

Phase 4B: tabs are grouped by category (banks, credit_card, 26as, gnucash,
utilities). Groups with 2+ skills render as nested sub-tabs.

Public surface:
    build_app(launch: bool = False)      — construct the Gradio Blocks object.
    main()                               — bind to 127.0.0.1 on a free port,
                                            write the port file, launch.

Source-mode invocation:
    python -m ui.webui
"""
from __future__ import annotations

# Frozen GUI build (console=False in paskills.spec) makes the PyInstaller
# runw.exe bootloader set sys.stdout / sys.stderr to None. uvicorn's
# DefaultFormatter then calls sys.stdout.isatty() during dictConfig and
# crashes - see pitfall #6 in project_paskills_frozen_pitfalls. Redirect
# the None streams to os.devnull before anything imports uvicorn.
import os as _os
import sys as _sys
if _sys.stdout is None:
    _sys.stdout = open(_os.devnull, "w", encoding="utf-8")
if _sys.stderr is None:
    _sys.stderr = open(_os.devnull, "w", encoding="utf-8")

import argparse
import json
import os
import socket
import sys
from pathlib import Path

# ── Managed warnings ─────────────────────────────────────────────────────────
# Known/triaged warnings we have decided to handle. Each is (substring, reason).
# Managed warnings are LOGGED to the warnings file (tagged "[MANAGED: ...]") but
# kept OFF the console, so the command window stays clean while the log keeps a
# full, clearly-labelled record. New/unknown warnings are tagged "[UNMANAGED]"
# and still surface live on the console. Add entries here as warnings are
# triaged.
_MANAGED_WARNINGS: list[tuple[str, str]] = [
    ("HTTP_422_UNPROCESSABLE_ENTITY",
     "Gradio 6.x references Starlette's deprecated HTTP_422_UNPROCESSABLE_ENTITY; "
     "harmless, pending an upstream Gradio fix"),
]


def _managed_note(message: str) -> str | None:
    for sub, note in _MANAGED_WARNINGS:
        if sub in message:
            return note
    return None

import gradio as gr

# Make `src/` importable when running in source mode (mirrors PyInstaller layout)
PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ui import _theme  # noqa: E402
from ui import _buildinfo  # noqa: E402
from ui.tabs import home as tab_home  # noqa: E402
from ui.tabs import _generic as tab_generic  # noqa: E402
from ui.tabs import settings as tab_settings  # noqa: E402
from ui.tabs import history as tab_history  # noqa: E402
from ui.tabs import help as tab_help  # noqa: E402
from ui.tabs import gnucash_review as tab_gnucash_review  # noqa: E402
from ui import _config as _config_mod  # noqa: E402
from ui import _help as _help_mod  # noqa: E402
from ui import _update  # noqa: E402

# Skill registry — auto-discovers agents/*/skill.yaml.
from agents.registry import discover as _discover_skills  # noqa: E402

# Kick off background update check early so it's ready when Home renders.
_update.start_check()


APP_TITLE = "PA Skills Portable"
PORT_FILE_NAME = "ui_port.json"   # written next to the executable / project root

# Override Gradio's default inline-code rendering so backtick spans don't render
# as light-on-light in our dark theme. Affects markdown inside any tab.
APP_CSS = """
code, .prose code, .markdown code, .gradio-container code {
    background: #262626 !important;
    color: #F5F5F5 !important;
    padding: 0.12em 0.36em;
    border-radius: 0.28em;
    font-family: "JetBrains Mono", ui-monospace, "Cascadia Code", Consolas, monospace;
    font-size: 0.92em;
}

/* ── Readable file names / paths ──────────────────────────────────────────
   Selected values in dropdowns + text inputs and the uploaded-file name were
   rendering low-contrast on the dark theme. Force them to the bright body
   colour so file names and paths are clearly legible. */
.gradio-container input[type="text"],
.gradio-container textarea,
.gradio-container .wrap-inner input,
.gradio-container [data-testid="dropdown"] input,
.gradio-container [data-testid="dropdown"] .single-select,
.gradio-container .secondary-wrap input {
    color: #F5F5F5 !important;
}
/* Uploaded-file component (gr.File): the file name lives in a <table>; the
   first/odd row uses --table-odd-background-fill which the theme leaves light,
   so light filename text was invisible on a light row. Force the rows dark with
   light text + a visible (blue) download/size link. */
.gradio-container .file-preview tbody > tr,
.gradio-container .file-preview tbody > tr:nth-child(odd),
.gradio-container .file-preview tbody > tr:nth-child(2n) {
    background: #262626 !important;
}
.gradio-container .file-preview,
.gradio-container .file-preview .filename,
.gradio-container .file-preview .filename .stem,
.gradio-container .file-preview .filename .ext {
    color: #F5F5F5 !important;
}
.gradio-container .file-preview .download > a {
    color: #60A5FA !important;
}

/* ── RAG status colouring for the run-result panel ────────────────────────
   Applied by _colorize_status() in ui/tabs/_generic.py. Makes success /
   warning / error states obvious at a glance across every skill tab. */
.gradio-container .rag-ok {
    color: #10B981 !important;
    font-weight: 600;
}
.gradio-container .rag-warn {
    color: #F59E0B !important;
    font-weight: 600;
}
.gradio-container .rag-error {
    color: #EF4444 !important;
    font-weight: 600;
}
"""

# Append the in-app help tooltip/output styling (single source: ui/_help.py).
APP_CSS = APP_CSS + _help_mod.HELP_CSS


def _setup_warning_log() -> Path | None:
    """Capture Python warnings to a rotating log file so a whole run session's
    warnings can be collected and shared for troubleshooting/suppression.

    File: Data/logs/warnings.log, capped at 10 MB with one rollover backup
    (warnings.log.1), UTF-8, timestamped. Every line is tagged "[MANAGED: ...]"
    (known/triaged) or "[UNMANAGED]" (new). Managed warnings are logged but kept
    off the console; unmanaged ones are logged AND printed live. Returns the log
    path (or None if it couldn't be set up).
    """
    import logging
    from logging.handlers import RotatingFileHandler

    class _TagFilter(logging.Filter):
        """Annotate each record as managed/unmanaged. Never drops a record."""
        def filter(self, record):
            note = _managed_note(record.getMessage())
            record.is_managed = note is not None
            record.managed_tag = f"[MANAGED: {note}]" if note else "[UNMANAGED]"
            return True

    class _DropManaged(logging.Filter):
        """Console-only: hide managed warnings (known noise)."""
        def filter(self, record):
            return not getattr(record, "is_managed", False)

    try:
        from ui import _config as _cfg
        logs_dir = _cfg.output_dir().parent / "logs"
    except Exception:
        logs_dir = PROJECT_ROOT / "Data" / "logs"
    try:
        logs_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        return None

    log_path = logs_dir / "warnings.log"
    wlog = logging.getLogger("py.warnings")
    # Idempotent — don't stack handlers if build_app is called more than once.
    if not any(getattr(h, "_pa_warnlog", False) for h in wlog.handlers):
        tag = _TagFilter()

        # File: every warning, tagged.
        fh = RotatingFileHandler(
            log_path, maxBytes=10 * 1024 * 1024, backupCount=1,
            encoding="utf-8", delay=True,
        )
        fh.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(managed_tag)s %(name)s: %(message)s"))
        fh.addFilter(tag)
        fh._pa_warnlog = True  # type: ignore[attr-defined]

        # Console: unmanaged warnings only (managed = known noise, file-only).
        ch = logging.StreamHandler(sys.stderr)
        ch.setFormatter(logging.Formatter("%(managed_tag)s %(message)s"))
        ch.addFilter(tag)          # sets the tag first
        ch.addFilter(_DropManaged())  # then drops managed
        ch._pa_warnlog = True  # type: ignore[attr-defined]

        wlog.addHandler(fh)
        wlog.addHandler(ch)
        wlog.setLevel(logging.WARNING)
        wlog.propagate = False     # we own the handlers; avoid double output
        logging.captureWarnings(True)
    return log_path


def build_app(launch: bool = False) -> gr.Blocks:
    """
    Construct the Gradio Blocks object.

    Args:
        launch: When True, calls .launch() with the project's networking
                policy (127.0.0.1, free port, no public share, no analytics).
                When False, returns the unlaunched object — used by tests.
    """
    _setup_warning_log()
    skills = _discover_skills()

    # Prime the model-list cache once (avoids a 2-second health-check timeout
    # per skill tab — see _generic._refresh_models).
    tab_generic._refresh_models()

    # ── Grouped navigation ──────────────────────────────────────────────────
    # Ordered list of (category_key, tab_label). Skills within each group
    # appear as sub-tabs; a group with exactly one skill renders flat (no nesting).
    # Skills whose category is not in GROUP_ORDER appear as flat top-level tabs.
    from collections import defaultdict  # noqa: PLC0415

    GROUP_ORDER = [
        ("banks",       "Banks"),
        ("credit_card", "Credit Card"),
        ("26as",        "26AS"),
        ("gnucash",     "GnuCash"),
        ("utilities",   "Other"),
    ]
    # "krc" has no top-level GROUP_ORDER entry of its own — it's nested as a
    # "KRChoksey" sub-tab inside "gnucash" (see below). Exclude it here too,
    # otherwise the fallback loop at the end of this function (for skills
    # whose category isn't in _known_cats) renders it a second time as a
    # flat top-level tab.
    _known_cats = {k for k, _ in GROUP_ORDER} | {"krc"}

    _grouped = defaultdict(list)
    for _s in skills:
        _grouped[_s.category].append(_s)

    with gr.Blocks(title=APP_TITLE, analytics_enabled=False) as app:
        with gr.Tabs():
            with gr.Tab("Home"):
                tab_home.render(skills=skills)

            for _cat_key, _cat_label in GROUP_ORDER:
                # 26AS is no longer a top-level tab — it's nested under GnuCash.
                if _cat_key == "26as":
                    continue

                _cat_skills = _grouped.get(_cat_key, [])

                # GnuCash is a container: a "Banks" sub-tab (statement import +
                # Review Mappings) and a "26AS" sub-tab (Convert + Journal).
                if _cat_key == "gnucash":
                    with gr.Tab(_cat_label):
                        with gr.Tabs():
                            with gr.Tab("Banks"):
                                with gr.Tabs():
                                    for _skill in _cat_skills:
                                        with gr.Tab(_skill.display_name) as _t:
                                            tab_generic.render(_skill, container_tab=_t)
                                    with gr.Tab("Review Mappings") as _rt:
                                        tab_gnucash_review.render(container_tab=_rt)
                            with gr.Tab("26AS"):
                                with gr.Tabs():
                                    for _skill in _grouped.get("26as", []):
                                        with gr.Tab(_skill.display_name) as _t:
                                            tab_generic.render(_skill, container_tab=_t)
                            with gr.Tab("KRChoksey"):
                                with gr.Tabs():
                                    # Order the KRChoksey sub-tabs by workflow
                                    # (Part I -> II -> III), not alphabetically.
                                    _krc_order = {"KRChoksey": 0, "Reconcile": 1,
                                                  "GnuCash Import": 2}
                                    for _skill in sorted(
                                        _grouped.get("krc", []),
                                        key=lambda s: _krc_order.get(s.display_name, 99),
                                    ):
                                        with gr.Tab(_skill.display_name) as _t:
                                            tab_generic.render(_skill, container_tab=_t)
                    continue

                if not _cat_skills:
                    continue
                with gr.Tab(_cat_label) as _ct:
                    if len(_cat_skills) == 1:
                        tab_generic.render(_cat_skills[0], container_tab=_ct)
                    else:
                        with gr.Tabs():
                            for _skill in _cat_skills:
                                with gr.Tab(_skill.display_name) as _t:
                                    tab_generic.render(_skill, container_tab=_t)

            # Fallback: uncategorised skills render as flat top-level tabs
            for _skill in skills:
                if _skill.category not in _known_cats:
                    with gr.Tab(_skill.display_name) as _t:
                        tab_generic.render(_skill, container_tab=_t)

            with gr.Tab("Help"):
                tab_help.render(skills=skills)
            with gr.Tab("History"):
                tab_history.render()
            with gr.Tab("Settings"):
                tab_settings.render()

    if launch:
        port = _pick_free_port()
        _write_port_file(port)
        # Gradio's file server only serves files under allowed_paths.
        # We allow ONLY the per-session download staging dir (not the full
        # outputs/ folder) so the file route cannot reach other run outputs.
        # Each run copies its result file into this dir before handing the path
        # to DownloadButton (see ui/tabs/_generic.py).
        # Security: finding #5 / MP-05 — narrows file-serving scope.
        try:
            allowed = [str(_config_mod.download_staging_dir().resolve())]
        except Exception:
            allowed = []

        app.launch(
            server_name="127.0.0.1",
            server_port=port,
            share=False,
            inbrowser=os.environ.get("PA_SKILLS_NO_BROWSER") != "1",
            quiet=False,
            theme=_theme.make_theme(),
            css=APP_CSS,
            allowed_paths=allowed,
        )
    return app


# ---------------------------------------------------------------------------
# Networking helpers.
# ---------------------------------------------------------------------------

def _pick_free_port() -> int:
    """Ask the OS for a free TCP port on 127.0.0.1, return it."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _write_port_file(port: int) -> None:
    """
    Write the chosen port to a small JSON file the PortableApps Launcher reads.

    Resolution order (mirrors ui._config):
        1. PA_SKILLS_PORT_FILE env var, if set.
        2. <cwd>/<PORT_FILE_NAME> — frozen build, PAL sets cwd to Data\\.
        3. <project>/<PORT_FILE_NAME> — source mode.
    """
    env_path = os.environ.get("PA_SKILLS_PORT_FILE")
    if env_path:
        target = Path(env_path)
    elif Path.cwd().resolve() != PROJECT_ROOT.resolve():
        target = Path.cwd() / PORT_FILE_NAME
    else:
        target = PROJECT_ROOT / PORT_FILE_NAME

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "port": port,
                "url": f"http://127.0.0.1:{port}",
                "version": _buildinfo.VERSION,
                "commit": _buildinfo.COMMIT_SHA,
            },
            indent=2,
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# CLI entry.
# ---------------------------------------------------------------------------

def _bundled_scripts_root() -> Path:
    """
    Return the root directory under which all bundled agent scripts live.

    - Frozen build (PyInstaller): scripts are extracted under sys._MEIPASS/agents.
    - Source mode: scripts live under <project>/src/agents.

    Used by _maybe_dispatch_script() to enforce path containment.
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return (Path(meipass) / "agents").resolve()
    return (PROJECT_ROOT / "src" / "agents").resolve()


def _maybe_dispatch_script(argv: list[str]) -> int | None:
    """
    PyInstaller-frozen sys.executable points to pa_skills.exe, not python.exe. The
    skills (agents/skill_*/tools.py) do `subprocess.run([sys.executable, script, ...])`
    to invoke their deterministic extraction scripts, which would otherwise just
    relaunch the UI. Detect that pattern and execute the requested .py file via
    runpy.run_path() with the remaining args as sys.argv.

    SECURITY (2026-06-09, fixes Tracker finding #2 / MP-02):
    The original dispatcher had no allowlist — any .py file passed as the first
    argument was executed unconditionally. This permits code execution through the
    trusted binary via file associations, crafted shortcuts, or sibling processes
    (living-off-the-land primitive).

    Fix: path containment. The resolved script path must be a child of the bundled
    scripts root (sys._MEIPASS/agents in frozen mode; src/agents in source mode).
    Anything outside that root is rejected with exit code 1 — it never falls through
    to launching the UI on the attacker-supplied argv.

    Follow-up (MP-02 phase 2): add a --pa-internal-script sentinel to the
    subprocess callers in src/agents/*/tools.py so the dispatch branch is
    unreachable from a bare `pa_skills.exe <path>` invocation. Deferred because
    it requires upstream changes in platform-agnostic-skills.

    Returns the script's exit code if a script was dispatched, else None.
    """
    if not argv:
        return None
    first = argv[0]
    if not first.lower().endswith(".py"):
        return None

    requested = Path(first)
    if not requested.is_file():
        return None

    # --- Path containment check -------------------------------------------
    # Resolve both paths (follows symlinks, collapses ..) so that traversal
    # tricks like "scripts_root/../../evil.py" are caught.
    try:
        resolved = requested.resolve()
        scripts_root = _bundled_scripts_root()
        contained = resolved.is_relative_to(scripts_root)
    except Exception:
        contained = False

    if not contained:
        # Reject and exit non-zero. Do NOT fall through to UI launch —
        # that would let an attacker trigger arbitrary UI argv processing.
        print(
            f"[pa-skills] SECURITY: rejecting script dispatch for {first!r} — "
            f"path is not inside the bundled scripts root ({scripts_root}). "
            "Exiting.",
            file=sys.stderr,
        )
        return 1
    # ----------------------------------------------------------------------

    import runpy
    sys.argv = [str(resolved)] + list(argv[1:])
    try:
        runpy.run_path(str(resolved), run_name="__main__")
        return 0
    except SystemExit as e:
        return int(e.code) if isinstance(e.code, int) else (0 if e.code is None else 1)


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    # Frozen-mode script-runner shim — see _maybe_dispatch_script().
    rc = _maybe_dispatch_script(raw_argv)
    if rc is not None:
        return rc

    parser = argparse.ArgumentParser(prog="ui.webui", description="Launch the PA Skills Portable UI.")
    parser.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser.")
    args = parser.parse_args(raw_argv)
    if args.no_browser:
        os.environ["PA_SKILLS_NO_BROWSER"] = "1"
    build_app(launch=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
