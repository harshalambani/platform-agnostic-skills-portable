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
import atexit
import json
import os
import socket
import sys
import threading
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

/* ── Selectable result text ────────────────────────────────────────────────
   WebView2 (native window) suppresses the right-click context menu, so
   drag-select is the only way to copy result/log text there. Nothing here
   sets user-select: none, but be explicit so it can't regress. */
.gradio-container .prose,
.gradio-container .markdown {
    user-select: text !important;
}
"""

# Append the in-app help tooltip/output styling (single source: ui/_help.py).
APP_CSS = APP_CSS + _help_mod.HELP_CSS


# ---------------------------------------------------------------------------
# Clean shutdown coordinator.
#
# The frozen build is a windowless Gradio server (console=False), so there is
# no OS window to close and closing the browser tab does NOT stop the process.
# Left running, pa_skills.exe lingers until it's force-killed, and the
# PortableApps Launcher (WaitForProgram=true) then reports "did not close
# correctly" and blocks upgrades.
#
# Fix: exit the process when the user closes the browser (Blocks.unload) after a
# short grace window so an ordinary reload/navigation doesn't kill the server —
# a fresh page load cancels the pending shutdown. An explicit "Exit" button in
# the header gives a deterministic manual path.
#
# Note: this targets the single-user local workflow. With multiple browser tabs
# open, closing one starts the grace timer; the Exit button is the reliable way
# to quit in that case.
# ---------------------------------------------------------------------------

_UNLOAD_GRACE_SECONDS = 3.0     # tab close -> wait this long -> exit (reload cancels)
_EXIT_BUTTON_DELAY = 0.8        # let the "shutting down" message render before exit

_shutdown_lock = threading.Lock()
_shutdown_timer: threading.Timer | None = None


def _terminate_process() -> None:
    """Run registered atexit handlers, then hard-exit with code 0.

    atexit._run_exitfuncs() fires the temp-dir cleanup registered by
    ui._config (which wipes the %TEMP% legacy-config dirs holding decrypted
    API keys) BEFORE we os._exit — os._exit alone would skip atexit and leave
    those files behind. Exit code 0 lets the PA Launcher record a clean close.
    """
    try:
        atexit._run_exitfuncs()
    except Exception:
        pass
    os._exit(0)


def _cancel_pending_shutdown() -> None:
    """Cancel a scheduled shutdown — called on every page load, so a reload or
    reconnect keeps the server alive."""
    global _shutdown_timer
    with _shutdown_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
            _shutdown_timer = None


def _schedule_shutdown(delay: float) -> None:
    """(Re)arm a one-shot timer that terminates the process after *delay* s."""
    global _shutdown_timer
    with _shutdown_lock:
        if _shutdown_timer is not None:
            _shutdown_timer.cancel()
        _shutdown_timer = threading.Timer(delay, _terminate_process)
        _shutdown_timer.daemon = True
        _shutdown_timer.start()


def _on_exit_click() -> str:
    """Header 'Exit' button handler — arm a near-immediate shutdown and tell the
    user they can close the window."""
    _schedule_shutdown(_EXIT_BUTTON_DELAY)
    return (
        "### PA Skills is shutting down\n\n"
        "You can close this browser window now."
    )


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
    # "krc" and "intercompany" have no top-level GROUP_ORDER entry of their
    # own — "krc" is nested as a "KRChoksey" sub-tab and "intercompany" as an
    # "Intercompany" sub-tab, both inside "gnucash" (see below). "itr" (ITR
    # Workbook) is likewise nested as its own "ITR Workbook" sub-tab inside
    # "gnucash" rather than getting a flat top-level tab. Exclude them here
    # too, otherwise the fallback loop at the end of this function (for
    # skills whose category isn't in _known_cats) renders them a second time
    # as flat top-level tabs.
    _known_cats = {k for k, _ in GROUP_ORDER} | {"krc", "intercompany", "itr"}

    _grouped = defaultdict(list)
    for _s in skills:
        _grouped[_s.category].append(_s)

    # Presentation mode decides the header + quit affordance. Resolve it BEFORE
    # constructing the Blocks so the Exit button is gated correctly: in native-
    # window mode closing the window quits, so the header Exit button is removed;
    # in browser/headless mode it stays (the only deterministic quit path). The
    # window/browser fallback is decided up front (WebView2 pre-check) so a
    # later fallback never leaves a windowless UI without an Exit button.
    mode = _resolve_launch_mode() if launch else "browser"
    window_mode = mode == "window"

    with gr.Blocks(title=APP_TITLE, analytics_enabled=False) as app:
        # Header. In browser/headless mode we add an always-visible Exit button
        # (the deterministic way to stop the windowless server — see shutdown
        # coordinator above) plus the tab-close shutdown wiring. In native-window
        # mode the OS window owns the lifecycle: closing it quits the process, so
        # the Exit button and the tab-close grace timer are both omitted (a webview
        # navigation must not arm a shutdown).
        with gr.Row():
            gr.Markdown(f"### {APP_TITLE}")
            if not window_mode:
                _exit_btn = gr.Button("Exit", variant="stop", scale=0, min_width=90)

        if not window_mode:
            _exit_msg = gr.Markdown(visible=False)

            def _on_exit_click_ui():
                return gr.update(value=_on_exit_click(), visible=True)

            _exit_btn.click(fn=_on_exit_click_ui, inputs=None, outputs=[_exit_msg])
            # A page load (initial or reload) cancels any pending browser-close
            # shutdown; closing the tab arms it after a short grace window.
            app.load(fn=_cancel_pending_shutdown, inputs=None, outputs=None)
            app.unload(lambda: _schedule_shutdown(_UNLOAD_GRACE_SECONDS))

        with gr.Tabs():
            with gr.Tab("Home"):
                tab_home.render(skills=skills)

            for _cat_key, _cat_label in GROUP_ORDER:
                # 26AS is no longer a top-level tab — it's nested under GnuCash.
                if _cat_key == "26as":
                    continue

                _cat_skills = _grouped.get(_cat_key, [])

                # GnuCash is a container: a "Banks" sub-tab (statement import +
                # Review Mappings), an "Intercompany" sub-tab (Reco + Matrix),
                # and a "26AS" sub-tab (Convert + Journal).
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
                            with gr.Tab("Intercompany"):
                                with gr.Tabs():
                                    # Pairwise Reco is the primary tool per the
                                    # skills' own manifests; Matrix is the
                                    # optional all-family roll-up built on it.
                                    _ic_order = {"Intercompany Reco": 0,
                                                 "Intercompany Matrix": 1}
                                    for _skill in sorted(
                                        _grouped.get("intercompany", []),
                                        key=lambda s: _ic_order.get(s.display_name, 99),
                                    ):
                                        with gr.Tab(_skill.display_name) as _t:
                                            tab_generic.render(_skill, container_tab=_t)
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
                            _itr_skills = _grouped.get("itr", [])
                            if _itr_skills:
                                with gr.Tab("ITR Workbook") as _t:
                                    tab_generic.render(_itr_skills[0], container_tab=_t)
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

        # In window mode, launch non-blocking (prevent_thread_lock=True) so the
        # Gradio server runs on a background thread and the main thread is free to
        # own the native-window GUI message pump. In browser/headless mode we keep
        # the historical blocking launch. inbrowser only fires in browser mode
        # (never headless, never window — the webview opens the URL itself).
        app.launch(
            server_name="127.0.0.1",
            server_port=port,
            share=False,
            inbrowser=mode == "browser",
            prevent_thread_lock=window_mode,
            quiet=False,
            theme=_theme.make_theme(),
            css=APP_CSS,
            allowed_paths=allowed,
        )
        if window_mode:
            url = f"http://127.0.0.1:{port}"
            _run_native_window(url)  # blocks until the window closes, then exits
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
# Native window (v2 — one-click quit). See
# docs/history/2026-07-06-PHASE-V2-NATIVE-WINDOW-PLAN.md (#40).
#
# The frozen app is otherwise a windowless Gradio server. In native-window mode
# the Gradio server runs on a background thread (prevent_thread_lock=True) and a
# pywebview window (WebView2 backend on Windows) hosts the URL on the main
# thread; closing the window quits the process in one click. If the native
# window is unavailable we silently fall back to the browser + Exit-button flow.
# ---------------------------------------------------------------------------

def _resolve_launch_mode() -> str:
    """Decide how to present the UI: 'headless', 'browser', or 'window'.

    - PA_SKILLS_NO_BROWSER=1 -> 'headless': server only, no window, no browser.
      This is the CI compat-check smoke-test path (it HTTP-GETs the port URL);
      the native window MUST be off here.
    - PA_SKILLS_NO_WINDOW=1  -> 'browser': today's browser + Exit-button flow
      (debug escape hatch, forces browser mode even when a window is available).
    - otherwise               -> 'window' if a native window is available, else
      'browser' (silent fallback — no install prompt).
    """
    if os.environ.get("PA_SKILLS_NO_BROWSER") == "1":
        return "headless"
    if os.environ.get("PA_SKILLS_NO_WINDOW") == "1":
        return "browser"
    return "window" if _native_window_available() else "browser"


def _native_window_available() -> bool:
    """True if we can host the UI in a pywebview + WebView2 native window.

    Gated to Windows for v2 (WebView2 backend). Requires both the pywebview
    package to import and the WebView2 runtime to be installed. Deciding this up
    front (rather than discovering it when the window fails to open) lets the
    window/browser choice — and therefore the Exit-button gating — be correct
    before the Blocks are built.
    """
    if sys.platform != "win32":
        return False
    try:
        import webview  # noqa: F401,PLC0415
    except Exception:
        return False
    return _webview2_runtime_present()


def _webview2_runtime_present() -> bool:
    """True if the Evergreen/standalone WebView2 runtime is installed.

    pywebview's EdgeChromium backend needs the WebView2 runtime, which ships with
    Windows 11 and current Edge but can be absent on older Windows 10. Check the
    runtime's registered version ("pv") under the EdgeUpdate client GUID across
    the per-machine (HKLM, incl. the 32-bit registry view) and per-user (HKCU)
    install locations.
    """
    import winreg  # noqa: PLC0415

    guid = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
    locations = [
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_LOCAL_MACHINE,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
        (winreg.HKEY_CURRENT_USER,
         rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{guid}"),
    ]
    for root, path in locations:
        try:
            with winreg.OpenKey(root, path) as key:
                pv, _ = winreg.QueryValueEx(key, "pv")
                if pv and pv not in ("", "0.0.0.0"):
                    return True
        except OSError:
            continue
    return False


def _window_icon_path() -> str | None:
    """Absolute path to appicon.ico for the native window title bar, or None.

    Frozen builds may bundle the icon under sys._MEIPASS; source mode reads it
    from bundling/icons/. Ships with the v2 icon refresh (#43).
    """
    candidates: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        candidates.append(Path(meipass) / "appicon.ico")
        candidates.append(Path(meipass) / "bundling" / "icons" / "appicon.ico")
    candidates.append(PROJECT_ROOT / "bundling" / "icons" / "appicon.ico")
    for c in candidates:
        try:
            if c.is_file():
                return str(c)
        except OSError:
            continue
    return None


def _wait_for_server(url: str, timeout: float = 30.0) -> bool:
    """Poll *url* until it responds (or *timeout* s elapse). Returns readiness.

    prevent_thread_lock=True returns before uvicorn is necessarily accepting
    connections, so give the background server a moment before pointing the
    window at it — otherwise the window can load an error page on a cold start.
    """
    import time  # noqa: PLC0415
    import urllib.request  # noqa: PLC0415

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as resp:  # noqa: S310
                if resp.status < 500:
                    return True
        except Exception:
            time.sleep(0.25)
    return False


def _enable_native_downloads() -> None:
    """Allow file downloads in the WebView2 native window.

    pywebview's EdgeChromium backend cancels every download unless
    ``settings['ALLOW_DOWNLOADS']`` is True — and it defaults to False. With it
    off, clicking any ``gr.DownloadButton`` (Download corrected CSV, Download
    Excel/CSV, History download, every generated-skill output) is a silent no-op
    in the native window: the file never reaches the user's Downloads folder.
    Turning it on makes WebView2 pop a native Save-As dialog (defaulting to
    Downloads) for all of them. No-op if pywebview isn't importable — the browser
    fallback downloads natively and is unaffected.
    """
    try:
        import webview  # noqa: PLC0415
        webview.settings['ALLOW_DOWNLOADS'] = True
    except Exception:  # noqa: BLE001 — pywebview optional; browser path unaffected
        pass


def _run_native_window(url: str) -> None:
    """Host *url* in a native window on the main thread; quit when it closes.

    webview.start() runs the GUI message pump and blocks until every window is
    closed, then we route through _terminate_process() so the atexit wipe of the
    decrypted-API-key %TEMP% dirs always runs. If the window fails to open at
    runtime (rare — _native_window_available() pre-checks the WebView2 runtime),
    fall back to opening the browser and keeping the already-running server alive
    rather than stranding the user. Phase D hardens this fallback + CI wiring.
    """
    import webview  # noqa: PLC0415

    _enable_native_downloads()
    _wait_for_server(url)
    try:
        webview.create_window(APP_TITLE, url, width=1200, height=820)
        icon = _window_icon_path()
        if icon:
            webview.start(icon=icon)
        else:
            webview.start()
    except Exception:
        import webbrowser  # noqa: PLC0415
        try:
            webbrowser.open(url)
        except Exception:
            pass
        threading.Event().wait()  # keep the background server thread alive
        return
    _terminate_process()


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
    except BaseException:  # noqa: BLE001
        # A bundled script raised an unhandled exception. In a windowed
        # (console=False) PyInstaller build, letting it propagate pops a modal
        # "Unhandled exception in script" dialog and BLOCKS this child until the
        # user dismisses it — which hangs the parent's subprocess.run(), leaving
        # the UI stuck on "Running…". Convert it to a clean non-zero exit with
        # the traceback on stderr so the caller (_run_script) surfaces
        # "ERROR: <traceback>" and the UI can report the failure immediately.
        import traceback
        traceback.print_exc()
        return 1


def _suppress_console_windows() -> None:
    """Stop child processes from flashing a console window (Windows only).

    In a windowed (console=False) frozen build the app has no console, so every
    console-subsystem child — the deterministic scripts' external tools (qpdf,
    pdftotext, tesseract) and each `sys.executable <script>` re-exec — briefly
    pops a black console window. subprocess.run / check_output all construct a
    subprocess.Popen, so adding CREATE_NO_WINDOW in Popen.__init__ (unless the
    caller set creationflags) suppresses the flash everywhere at one wiring
    point. Both the UI process and each re-exec'd child run through main(), so
    the child's own grandchildren are covered too. No-op off Windows and
    idempotent (guarded so repeated calls don't stack the wrapper)."""
    if sys.platform != "win32":
        return
    import subprocess
    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if not flag or getattr(subprocess.Popen, "_pa_no_window", False):
        return
    _orig_init = subprocess.Popen.__init__

    def _init(self, *args, **kwargs):
        if not kwargs.get("creationflags"):
            kwargs["creationflags"] = flag
        _orig_init(self, *args, **kwargs)

    subprocess.Popen.__init__ = _init
    subprocess.Popen._pa_no_window = True


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(argv) if argv is not None else sys.argv[1:]

    # Suppress console-window flashes from every child process (see the
    # function docstring). Done first so it also covers the frozen script
    # dispatcher's grandchildren below.
    _suppress_console_windows()

    # Frozen-mode script-runner shim — see _maybe_dispatch_script().
    rc = _maybe_dispatch_script(raw_argv)
    if rc is not None:
        return rc

    parser = argparse.ArgumentParser(prog="ui.webui", description="Launch the PA Skills Portable UI.")
    parser.add_argument("--no-browser", action="store_true",
                        help="Headless: server only, no window and no browser (CI smoke test).")
    parser.add_argument("--no-window", action="store_true",
                        help="Force browser mode (today's Exit-button flow) instead of the native window.")
    args = parser.parse_args(raw_argv)
    if args.no_browser:
        os.environ["PA_SKILLS_NO_BROWSER"] = "1"
    if args.no_window:
        os.environ["PA_SKILLS_NO_WINDOW"] = "1"
    build_app(launch=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
