"""MAP-20: one shared pacer for every AI call, whatever route makes it.

Two routes reach a model: the LangChain factory in ``base_agent.load_model``
(via a ``rate_limiter``) and the mapper's direct-HTTP ``_llm_chat``. Both
wait on the SAME ``Pacer`` object, looked up by endpoint (provider + base
URL), so a per-endpoint minimum gap and daily cap hold across skills.

Process model: skills run as worker threads inside ONE Gradio process, so
the limiter state is in-memory behind a threading lock. Only the daily
count and the last-call time are persisted (small JSON file, atomic
replace) so a restart does not reset the daily cap.

Default is OFF (min_gap_seconds = 0, daily_cap = 0): ``acquire`` returns
immediately and touches nothing, so paid and local endpoints behave as
before.
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Dict, Optional

_SLICE_SECONDS = 0.5          # how often a wait re-checks for Stop
_REMIND_SECONDS = 60.0        # long waits re-announce this often


class PacingStopped(Exception):
    """Base: the pacer refused / abandoned a call. ``str(e)`` is user-facing."""


class PacingCancelled(PacingStopped):
    """The user pressed Stop while a call was waiting for its slot."""


class DailyCapReached(PacingStopped):
    """The endpoint's daily call cap is used up."""


def _default_emit(message: str) -> None:
    try:
        from agents.base_agent import get_progress_queue  # noqa: PLC0415
        q = get_progress_queue()
    except Exception:  # noqa: BLE001
        q = None
    if q is not None:
        q.put({"step": 5, "type": "pipeline", "snippet": f"pacing: {message}"})
    print(f"[pacing] {message}")


def _default_cancel() -> bool:
    try:
        from ui._runner import is_cancelled  # noqa: PLC0415
        return bool(is_cancelled())
    except Exception:  # noqa: BLE001
        return False


def default_state_path() -> Path:
    from ui._config import data_root_dir  # noqa: PLC0415
    return data_root_dir() / "settings" / "llm_pacing_state.json"


def _fmt_clock(epoch: float) -> str:
    return datetime.fromtimestamp(epoch).strftime("%H:%M:%S")


class Pacer:
    def __init__(self, key: str, min_gap: float = 0.0, daily_cap: int = 0, *,
                 state_path: Optional[Path] = None,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], None] = time.sleep,
                 cancel_check: Callable[[], bool] = _default_cancel,
                 emit: Callable[[str], None] = _default_emit):
        self.key = key
        self.min_gap = max(0.0, float(min_gap or 0))
        self.daily_cap = max(0, int(daily_cap or 0))
        self._state_path = state_path
        self._clock = clock
        self._sleep = sleep
        self._cancel = cancel_check
        self._emit = emit
        self._lock = threading.Lock()
        self._next_slot = 0.0      # earliest epoch the next call may start
        self._day = ""
        self._count = 0
        self._loaded = False

    # -- config ---------------------------------------------------------
    @property
    def enabled(self) -> bool:
        return self.min_gap > 0 or self.daily_cap > 0

    def update(self, min_gap: float, daily_cap: int) -> None:
        with self._lock:
            self.min_gap = max(0.0, float(min_gap or 0))
            self.daily_cap = max(0, int(daily_cap or 0))

    # -- persistence ----------------------------------------------------
    def _path(self) -> Path:
        return self._state_path if self._state_path is not None else default_state_path()

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self._path().read_text(encoding="utf-8")).get(self.key) or {}
            self._day = str(data.get("day", ""))
            self._count = int(data.get("count", 0))
            self._next_slot = float(data.get("next_slot", 0.0))
        except Exception:  # noqa: BLE001 - missing / corrupt state = fresh state
            pass

    def _save(self) -> None:
        try:
            p = self._path()
            try:
                allstate = json.loads(p.read_text(encoding="utf-8"))
            except Exception:  # noqa: BLE001
                allstate = {}
            allstate[self.key] = {"day": self._day, "count": self._count,
                                  "next_slot": self._next_slot}
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_text(json.dumps(allstate), encoding="utf-8")
            os.replace(tmp, p)
        except Exception:  # noqa: BLE001 - pacing must never break a run
            pass

    def _today(self, now: float) -> str:
        return datetime.fromtimestamp(now).strftime("%Y-%m-%d")

    def calls_today(self) -> int:
        with self._lock:
            self._load()
            return self._count if self._day == self._today(self._clock()) else 0

    # -- the one entry point -------------------------------------------
    def _reserve(self, now: float) -> float:
        """Under the lock: enforce the cap, claim the next slot, return it."""
        self._load()
        today = self._today(now)
        if self._day != today:
            self._day, self._count = today, 0
        if self.daily_cap and self._count >= self.daily_cap:
            midnight = (datetime.fromtimestamp(now) + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0)
            raise DailyCapReached(
                f"Daily AI call cap reached: {self._count} of {self.daily_cap} calls "
                f"used today on {self.key}. No more AI calls will be made until "
                f"{midnight.strftime('%d %b %H:%M')}, or raise the cap in Settings.")
        self._count += 1
        slot = max(now, self._next_slot)
        self._next_slot = slot + self.min_gap
        self._save()
        return slot

    def acquire(self) -> None:
        """Block until this call may run. Raises PacingStopped subclasses
        (never hangs on a spent cap; returns promptly on Stop)."""
        if not self.enabled:
            return
        if self._cancel():
            raise PacingCancelled("Stopped by user before the next AI call.")
        with self._lock:
            slot = self._reserve(self._clock())
        wait = slot - self._clock()
        if wait <= 0:
            return
        if wait > 1.0:
            self._emit(f"next AI call at {_fmt_clock(slot)} (in {wait:.0f}s) "
                       f"- gap {self.min_gap:.0f}s between calls on {self.key}")
        last_remind = self._clock()
        while True:
            if self._cancel():
                with self._lock:            # the call never happened: hand back its count
                    self._count = max(0, self._count - 1)
                    self._save()
                raise PacingCancelled("Stopped by user while waiting for the next AI call.")
            remaining = slot - self._clock()
            if remaining <= 0:
                return
            if self._clock() - last_remind >= _REMIND_SECONDS:
                last_remind = self._clock()
                self._emit(f"still waiting: next AI call at {_fmt_clock(slot)} "
                           f"({remaining:.0f}s to go)")
            self._sleep(min(_SLICE_SECONDS, remaining))

    def try_acquire(self) -> bool:
        """Non-blocking variant: True if the call may run right now."""
        if not self.enabled:
            return True
        with self._lock:
            now = self._clock()
            if max(now, self._next_slot) > now:
                return False
            try:
                self._reserve(now)
            except DailyCapReached:
                return False
            return True

    def estimate_finish(self, calls: int) -> Optional[float]:
        """Epoch by which ``calls`` more calls will have STARTED, or None if unpaced."""
        if not self.enabled or calls <= 0 or self.min_gap <= 0:
            return None
        first = max(self._clock(), self._next_slot)
        return first + (calls - 1) * self.min_gap


