"""MAP-20: every AI call is paced to a user-set rate by ONE shared pacer.

All state goes under tmp_path; fake clock so the suite stays fast.
"""
import sys
import threading
import time
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

from agents import llm_pacing as lp  # noqa: E402
import agents.skill_gnucash_account_mapper.agent as m  # noqa: E402


class FakeClock:
    def __init__(self):
        self.t = 1_800_000_000.0
        self.lock = threading.Lock()

    def now(self):
        with self.lock:
            return self.t

    def sleep(self, s):
        with self.lock:
            self.t += s


def make(tmp_path, gap=0.0, cap=0, clock=None, cancel=lambda: False, emit=None):
    clock = clock or FakeClock()
    msgs = [] if emit is None else emit
    p = lp.Pacer("ollama|http://x", gap, cap, state_path=tmp_path / "st.json",
                 clock=clock.now, sleep=clock.sleep, cancel_check=cancel,
                 emit=msgs.append)
    return p, clock, msgs


@pytest.fixture(autouse=True)
def _clean(tmp_path, monkeypatch):
    lp.reset_registry()
    monkeypatch.setattr(lp, "default_state_path", lambda: tmp_path / "default_state.json")
    monkeypatch.setattr(lp, "_default_cancel", lambda: False)
    yield
    lp.reset_registry()


# -- pacing off: nothing waits, nothing is written ---------------------------

def test_pacing_off_never_waits_or_writes(tmp_path):
    p, clock, msgs = make(tmp_path)
    t0 = time.perf_counter()
    for _ in range(200):
        p.acquire()
    assert time.perf_counter() - t0 < 0.5
    assert clock.now() == 1_800_000_000.0          # fake clock never slept
    assert not (tmp_path / "st.json").exists()      # negative: no state file
    assert msgs == []


def test_pacing_off_is_the_default_config():
    p = lp.configure_from_legacy({"provider": "ollama", "ollama": {"base_url": "http://h"}})
    assert p.enabled is False
    assert lp.langchain_rate_limiter(p) is None     # load_model builds as before


# -- the gap is enforced ------------------------------------------------------

def test_min_gap_spaces_calls(tmp_path):
    p, clock, _ = make(tmp_path, gap=60)
    starts = []
    for _ in range(4):
        p.acquire()
        starts.append(clock.now())
    diffs = [b - a for a, b in zip(starts, starts[1:])]
    assert all(d >= 60 - 1e-6 for d in diffs), diffs
    assert all(d < 61 for d in diffs)               # and not padded beyond the gap


def test_two_concurrent_callers_never_exceed_the_rate(tmp_path):
    clock = FakeClock()
    p, _, _ = make(tmp_path, gap=10, clock=clock)
    starts, lk = [], threading.Lock()

    def worker():
        for _ in range(5):
            p.acquire()
            with lk:
                starts.append(clock.now())

    ts = [threading.Thread(target=worker) for _ in range(2)]
    [t.start() for t in ts]
    [t.join(timeout=20) for t in ts]
    assert len(starts) == 10
    # Every call was released no earlier than its reserved slot: slots are 10s apart.
    # Threads share the fake clock, so time only moved by waiting; total elapsed
    # must cover 9 gaps (never faster than 1 call / 10 s).
    assert clock.now() - 1_800_000_000.0 >= 9 * 10 - 1e-6


# -- daily cap ----------------------------------------------------------------

def test_daily_cap_stops_with_message_and_does_not_hang(tmp_path):
    p, clock, _ = make(tmp_path, cap=3)
    for _ in range(3):
        p.acquire()
    t0 = clock.now()
    with pytest.raises(lp.DailyCapReached) as ei:
        p.acquire()
    assert "Daily AI call cap reached" in str(ei.value)
    assert "3 of 3" in str(ei.value)
    assert clock.now() == t0                         # negative: it did not sleep/hang


