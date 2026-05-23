"""
ui/_runner.py — shared helper for skill tabs.

Runs a blocking callable in a background thread and yields periodic
status-tick tuples so the Gradio UI can show elapsed seconds while the
agent loop is in flight.

`run_with_progress` is itself a generator. The outer skill-tab generator
should consume it with `agent_reply = yield from runner_gen` — that
pattern both forwards the tick yields to Gradio AND captures the
worker's return value (since `yield from` surfaces StopIteration.value).
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable


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
