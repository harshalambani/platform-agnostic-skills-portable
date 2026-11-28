"""AUD-15: the locked packages are checked against the public advisory list.

scripts/lock_advisory_check.py is what CI runs. The logic tests use a stand-in
for pip-audit (no network). The two end-to-end tests run the real pip-audit on
a synthetic lock; they skip only when pip-audit is not installed or the network
is down, and in CI (PASK_REQUIRE_PIP_AUDIT=1) a skip is a failure.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "lock_advisory_check.py"
import lock_advisory_check as lac  # noqa: E402

HASH = "--hash=sha256:" + "0" * 64


def _lock(tmp_path: Path, pins: list[str]) -> Path:
    p = tmp_path / "lock.txt"
    body = "".join(f"{x} \\\n    {HASH}\n" for x in pins)
    with open(p, "w", encoding="utf-8", newline="\n") as f:      # LF, like the real lock
        f.write(body)
    return p


def _fake_audit(tmp_path: Path, report: dict, code: int = 0) -> str:
    """A stand-in 'pip-audit' that prints `report` and records its arguments."""
    stub = tmp_path / "fake_audit.py"
    stub.write_text(
        "import json, sys\n"
        f"open({str(tmp_path / 'args.txt')!r}, 'w').write(' '.join(sys.argv[1:]))\n"
        f"print(json.dumps({report!r}))\n"
        f"sys.exit({code})\n", encoding="utf-8")
    return f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}"


def _run(tmp_path, lock, audit_cmd, floor=2, ignore=None):
    ign = tmp_path / "ignore.txt"
    ign.write_text(ignore or "# none\n", encoding="utf-8")
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--lock", str(lock), "--ignore-file", str(ign),
         "--floor", str(floor), "--pip-audit", audit_cmd],
        capture_output=True, text=True)


def _report(*deps):
    return {"dependencies": list(deps), "fixes": []}


def _dep(name, ver, vulns=()):
    return {"name": name, "version": ver, "vulns": list(vulns)}


# ---- the floor: never green over zero packages ------------------------------

def test_an_empty_lock_fails_the_floor(tmp_path):
    lock = tmp_path / "lock.txt"
    lock.write_text("# nothing here\n", encoding="utf-8")
    cp = _run(tmp_path, lock, _fake_audit(tmp_path, _report()))
    assert cp.returncode == 2
    assert not (tmp_path / "args.txt").exists()      # pip-audit was not even run


def test_a_missing_lock_fails_the_floor(tmp_path):
    cp = _run(tmp_path, tmp_path / "nope.txt", _fake_audit(tmp_path, _report()))
    assert cp.returncode == 2


def test_a_lock_below_the_floor_fails(tmp_path):
    cp = _run(tmp_path, _lock(tmp_path, ["a==1"]), _fake_audit(tmp_path, _report(_dep("a", "1"))), floor=2)
    assert cp.returncode == 2


def test_an_audit_that_checked_nothing_fails_even_with_a_full_lock(tmp_path):
    lock = _lock(tmp_path, ["a==1", "b==2"])
    cp = _run(tmp_path, lock, _fake_audit(tmp_path, _report()))      # report lists no packages
    assert cp.returncode == 2


def test_skipped_packages_do_not_count_as_checked(tmp_path):
    lock = _lock(tmp_path, ["a==1", "b==2"])
    rep = _report({"name": "a", "skip_reason": "not on the index"},
                  {"name": "b", "skip_reason": "not on the index"})
    assert _run(tmp_path, lock, _fake_audit(tmp_path, rep)).returncode == 2


# ---- findings and the ignore list -------------------------------------------

def test_a_finding_fails_the_run_and_is_listed(tmp_path):
    lock = _lock(tmp_path, ["a==1", "b==2"])
    rep = _report(_dep("a", "1", [{"id": "GHSA-test-0001", "fix_versions": ["1.1"]}]), _dep("b", "2"))
    cp = _run(tmp_path, lock, _fake_audit(tmp_path, rep, code=1))
    assert cp.returncode == 1
    assert "a==1" in cp.stdout and "GHSA-test-0001" in cp.stdout and "1.1" in cp.stdout


def test_a_clean_report_passes(tmp_path):
    lock = _lock(tmp_path, ["a==1", "b==2"])
    cp = _run(tmp_path, lock, _fake_audit(tmp_path, _report(_dep("a", "1"), _dep("b", "2"))))
    assert cp.returncode == 0


def test_ignored_ids_are_passed_to_the_audit_and_comments_are_skipped(tmp_path):
    lock = _lock(tmp_path, ["a==1", "b==2"])
    _run(tmp_path, lock, _fake_audit(tmp_path, _report(_dep("a", "1"), _dep("b", "2"))),
         ignore="# reviewed\nGHSA-aaaa-0001  # not reachable from the app\n\nPYSEC-2000-1\n")
    args = (tmp_path / "args.txt").read_text()
    assert "--ignore-vuln GHSA-aaaa-0001" in args and "--ignore-vuln PYSEC-2000-1" in args
    assert "reachable" not in args


def test_the_tracked_ignore_list_starts_empty():
    text = (ROOT / ".github" / "pip-audit-ignore.txt").read_text(encoding="utf-8")
    assert lac.read_ignore_ids(text) == []


def test_pin_counter_matches_a_real_hashed_lock():
    n = lac.count_pins((ROOT / "requirements-lock.txt").read_text(encoding="utf-8"))
    assert n >= lac.DEFAULT_FLOOR


def test_the_ci_job_runs_the_script_with_the_pinned_tool():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/lock_advisory_check.py" in ci
    assert "pip-audit==" in ci
    assert "PASK_REQUIRE_PIP_AUDIT" in ci


# ---- end to end with the real pip-audit (network) ---------------------------

def _real_audit() -> str | None:
    env = os.environ.get("PIP_AUDIT_BIN")
    return env or shutil.which("pip-audit")


def _need_real(reason: str):
    if os.environ.get("PASK_REQUIRE_PIP_AUDIT") == "1":
        pytest.fail(reason + " (a skip is not allowed in CI)")
    pytest.skip(reason)


def _online() -> bool:
    import socket
    try:
        socket.create_connection(("pypi.org", 443), timeout=5).close()
        return True
    except OSError:
        return False


def test_real_audit_fails_on_a_known_vulnerable_pin(tmp_path):
    exe = _real_audit()
    if not exe:
        _need_real("pip-audit is not installed")
    if not _online():
        _need_real("no network")
    lock = _lock(tmp_path, ["requests==2.19.1"])           # old release with published advisories
    cp = _run(tmp_path, lock, shlex.quote(exe), floor=1)
    assert cp.returncode == 1, cp.stdout + cp.stderr
    assert "requests==2.19.1" in cp.stdout


def test_real_audit_passes_a_current_pin_so_the_failure_above_is_not_blind(tmp_path):
    exe = _real_audit()
    if not exe:
        _need_real("pip-audit is not installed")
    if not _online():
        _need_real("no network")
    # six has no published advisories; one package, floor 1.
    lock = _lock(tmp_path, ["six==1.17.0"])
    cp = _run(tmp_path, lock, shlex.quote(exe), floor=1)
    assert cp.returncode == 0, cp.stdout + cp.stderr
