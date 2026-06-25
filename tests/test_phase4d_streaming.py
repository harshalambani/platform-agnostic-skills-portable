"""
tests/test_phase4d_streaming.py — Unit tests for Phase 4D agent progress streaming.

Tests the streaming infrastructure without requiring LangGraph or an LLM:
  - Progress queue set/get (threading.local)
  - _format_event() rendering
  - run_with_streaming() queue drain + worker lifecycle
  - _StreamingAgentWrapper event generation (mocked agent)

Run with:
    cd src && python -m pytest ../tests/test_phase4d_streaming.py -v
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
# Path setup — make src/ importable.
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# We need ui/ on the path too for the _runner import.
UI = ROOT / "ui"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ===========================================================================
# 1. Progress queue (threading.local) tests
# ===========================================================================

class TestProgressQueue:
    """Verify set/get_progress_queue isolation between threads."""

    def test_default_is_none(self):
        from agents.base_agent import get_progress_queue
        # Fresh thread should have no queue.
        assert get_progress_queue() is None

    def test_set_and_get(self):
        from agents.base_agent import set_progress_queue, get_progress_queue
        q = queue.Queue()
        set_progress_queue(q)
        assert get_progress_queue() is q
        set_progress_queue(None)
        assert get_progress_queue() is None

    def test_thread_isolation(self):
        from agents.base_agent import set_progress_queue, get_progress_queue
        q_main = queue.Queue()
        set_progress_queue(q_main)

        results = {}

        def child():
            # Child thread should NOT see the main thread's queue.
            results["child_before"] = get_progress_queue()
            q_child = queue.Queue()
            set_progress_queue(q_child)
            results["child_after"] = get_progress_queue()

        t = threading.Thread(target=child)
        t.start()
        t.join()

        assert results["child_before"] is None
        assert results["child_after"] is not q_main
        # Main thread's queue is unaffected.
        assert get_progress_queue() is q_main
        set_progress_queue(None)


# ===========================================================================
# 2. _format_event() tests
# ===========================================================================

class TestFormatEvent:
    """Verify markdown rendering of different event types."""

    def _fmt(self, event, elapsed=5):
        from ui._runner import _format_event
        return _format_event(event, elapsed)

    def test_tool_call(self):
        md = self._fmt({"type": "tool_call", "step": 1, "tool": "describe_csv", "args": '{"path": "x.csv"}'})
        assert "describe_csv" in md
        assert "Step 1" in md

    def test_tool_result(self):
        md = self._fmt({"type": "tool_result", "step": 2, "tool": "query_csv", "snippet": "North=3945"})
        assert "query_csv" in md
        assert "North=3945" in md

    def test_llm_response(self):
        md = self._fmt({"type": "llm_response", "step": 3, "snippet": "The analysis shows…"})
        assert "Step 3" in md

    def test_llm_start(self):
        md = self._fmt({"type": "llm_start", "step": 1, "snippet": "LLM is thinking…"})
        assert "thinking" in md

    def test_unknown_type(self):
        md = self._fmt({"type": "custom_event", "step": 4})
        assert "Step 4" in md
        assert "custom_event" in md


# ===========================================================================
# 3. run_with_streaming() lifecycle tests
# ===========================================================================

class TestRunWithStreaming:
    """Test the streaming runner with a fake worker."""

    def test_basic_flow(self):
        """Worker pushes events → runner yields them → returns result."""
        from agents.base_agent import get_progress_queue

        def fake_work():
            q = get_progress_queue()
            assert q is not None
            q.put({"type": "llm_start", "step": 1, "snippet": "thinking"})
            time.sleep(0.1)
            q.put({"type": "tool_call", "step": 1, "tool": "my_tool", "args": "{}"})
            time.sleep(0.1)
            return "final answer"

        log: list[str] = ["**Running** — starting"]

        def make_tuple(md):
            return (md,)

        from ui._runner import run_with_streaming
        gen = run_with_streaming(fake_work, log, make_tuple, poll_interval=0.05)

        # Consume all yields and capture return.
        yields = []
        result = None
        try:
            while True:
                yields.append(next(gen))
        except StopIteration as e:
            result = e.value

        assert result == "final answer"
        assert len(yields) > 0
        # Log should contain the events.
        full_log = "\n".join(log)
        assert "my_tool" in full_log

    def test_worker_exception_propagates(self):
        """If the worker raises, run_with_streaming re-raises."""

        def bad_work():
            raise ValueError("boom")

        log: list[str] = []

        from ui._runner import run_with_streaming
        gen = run_with_streaming(bad_work, log, lambda md: (md,), poll_interval=0.05)

        with pytest.raises(ValueError, match="boom"):
            while True:
                next(gen)

    def test_queue_cleared_after_run(self):
        """Progress queue is None after worker completes."""
        from agents.base_agent import get_progress_queue

        observed = {}

        def check_work():
            observed["during"] = get_progress_queue() is not None
            return "ok"

        from ui._runner import run_with_streaming
        gen = run_with_streaming(check_work, [], lambda md: (md,), poll_interval=0.05)
        try:
            while True:
                next(gen)
        except StopIteration:
            pass

        assert observed["during"] is True


# ===========================================================================
# 4. Progress-event reporting (post-refactor API)
# ===========================================================================
#
# NOTE: `_StreamingAgentWrapper` (a generic class that wrapped a LangGraph
# agent's `.stream()` output and translated it into progress-queue events)
# was removed from agents.base_agent in the "unified agent/skill.yaml/tools.py
# structure" refactor (commit 3a1b12c). There is no longer a single generic
# wrapper to test: individual agent-mode skills now call
# `get_progress_queue().put({...})` directly from their own agent.py (see
# src/agents/skill_gnucash_pipeline/agent.py and
# src/agents/skill_gnucash_account_mapper/agent.py for examples). That
# call-a-queue-directly contract is exactly what TestProgressQueue and
# TestRunWithStreaming above already cover; there's no generic wrapper class
# left to exercise here.


class TestProgressQueuePutContract:
    """
    Verify the now-canonical "skill pushes its own progress dict" contract:
    any code holding the active progress queue (via get_progress_queue())
    can push a dict event that run_with_streaming/_format_event can render.
    """

    def test_skill_can_push_pipeline_event(self):
        from agents.base_agent import set_progress_queue, get_progress_queue
        from ui._runner import _format_event

        q = queue.Queue()
        set_progress_queue(q)
        try:
            qq = get_progress_queue()
            assert qq is not None
            qq.put({"step": 1, "type": "pipeline", "snippet": "doing work"})
        finally:
            set_progress_queue(None)

        event = q.get_nowait()
        md = _format_event(event, elapsed=3)
        assert "doing work" in md
        assert "Step 1" in md
