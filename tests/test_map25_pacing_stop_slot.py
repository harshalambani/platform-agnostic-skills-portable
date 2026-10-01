"""
MAP-25 -- a stopped run's AI wait must not carry into the next run.

The pacer claims the next slot when a call reserves it. Stopping while that call
waited handed back the daily count but not the slot, and lowering the gap never
shortened an already pending slot, so the next run waited out the old gap
(a 60s gap Stop left a ~51s first wait at a 2s gap).

Fixed: Stop gives the slot back; a lowered gap caps a pending slot at the last
real call's start plus the new gap. Persisting across runs stays (provider
limits are real). Fake clock and sleep: nothing really sleeps.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import llm_pacing as lp  # noqa: E402

T0 = 1_800_000_000.0   # a fixed epoch; the same calendar day throughout


class Clock:
    def __init__(self, t=T0):
        self.t = t
        self.sleeps = []
        self.stop_after = None      # stop once this many seconds were slept
        self.stopped = False

    def now(self):
        return self.t

    def sleep(self, s):
        self.sleeps.append(s)
        self.t += s
        if self.stop_after is not None and sum(self.sleeps) >= self.stop_after:
            self.stopped = True


def make(tmp_path, clock, gap, cap=0):
    return lp.Pacer("ollama|http://x.invalid", gap, cap,
                    state_path=tmp_path / "state.json", clock=clock.now,
                    sleep=clock.sleep, cancel_check=lambda: clock.stopped,
                    emit=lambda m: None)


def first_wait(p, clock):
    before = clock.t
    p.acquire()
    return clock.t - before


def test_a_stopped_wait_does_not_delay_the_next_runs_first_call(tmp_path):
    """NEGATIVE: Stop mid-wait; a fresh process's first call starts no later
    than the stopped call's slot (it used to start a whole gap after it)."""
    c = Clock()
    p = make(tmp_path, c, 60)
    p.acquire()                       # call 1: starts now, next slot T0+60
    c.stop_after = 10
    with pytest.raises(lp.PacingCancelled):
        p.acquire()                   # call 2 waits for T0+60, Stopped after ~10s
    stopped_at = c.t
    assert stopped_at < T0 + 60

    c2 = Clock(stopped_at)
    p2 = make(tmp_path, c2, 60)       # next run, fresh process, same state file
    wait = first_wait(p2, c2)
    assert wait <= (T0 + 60) - stopped_at + 1e-6          # at most the stopped slot
    assert c2.t < T0 + 60 + 60                           # never the second gap on top


def test_stop_rolls_back_the_slot_in_the_same_process(tmp_path):
    c = Clock()
    p = make(tmp_path, c, 60)
    p.acquire()
    c.stop_after = 5
    with pytest.raises(lp.PacingCancelled):
        p.acquire()
    assert p._next_slot == T0 + 60            # the cancelled call's slot, not +120
    c.stopped, c.stop_after = False, None
    assert first_wait(p, c) <= 60 - 5 + 1e-6


def test_stop_does_not_free_a_slot_someone_else_reserved_after(tmp_path):
    """NEGATIVE: if another call reserved behind the cancelled one, the slot
    chain is left alone (no call may start before the old slot)."""
    c = Clock()
    p = make(tmp_path, c, 60)
    p.acquire()
    state = {"n": 0}

    def cancel():
        state["n"] += 1
        if state["n"] == 2:               # during call 2's wait: call 3 reserves
            with p._lock:
                state["s3"] = p._reserve(c.now())
            return True
        return False

    p._cancel = cancel
    with pytest.raises(lp.PacingCancelled):
        p.acquire()
    assert state["s3"] == T0 + 120
    assert p._next_slot == T0 + 180       # untouched: 3's claim stands


def test_lowering_the_gap_never_leaves_the_old_long_first_wait(tmp_path):
    """NEGATIVE: the real case. A 60s gap run leaves a pending slot; the next
    run is at a 2s gap and must not wait ~60s."""
    c = Clock()
    p = make(tmp_path, c, 60)
    p.acquire()                       # last call starts at T0, pending slot T0+60
    c2 = Clock(T0 + 1)
    p2 = make(tmp_path, c2, 2)        # fresh process, lowered gap, same state
    wait = first_wait(p2, c2)
    assert wait <= 1 + 1e-6           # last start + 2s = T0+2, we are at T0+1
    assert wait < 59


def test_lowering_the_gap_in_the_same_process(tmp_path):
    c = Clock()
    p = make(tmp_path, c, 60)
    p.acquire()
    p.update(2, 0)
    assert p._next_slot == T0 + 2
    assert first_wait(p, c) <= 2 + 1e-6


def test_raising_the_gap_never_lets_a_call_start_before_the_old_slot(tmp_path):
    """NEGATIVE: raising the gap leaves the pending slot alone."""
    c = Clock()
    p = make(tmp_path, c, 10)
    p.acquire()                       # pending slot T0+10
    p.update(60, 0)
    assert p._next_slot >= T0 + 10
    assert first_wait(p, c) >= 10 - 1e-6
    # and across a fresh process with a larger gap
    c2 = Clock(T0)
    q = make(tmp_path / "other", c2, 10)
    q.acquire()
    c3 = Clock(T0)
    q2 = make(tmp_path / "other", c3, 60)
    assert first_wait(q2, c3) >= 10 - 1e-6


def test_a_fresh_process_still_honours_a_genuinely_recent_call(tmp_path):
    """NEGATIVE of over-fixing: persistence across runs stays, same gap."""
    c = Clock()
    p = make(tmp_path, c, 30)
    p.acquire()
    c2 = Clock(T0 + 5)
    p2 = make(tmp_path, c2, 30)
    assert first_wait(p2, c2) == pytest.approx(25)


def test_a_completed_wait_is_not_rolled_back(tmp_path):
    """NEGATIVE: a call that really ran keeps its slot for the next call."""
    c = Clock()
    p = make(tmp_path, c, 20)
    p.acquire()
    p.acquire()                       # waits 20s, then runs
    assert c.t == pytest.approx(T0 + 20)
    assert first_wait(p, c) == pytest.approx(20)


def test_daily_count_and_cap_are_unchanged(tmp_path):
    c = Clock()
    p = make(tmp_path, c, 1, cap=3)
    for _ in range(3):
        p.acquire()
    assert p.calls_today() == 3
    with pytest.raises(lp.DailyCapReached):
        p.acquire()
    assert p.calls_today() == 3

    # a Stopped wait still hands its count back
    c2 = Clock()
    p2 = make(tmp_path / "b", c2, 60, cap=10)
    p2.acquire()
    c2.stop_after = 3
    with pytest.raises(lp.PacingCancelled):
        p2.acquire()
    assert p2.calls_today() == 1
