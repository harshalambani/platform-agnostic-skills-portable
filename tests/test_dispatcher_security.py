"""
tests/test_dispatcher_security.py — Security regression tests for MP-02.

Tracker finding #2 (High): `_maybe_dispatch_script()` in ui/webui.py executed
any .py file passed as the first CLI argument via runpy.run_path(), with no
path containment check.  An attacker who can influence argv (file association,
crafted shortcut, sibling process) could run arbitrary code through the trusted
pa_skills.exe binary.

Fix: the resolved script path must be a child of the bundled scripts root
(sys._MEIPASS/agents in frozen mode; PROJECT_ROOT/src/agents in source mode).
Anything outside that root is rejected with exit code 1 and never falls
through to UI launch.

These tests cover:
  - External/arbitrary scripts are rejected (exit 1, runpy never called)
  - Path traversal into the root via ".." is rejected
  - Symlinks pointing outside the root are rejected
  - A genuine bundled script is accepted and dispatched
  - Non-.py args / empty argv / missing file → None (UI path unaffected)
  - _bundled_scripts_root() resolves correctly in source vs frozen mode

Run with:
    cd src && python -m pytest ../tests/test_dispatcher_security.py -v
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
WEBUI_PATH = ROOT / "ui" / "webui.py"


# ---------------------------------------------------------------------------
# Load _maybe_dispatch_script and _bundled_scripts_root from webui.py source
# without triggering the module's heavy top-level imports (gradio, _update, etc.).
# We extract only the two target functions, supply the minimal symbols they need
# (sys, Path, print), and exec them into an isolated namespace.
# ---------------------------------------------------------------------------

def _extract_dispatcher_source(webui_source: str) -> str:
    """
    Extract _bundled_scripts_root and _maybe_dispatch_script from the webui source,
    plus the PROJECT_ROOT definition they depend on.
    """
    lines = webui_source.splitlines(keepends=True)

    # Grab PROJECT_ROOT definition (single line assignment near the top).
    project_root_line = ""
    for line in lines:
        if line.strip().startswith("PROJECT_ROOT"):
            project_root_line = line
            break

    # Extract each function: from its `def` line up to (but not including) the
    # next top-level `def` or `class` statement.
    def _extract_func(name: str) -> str:
        in_func = False
        func_lines: list[str] = []
        for line in lines:
            if not in_func:
                if re.match(rf"^def {name}\b", line):
                    in_func = True
                    func_lines.append(line)
            else:
                # Next top-level definition signals end of this function.
                if re.match(r"^(def |class )", line):
                    break
                func_lines.append(line)
        return "".join(func_lines)

    root_fn = _extract_func("_bundled_scripts_root")
    dispatch_fn = _extract_func("_maybe_dispatch_script")

    return (
        "import sys\n"
        "import runpy\n"
        "from pathlib import Path\n"
        "\n"
        + project_root_line
        + "\n"
        + root_fn
        + "\n"
        + dispatch_fn
    )


@pytest.fixture(scope="module")
def dispatcher_ns():
    """Namespace with _bundled_scripts_root and _maybe_dispatch_script loaded."""
    source = WEBUI_PATH.read_text(encoding="utf-8")
    extracted = _extract_dispatcher_source(source)
    ns: dict[str, Any] = {"__file__": str(WEBUI_PATH)}
    exec(compile(extracted, "webui_extracted.py", "exec"), ns)
    assert "_bundled_scripts_root" in ns, "Failed to extract _bundled_scripts_root"
    assert "_maybe_dispatch_script" in ns, "Failed to extract _maybe_dispatch_script"
    return ns


# ---------------------------------------------------------------------------
# Convenience wrappers that run functions in the extracted namespace.
# ---------------------------------------------------------------------------

def _dispatch(ns, argv, *, mock_root=None, mock_runpy=True):
    """
    Call ns['_maybe_dispatch_script'](argv) with optional overrides.

    mock_root: if given, override _bundled_scripts_root to return this Path.
    mock_runpy: if True, intercept runpy.run_path at sys.modules level so that
                the `import runpy` inside the function body picks up the mock.
    Returns (rc, runpy_calls) where runpy_calls is the list of paths passed to
    the mock (empty if mock_runpy=False or runpy was never called).
    """
    runpy_calls: list[str] = []

    def _fake_runpy(path, run_name="__main__"):
        runpy_calls.append(path)

    saved_root = ns.get("_bundled_scripts_root")

    try:
        if mock_root is not None:
            ns["_bundled_scripts_root"] = lambda: Path(mock_root).resolve()

        if mock_runpy:
            # Patch at sys.modules so that `import runpy` inside the function
            # returns our mock — patching ns["runpy"] alone does not work because
            # the function re-imports runpy as a local name.
            with patch("runpy.run_path", side_effect=_fake_runpy):
                rc = ns["_maybe_dispatch_script"](argv)
        else:
            rc = ns["_maybe_dispatch_script"](argv)
    finally:
        ns["_bundled_scripts_root"] = saved_root

    return rc, runpy_calls


# ---------------------------------------------------------------------------
# 1. Negative / rejection tests
# ---------------------------------------------------------------------------

class TestDispatcherRejectsExternal:
    """Scripts outside the bundled root must be rejected with rc=1."""

    def test_reject_external_tmp_script(self, dispatcher_ns, tmp_path):
        """A .py file in /tmp (or system temp) is outside the bundle — reject."""
        evil = tmp_path / "evil.py"
        evil.write_text("import os; os.system('id')\n")

        rc, calls = _dispatch(dispatcher_ns, [str(evil)])
        assert rc == 1, f"Expected exit 1, got {rc}"
        assert calls == [], "runpy.run_path must not have been called"

    def test_reject_traversal_dotdot(self, dispatcher_ns, tmp_path):
        """
        A script path like <root>/../../evil.py resolves outside the root.
        After resolve() the containment check catches it.
        """
        # Build a fake scripts root inside tmp_path
        scripts_root = tmp_path / "agents"
        scripts_root.mkdir()
        # Create the evil script one level above agents/ (i.e. in tmp_path, not inside agents/)
        evil = tmp_path / "evil.py"
        evil.write_text("pass\n")
        # Craft a traversal path: agents/../evil.py → resolves to tmp_path/evil.py
        # (One ".." steps out of agents/ into tmp_path; two would step above tmp_path
        #  to a non-existent file, causing is_file() to short-circuit before the check.)
        traversal = scripts_root / ".." / evil.name
        # Make sure traversal.resolve() == evil.resolve() (outside root)
        assert traversal.resolve() == evil.resolve()

        rc, calls = _dispatch(dispatcher_ns, [str(traversal)], mock_root=scripts_root)
        assert rc == 1, f"Traversal should be rejected, got rc={rc}"
        assert calls == []

    def test_reject_symlink_outside_root(self, dispatcher_ns, tmp_path):
        """A symlink inside the scripts root that points outside it is rejected."""
        scripts_root = tmp_path / "agents"
        scripts_root.mkdir()
        target = tmp_path / "real_evil.py"
        target.write_text("pass\n")
        link = scripts_root / "link.py"
        try:
            link.symlink_to(target)
        except (OSError, NotImplementedError):
            pytest.skip("symlinks not supported on this platform")

        rc, calls = _dispatch(dispatcher_ns, [str(link)], mock_root=scripts_root)
        assert rc == 1, f"Symlink pointing outside root should be rejected, got rc={rc}"
        assert calls == []

    def test_reject_returns_nonzero_not_none(self, dispatcher_ns, tmp_path):
        """Rejection must return a non-None value so main() never falls through to the UI."""
        evil = tmp_path / "not_in_bundle.py"
        evil.write_text("pass\n")
        rc, _ = _dispatch(dispatcher_ns, [str(evil)])
        assert rc is not None, "Rejected dispatch must return non-None (not fall through to UI)"
        assert rc != 0, f"Rejected dispatch should exit non-zero, got {rc}"


# ---------------------------------------------------------------------------
# 2. Non-dispatch cases (must return None — UI path unaffected)
# ---------------------------------------------------------------------------

class TestDispatcherPassThrough:
    """Non-.py args, empty argv, missing files must return None so the UI launches normally."""

    def test_empty_argv(self, dispatcher_ns):
        rc, _ = _dispatch(dispatcher_ns, [])
        assert rc is None

    def test_non_py_arg(self, dispatcher_ns):
        rc, _ = _dispatch(dispatcher_ns, ["--no-browser"])
        assert rc is None

    def test_non_py_arg_with_path(self, dispatcher_ns):
        rc, _ = _dispatch(dispatcher_ns, ["/some/file.txt"])
        assert rc is None

    def test_missing_file(self, dispatcher_ns):
        rc, _ = _dispatch(dispatcher_ns, ["/nonexistent/path/script.py"])
        assert rc is None

    def test_missing_file_in_root(self, dispatcher_ns, tmp_path):
        """Even a path that looks inside the root must exist as a file."""
        scripts_root = tmp_path / "agents"
        scripts_root.mkdir()
        ghost = scripts_root / "ghost.py"  # doesn't actually exist
        rc, _ = _dispatch(dispatcher_ns, [str(ghost)], mock_root=scripts_root)
        assert rc is None


# ---------------------------------------------------------------------------
# 3. Acceptance test — bundled script is dispatched
# ---------------------------------------------------------------------------

class TestDispatcherAcceptsBundled:

    def test_bundled_script_dispatched(self, dispatcher_ns, tmp_path):
        """A .py file that resolves inside the bundled scripts root is accepted."""
        scripts_root = tmp_path / "agents"
        scripts_root.mkdir()
        bundled = scripts_root / "run_pipeline.py"
        bundled.write_text("pass\n")

        rc, calls = _dispatch(dispatcher_ns, [str(bundled)], mock_root=scripts_root)
        assert rc == 0, f"Bundled script should succeed, got rc={rc}"
        assert len(calls) == 1, f"runpy.run_path should have been called once, got {calls}"
        assert str(bundled.resolve()) in calls[0], f"Wrong path dispatched: {calls}"

    def test_bundled_script_in_subdirectory(self, dispatcher_ns, tmp_path):
        """Scripts in subdirs of the root are still accepted."""
        scripts_root = tmp_path / "agents"
        subdir = scripts_root / "skill_hsbc" / "scripts"
        subdir.mkdir(parents=True)
        bundled = subdir / "run_pipeline.py"
        bundled.write_text("pass\n")

        rc, calls = _dispatch(dispatcher_ns, [str(bundled)], mock_root=scripts_root)
        assert rc == 0
        assert len(calls) == 1

    def test_script_exit_code_propagated(self, dispatcher_ns, tmp_path):
        """The script's SystemExit code is forwarded as the return value."""
        scripts_root = tmp_path / "agents"
        scripts_root.mkdir()
        bundled = scripts_root / "exiting_script.py"
        bundled.write_text("pass\n")

        saved_root = dispatcher_ns["_bundled_scripts_root"]
        try:
            dispatcher_ns["_bundled_scripts_root"] = lambda: scripts_root.resolve()
            with patch("runpy.run_path", side_effect=SystemExit(42)):
                rc = dispatcher_ns["_maybe_dispatch_script"]([str(bundled)])
        finally:
            dispatcher_ns["_bundled_scripts_root"] = saved_root

        assert rc == 42, f"Expected exit code 42, got {rc}"

    def test_script_exception_is_caught_not_propagated(self, dispatcher_ns, tmp_path):
        """An unhandled exception in the dispatched script must be caught and
        converted to a non-zero exit — NOT propagated. In a windowed frozen
        build a propagated exception pops a modal 'Unhandled exception in
        script' dialog and blocks until dismissed, which hangs the parent's
        subprocess.run() and leaves the UI stuck on 'Running…'."""
        scripts_root = tmp_path / "agents"
        scripts_root.mkdir()
        bundled = scripts_root / "crashing_script.py"
        bundled.write_text("pass\n")

        saved_root = dispatcher_ns["_bundled_scripts_root"]
        try:
            dispatcher_ns["_bundled_scripts_root"] = lambda: scripts_root.resolve()
            with patch("runpy.run_path",
                       side_effect=ValueError("invalid literal for int() with base 10: 'Sr 7'")):
                # Must NOT raise; must return a non-zero exit code.
                rc = dispatcher_ns["_maybe_dispatch_script"]([str(bundled)])
        finally:
            dispatcher_ns["_bundled_scripts_root"] = saved_root

        assert rc == 1, f"Unhandled script exception should yield rc=1, got {rc}"


