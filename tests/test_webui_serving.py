"""
tests/test_webui_serving.py — Regression guards for Gradio file-serving
security configuration (MP-05, tracker finding #5).

These are intentionally static (source-text) checks rather than live Gradio
integration tests.  They are fast, dependency-free, and catch the most
dangerous regressions:
  - bind address flipped from 127.0.0.1 to 0.0.0.0 / ""
  - share=False removed or set to True
  - allowed_paths widened back to the full outputs/ directory

Dynamic checks (confirming that sibling files are NOT reachable, or that
traversal is blocked at the HTTP layer) require a running Gradio server and
are noted as future integration tests.

Also tests the _config.py helpers introduced by MP-05:
  - download_staging_dir() returns a real directory
  - calling it twice returns the same directory (singleton)
  - it is different from output_dir()
  - download_staging_dir() is *not* a child of output_dir()
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WEBUI_PATH = ROOT / "ui" / "webui.py"
CONFIG_PATH = ROOT / "ui" / "_config.py"


# ---------------------------------------------------------------------------
# 1. Static source checks on webui.py
# ---------------------------------------------------------------------------

class TestWebUIServingConfig:

    def _source(self) -> str:
        return WEBUI_PATH.read_text(encoding="utf-8")

    def test_bind_address_is_loopback(self):
        """server_name must be exactly '127.0.0.1' — never 0.0.0.0 or empty."""
        source = self._source()
        assert 'server_name="127.0.0.1"' in source, (
            "webui.py must bind to 127.0.0.1, not 0.0.0.0 or any other address"
        )

    def test_share_is_false(self):
        """share=False must be explicit — prevents Gradio tunnelling to share.gradio.app."""
        source = self._source()
        assert "share=False" in source, (
            "webui.py must set share=False in app.launch()"
        )

    def test_allowed_paths_uses_staging_dir_not_output_dir(self):
        """
        allowed_paths must use download_staging_dir(), not output_dir().
        Using output_dir() would expose the entire outputs/ folder via HTTP.
        """
        source = self._source()
        assert "download_staging_dir()" in source, (
            "webui.py must pass download_staging_dir() to allowed_paths"
        )
        # The old broad allow-list must be gone from the allowed_paths block.
        # We check that output_dir() is not referenced near allowed_paths.
        # Crude but effective: count occurrences. output_dir() may legitimately
        # appear elsewhere (e.g. in comments) — verify it's not in the block.
        lines = source.splitlines()
        in_allowed_block = False
        for line in lines:
            stripped = line.strip()
            if "allowed_paths" in stripped and "=" in stripped:
                in_allowed_block = True
            if in_allowed_block and "output_dir()" in stripped and "#" not in stripped.split("output_dir()")[0]:
                raise AssertionError(
                    "allowed_paths block must not reference output_dir() — "
                    "that would expose all of outputs/ to the HTTP file route"
                )
            if in_allowed_block and stripped.startswith("app.launch"):
                break

    def test_containment_check_present_in_generic(self):
        """_generic.py must contain an is_relative_to containment check before download."""
        source = (ROOT / "ui" / "tabs" / "_generic.py").read_text(encoding="utf-8")
        assert "is_relative_to" in source, (
            "_generic.py must use is_relative_to() to verify the output path "
            "is inside output_dir before staging for download"
        )

    def test_staging_dir_referenced_in_generic(self):
        """_generic.py must copy files to download_staging_dir(), not serve from output_dir."""
        source = (ROOT / "ui" / "tabs" / "_generic.py").read_text(encoding="utf-8")
        assert "download_staging_dir()" in source, (
            "_generic.py must copy output files into download_staging_dir() "
            "before passing the path to DownloadButton"
        )


# ---------------------------------------------------------------------------
# 1b. Native-window downloads must be enabled (else every DownloadButton is a
#     silent no-op inside the WebView2 window — pywebview cancels downloads
#     unless settings['ALLOW_DOWNLOADS'] is True).
# ---------------------------------------------------------------------------

class TestNativeWindowDownloads:

    def _source(self) -> str:
        return WEBUI_PATH.read_text(encoding="utf-8")

    def test_run_native_window_enables_downloads(self):
        """_run_native_window must turn on downloads before starting the window,
        or all six gr.DownloadButtons are dead in the native window."""
        source = self._source()
        assert "ALLOW_DOWNLOADS" in source, (
            "webui.py must set webview.settings['ALLOW_DOWNLOADS'] = True — "
            "pywebview cancels downloads by default, killing every DownloadButton"
        )
        # The enable step must be wired into the native-window launch path.
        assert "_enable_native_downloads()" in source, (
            "_run_native_window must call _enable_native_downloads()"
        )

    def test_enable_downloads_actually_sets_flag(self):
        """When pywebview is importable, the helper must flip the real flag."""
        try:
            import webview  # noqa: PLC0415
        except Exception:
            import pytest  # noqa: PLC0415
            pytest.skip("pywebview not installed (CI minimal deps)")

        # Load the helper from webui without triggering a full Gradio import
        # chain: exec the module in a namespace is heavy, so import the package.
        ui_path = str(ROOT)
        if ui_path not in sys.path:
            sys.path.insert(0, ui_path)
        src = str(ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        webview.settings["ALLOW_DOWNLOADS"] = False  # start from the unsafe default
        from ui.webui import _enable_native_downloads  # noqa: PLC0415
        _enable_native_downloads()
        assert webview.settings["ALLOW_DOWNLOADS"] is True


# ---------------------------------------------------------------------------
# 1c. The native window must exit on `closing`, not on webview.start() returning.
#     Waiting for start() to return means waiting out the WebView2 unwind, which
#     measured 10.4s warm / 17.1s cold with the window already off screen. The
#     PA launcher holds its SinglePortableAppInstance mutex for that whole time,
#     so relaunching inside it silently does nothing at all.
# ---------------------------------------------------------------------------

class TestNativeWindowShutdown:

    def _source(self) -> str:
        return WEBUI_PATH.read_text(encoding="utf-8")

    def test_closing_event_terminates_process(self):
        """The window's `closing` event must be wired to process termination."""
        source = self._source()
        assert "events.closing" in source, (
            "_run_native_window must subscribe to the window's `closing` event - "
            "without it, shutdown waits out the WebView2 teardown (~10s) while "
            "the launcher still holds its single-instance mutex, so relaunching "
            "does nothing and the app looks broken"
        )
        # The handler has to actually exit; subscribing and doing something else
        # would satisfy the check above while restoring the stall.
        idx = source.index("events.closing")
        assert "_terminate_process" in source[idx:idx + 200], (
            "the `closing` handler must call _terminate_process() (which runs the "
            "atexit temp-dir wipe before os._exit)"
        )

    def test_terminate_does_not_run_the_whole_atexit_set(self):
        """_terminate_process must run OUR cleanups, not atexit._run_exitfuncs().

        The full set includes concurrent.futures' _python_exit, which joins every
        ThreadPoolExecutor worker; with Gradio's server threads still up that
        measured 18.4s on a real close. Firing the `closing` hook without this
        half made shutdown worse than before the fix (22.8s warm vs 10.4s), so
        the two belong together.
        """
        source = self._source()
        start = source.index("def _terminate_process")
        body = source[start:start + 1600]
        # The docstring names _run_exitfuncs to explain why it is NOT used, so
        # look only at what follows it; a call there is the regression.
        after_doc = body.split('"""')[2] if body.count('"""') >= 2 else body
        assert "_run_exitfuncs" not in after_doc, (
            "_terminate_process must not call atexit._run_exitfuncs() - it joins "
            "Gradio's server threads and does not come back"
        )
        assert "run_exit_cleanups()" in after_doc, (
            "_terminate_process must call _config.run_exit_cleanups() so the "
            "temp dirs holding decrypted API keys are still wiped before os._exit"
        )

    def test_config_registers_cleanups_through_the_shared_registry(self):
        """Both temp-dir cleanups must go through register_exit_cleanup, or
        run_exit_cleanups() silently stops wiping them."""
        source = CONFIG_PATH.read_text(encoding="utf-8")
        assert source.count("register_exit_cleanup(") >= 3, (
            "ui/_config.py must define register_exit_cleanup and route BOTH the "
            "legacy-config and download-staging cleanups through it"
        )
        assert "atexit.register(_cleanup" not in source, (
            "cleanups registered with atexit alone are invisible to "
            "run_exit_cleanups(), so the native-window close path would skip them"
        )

    def test_window_handle_is_captured(self):
        """create_window's return value must be bound - the event can only be
        attached to the window object, not to the webview module."""
        source = self._source()
        assert "= webview.create_window(" in source, (
            "_run_native_window must bind the created window to a name so its "
            "`closing` event can be subscribed to"
        )


