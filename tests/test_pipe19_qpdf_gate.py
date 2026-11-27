"""PIPE-19: a qpdf-needing test fails (not skips) when PASK_REQUIRE_QPDF=1 and
qpdf is missing; without the variable it skips. Synthetic only."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import qpdf_helper  # noqa: E402


def test_missing_qpdf_fails_when_required(monkeypatch):                 # NEGATIVE
    monkeypatch.setattr(qpdf_helper, "working_qpdf", lambda: None)
    monkeypatch.setenv("PASK_REQUIRE_QPDF", "1")
    with pytest.raises(pytest.fail.Exception) as ei:
        qpdf_helper.qpdf_or_skip_or_fail()
    assert "PASK_REQUIRE_QPDF" in str(ei.value)


@pytest.mark.parametrize("value", [None, "", "0", "yes"])
def test_missing_qpdf_skips_when_not_required(monkeypatch, value):
    monkeypatch.setattr(qpdf_helper, "working_qpdf", lambda: None)
    if value is None:
        monkeypatch.delenv("PASK_REQUIRE_QPDF", raising=False)
    else:
        monkeypatch.setenv("PASK_REQUIRE_QPDF", value)
    with pytest.raises(pytest.skip.Exception):
        qpdf_helper.qpdf_or_skip_or_fail()


def test_a_working_qpdf_is_returned_in_both_modes(monkeypatch):
    monkeypatch.setattr(qpdf_helper, "working_qpdf", lambda: "/usr/bin/qpdf")
    for v in ("1", "0"):
        monkeypatch.setenv("PASK_REQUIRE_QPDF", v)
        assert qpdf_helper.qpdf_or_skip_or_fail() == "/usr/bin/qpdf"


def test_the_marker_runs_the_gate_fixture():
    assert qpdf_helper.needs_qpdf.args == ("require_qpdf",)


def test_the_workflow_installs_qpdf_and_sets_the_variable():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "apt-get install -y qpdf" in ci and "qpdf --version" in ci
    assert 'PASK_REQUIRE_QPDF: "1"' in ci
    assert "lfs: true" not in ci          # the LFS quota stays untouched