def test_daily_cap_survives_restart_and_resets_next_day(tmp_path):
    p, clock, _ = make(tmp_path, cap=2)
    p.acquire(); p.acquire()
    p2 = lp.Pacer("ollama|http://x", 0, 2, state_path=tmp_path / "st.json",
                  clock=clock.now, sleep=clock.sleep, cancel_check=lambda: False,
                  emit=lambda s: None)
    with pytest.raises(lp.DailyCapReached):
        p2.acquire()
    clock.t += 2 * 86400
    p2.acquire()                                     # new day: allowed again


# -- Stop --------------------------------------------------------------------

def test_stop_during_wait_returns_promptly(tmp_path):
    state = {"stop": False}
    clock = FakeClock()
    p, _, _ = make(tmp_path, gap=600, clock=clock, cancel=lambda: state["stop"])
    p.acquire()                                      # first is immediate

    orig_sleep = clock.sleep

    def sleep_then_stop(s):
        orig_sleep(s)
        if clock.now() - 1_800_000_000.0 > 5:
            state["stop"] = True
    p._sleep = sleep_then_stop
    with pytest.raises(lp.PacingCancelled):
        p.acquire()
    assert clock.now() - 1_800_000_000.0 < 10        # nowhere near the 600 s gap
    assert p.calls_today() == 1                      # the abandoned call is not counted


def test_long_wait_is_announced(tmp_path):
    p, clock, msgs = make(tmp_path, gap=300)
    p.acquire()
    p.acquire()
    assert any("next AI call at" in s and "300s" in s for s in msgs)


# -- mapper wiring (route ii) -------------------------------------------------

def _cfg(tmp_path, gap=0, cap=0):
    ep = {"base_url": "http://h:1", "default_model": "mdl", "temperature": 0.0}
    if gap:
        ep["min_gap_seconds"] = gap
    if cap:
        ep["daily_cap"] = cap
    f = tmp_path / "cfg.yaml"
    f.write_text(yaml.safe_dump({"provider": "ollama", "ollama": ep}), encoding="utf-8")
    return str(f)


def test_llm_chat_waits_on_shared_pacer_and_cap_stops_pass(tmp_path, monkeypatch):
    m._resolve_llm_endpoint_config(_cfg(tmp_path, gap=30, cap=2))
    clock = FakeClock()
    pacer = lp.pacer_for("ollama", "http://h:1")
    pacer._clock, pacer._sleep, pacer._state_path = clock.now, clock.sleep, tmp_path / "st.json"
    pacer._emit = lambda s: None
    pacer._cancel = lambda: False
    calls = []
    monkeypatch.setattr(m, "_ollama_chat", lambda *a, **k: calls.append(clock.now()) or "ok")
    guard = m._LLMGuard("mdl")
    assert guard.ask("ollama", "http://h:1", "mdl", "s", "u") == "ok"
    assert guard.ask("ollama", "http://h:1", "mdl", "s", "u") == "ok"
    assert calls[1] - calls[0] >= 30 - 1e-6
    assert guard.ask("ollama", "http://h:1", "mdl", "s", "u") is None   # cap of 2
    assert guard.stopped and "Daily AI call cap reached" in guard.reason
    assert len(calls) == 2                            # negative: no third call was made


def _rows(n):
    return [{"row": i, "description": f"PAYEE {i}", "deposit": "0", "withdrawal": "10.00"}
            for i in range(1, n + 1)]


def _pass(cfg):
    return m.llm_fallback_mapping(
        unmatched_rows=_rows(4), account_tree=[], example_mappings=[],
        config_path=cfg, historical_mappings=[])


def test_mapper_pass_stopped_by_cap_keeps_rows_unattempted(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, cap=1)
    m._resolve_llm_endpoint_config(cfg)
    pacer = lp.pacer_for("ollama", "http://h:1")
    pacer._state_path, pacer._emit, pacer._cancel = tmp_path / "st.json", (lambda s: None), (lambda: False)
    calls = []
    monkeypatch.setattr(m, "_ollama_chat", lambda *a, **k: calls.append(a) or "OK")
    monkeypatch.setattr(m, "_emit_mapper_progress", lambda s: None)
    out = _pass(cfg)
    assert len(calls) == 1                            # only the warm-up got through
    st = m._LLM_RUN_STATUS
    assert st["stopped"] is True and "Daily AI call cap reached" in st["reason"]
    assert sorted(st["unattempted_rows"]) == [2, 3, 4]   # row 1 was refused, 2-4 never tried; none mapped, all Suspense (MAP-19 route)
    assert out == {}                                  # negative: nothing guessed


