"""AUD-13: run logs are kept 90 days; older ones in Data/logs are removed.

Only run-log files in that one folder, by their own modified date. Nothing
else is touched, and a failed prune never stops a run. Synthetic folders only.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from ui import _config, _runlog  # noqa: E402

DAY = 86400


def _file(folder: Path, name: str, age_days: float, now: float, text="x") -> Path:
    p = folder / name
    p.write_text(text, encoding="utf-8")
    t = now - age_days * DAY
    os.utime(p, (t, t))
    return p


def test_the_limit_is_ninety_days():
    assert _runlog.RETENTION_DAYS == 90


def test_a_91_day_log_goes_an_89_day_log_and_a_non_log_stay(tmp_path):
    now = time.time()
    old = _file(tmp_path, "2026-08-26-101500-bank.log", 91, now)
    young = _file(tmp_path, "2026-08-28-101500-bank.log", 89, now)
    other_old = _file(tmp_path, "notes.txt", 400, now)
    warn_old = _file(tmp_path, "warnings.log", 400, now)       # not a run-log name
    n = _runlog.prune_old_logs(tmp_path, now=now)
    assert n == 1
    assert not old.exists()
    assert young.exists() and other_old.exists() and warn_old.exists()


def test_a_folder_named_like_a_log_is_left_alone(tmp_path):
    now = time.time()
    d = tmp_path / "2025-01-01-000000-x.log"
    d.mkdir()
    (d / "inner.txt").write_text("keep", encoding="utf-8")
    os.utime(d, (now - 400 * DAY, now - 400 * DAY))
    assert _runlog.prune_old_logs(tmp_path, now=now) == 0
    assert (d / "inner.txt").exists()


def _make_link(link: Path, target: Path) -> bool:
    try:
        os.symlink(target, link, target_is_directory=True)
        return True
    except (OSError, NotImplementedError):
        pass
    if os.name == "nt":
        cp = subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)],
                            capture_output=True, text=True)
        return cp.returncode == 0 and link.exists()
    return False


def test_a_link_inside_the_folder_pointing_out_is_not_followed(tmp_path):
    now = time.time()
    logs = tmp_path / "logs"
    logs.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _file(outside, "2020-01-01-000000-x.log", 900, now)
    if not _make_link(logs / "2020-01-01-000000-link.log", outside):
        pytest.skip("cannot make a link here")
    _runlog.prune_old_logs(logs, now=now)
    assert victim.exists()                      # nothing outside was touched


def test_the_folder_itself_being_a_link_out_prunes_nothing(tmp_path):
    now = time.time()
    outside = tmp_path / "outside"
    outside.mkdir()
    victim = _file(outside, "2020-01-01-000000-x.log", 900, now)
    logs = tmp_path / "logs"
    if not _make_link(logs, outside):
        pytest.skip("cannot make a link here")
    assert _runlog.prune_old_logs(logs, now=now) == 0
    assert victim.exists()


def test_a_log_that_cannot_be_removed_does_not_stop_the_prune(tmp_path, monkeypatch):
    now = time.time()
    stuck = _file(tmp_path, "2025-01-01-000000-a.log", 500, now)
    gone = _file(tmp_path, "2025-01-02-000000-b.log", 500, now)
    real = os.unlink

    def fake(path, *a, **k):
        if str(path).endswith("-a.log"):
            raise PermissionError("locked")
        return real(path, *a, **k)

    monkeypatch.setattr(os, "unlink", fake)
    n = _runlog.prune_old_logs(tmp_path, now=now)      # must not raise
    assert stuck.exists() and not gone.exists() and n == 1


def test_a_read_only_old_log_does_not_crash(tmp_path):
    now = time.time()
    p = _file(tmp_path, "2025-01-01-000000-a.log", 500, now)
    os.chmod(p, 0o444)
    _runlog.prune_old_logs(tmp_path, now=now)          # removed or kept, never raises
    assert True


def test_a_missing_folder_does_not_raise(tmp_path):
    assert _runlog.prune_old_logs(tmp_path / "nope") == 0


def test_a_failing_prune_never_stops_a_new_run_log(tmp_path, monkeypatch):
    monkeypatch.setattr(_config, "data_root_dir", lambda: tmp_path)
    monkeypatch.setattr(os, "scandir", lambda *a, **k: (_ for _ in ()).throw(OSError("boom")))
    path = _runlog.new_log_path("Bank Skill")          # must still hand back a path
    assert path.parent == tmp_path / "logs" and path.name.endswith("-Bank_Skill.log")


def test_a_new_run_log_prunes_only_that_folders_old_logs(tmp_path, monkeypatch):
    now = time.time()
    monkeypatch.setattr(_config, "data_root_dir", lambda: tmp_path)
    logs = tmp_path / "logs"
    logs.mkdir()
    old = _file(logs, "2025-01-01-000000-a.log", 500, now)
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    out_old = _file(outputs, "2025-01-01-000000-a.log", 500, now)   # same name, other folder
    _runlog.new_log_path("x")
    assert not old.exists()
    assert out_old.exists()
