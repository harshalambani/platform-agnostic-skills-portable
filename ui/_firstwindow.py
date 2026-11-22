"""
ui/_firstwindow.py -- PERF-06: open the app's window first, load the app into it.

Today the window opens only after gradio is imported, the screens are built and
the server answers. This module opens the native window at once with a small
local "Starting PA Skills..." page, loads the rest on a worker thread, and
switches the SAME window to the app once the server answers.

Light on purpose: only the standard library and (lazily) pywebview. It must be
importable BEFORE gradio, so nothing here may import gradio, ui.webui or any
screen at module level.

How it fits (see ui/webui.py, top of the module):
  * `python -m ui.webui` (and the frozen exe) run webui.py as __main__. Before
    gradio is imported, that file calls run(). If this module takes over, the
    main thread owns the window's message pump (webview.start) and a worker
    thread imports `ui.webui` as a normal module and calls its main().
  * build_app() then finds the pre-opened window (webui._PREOPENED_WINDOW) and
    calls attach_app() instead of opening a second window.
  * Closing the window at any time runs the same sequence as today's close:
    the registered exit cleanups, then os._exit(0).
  * A failure while loading is shown in the window (show_error), never left on
    "Starting..." forever.
"""
from __future__ import annotations

import html
import os
import sys
import threading
import traceback
from pathlib import Path

APP_TITLE = "PA Skills Portable"
PROJECT_ROOT = Path(__file__).resolve().parent.parent

_BASE_CSS = (
    "html,body{height:100%;margin:0;background:#0b0f14;color:#e6edf3;"
    "font-family:Segoe UI,system-ui,sans-serif}"
    "main{height:100%;display:flex;flex-direction:column;align-items:center;"
    "justify-content:center;text-align:center;padding:24px;box-sizing:border-box}"
    "h1{font-size:1.6rem;font-weight:600;margin:0 0 .6rem}"
    "p{margin:.3rem 0;color:#9fb0c3}"
    ".dot{color:#3b82f6}"
    "pre{max-width:90%;max-height:50vh;overflow:auto;text-align:left;background:#111823;"
    "border:1px solid #243244;border-radius:8px;padding:12px;font-size:.8rem;"
    "color:#e6edf3;white-space:pre-wrap}"
    "button{margin-top:1rem;background:#3b82f6;color:#fff;border:0;border-radius:8px;"
    "padding:.6rem 1.4rem;font-size:1rem;cursor:pointer}"
)


def loading_html() -> str:
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{APP_TITLE}</title><style>{_BASE_CSS}</style></head><body><main>"
        "<h1>Starting PA Skills<span class='dot'>...</span></h1>"
        "<p>The app is loading. This window will switch to it by itself.</p>"
        "</main></body></html>"
    )


def error_html(headline: str, detail: str = "") -> str:
    """The same look as the loading page, with the problem in plain text and a
    way to close. Everything is escaped: the text can come from an exception."""
    pre = f"<pre>{html.escape(detail)}</pre>" if detail else ""
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{APP_TITLE}</title><style>{_BASE_CSS}</style></head><body><main>"
        f"<h1>{html.escape(headline)}</h1>"
        "<p>Close this window to exit, then start the app again.</p>"
        f"{pre}"
        "<button onclick=\"pywebview.api.close()\">Close</button>"
        "</main></body></html>"
    )


# -- can the native window be used? (the same rules webui.py always used) -----------

def webview2_runtime_present() -> bool:
    """True if the Evergreen/standalone WebView2 runtime is installed (per-machine
    HKLM incl. the 32-bit view, or per-user HKCU)."""
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


def native_window_available() -> bool:
    if sys.platform != "win32":
        return False
    try:
        import webview  # noqa: F401,PLC0415
    except Exception:  # noqa: BLE001
        return False
    return webview2_runtime_present()


def window_icon_path() -> str | None:
    """Absolute path to appicon.ico for the window title bar, or None."""
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


def wants_window_first(argv, env=None, available=None) -> bool:
    """True when the run is an ordinary window launch: not headless, not the
    browser escape hatch, not a frozen-mode script dispatch (first argument a
    .py file), and the native window is usable."""
    env = os.environ if env is None else env
    argv = list(argv or ())
    if env.get("PA_SKILLS_NO_BROWSER") == "1" or env.get("PA_SKILLS_NO_WINDOW") == "1":
        return False
    if "--no-browser" in argv or "--no-window" in argv:
        return False
    if argv and argv[0].lower().endswith(".py"):
        return False
    return (native_window_available if available is None else available)()