def test_stop_during_wait_keeps_mapped_rows(tmp_path, monkeypatch):
    cfg = _cfg(tmp_path, gap=600)
    m._resolve_llm_endpoint_config(cfg)
    clock = FakeClock()
    pacer = lp.pacer_for("ollama", "http://h:1")
    stop = {"on": False}
    pacer._clock, pacer._state_path, pacer._emit = clock.now, tmp_path / "st.json", (lambda s: None)
    pacer._cancel = lambda: stop["on"]

    def sleep(s):
        clock.sleep(s)
        if clock.now() - 1_800_000_000.0 > 700:    # user presses Stop during 3rd gap
            stop["on"] = True
    pacer._sleep = sleep
    monkeypatch.setattr(m, "_ollama_chat", lambda *a, **k: "OK")
    monkeypatch.setattr(m, "_emit_mapper_progress", lambda s: None)
    t0 = clock.now()
    _pass(cfg)
    st = m._LLM_RUN_STATUS
    assert st["stopped"] is True and "Stopped by user" in st["reason"]
    assert clock.now() - t0 < 1300                    # returned promptly, not after 4 x 600 s
    assert st["unattempted_rows"]                     # the rest are left for Suspense


def test_no_pacing_registered_means_llm_chat_does_not_wait(tmp_path, monkeypatch):
    m._resolve_llm_endpoint_config(_cfg(tmp_path))
    monkeypatch.setattr(m, "_ollama_chat", lambda *a, **k: "ok")
    t0 = time.perf_counter()
    for _ in range(50):
        assert m._llm_chat("ollama", "http://h:1", "mdl", "s", "u") == "ok"
    assert time.perf_counter() - t0 < 0.5


# -- LangChain route (i) ------------------------------------------------------

def test_load_model_attaches_shared_limiter_only_when_on(tmp_path):
    from agents.base_agent import load_model
    off = load_model(_cfg(tmp_path))
    assert getattr(off, "rate_limiter", None) is None      # negative: default unchanged
    on = load_model(_cfg(tmp_path, gap=45))
    assert on.rate_limiter is not None
    # and it is the same pacer the mapper route waits on
    assert lp.pacer_for("ollama", "http://h:1").min_gap == 45


def test_langchain_limiter_blocks_via_pacer(tmp_path):
    p, clock, _ = make(tmp_path, gap=20)
    lim = lp.langchain_rate_limiter(p)
    assert lim.acquire() is True
    assert lim.acquire(blocking=False) is False            # slot not free yet
    assert lim.acquire() is True
    assert clock.now() - 1_800_000_000.0 >= 20 - 1e-6


# -- settings persistence ----------------------------------------------------

def test_legacy_config_carries_pacing_only_when_on():
    from ui._config import _legacy_from_endpoint
    base = {"provider": "ollama", "base_url": "http://h", "default_model": "x"}
    off = _legacy_from_endpoint(base)["ollama"]
    assert "min_gap_seconds" not in off and "daily_cap" not in off
    on = _legacy_from_endpoint({**base, "min_gap_seconds": 60, "daily_cap": 100})["ollama"]
    assert on["min_gap_seconds"] == 60 and on["daily_cap"] == 100


def test_settings_save_persists_pacing(tmp_path, monkeypatch):
    from ui import _config
    from ui.tabs import settings
    store = {}
    monkeypatch.setattr(_config, "load_portable_config", lambda: store)
    monkeypatch.setattr(_config, "write_portable_config", lambda c: store.update(c))
    settings._save_endpoint("e1", "ollama", "http://h", "m", "", 0.0, False, 600, 50)
    ep = store["endpoints"]["e1"]
    assert ep["min_gap_seconds"] == 600 and ep["daily_cap"] == 50
    settings._save_endpoint("e2", "ollama", "http://h", "m", "", 0.0, False)
    assert "min_gap_seconds" not in store["endpoints"]["e2"]   # default OFF, not stored
