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
from ui import _config as _config_mod  # noqa: E402
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
"""


def build_app(launch: bool = False) -> gr.Blocks:
    """
    Construct the Gradio Blocks object.

    Args:
        launch: When True, calls .launch() with the project's networking
                policy (127.0.0.1, free port, no public share, no analytics).
                When False, returns the unlaunched object — used by tests.
    """
    skills = _discover_skills()

    with gr.Blocks(title=APP_TITLE, analytics_enabled=False) as app:
        with gr.Tabs():
            with gr.Tab("Home"):
                tab_home.render(skills=skills)
            for skill in skills:
                with gr.Tab(skill.name):
                    tab_generic.render(skill)
            with gr.Tab("History"):
                tab_history.render()
            with gr.Tab("Settings"):
                tab_settings.render()

    if launch:
        port = _pick_free_port()
        _write_port_file(port)
        # Gradio 6 only serves files from cwd by default — explicitly allow
        # the outputs folder so download links resolve outside the cwd tree.
        try:
            allowed = [str(_config_mod.output_dir().resolve())]
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

    Follow-up (MP-02 phas