# ---------------------------------------------------------------------------
# 4. _bundled_scripts_root() resolves correctly in source vs frozen mode
# ---------------------------------------------------------------------------

class TestBundledScriptsRoot:

    def test_source_mode_no_meipass(self, dispatcher_ns):
        """Without sys._MEIPASS, root should be PROJECT_ROOT/src/agents."""
        fn = dispatcher_ns["_bundled_scripts_root"]
        project_root = dispatcher_ns["PROJECT_ROOT"]

        saved = getattr(sys, "_MEIPASS", _SENTINEL := object())
        try:
            if hasattr(sys, "_MEIPASS"):
                del sys._MEIPASS
            result = fn()
        finally:
            if saved is not _SENTINEL:
                sys._MEIPASS = saved

        expected = (project_root / "src" / "agents").resolve()
        assert result == expected, f"Source mode root wrong: {result} != {expected}"

    def test_frozen_mode_with_meipass(self, dispatcher_ns, tmp_path):
        """With sys._MEIPASS set, root should be sys._MEIPASS/agents."""
        fn = dispatcher_ns["_bundled_scripts_root"]
        fake_meipass = str(tmp_path / "meipass")
        Path(fake_meipass).mkdir()

        saved = getattr(sys, "_MEIPASS", _SENTINEL := object())
        try:
            sys._MEIPASS = fake_meipass
            result = fn()
        finally:
            if saved is _SENTINEL:
                if hasattr(sys, "_MEIPASS"):
                    del sys._MEIPASS
            else:
                sys._MEIPASS = saved

        expected = (Path(fake_meipass) / "agents").resolve()
        assert result == expected, f"Frozen mode root wrong: {result} != {expected}"