# ---------------------------------------------------------------------------
# 2. Functional checks on _config.download_staging_dir()
# ---------------------------------------------------------------------------

class TestDownloadStagingDir:
    """
    Import _config directly and exercise download_staging_dir().
    We reset the module singleton between tests via monkeypatching so tests
    don't bleed into each other.
    """

    def _get_fn(self):
        # Add the src/ dir to path if needed so _config is importable.
        src = str(ROOT / "src")
        if src not in sys.path:
            sys.path.insert(0, src)
        # Import from the ui package relative to root.
        ui_path = str(ROOT)
        if ui_path not in sys.path:
            sys.path.insert(0, ui_path)
        from ui import _config
        return _config

    def test_staging_dir_is_directory(self):
        cfg = self._get_fn()
        import ui._config as _config_mod
        # Reset singleton to get a fresh dir for this test.
        _config_mod._DOWNLOAD_STAGING_DIR = None
        d = cfg.download_staging_dir()
        assert d.is_dir(), f"download_staging_dir() must return an existing directory, got {d}"
        # Cleanup
        _config_mod._DOWNLOAD_STAGING_DIR = None
        import shutil; shutil.rmtree(d, ignore_errors=True)

    def test_staging_dir_is_singleton(self):
        cfg = self._get_fn()
        import ui._config as _config_mod
        _config_mod._DOWNLOAD_STAGING_DIR = None
        d1 = cfg.download_staging_dir()
        d2 = cfg.download_staging_dir()
        assert d1 == d2, "download_staging_dir() must return the same path on repeated calls"
        _config_mod._DOWNLOAD_STAGING_DIR = None
        import shutil; shutil.rmtree(d1, ignore_errors=True)

    def test_staging_dir_not_inside_output_dir(self, tmp_path, monkeypatch):
        """The staging dir must be a temp dir, not nested inside output_dir."""
        cfg = self._get_fn()
        import ui._config as _config_mod
        _config_mod._DOWNLOAD_STAGING_DIR = None

        # Override output_dir to a known path inside tmp_path.
        fake_output = tmp_path / "outputs"
        fake_output.mkdir()
        monkeypatch.setattr(_config_mod, "output_dir", lambda: fake_output)

        staging = cfg.download_staging_dir()
        assert staging.is_dir()
        # Staging dir must NOT be inside the (fake) output dir.
        try:
            is_inside = staging.resolve().is_relative_to(fake_output.resolve())
        except TypeError:
            is_inside = str(staging.resolve()).startswith(str(fake_output.resolve()))
        assert not is_inside, (
            f"download_staging_dir() {staging} must not be inside output_dir {fake_output}"
        )
        _config_mod._DOWNLOAD_STAGING_DIR = None
        import shutil; shutil.rmtree(staging, ignore_errors=True)
