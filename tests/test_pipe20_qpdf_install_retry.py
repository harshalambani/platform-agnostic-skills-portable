"""PIPE-20: the "Install qpdf" step in ci.yml retries the install and does not let a
failed package-list refresh fail the job, but a qpdf that cannot be installed still
fails it. Static checks on the workflow, negative mutations of its text, and a real
run of the step's script under bash with stub apt-get / sudo / qpdf / sleep.
Synthetic only."""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
CI = ROOT / ".github" / "workflows" / "ci.yml"
STEP_NAME = "Install qpdf"
JOB_NAME = "Lint and test"


def _steps(text: str) -> list[dict]:
    doc = yaml.safe_load(text)
    jobs = [j for j in doc["jobs"].values() if j.get("name") == JOB_NAME]
    assert len(jobs) == 1, "expected exactly one 'Lint and test' job"
    return jobs[0]["steps"]


def _install_step(text: str) -> dict:
    hits = [s for s in _steps(text) if s.get("name") == STEP_NAME]
    assert len(hits) == 1, "expected exactly one 'Install qpdf' step"
    return hits[0]


def check_workflow(text: str) -> list[str]:
    """Every way the step can stop doing its job; [] means it is sound."""
    problems: list[str] = []
    try:
        steps = _steps(text)
        step = _install_step(text)
    except AssertionError as e:
        return [str(e)]
    run = step.get("run", "")
    if "continue-on-error" in step or any("continue-on-error" in s for s in steps if s is step):
        problems.append("continue-on-error on the install step")
    names = [s.get("name") for s in steps]
    test_steps = [i for i, s in enumerate(steps) if (s.get("env") or {}).get("PASK_REQUIRE_QPDF") == "1"]
    if not test_steps:
        problems.append("PASK_REQUIRE_QPDF=1 is not set on any step")
    elif names.index(STEP_NAME) > test_steps[0]:
        problems.append("the install step comes after the step that requires qpdf")
    if not re.search(r"for\s+attempt\s+in\s+1\s+2\s+3\s*;\s*do", run):
        problems.append("no bounded 3-attempt loop")
    if "sleep" not in run:
        problems.append("no sleep between attempts")
    if "exit 1" not in run:
        problems.append("no exit 1 after the last failed attempt")
    install_lines = [ln for ln in run.splitlines() if "apt-get install" in ln]
    if not install_lines:
        problems.append("no apt-get install line")
    for ln in run.splitlines():
        if re.search(r"\|\|\s*(true|:|echo)\b", ln) or "|| exit 0" in ln:
            problems.append(f"a failure is tolerated: {ln.strip()}")
    last = [ln.strip() for ln in run.splitlines() if ln.strip() and not ln.strip().startswith("#")]
    if not last or last[-1] != "qpdf --version":
        problems.append("the step does not end with an untolerated qpdf --version")
    if "Acquire::Retries=3" not in run:
        problems.append("apt is not told to retry")
    return problems


# ---- static checks on the real workflow ---------------------------------------

def test_the_real_workflow_is_sound():
    assert check_workflow(CI.read_text(encoding="utf-8")) == []


def test_the_install_line_keeps_the_text_the_pipe19_test_looks_for():
    assert "apt-get install -y qpdf" in CI.read_text(encoding="utf-8")


def test_the_refresh_is_a_warning_not_a_failure():
    run = _install_step(CI.read_text(encoding="utf-8"))["run"]
    assert "::warning::" in run and "if ! sudo apt-get" in run


# ---- negative: each broken variant is flagged ---------------------------------

def _mutate(fn):
    text = CI.read_text(encoding="utf-8")
    out = fn(text)
    assert out != text, "the mutation changed nothing"
    return check_workflow(out)


def test_dropping_the_loop_is_flagged():                                         # NEGATIVE
    assert _mutate(lambda t: t.replace("for attempt in 1 2 3; do", "for attempt in 1; do"))


def test_tolerating_the_install_is_flagged():                                    # NEGATIVE
    assert _mutate(lambda t: t.replace("install -y qpdf -o Acquire::Retries=3; then",
                                       "install -y qpdf -o Acquire::Retries=3 || true; then"))


def test_removing_qpdf_version_is_flagged():                                     # NEGATIVE
    assert _mutate(lambda t: t.replace("          qpdf --version\n", "", 1))


def test_tolerating_qpdf_version_is_flagged():                                   # NEGATIVE
    assert _mutate(lambda t: t.replace("          qpdf --version\n", "          qpdf --version || true\n", 1))


def test_continue_on_error_is_flagged():                                         # NEGATIVE
    assert _mutate(lambda t: t.replace("      - name: Install qpdf\n",
                                       "      - name: Install qpdf\n        continue-on-error: true\n"))