# ---------------------------------------------------------------------------
# 5. Static: verify the containment + reject path is present in webui source
# ---------------------------------------------------------------------------

class TestWebUISourceContainment:

    def test_is_relative_to_used(self):
        """The containment check must use is_relative_to (or equivalent)."""
        source = WEBUI_PATH.read_text()
        assert "is_relative_to" in source, (
            "webui.py must use Path.is_relative_to() for containment check"
        )

    def test_reject_exits_nonzero(self):
        """The reject path must return a non-zero exit code, not None."""
        source = WEBUI_PATH.read_text()
        # The reject branch should have `return 1` (or similar non-zero literal)
        assert "return 1" in source, (
            "webui.py must return non-zero from the reject path"
        )

    def test_bundled_scripts_root_helper_present(self):
        """_bundled_scripts_root helper must exist in webui.py."""
        source = WEBUI_PATH.read_text()
        assert "def _bundled_scripts_root" in source

    def test_meipass_guarded(self):
        """sys._MEIPASS access must be guarded (getattr with default)."""
        source = WEBUI_PATH.read_text()
        assert 'getattr(sys, "_MEIPASS"' in source or "getattr(sys, '_MEIPASS'" in source, (
            "sys._MEIPASS must be accessed via getattr to be safe in source mode"
        )

    def test_dispatch_catches_unhandled_exceptions(self):
        """The dispatcher must catch non-SystemExit exceptions so a crashing
        bundled script can't pop PyInstaller's modal dialog and hang the parent."""
        source = WEBUI_PATH.read_text()
        assert "except BaseException" in source, (
            "dispatcher must catch BaseException from runpy so a script crash "
            "exits cleanly instead of surfacing PyInstaller's modal error dialog"
        )

    def test_console_suppression_present_and_wired(self):
        """The console-window suppression helper must exist, be called from
        main(), and use CREATE_NO_WINDOW."""
        source = WEBUI_PATH.read_text()
        assert "def _suppress_console_windows" in source
        assert "_suppress_console_windows()" in source, (
            "_suppress_console_windows must be called (from main())"
        )
        assert "CREATE_NO_WINDOW" in source
