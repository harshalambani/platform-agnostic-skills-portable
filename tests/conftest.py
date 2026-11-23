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


# Prefixes the app's own code uses for scratch folders it never removes (or
# removes only on a clean path). Under test they are all routed into pytest's
# base temp, which pytest prunes, so none are left in the system temp folder.
@pytest.fixture
def require_qpdf():
    """PIPE-19: skip without a working qpdf, or fail when PASK_REQUIRE_QPDF=1."""
    from qpdf_helper import qpdf_or_skip_or_fail
    return qpdf_or_skip_or_fail()


APP_TEMP_PREFIXES = ("pa-skills-", "hsbc_bankskill_", "hsbc_run_", "pask-eml-", "pask-intake-")


@pytest.fixture(autouse=True)
def _app_stage_dirs_under_tmp_path(tmp_path_factory, monkeypatch):
    """LEG-12(b): the app stages uploads and parser runs in
    tempfile.mkdtemp(prefix="pa-skills-..." / "hsbc_bankskill_" / "pask-eml-")
    and some paths never remove them, so every test reaching them left a folder
    in the system temp dir. Route those prefixes (and only those, and only when
    no explicit dir was given) under pytest's own base temp. TemporaryDirectory
    calls mkdtemp through the module global, so it is covered too."""
    import tempfile

    real = tempfile.mkdtemp
    base = tmp_path_factory.getbasetemp().resolve() / "_app_stage"

    def _mkdtemp(suffix=None, prefix=None, dir=None):
        if dir is None and isinstance(prefix, str) and prefix.startswith(APP_TEMP_PREFIXES):
            base.mkdir(parents=True, exist_ok=True)
            dir = str(base)
        return real(suffix=suffix, prefix=prefix, dir=dir)

    monkeypatch.setattr(tempfile, "mkdtemp", _mkdtemp)


# ---------------------------------------------------------------------------
# UI-54: nothing a test does may reach the real data root.
# Under pytest ui._config.data_root_dir() resolves to <repo>/Data (or the folder
# of a live config). Three layers keep tests out of it:
#   1. a session-wide FLOOR (its own MonkeyPatch, undone only at session end)
#      points data_root_dir at a folder inside pytest's base temp, so a test's
#      own monkeypatch.undo() falls back to the floor, never to the real root;
#   2. a per-test autouse redirect on top of it;
#   3. a backstop that lists every file under the real root at session start and
#      fails the run if anything was added, changed or removed (temp files too).
# ---------------------------------------------------------------------------

_REAL_ROOT = {"path": None, "before": None}


def _real_root_snapshot(root):
    """{'exists': bool, 'entries': {relative path: (size, mtime_ns) or 'dir'}}; stat only."""
    import os
    snap = {"exists": False, "entries": {}}
    try:
        snap["exists"] = os.path.isdir(root)
        if not snap["exists"]:
            return snap
        for here, dirs, files in os.walk(root):
            for d in dirs:
                snap["entries"][os.path.relpath(os.path.join(here, d), root)] = "dir"
            for f in files:
                full = os.path.join(here, f)
                try:
                    st = os.stat(full)
                    snap["entries"][os.path.relpath(full, root)] = (st.st_size, st.st_mtime_ns)
                except OSError:
                    snap["entries"][os.path.relpath(full, root)] = "unreadable"
    except OSError:
        pass
    return snap


def _snapshot_diff(before, after) -> list:
    out = []
    if before["exists"] != after["exists"]:
        out.append(f"root exists: {before['exists']} -> {after['exists']}")
    b, a = before["entries"], after["entries"]
    out += [f"added: {k}" for k in sorted(set(a) - set(b))]
    out += [f"removed: {k}" for k in sorted(set(b) - set(a))]
    out += [f"changed: {k}" for k in sorted(set(a) & set(b)) if a[k] != b[k]]
    return out


def pytest_sessionstart(session):
    try:
        from ui import _config
        root = _config.data_root_dir()
    except Exception:  # noqa: BLE001 -- UI package not importable
        return
    _REAL_ROOT["path"] = root
    _REAL_ROOT["before"] = _real_root_snapshot(root)


@pytest.fixture(scope="session", autouse=True)
def _data_root_floor(tmp_path_factory):
    try:
        from ui import _config
    except Exception:  # noqa: BLE001
        yield
        return
    floor = tmp_path_factory.getbasetemp().resolve() / "_data_root_floor"
    floor.mkdir(parents=True, exist_ok=True)
    mp = pytest.MonkeyPatch()
    mp.setattr(_config, "data_root_dir", lambda: floor)
    try:
        yield
    finally:
        mp.undo()


@pytest.fixture(autouse=True)
def _data_root_under_pytest_tmp(tmp_path_factory, monkeypatch):
    try:
        from ui import _config
    except Exception:  # noqa: BLE001 -- UI package not importable in this test
        return
    root = tmp_path_factory.getbasetemp().resolve() / "_data_root"
    root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(_config, "data_root_dir", lambda: root)


def pytest_sessionfinish(session, exitstatus):
    """Backstops: LEG-12 (a stray file in the template settings folder; only
    the tracked config.yaml belongs there) and UI-54 (nothing added, changed or
    removed under the real data root)."""
    if _REAL_ROOT["before"] is not None:
        diff = _snapshot_diff(_REAL_ROOT["before"], _real_root_snapshot(_REAL_ROOT["path"]))
        if diff:
            print(f"\nUI-54: tests touched the real data root {_REAL_ROOT['path']}:")
            for line in diff:
                print(f"  {line}")
            session.exitstatus = 1
    extra = _template_settings_files() - _TEMPLATE_SETTINGS_TRACKED
    if extra:
        print(f"\nLEG-12: tests left files in {_TEMPLATE_SETTINGS}: {sorted(extra)}")
        session.exitstatus = 1