# ---------------------------------------------------------------------------
# Registry: one pacer per endpoint, shared by every route and skill.
# ---------------------------------------------------------------------------
_REGISTRY: Dict[str, Pacer] = {}
_REG_LOCK = threading.Lock()


def endpoint_key(provider: str, base_url: str) -> str:
    return f"{provider}|{(base_url or '').rstrip('/')}"


def configure(provider: str, base_url: str, min_gap: float = 0.0, daily_cap: int = 0,
              **pacer_kwargs) -> Pacer:
    """Create or update the pacer for an endpoint and return it."""
    key = endpoint_key(provider, base_url)
    with _REG_LOCK:
        p = _REGISTRY.get(key)
        if p is None:
            p = _REGISTRY[key] = Pacer(key, min_gap, daily_cap, **pacer_kwargs)
        else:
            p.update(min_gap, daily_cap)
            for k, v in pacer_kwargs.items():
                setattr(p, "_" + k if k != "cancel_check" else "_cancel", v)
        return p


def configure_from_legacy(cfg: dict) -> Optional[Pacer]:
    """Register pacing from a materialised legacy config dict (the shape
    ``ui._config._legacy_from_endpoint`` writes). Returns the pacer."""
    provider = cfg.get("provider")
    ep = cfg.get(provider) or {}
    try:
        gap = float(ep.get("min_gap_seconds") or 0)
        cap = int(ep.get("daily_cap") or 0)
    except (TypeError, ValueError):
        gap, cap = 0.0, 0
    return configure(provider, ep.get("base_url", ""), gap, cap)


def pacer_for(provider: str, base_url: str) -> Optional[Pacer]:
    with _REG_LOCK:
        return _REGISTRY.get(endpoint_key(provider, base_url))


def reset_registry() -> None:
    with _REG_LOCK:
        _REGISTRY.clear()


def langchain_rate_limiter(pacer: Optional[Pacer]):
    """A LangChain ``BaseRateLimiter`` that waits on ``pacer``; None when the
    pacer is missing or off, so unpaced endpoints are built exactly as before."""
    if pacer is None or not pacer.enabled:
        return None
    from langchain_core.rate_limiters import BaseRateLimiter  # noqa: PLC0415

    class _PacedLimiter(BaseRateLimiter):
        def acquire(self, *, blocking: bool = True) -> bool:
            if not blocking:
                return pacer.try_acquire()
            pacer.acquire()
            return True

        async def aacquire(self, *, blocking: bool = True) -> bool:
            import asyncio  # noqa: PLC0415
            return await asyncio.to_thread(self.acquire, blocking=blocking)

    return _PacedLimiter()
