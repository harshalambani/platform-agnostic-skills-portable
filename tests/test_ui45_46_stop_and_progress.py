"""tests/test_ui45_46_stop_and_progress.py -- UI-46 (a stale Stop never cancels a new direct-mode run)
and UI-45 (Book Spends: stops cleanly, per-statement progress, step line kept under the AI pacing).
Synthetic data only."""
from __future__ import annotations

import threading
import time

import pytest

from agents.skill_cc_spend_booking import agent as A
from tests.test_cc05_sbi_statements import BLOCK, LEGEND, _head, m
from ui import _runner
from ui._runner import CancelledError, request_cancel, reset_cancel, run_with_progress
from ui.tabs import _generic as G


@pytest.fixture(autouse=True)
def _clean_flag():
    reset_cancel()
    yield
    reset_cancel()


def _drive(gen):
    """Run a run_with_progress generator to the end; return its value."""
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


# ---- UI-46 -----------------------------------------------------------------------------------

def test_a_stop_from_an_earlier_run_does_not_cancel_a_new_run():              # NEGATIVE
    request_cancel()                                    # the user pressed Stop on the previous run
    seen = {}

    def work():
        seen["cancelled_at_start"] = _runner.is_cancelled()
        return "done"

    out = _drive(run_with_progress(work, lambda s: (s,), poll_interval=0.05))
    assert seen["cancelled_at_start"] is False
    assert out == "done" and out != "__CANCELLED__"


def test_a_stop_during_a_run_still_cancels():
    def work():
        for _ in range(100):
            if _runner.is_cancelled():
                raise CancelledError("stopped")
            time.sleep(0.02)
        return "finished"

    threading.Timer(0.1, request_cancel).start()
    out = _drive(run_with_progress(work, lambda s: (s,), poll_interval=0.05))
    assert out == "__CANCELLED__"


def test_a_real_error_is_still_raised_not_reported_as_cancelled():            # NEGATIVE
    def work():
        raise ValueError("boom")

    with pytest.raises(ValueError):
        _drive(run_with_progress(work, lambda s: (s,), poll_interval=0.05))


# ---- UI-45 (2): the per-statement line --------------------------------------------------------

def test_no_estimate_before_the_first_statement_is_done():                     # NEGATIVE
    line = A.statement_progress_line(1, 8, started=100.0, now=100.0)
    assert line == "Reading statement 1 of 8"
    assert "min left" not in line
    assert "min left" not in A.statement_progress_line(1, 8, started=100.0, now=900.0)


def test_the_estimate_uses_the_measured_average():
    # 2 done in 120 s -> 60 s each; 6 left -> 360 s -> 6 min
    assert A.statement_progress_line(3, 8, started=0.0, now=120.0) == \
        "Reading statement 3 of 8 - about 6 min left"


def test_an_out_of_range_index_is_clamped_not_invented():                    # NEGATIVE
    assert A.statement_progress_line(9, 8, started=0.0, now=700.0).startswith("Reading statement 8 of 8")
    assert A.statement_progress_line(0, 8, started=0.0, now=700.0) == "Reading statement 1 of 8"


# ---- UI-45 (1): Stop ends the run before the next statement and writes nothing ----------------

def _three_pdfs(tmp_path):
    d = tmp_path / "in" / "SBI-BPCL-Octane"
    d.mkdir(parents=True, exist_ok=True)
    for n in ("a", "b", "c"):
        (d / f"{n}.pdf").write_bytes(b"%PDF-1.4 synthetic " + n.encode())
    return tmp_path / "in"


def test_stop_during_extraction_ends_before_the_next_statement_and_writes_nothing(tmp_path, monkeypatch, capsys):
    lines = _head() + BLOCK + LEGEND
    reads = []

    def fake_read_lines(p):
        reads.append(p.name)
        request_cancel()                                # Stop pressed while the first statement is read
        return lines

    monkeypatch.setattr(m, "read_lines", fake_read_lines)
    monkeypatch.setattr("agents.skill_cc_transactions.agent._load_script", lambda: m)
    xlsx = tmp_path / "o.xlsx"
    with pytest.raises(CancelledError):
        A._extract(_three_pdfs(tmp_path), xlsx, None, None, "x")
    assert len(reads) == 1                              # the 2nd and 3rd statements were never read
    assert not xlsx.exists()                            # nothing written
    assert "Stopping..." in capsys.readouterr().out


def test_without_a_stop_every_statement_is_read_and_progress_is_logged(tmp_path, monkeypatch, capsys):  # NEGATIVE
    lines = _head() + BLOCK + LEGEND
    reads = []
    monkeypatch.setattr(m, "read_lines", lambda p: (reads.append(p.name), lines)[1])
    monkeypatch.setattr("agents.skill_cc_transactions.agent._load_script", lambda: m)
    A._extract(_three_pdfs(tmp_path), tmp_path / "o.xlsx", None, None, "x")
    out = capsys.readouterr().out
    assert len(reads) == 3
    assert "Stopping" not in out
    assert "Reading statement 1 of 3" in out and "Reading statement 3 of 3" in out


def test_stop_outside_the_ui_raises_run_stopped(monkeypatch):
    monkeypatch.setattr(A, "_stop_requested", lambda: True)
    import sys
    monkeypatch.setitem(sys.modules, "ui._runner", None)   # the UI is not importable here
    with pytest.raises(A.RunStopped):
        A._raise_if_stopped("x")


def test_no_stop_means_no_raise(monkeypatch):                                  # NEGATIVE
    monkeypatch.setattr(A, "_stop_requested", lambda: False)
    A._raise_if_stopped("x")


# ---- UI-45 (3): the pacing line never replaces the step line ----------------------------------

STEP = {"step": 5, "type": "pipeline", "snippet": "Mapping 12 spend row(s)"}
PACE = {"step": 5, "type": "pipeline", "snippet": "pacing: next AI call at 10:31:05"}


def _panel(events, stopping=False):
    latest = {"line": "", "pacing": ""}
    for ev in events:
        G._note_direct_event(latest, ev, _runner._format_event(ev, 3))
    return G._direct_panel_text(3, latest, stopping)


def test_pacing_line_sits_under_the_step_line():
    text = _panel([STEP, PACE])
    assert "Mapping 12 spend row(s)" in text and "next AI call" in text
    assert text.index("Mapping 12") < text.index("next AI call")


def test_pacing_line_never_replaces_the_step_line():                           # NEGATIVE
    latest = {"line": "", "pacing": ""}
    G._note_direct_event(latest, STEP, "STEP LINE")
    G._note_direct_event(latest, PACE, "PACING LINE")
    assert latest["line"] == "STEP LINE"
    G._note_direct_event(latest, PACE, "PACING LINE 2")
    assert latest["line"] == "STEP LINE" and latest["pacing"] == "PACING LINE 2"


def test_a_new_step_clears_the_stale_pacing_note():                            # NEGATIVE
    text = _panel([STEP, PACE, {"step": 6, "type": "pipeline", "snippet": "Writing the output"}])
    assert "Writing the output" in text and "next AI call" not in text


def test_panel_says_stopping_at_once_after_stop():
    assert "Stopping..." in _panel([STEP], stopping=True)
    assert "Stopping" not in _panel([STEP])                                    # NEGATIVE: not before Stop
