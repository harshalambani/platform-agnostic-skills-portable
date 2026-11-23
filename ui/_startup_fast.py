"""PERF-04 A: cheap start-up wins that need no change to any tab.

Three things, each used only on versions it was measured and tested on, and
each falling back to the stock path on any other version:

1. The SSL context httpx builds on every Client() (about 0.85s: certifi is
   re-read each time) is built once in a background thread right after the
   gradio import (overlapping the tab build), and handed to httpx.
2. ``Blocks.__exit__`` builds the config and the web app, and launch() builds
   both again. For the launched app the first build is skipped.
3. launch(_frontend=False) skips gradio's own localhost probe; the window code
   already waits for the server with one request.
"""
from __future__ import annotations

import contextlib
import os
import threading

# Versions this was measured and tested on. Anything else uses the stock path.
TESTED_GRADIO = frozenset({"6.32.0", "6.29.0"})
TESTED_HTTPX = frozenset({"0.28.1"})

_SSL: dict = {}
_SSL_THREAD: threading.Thread | None = None
_ssl_installed = False


def _warm_ssl() -> None:
    try:
        import ssl  # noqa: PLC0415

        import certifi  # noqa: PLC0415
        _SSL["ctx"] = ssl.create_default_context(cafile=certifi.where())
    except Exception:  # noqa: BLE001 - a missed head start is not an error
        pass


def start_ssl_warm() -> threading.Thread:
    """Start building the SSL context in the background (idempotent)."""
    global _SSL_THREAD
    if _SSL_THREAD is None:
        _SSL_THREAD = threading.Thread(target=_warm_ssl, name="pa-ssl-warm", daemon=True)
        _SSL_THREAD.start()
    return _SSL_THREAD


def _ssl_env_overrides() -> bool:
    return bool(os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"))


def install_ssl_cache() -> bool:
    """Make httpx reuse the warmed context. False when it does not apply."""
    global _ssl_installed
    if _ssl_installed:
        return True
    try:
        import httpx  # noqa: PLC0415
        import httpx._config as _cfg  # noqa: PLC0415
        import httpx._transports.default as _dflt  # noqa: PLC0415
        if httpx.__version__ not in TESTED_HTTPX or _SSL_THREAD is None:
            return False
        orig = _cfg.create_ssl_context

        def cached(verify=True, cert=None, trust_env=True):
            if verify is True and cert is None and not _ssl_env_overrides():
                _SSL_THREAD.join()
                ctx = _SSL.get("ctx")
                if ctx is not None:
                    return ctx
            return orig(verify=verify, cert=cert, trust_env=trust_env)

        _dflt.create_ssl_context = cached
        _ssl_installed = True
        return True
    except Exception:  # noqa: BLE001
        return False


def gradio_is_tested(gr) -> bool:
    return getattr(gr, "__version__", "") in TESTED_GRADIO


_tls = threading.local()


@contextlib.contextmanager
def skip_exit_build(gr, enabled: bool = True):
    """While active, ``Blocks.__exit__`` skips its config and web-app build.

    Yields True when the skip is active. Inactive (stock behaviour) when
    *enabled* is False or the gradio version is not one that was tested."""
    if not enabled or not gradio_is_tested(gr):
        yield False
        return
    import gradio.blocks as _blocks  # noqa: PLC0415
    import gradio.routes as _routes  # noqa: PLC0415
    orig_exit = _blocks.Blocks.__exit__
    orig_cfg = _blocks.Blocks.get_config_file
    orig_app = _routes.App.create_app

    def exit_(self, *a, **k):
        _tls.in_exit = True
        try:
            return orig_exit(self, *a, **k)
        finally:
            _tls.in_exit = False

    def cfg(self):
        if getattr(_tls, "in_exit", False):
            return getattr(self, "config", None) or {}
        return orig_cfg(self)

    def app_(*a, **k):
        if getattr(_tls, "in_exit", False) and k.get("mcp_server", None) is False:
            return None
        return orig_app(*a, **k)

    _blocks.Blocks.__exit__ = exit_
    _blocks.Blocks.get_config_file = cfg
    _routes.App.create_app = staticmethod(app_)
    try:
        yield True
    finally:
        _blocks.Blocks.__exit__ = orig_exit
        _blocks.Blocks.get_config_file = orig_cfg
        _routes.App.create_app = orig_app
        _tls.in_exit = False
