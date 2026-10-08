"""
Shared pytest fixtures.

SEC-19: the review tabs refuse a file outside the known folders. Most review
tests write their synthetic CSVs under pytest's tmp_path, so by default the
pytest base temp directory counts as a known folder. tests/test_sec19_path_guard.py
opts out (it tests the guard itself).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _review_tabs_known_folders(request, tmp_path_factory, monkeypatch):
    if Path(str(request.node.fspath)).name in ("test_sec19_path_guard.py",
                                               "test_registered_book_folders.py"):
        return
    try:
        from ui import _safe_paths
    except Exception:  # noqa: BLE001 -- UI package not importable in this test
        return
    base = tmp_path_factory.getbasetemp().resolve()
    real = _safe_paths.known_folders
    monkeypatch.setattr(_safe_paths, "known_folders", lambda: list(real()) + [base])


# ---------------------------------------------------------------------------
# LEG-12: tests must never leave files behind in the repo or the system temp.
# ---------------------------------------------------------------------------

_TEMPLATE_SETTINGS = (_ROOT / "bundling" / "templates" / "DefaultData" / "settings").resolve()
_TEMPLATE_SETTINGS_TRACKED = {"config.yaml"}


def _template_settings_files() -> set:
    if not _TEMPLATE_SETTINGS.is_dir():
        return set()
    return {p.name for p in _TEMPLATE_SETTINGS.iterdir()}


@pytest.fixture(autouse=True)
def _pin_rules_path_out_of_the_repo(tmp_path_factory, monkeypatch):
    """LEG-12(a): persistent_rules.rules_path falls back to the directory of
    config_path when the book sits in a temp dir. A test that reaches that
    fallback with the repo's template config (ui._config.PORTABLE_CONFIG_PATH
    in a dev checkout) wrote *_mapping_rules.yaml into
    bundling/templates/DefaultData/settings/. Redirect any such result into
    the pytest base temp folder instead; every other result is untouched."""
    try:
        from agents.skill_gnucash_account_mapper import persistent_rules as pr
    except Exception:  # noqa: BLE001 -- not importable in this test
        return
    real = pr.rules_path
    sink = tmp_path_factory.getbasetemp().resolve() / "_redirected_rules"

    def _guarded(gnucash_file, config_path=None):
        p = real(gnucash_file, config_path)
        try:
            inside = Path(p).resolve().parent == _TEMPLATE_SETTINGS
        except OSError:
            inside = False
        if inside:
            sink.mkdir(parents=True, exist_ok=True)
            return sink / Path(p).name
        return p

    monkeypatch.setattr(pr, "rules_path", _guarded)


@pytest.fixture(autouse=True)
def _hsbc_stage_dirs_under_tmp_path(tmp_path_factory, monkeypatch):
    """LEG-12(b): skill_hsbc.agent stages a single PDF in
    tempfile.mkdtemp(prefix="hsbc_run_") and never removes it, so every test
    reaching that path left a folder in the system temp dir. Route that prefix
    (and only that prefix) under pytest's own base temp, which pytest prunes."""
    import tempfile

    real = tempfile.mkdtemp
    base = tmp_path_factory.getbasetemp().resolve() / "_hsbc_stage"

    def _mkdtemp(suffix=None, prefix=None, dir=None):
        if prefix == "hsbc_run_" and dir is None:
            base.mkdir(parents=True, exist_ok=True)
            dir = str(base)
        return real(suffix=suffix, prefix=prefix, dir=dir)

    monkeypatch.setattr(tempfile, "mkdtemp", _mkdtemp)


def pytest_sessionfinish(session, exitstatus):
    """LEG-12 backstop: fail the run if any test left a stray file in the
    template settings folder (only the tracked config.yaml belongs there)."""
    extra = _template_settings_files() - _TEMPLATE_SETTINGS_TRACKED
    if extra:
        print(f"\nLEG-12: tests left files in {_TEMPLATE_SETTINGS}: {sorted(extra)}")
        session.exitstatus = 1