def test_removing_the_requirement_variable_is_flagged():                         # NEGATIVE
    assert _mutate(lambda t: t.replace('PASK_REQUIRE_QPDF: "1"', 'PASK_OTHER: "1"'))


def test_removing_the_final_exit_is_flagged():                                   # NEGATIVE
    assert _mutate(lambda t: t.replace("            exit 1\n          fi\n          qpdf", "            :\n          fi\n          qpdf"))


# ---- behaviour: run the real script with stubs ---------------------------------

BASH = shutil.which("bash")


def _need_bash():
    if BASH is None:
        if os.environ.get("CI"):
            pytest.fail("bash is missing on CI")
        pytest.skip("bash not available")


def _stubs(tmp: Path) -> Path:
    d = tmp / "bin"
    d.mkdir()
    log = tmp / "log"
    log.mkdir()
    scripts = {
        "sudo": '#!/bin/bash\nexec "$@"\n',
        # present on disk from the start, but behaves as "not installed" until the
        # apt-get stub marks the install as done
        "qpdf": (
            '#!/bin/bash\n'
            'if [ ! -f "$LOG/installed" ]; then echo "qpdf: command not found" >&2; exit 127; fi\n'
            'echo x >> "$LOG/qpdf"\n'
            'echo qpdf version 0\n'),
        "sleep": '#!/bin/bash\necho "$1" >> "$LOG/sleep"\nexit 0\n',
        "apt-get": (
            '#!/bin/bash\n'
            'for a in "$@"; do\n'
            '  if [ "$a" = update ]; then echo x >> "$LOG/update"; exit "${UPDATE_RC:-0}"; fi\n'
            '  if [ "$a" = install ]; then\n'
            '    n=0\n'
            '    if [ -f "$LOG/install" ]; then while read -r _; do n=$((n+1)); done < "$LOG/install"; fi\n'
            '    echo x >> "$LOG/install"\n'
            '    if [ "$n" -lt "${INSTALL_FAIL_FIRST:-0}" ]; then exit 100; fi\n'
            '    if [ "${INSTALL_PUTS_QPDF:-1}" = 1 ]; then echo x > "$LOG/installed"; fi\n'
            '    exit 0\n'
            '  fi\n'
            'done\n'
            'exit 0\n'),
    }
    for name, body in scripts.items():
        p = d / name
        p.write_text(body, encoding="utf-8", newline="\n")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def _run_step(tmp_path: Path, **env_over) -> tuple[subprocess.CompletedProcess, dict]:
    _need_bash()
    run = _install_step(CI.read_text(encoding="utf-8"))["run"]
    script = tmp_path / "step.sh"
    script.write_text(run, encoding="utf-8", newline="\n")
    stubdir = _stubs(tmp_path)
    log = tmp_path / "log"
    env = {"PATH": str(stubdir), "LOG": str(log), "STUBDIR": str(stubdir)}
    env.update({k: str(v) for k, v in env_over.items()})
    # GitHub's default shell for `run:` is `bash -e {0}`
    cp = subprocess.run([BASH, "-e", str(script)], env=env, capture_output=True, text=True, timeout=60)

    def count(name):
        f = log / name
        return len(f.read_text().split()) if f.exists() else 0

    return cp, {k: count(k) for k in ("update", "install", "sleep", "qpdf")}


def test_a_failed_refresh_with_a_good_install_passes(tmp_path):
    cp, n = _run_step(tmp_path, UPDATE_RC=100)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert "::warning::" in cp.stdout
    assert n["install"] == 1 and n["qpdf"] == 1


def test_an_install_that_always_fails_stops_after_three_attempts(tmp_path):      # NEGATIVE
    cp, n = _run_step(tmp_path, INSTALL_FAIL_FIRST=99)
    assert cp.returncode != 0
    assert n["install"] == 3 and n["update"] == 3
    assert n["qpdf"] == 0
    assert "::error::" in cp.stdout


def test_an_install_that_fails_once_passes_on_the_second_attempt(tmp_path):
    cp, n = _run_step(tmp_path, INSTALL_FAIL_FIRST=1)
    assert cp.returncode == 0, cp.stdout + cp.stderr
    assert n["install"] == 2 and n["sleep"] == 1 and n["qpdf"] == 1


def test_an_install_that_leaves_no_qpdf_fails_the_step(tmp_path):                # NEGATIVE
    cp, n = _run_step(tmp_path, INSTALL_PUTS_QPDF=0)
    assert cp.returncode != 0
    assert n["install"] == 1 and n["qpdf"] == 0
