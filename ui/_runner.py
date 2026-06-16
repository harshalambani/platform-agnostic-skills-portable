"""
ui/_runner.py — shared helpers for skill tabs.

Two runners:

    run_with_progress()    — elapsed-time ticks only (direct-mode skills).
    run_with_streaming()   — real-time agent progress events via a shared
                             queue (agent-mode skills, Phase 4D / C4).

Both are generators consumed with ``yield from`` — the pattern forwards
yields to Gradio AND surfaces the worker's return value.

Cancellation:

    cancel_event     — threading.Event, set by the Stop button.
    is_cancelled()   — check from any thread (workers, mappers, LLM calls).
    request_cancel() — called by the Gradio Stop button handler.
    reset_cancel()   — cleared at the start of each run.
"""
from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Cancellation support
# ---------------------------------------------------------------------------

_cancel_event = threading.Event()


def is_cancelled() -> bool:
    """Check if the current run has been cancelled (thread-safe)."""
    return _cancel_event.is_set()


def request_cancel() -> None:
    """Signal cancellation (called by the Stop button)."""
    _cancel_event.set()


def reset_cancel() -> None:
    """Clear the cancellation flag (called at start of each run)."""
    _cancel_event.clear()


class CancelledError(Exception):
    """Raised when a run is cancelled by the user."""
    pass


# ---------------------------------------------------------------------------
# Original elapsed-tick runner (unchanged, used for direct-mode skills).
# ---------------------------------------------------------------------------

def run_with_progress(
    work: Callable[[], Any],
    tick_factory: Callable[[int], tuple],
    *,
    poll_interval: float = 2.0,
):
    """
    Background-thread + poll runner. Yields whatever `tick_factory(elapsed)`
    produces every `poll_interval` seconds while `work()` is in flight.

    Returns the worker's value (surfaced via StopIteration.value when
    used with `yield from`).

    Args:
        work:         zero-arg callable that does the slow blocking work.
        tick_factory: f(elapsed_seconds) -> tuple to yield to Gradio.
                      Typical use: builds the (markdown, download_update) tuple
                      with elapsed time interpolated.
        poll_interval: seconds between ticks.
    """
    result: dict[str, Any] = {}

    def _runner() -> None:
        try:
            result["value"] = work()
        except BaseException as e:  # noqa: BLE001
            result["error"] = e

    t = threading.Thread(target=_runner, daemon=True)
    start = time.monotonic()
    t.start()

    while True:
        t.join(timeout=poll_interval)
        if not t.is_alive():
            break
        elapsed = int(time.monotonic() - start)
        yield tick_factory(elapsed)

    if "error" in result:
        raise result["error"]
    return result.get("value")


# ---------------------------------------------------------------------------
# Streaming runner (Phase 4D / C4) — agent-mode skills.
# ---------------------------------------------------------------------------

def _format_event(event: dict, elapsed: int) -> str:
    """
    Turn a progress-queue event dict into a single Markdown line for the
    Gradio result area.
    """
    step = event.get("step", "?")
    etype = event.get("type", "")

    if etype == "tool_call":
        tool = event.get("tool", "?")
        args = event.get("args", "")
        return (
            f"**Step {step}** — Calling tool: `{tool}`\n"
            f"> {args}"
        )
    elif etype == "tool_result":
        tool = event.get("tool", "?")
        snippet = event.get("snippet", "")
        return f"**Step {step}** — `{tool}` returned:\n> {snippet}"
    elif etype == "llm_response":
        snippet = event.get("snippet", "")
        if snippet:
            return f"**Step {step}** — LLM composing answer…"
        return f"**Step {step}** — LLM responded."
    elif etype == "llm_start":
        return f"**Step {step}** — LLM is thinking…"
    elif etype == "pipeline":
        snippet = event.get("snippet", "")
        return f"**Step {step}** — {snippet}" if snippet else f"**Step {step}** — pipeline ({elapsed}s)"
    else:
        return f"**Step {step}** — {etype} ({elapsed}s)"


def run_with_streaming(
    work: Callable[[], Any],
    log: list[str],
    make_tuple: Callable[[str], tuple],
    *,
    poll_interval: float = 0.5,
):
    """
    Like run_with_progress, but also drains a progress queue populated
    by the _StreamingAgentWrapper in base_agent.py.

    Before calling ``work()``, sets a progress queue on the current
    worker thread via ``base_agent.set_progress_queue()``.  The main
    thread polls the queue and yields formatted markdown.

    Args:
        work:       zero-arg callable (runs in background thread).
        log:        mutable list of markdown lines (shared with caller so
                    it can prepend setup lines before calling us).
        make_tuple: f(full_markdown) -> Gradio output tuple.
        poll_interval: seconds between polls (default 0.5 for snappier UI).

    Returns:
        The worker's return value.
    """
    from agents.base_agent import set_progress_queue

    reset_cancel()
    progress_q: queue.Queue = queue.Queue()
    result: dict[str, Any] = {}

    def _worker() -> None:
        # Install the queue so build_agent() / run_direct() can find it.
        set_progress_queue(progress_q)
        try:
            result["value"] = work()
        except CancelledError:
            result["cancelled"] = True
        except BaseException as e:  # noqa: BLE001
            result["error"] = e
        finally:
            set_progress_queue(None)
            # Sentinel so the main loop knows the worker is done even if
            # the queue still has items.
            progress_q.put(None)

    t = threading.Thread(target=_worker, daemon=True)
    start = time.monotonic()
    t.start()

    # Track last yielded state to avoid duplicate yields.
    last_len = len(log)

    while True:
        # Drain all available events.
        while True:
            try:
                event = progress_q.get_nowait()
            except queue.Empty:
                break
            if event is None:
                # Sentinel — worker is done.
                break
            elapsed = int(time.monotonic() - start)
            log.append(_format_event(event, elapsed))

        # Yield if log changed.
        if len(log) != last_len:
            last_len = len(log)
            yield make_tuple("\n\n".join(log))

        # Check if worker finished.
        if not t.is_alive():
            # Final drain.
            while True:
                try:
                    event = progress_q.get_nowait()
                except queue.Empty:
                    break
                if event is None:
                    continue
                elapsed = int(time.monotonic() - start)
                log.append(_format_event(event, elapsed))
            if len(log) != last_len:
                yield make_tuple("\n\n".join(log))
            break

        # If no events came, yield an elapsed-time tick so the UI
        # doesn't appear frozen.
        if len(log) == last_len:
            elapsed = int(time.monotonic() - start)
            # Update the last "still working" line in-place.
            working_line = f"**Running** — still working ({elapsed}s elapsed)"
            if log and log[-1].startswith("**Running** —"):
                log[-1] = working_line
            else:
                log.append(working_line)
            last_len = len(log)
            yield make_tuple("\n\n".join(log))

        time.sleep(poll_interval)

    if result.get("cancelled"):
        return "__CANCELLED__"
    if "error" in result:
        raise result["error"]
    return result.get("value")
