"""
tests/test_runner.py — Unit tests for ui/_runner.py (Phase 6, task 6-2).

Tests cover:
  - run_with_progress() — fast completion, elapsed ticks, exception propagation
  - _format_event() — all event type branches
  - run_with_streaming() — queue drain, sentinel, final drain, "still working" line

Run with:
    cd src && python -m pytest ../tests/test_runner.py -v
"""
from __future__ import annotations

import queue
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ui._runner import run_with_progress, run_with_streaming, _format_event


# ---------------------------------------------------------------------------
# run_with_progress()
# ---------------------------------------------------------------------------

class TestRunWithProgress:
    def test_fast_work_returns_value(self):
        """Work that finishes before the first tick yields nothing."""
        gen = run_with_progress(
            work=lambda: 42,
            tick_factory=lambda e: (f"tick {e}",),
            poll_interval=0.05,
        )
        ticks = list(gen)
        # Fast work may yield 0 ticks; the return value is via StopIteration
        # but list() discards it. We just confirm no crash.
        assert isinstance(ticks, list)

    def test_return_value_surfaced(self):
        """yield-from surfaces the worker's return value."""
        def _consumer():
            return (yield from run_with_progress(
                work=lambda: "hello",
                tick_factory=lambda e: (f"tick {e}",),
                poll_interval=0.01,
            ))

        gen = _consumer()
        # Exhaust the generator and capture the return value.
        result = None
        try:
            while True:
                next(gen)
        except StopIteration as e:
            result = e.value
        assert result == "hello"

    def test_ticks_emitted_during_slow_work(self):
        """A slow worker should produce at least one tick."""
        def slow():
            time.sleep(0.15)
            return "done"

        ticks = list(run_with_progress(
            work=slow,
            tick_factory=lambda e: (f"elapsed={e}",),
            poll_interval=0.04,
        ))
        assert len(ticks) >= 1

    def test_worker_exception_propagates(self):
        """Exceptions raised inside work() must propagate to the caller."""
        def boom():
            raise ValueError("kaboom")

        with pytest.raises(ValueError, match="kaboom"):
            list(run_with_progress(
                work=boom,
                tick_factory=lambda e: ("tick",),
                poll_interval=0.01,
            ))

    def test_tick_factory_receives_elapsed_seconds(self):
        """tick_factory should receive an integer elapsed time."""
        received = []

        def slow():
            time.sleep(0.12)
            return True

        list(run_with_progress(
            work=slow,
            tick_factory=lambda e: (received.append(e), None),
            poll_interval=0.03,
        ))
        assert len(received) >= 1
        assert all(isinstance(e, int) for e in received)


# ---------------------------------------------------------------------------
# _format_event()
# ---------------------------------------------------------------------------

class TestFormatEvent:
    def test_tool_call(self):
        event = {"step": 1, "type": "tool_call", "tool": "search", "args": "q=hello"}
        out = _format_event(event, elapsed=5)
        assert "Step 1" in out
        assert "search" in out
        assert "q=hello" in out

    def test_tool_result(self):
        event = {"step": 2, "type": "tool_result", "tool": "search", "snippet": "found 3"}
        out = _format_event(event, elapsed=10)
        assert "Step 2" in out
        assert "search" in out
        assert "found 3" in out

    def test_llm_response_with_snippet(self):
        event = {"step": 3, "type": "llm_response", "snippet": "Here is..."}
        out = _format_event(event, elapsed=7)
        assert "composing" in out.lower() or "Step 3" in out

    def test_llm_response_without_snippet(self):
        event = {"step": 3, "type": "llm_response", "snippet": ""}
        out = _format_event(event, elapsed=7)
        assert "responded" in out.lower()

    def test_llm_start(self):
        event = {"step": 4, "type": "llm_start"}
        out = _format_event(event, elapsed=2)
        assert "thinking" in out.lower()

    def test_unknown_type(self):
        event = {"step": 5, "type": "custom_event"}
        out = _format_event(event, elapsed=3)
        assert "custom_event" in out
        assert "3s" in out

    def test_missing_step_defaults_to_question_mark(self):
        event = {"type": "tool_call", "tool": "x", "args": ""}
        out = _format_event(event, elapsed=0)
        assert "?" in out


# ---------------------------------------------------------------------------
# run_with_streaming()
# ---------------------------------------------------------------------------

class TestRunWithStreaming:
    def _mock_set_progress_queue(self):
        """Return a patched set_progress_queue that captures the queue."""
        captured = {}

        def fake_set(q):
            captured["queue"] = q

        return fake_set, captured

    def test_fast_work_returns_value(self):
        """Worker that finishes immediately should still return its value."""
        fake_set, _ = self._mock_set_progress_queue()

        with patch("agents.base_agent.set_progress_queue", fake_set):
            def _consumer():
                return (yield from run_with_streaming(
                    work=lambda: "result",
                    log=[],
                    make_tuple=lambda md: (md,),
                    poll_interval=0.01,
                ))

            gen = _consumer()
            result = None
            try:
                while True:
                    next(gen)
            except StopIteration as e:
                result = e.value
            assert result == "result"

    def test_events_appear_in_log(self):
        """Events pushed to the progress queue should appear in the log."""
        fake_set, captured = self._mock_set_progress_queue()

        def work_with_events():
            q = captured.get("queue")
            if q:
                q.put({"step": 1, "type": "tool_call", "tool": "t", "args": ""})
                q.put({"step": 2, "type": "llm_response", "snippet": "done"})
            time.sleep(0.05)
            return "ok"

        log: list[str] = []
        with patch("agents.base_agent.set_progress_queue", fake_set):
            list(run_with_streaming(
                work=work_with_events,
                log=log,
                make_tuple=lambda md: (md,),
                poll_interval=0.02,
            ))
        # Log should have the two events (plus possibly "still working" lines)
        event_lines = [l for l in log if "Step" in l]
        assert len(event_lines) >= 2

    def test_worker_exception_propagates(self):
        """Exceptions in the worker should propagate."""
        fake_set, _ = self._mock_set_progress_queue()

        def boom():
            raise RuntimeError("stream-boom")

        with patch("agents.base_agent.set_progress_queue", fake_set):
            with pytest.raises(RuntimeError, match="stream-boom"):
                list(run_with_streaming(
                    work=boom,
                    log=[],
                    make_tuple=lambda md: (md,),
                    poll_interval=0.01,
                ))

    def test_still_working_line_appears(self):
        """If no events come, a 'still working' line should appear."""
        fake_set, _ = self._mock_set_progress_queue()

        def slow():
            time.sleep(0.15)
            return "done"

        log: list[str] = []
        with patch("agents.base_agent.set_progress_queue", fake_set):
            list(run_with_streaming(
                work=slow,
                log=log,
                make_tuple=lambda md: (md,),
                poll_interval=0.03,
            ))
        working_lines = [l for l in log if "Running" in l or "working" in l]
        assert len(working_lines) >= 1
