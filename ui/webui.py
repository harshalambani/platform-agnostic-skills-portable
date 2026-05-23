"""
ui/webui.py — Gradio web UI entry point for PA Skills Portable.

Spec mapping:
    §9.1   — Gradio chosen as the UI framework.
    §9.2   — Sidebar of tabs; Home is the default on first launch.
    §9.3   — Bind 127.0.0.1, choose a free port, write the chosen port to a
             small JSON file the PortableApps Launcher reads to open the
             browser. In source mode we let Gradio open the browser directly.
    §9.4   — Custom black + electric-blue theme (see ui._theme).
    §14.1  — Phase 1 covers Home + 26AS tabs.
    §14.2  — Phase 2a adds BoB + HSBC tabs.

Public surface:
    build_app(launch: bool = False)      — construct the Gradio Blocks object.
    main()                               — bind to 127.0.0.1 on a free port,
                                            write the port file, launch.

Source-mode invocation:
    python -m ui.webui
"""
from __future__ import annotations

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
from ui.tabs import skill_26as as tab_26as  # noqa: E402
from ui.tabs import skill_bob as tab_bob  # noqa: E402
from ui.tabs import skill_hsbc as tab_hsbc  # noqa: E402
from ui import _config as _config_mod  # noqa: E402


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
    with gr.Blocks(title=APP_TITLE, analytics_enabled=False) as app:
        with gr.Tabs():
            with gr.Tab("Home"):
                tab_home.render()
            with gr.Tab("26AS"):
                tab_26as.render()
            with gr.Tab("BoB"):
                tab_bob.render()
            with gr.Tab("HSBC"):
                tab_hsbc.render()

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

def _maybe_dispatch_script(argv: list[str]) -> int | None:
    """
    PyInstaller-frozen sys.executable points to pa_skills.exe, not python.exe. The
    skills (agents/skill_*/tools.py) do `subprocess.run([sys.executable, script, ...])`
    to invoke their deterministic extraction scripts, which would otherwise just
    relaunch the UI. Detect that pattern and execute the requested .py file via
    runpy.run_path() with the remaining args as sys.argv.

    Returns the script's exit code if a script was dispatched, else None.
    """
    if not argv:
        return None
    first = argv[0]
    if not first.lower().endswith(".py"):
        return None
    if not Path(first).is_file():
        return None
    import runpy
    sys.argv = [first] + list(argv[1:])
    try:
        runpy.run_path(first, run_name="__main__")
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
