"""AUD-19: the locked-packages advisory check also runs every week.

The workflows are never run here; these tests read them as data. The weekly
workflow must run the same pinned tool and the same script as the CI job, with
nothing that skips the package floor or the ignore-file rule, and the CI
workflow must keep its push and pull-request triggers exactly as they were.
"""
import copy
from pathlib import Path

import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WEEKLY = WORKFLOWS / "lock-advisory-weekly.yml"
CI = WORKFLOWS / "ci.yml"
IGNORE_FILE = WORKFLOWS.parent / "pip-audit-ignore.txt"


def _load(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    # PyYAML reads the bare key `on` as the boolean True.
    if True in doc:
        doc["on"] = doc.pop(True)
    return doc


def _job(doc):
    jobs = doc["jobs"]
    assert list(jobs) == ["advisory-check"], "expected exactly the advisory-check job"
    return jobs["advisory-check"]


def _job_by_name(doc, name):
    return doc["jobs"][name]


def _run_lines(job):
    return [s["run"] for s in job["steps"] if "run" in s]


def _audit_runs(job):
    """The run blocks that install the tool or call the check script."""
    return [r for r in _run_lines(job)
            if "pip-audit==" in r or "scripts/lock_advisory_check.py" in r]


def _check_text(doc):
    return " ".join(_audit_runs(_job(doc)))


# --- the schedule exists --------------------------------------------------

def test_weekly_workflow_has_a_schedule_and_a_manual_trigger():
    on = _load(WEEKLY)["on"]
    assert "schedule" in on and "workflow_dispatch" in on
    crons = [e["cron"] for e in on["schedule"]]
    assert len(crons) == 1
    fields = crons[0].split()
    assert len(fields) == 5
    minute, hour, dom, month, dow = fields
    assert dom == "*" and month == "*", "must be a weekly cron, not monthly or yearly"
    assert dow.isdigit() and 0 <= int(dow) <= 7, "must name one weekday"
    assert minute.isdigit() and hour.isdigit()


def test_weekly_workflow_does_not_run_on_push_or_pull_request():
    on = _load(WEEKLY)["on"]
    assert "push" not in on and "pull_request" not in on


# --- it runs the same pinned check ------------------------------------------

def test_weekly_runs_the_same_commands_as_ci():
    assert _audit_runs(_job(_load(WEEKLY))) == _audit_runs(_job_by_name(_load(CI), "advisory-check"))


def test_weekly_uses_the_pinned_tool_and_the_check_script():
    text = _check_text(_load(WEEKLY))
    assert "pip-audit==2.10.1" in text
    assert "scripts/lock_advisory_check.py" in text
    assert "--pip-audit" in text


# --- it cannot skip the floor or the ignore-file rule ------------------------

def test_weekly_check_passes_no_flag_that_weakens_the_check():
    text = _check_text(_load(WEEKLY))
    for flag in ("--floor", "--ignore-file", "--lock"):
        assert flag not in text, f"{flag} would change what is checked"
    assert "|| true" not in text and "|| :" not in text


def test_weekly_steps_and_job_are_not_conditional_or_tolerant():
    job = _job(_load(WEEKLY))
    assert "if" not in job and "continue-on-error" not in job
    for step in job["steps"]:
        assert "if" not in step, step.get("name")
        assert "continue-on-error" not in step, step.get("name")


def test_ignore_file_the_scheduled_run_reads_is_still_empty():
    lines = [ln.strip() for ln in IGNORE_FILE.read_text(encoding="utf-8").splitlines()]
    assert [ln for ln in lines if ln and not ln.startswith("#")] == []


# --- CI behaviour on push and pull request is unchanged ---------------------

def test_ci_triggers_are_still_push_and_pull_request_only():
    on = _load(CI)["on"]
    assert set(on) == {"push", "pull_request"}
    assert on["push"]["branches"] == ["main", "dev", "release/**"]
    assert on["pull_request"]["branches"] == ["main"]


def test_the_ci_advisory_job_still_exists_and_is_not_gated():
    job = _job_by_name(_load(CI), "advisory-check")
    assert "needs" not in job and "if" not in job


# --- negative: the checks above really do fail on a broken workflow ---------

def _weekly_copy():
    return copy.deepcopy(_load(WEEKLY))


def _schedule_ok(doc):
    on = doc["on"]
    return "schedule" in on and len(on["schedule"]) == 1


def test_a_missing_schedule_is_detected():
    doc = _weekly_copy()
    del doc["on"]["schedule"]
    assert not _schedule_ok(doc)
    assert _schedule_ok(_weekly_copy())


def test_a_changed_command_is_detected():
    ci = _audit_runs(_job_by_name(_load(CI), "advisory-check"))
    doc = _weekly_copy()
    steps = _job(doc)["steps"]
    for s in steps:
        if "scripts/lock_advisory_check.py" in s.get("run", ""):
            s["run"] = s["run"] + " --floor 0"
    assert _audit_runs(_job(doc)) != ci
    assert "--floor" in _check_text(doc)


def test_a_changed_tool_pin_is_detected():
    ci = _audit_runs(_job_by_name(_load(CI), "advisory-check"))
    doc = _weekly_copy()
    for s in _job(doc)["steps"]:
        if "pip-audit==" in s.get("run", ""):
            s["run"] = s["run"].replace("pip-audit==2.10.1", "pip-audit")
    assert _audit_runs(_job(doc)) != ci
    assert "pip-audit==2.10.1" not in _check_text(doc)


def test_a_tolerant_step_is_detected():
    doc = _weekly_copy()
    _job(doc)["steps"][-1]["continue-on-error"] = True
    assert any("continue-on-error" in s for s in _job(doc)["steps"])