# -- closing --------------------------------------------------------------------------

def terminate_process() -> None:
    """The same close sequence as webui._terminate_process: run the registered
    exit cleanups, then hard-exit 0. ui._config is only asked if it was already
    imported: if it was not, nothing has registered a cleanup yet, and importing
    it here would wait behind the loader thread's imports."""
    try:
        cfg = sys.modules.get("ui._config")
        if cfg is not None:
            cfg.run_exit_cleanups()
    except BaseException:  # noqa: BLE001
        pass
    os._exit(0)


class _Api:
    """What the error page's Close button calls."""

    def close(self):
        terminate_process()


# -- the window ------------------------------------------------------------------------

class PreopenedWindow:
    """The pre-opened window as the loader thread sees it. The loader starts
    importing at once, in parallel with the window coming up, so a page change
    asked for before the window is shown waits (bounded) until it is."""

    def __init__(self, window, shown: threading.Event, wait_s: float = 60.0):
        self._window = window
        self._shown = shown
        self._wait_s = wait_s

    def _ready(self) -> None:
        self._shown.wait(self._wait_s)

    def load_url(self, url):
        self._ready()
        self._window.load_url(url)

    def load_html(self, html_text):
        self._ready()
        self._window.load_html(html_text)


def show_error(window, headline: str, detail: str = "") -> None:
    """Replace the page with the error page. Never raises."""
    try:
        window.load_html(error_html(headline, detail))
    except Exception:  # noqa: BLE001
        pass


def attach_app(window, url: str, wait_for_server, timeout: float = 30.0) -> bool:
    """Switch *window* to the app, but only once the server answers.

    Returns True when the window was switched. When the server does not answer
    in time, the window shows an error instead (never "Starting..." forever)
    and False is returned: there is no switch while the server is down."""
    try:
        ready = wait_for_server(url, timeout)
    except Exception:  # noqa: BLE001
        ready = False
    if not ready:
        show_error(window, "The app did not start in time",
                   f"The local server at {url} did not answer.")
        return False
    window.load_url(url)
    return True


def _load(window, argv) -> None:
    """Worker thread: import the real module, build and start the app."""
    try:
        import importlib  # noqa: PLC0415
        mod = importlib.import_module("ui.webui")
        mod._PREOPENED_WINDOW = window
        mod.main(list(argv))
    except BaseException:  # noqa: BLE001
        show_error(window, "PA Skills could not start", traceback.format_exc())


_LOADER_THREAD: threading.Thread | None = None


def run(argv) -> bool:
    """Open the window first and load the app into it.

    Returns False when this launch is not a window launch (the caller carries on
    exactly as before). When it takes over it does not return: closing the window
    ends the process.

    The window is created first; the loader thread then starts importing at
    once, in parallel with the window coming up (starting it from webview.start's
    own callback would delay the whole load by the time the window takes)."""
    global _LOADER_THREAD
    if not wants_window_first(argv):
        return False
    root = str(PROJECT_ROOT)
    if root not in sys.path:
        sys.path.insert(0, root)
    started = {"loader": False}
    try:
        import webview  # noqa: PLC0415

        try:
            webview.settings["ALLOW_DOWNLOADS"] = True
        except Exception:  # noqa: BLE001
            pass
        window = webview.create_window(
            APP_TITLE, html=loading_html(), width=1200, height=820, js_api=_Api())
        shown = threading.Event()
        window.events.closing += lambda: terminate_process()
        window.events.shown += lambda: shown.set()
        try:
            from ui import _splash  # noqa: PLC0415
            # `shown` is not a locking event: it runs on pywebview's own thread,
            # so the splash search may take its time there (see ui/_splash.py).
            window.events.shown += lambda: _splash.dismiss_launcher_splash()
        except Exception:  # noqa: BLE001
            pass

        loader = threading.Thread(
            target=_load, args=(PreopenedWindow(window, shown), list(argv)),
            name="pa-skills-loader", daemon=True)
        _LOADER_THREAD = loader
        started["loader"] = True
        loader.start()

        icon = window_icon_path()
        if icon:
            webview.start(icon=icon)
        else:
            webview.start()
    except Exception:  # noqa: BLE001
        if not started["loader"]:
            return False          # the window never came up: the old path takes over
    terminate_process()
    return True
