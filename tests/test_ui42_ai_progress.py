"""
tests/test_ui42_ai_progress.py -- UI-42: a direct-mode skill (Book Spends) shows "AI row n of N"
and a time-left estimate while the AI pass runs. Fake pacer clock, scripted LLM, synthetic rows.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src"), str(ROOT), str(ROOT / "tests")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import agents.skill_gnucash_account_mapper.agent as m  # noqa: E402
from agents import base_agent  # noqa: E402
from scripted_llm import ScriptedLLM  # noqa: E402
from ui import _runner  # noqa: E402

REAL_EMIT = m._emit_mapper_progress


def _close(h):
    """ScriptedLLM.close(), except that gradio's own telemetry thread (started by other tests in
    the same process) may touch api.gradio.app while the socket tripwire is installed."""
    h.http_attempts[:] = [a for a in h.http_attempts if "gradio" not in a]
    h.close()


def _rows(n):
    return [{"row": i, "description": f"SAMPLE PAYEE {i}", "deposit": "0", "withdrawal": "10.00"}
            for i in range(1, n + 1)]


def _pass(h, n, **kw):
    return m.llm_fallback_mapping(unmatched_rows=_rows(n), account_tree=[], example_mappings=[],
                                  config_path=h.config_path, historical_mappings=[], **kw)


@pytest.fixture
def h(monkeypatch, tmp_path):
    h = ScriptedLLM(monkeypatch, tmp_path, min_gap=60)
    monkeypatch.setattr(m, "_AI_CLOCK", h.clock.now)
    yield h
    _close(h)


def _row_lines(lines):
    return [s for s in lines if s.startswith("AI row ")]


# --- the mapper's progress lines ---------------------------------------------

def test_every_row_reports_n_of_total_with_a_time_left(h):
    _pass(h, 4)
    lines = _row_lines(h.progress)
    assert [s.split(":")[0] for s in lines] == [f"AI row {i} of 4" for i in range(1, 5)]
    assert all("min left" in s for s in lines)
    assert "about 4 min left" in lines[0]            # 4 rows x the 60 s pacing gap
    assert "about 1 min left" in lines[-1]           # never "0 min left"


def test_the_counter_never_goes_past_the_total(h):
    _pass(h, 3)
    for s in _row_lines(h.progress):
        n = int(s.split(" of ")[0].split()[-1])
        assert 1 <= n <= 3
    assert not [s for s in h.progress if "AI row 4" in s]
    assert m._ai_row_line(9, 3, "X", 0.0).startswith("AI row 3 of 3")   # clamped


def test_no_ai_rows_no_counter_and_no_eta(h):
    out = m.llm_fallback_mapping(unmatched_rows=[], account_tree=[], example_mappings=[],
                                 config_path=h.config_path, historical_mappings=[])
    assert out == {}
    assert not _row_lines(h.progress)
    assert not [s for s in h.progress if "min left" in s]


def test_early_stop_is_loud_does_not_claim_completion_and_rows_go_to_suspense(tmp_path, monkeypatch):
    h = ScriptedLLM(monkeypatch, tmp_path, min_gap=60, daily_cap=3)   # warm-up + 2 rows, then cap
    monkeypatch.setattr(m, "_AI_CLOCK", h.clock.now)
    try:
        _pass(h, 6)
        lines = _row_lines(h.progress)
        assert 1 <= len(lines) < 6                                    # did not reach 6 of 6
        assert not [s for s in lines if s.startswith("AI row 6 of 6")]
        stop = [s for s in h.progress if s.startswith("AI pass stopped at row")]
        assert stop and "go to Suspense" in stop[0]
        assert any("WARNING: AI pass stopped" in s for s in h.progress)
        st = m._LLM_RUN_STATUS
        assert st["stopped"] is True and st["unattempted_rows"]       # unreached rows -> Suspense
        assert 6 in st["unattempted_rows"]
    finally:
        _close(h)


def test_the_api_key_never_appears_in_a_stop_line(tmp_path, monkeypatch):
    h = ScriptedLLM(monkeypatch, tmp_path, min_gap=60)
    secret = "sk-TEST-SECRET-123"
    real_resolve = m._resolve_llm_endpoint_config
    monkeypatch.setattr(m, "_resolve_llm_endpoint_config",
                        lambda *a, **k: (*real_resolve(*a, **k)[:3], secret, 0.0))
    orig_ask = m._LLMGuard.ask

    def ask(self, *a, **k):
        r = orig_ask(self, *a, **k)
        if len(h.calls) >= 2 and not self.stopped:
            self.stopped, self.reason = True, f"the provider rejected key {secret}"
        return r
    monkeypatch.setattr(m._LLMGuard, "ask", ask)
    try:
        _pass(h, 4)
        stop = [s for s in h.progress if s.startswith("AI pass stopped at row")]
        assert stop and secret not in stop[0] and "***" in stop[0]
        assert not [s for s in _row_lines(h.progress) if secret in s]
    finally:
        _close(h)


# --- the runner: direct-mode work now feeds the queue the UI drains ----------

def test_direct_runner_delivers_row_1_to_N_and_an_eta(monkeypatch, tmp_path):
    h = ScriptedLLM(monkeypatch, tmp_path, min_gap=60)
    monkeypatch.setattr(m, "_AI_CLOCK", h.clock.now)
    monkeypatch.setattr(m, "_emit_mapper_progress", REAL_EMIT)    # the real queue path
    got = []
    try:
        gen = _runner.run_with_progress(lambda: _pass(h, 5), lambda e: ("tick", e),
                                        poll_interval=0.01,
                                        on_event=lambda ev, el: got.append(_runner._format_event(ev, el)))
        try:
            while True:
                next(gen)
        except StopIteration:
            pass
        rows = [s for s in got if "AI row " in s]
        assert [s.split("AI row ")[1].split(":")[0] for s in rows] == [f"{i} of 5" for i in range(1, 6)]
        assert any("min left" in s for s in rows)
    finally:
        _close(h)


def test_without_on_event_no_queue_is_installed():
    seen = {}

    def work():
        seen["q"] = base_agent.get_progress_queue()
        return 1
    gen = _runner.run_with_progress(work, lambda e: e, poll_interval=0.01)
    try:
        while True:
            next(gen)
    except StopIteration:
        pass
    assert seen["q"] is None


def test_the_queue_is_removed_from_the_worker_when_the_run_ends():
    seen = {}

    def work():
        seen["q"] = base_agent.get_progress_queue()
    gen = _runner.run_with_progress(work, lambda e: e, poll_interval=0.01,
                                    on_event=lambda ev, el: None)
    try:
        while True:
            next(gen)
    except StopIteration:
        pass
    assert seen["q"] is not None and base_agent.get_progress_queue() is None


def test_a_failing_display_callback_does_not_stop_the_run():
    def work():
        base_agent.get_progress_queue().put({"step": 5, "type": "pipeline", "snippet": "mapper: x"})
        return "done"

    def boom(ev, el):
        raise RuntimeError("display problem")
    gen = _runner.run_with_progress(work, lambda e: e, poll_interval=0.01, on_event=boom)
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        assert stop.value == "done"


# --- the generic tab wiring: every direct-mode skill gets it, logs stay redacted ----

def test_generic_tab_feeds_direct_events_to_the_run_log_and_redacts():
    src = (ROOT / "ui" / "tabs" / "_generic.py").read_text(encoding="utf-8")
    assert "on_event=on_event" in src
    assert src.count("run_log_lines=log + direct_events") == 3
    assert "_runlog.redact(_runner._format_event(event, elapsed), secrets)" in src


def test_book_spends_is_direct_mode_and_that_is_why_it_was_silent():
    import yaml
    for name in ("skill_cc_spend_booking",):
        cfg = yaml.safe_load((ROOT / "src" / "agents" / name / "skill.yaml").read_text(encoding="utf-8"))
        assert cfg.get("mode") == "direct"
