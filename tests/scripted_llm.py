"""
MAP-06 -- ScriptedLLM: run the mapper's AI pass fully offline and deterministically.

Seam. The mapper's AI calls go  guard.ask (MAP-19 stop/backoff)  ->  _llm_chat
(MAP-20 pacer wait, then dispatch)  ->  _ollama_chat / _openai_compatible_chat
(the only code that opens a socket). ScriptedLLM replaces that last transport
layer, so everything above it is the REAL code: the MAP-19 breaker and 429
backoff, the MAP-20 pacer (with a fake clock), the shortlist prompt builder,
the answer parser, the MAP-12 direction rule and the MAP-14 own-account gate.
Patching _llm_chat itself would bypass the pacer, which is why it is not used.

Rules. Add answers with ``say(matcher, reply)``; the first rule whose matcher
matches the call's user prompt wins (later rules are fallbacks). ``matcher`` is
a substring, a compiled regex, or a callable(prompt) -> bool. ``reply`` is:

  * a str                     returned verbatim
  * choose("Account:Path")    the list number of that account in the numbered
                              shortlist (an invalid answer if it is not listed)
  * RateLimit(retry_after)    an HTTP 429 (raises the mapper's _LLMRateLimited)
  * FAIL                      a provider failure (no reply -> None)
  * a callable(prompt)->reply computed per call
  * a list                    consumed one per matching call (last one repeats)

``INVALID`` is a ready-made unparseable answer. The warm-up ping is answered
"OK" unless a rule matches it first. Calls are recorded in ``calls``.

Guarantees. No network: urllib's urlopen and socket connects are replaced by a
tripwire that records the attempt; ``close()`` (the fixture teardown) fails the
test if anything tried. Time never sleeps: the mapper's backoff sleep and the
pacer's clock/sleep are the same fake clock. Everything is deterministic and
writes only under tmp_path.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, List, Optional

import yaml

from agents import llm_pacing as lp
from agents.skill_gnucash_account_mapper import agent as m

INVALID = "I think it is probably the second one, but it depends"
FAIL = object()


class RateLimit:
    def __init__(self, retry_after: Optional[float] = None):
        self.retry_after = retry_after


class _Choose:
    def __init__(self, account: str):
        self.account = account


def choose(account: str) -> _Choose:
    """Reply with the number of `account` in the shortlist (substring match on
    the full path). If it is not listed the reply is INVALID -- never a guess."""
    return _Choose(account)


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.t = start
        self.sleeps: List[float] = []

    def now(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.t += s


class ScriptedLLM:
    PROVIDER = "ollama"
    BASE_URL = "http://scripted.invalid:11434"

    def __init__(self, monkeypatch, tmp_path: Path, *, min_gap: float = 0,
                 daily_cap: int = 0, model: str = "scripted-model"):
        self.mp = monkeypatch
        self.tmp = Path(tmp_path)
        self.clock = FakeClock()
        self.rules: list = []
        self.calls: List[dict] = []
        self.http_attempts: List[str] = []
        self.stop_requested = False
        self.model = model
        self.progress: List[str] = []

        lp.reset_registry()
        (self.tmp / "settings").mkdir(exist_ok=True)
        self.config_path = str(self.tmp / "settings" / "config.yaml")
        ep = {"base_url": self.BASE_URL, "default_model": model, "temperature": 0.0}
        if min_gap:
            ep["min_gap_seconds"] = min_gap
        if daily_cap:
            ep["daily_cap"] = daily_cap
        Path(self.config_path).write_text(
            yaml.safe_dump({"provider": self.PROVIDER, self.PROVIDER: ep}), encoding="utf-8")

        # The mapper registers the endpoint's pacer when it reads the config;
        # do it now so the fake clock is in place before the first call.
        m._resolve_llm_endpoint_config(self.config_path)
        self.pacer = lp.pacer_for(self.PROVIDER, self.BASE_URL)
        self.pacer._clock = self.clock.now
        self.pacer._sleep = self.clock.sleep
        self.pacer._cancel = lambda: self.stop_requested
        self.pacer._emit = self.progress.append
        self.pacer._state_path = self.tmp / "pacing_state.json"

        mp = self.mp
        mp.setattr(m, "_ollama_chat", self._transport)
        mp.setattr(m, "_openai_compatible_chat", self._transport_openai)
        mp.setattr(m, "_llm_sleep", self.clock.sleep)          # MAP-19 backoff
        mp.setattr(lp, "_default_cancel", lambda: self.stop_requested)
        mp.setattr(m, "_emit_mapper_progress", self.progress.append)
        self._install_tripwire()

    # -- scripting --------------------------------------------------------
    def say(self, matcher, reply) -> "ScriptedLLM":
        self.rules.append([matcher, reply, 0])
        return self

    def stop(self) -> None:
        """Simulate the user pressing Stop."""
        self.stop_requested = True

    # -- transport --------------------------------------------------------
    def _transport(self, base_url, model, system, user, timeout=60.0):
        return self._answer(user, system)

    def _transport_openai(self, base_url, model, system, user, api_key=None, timeout=60.0):
        return self._answer(user, system)

    def _answer(self, user: str, system: str):
        self.calls.append({"user": user, "system": system, "at": self.clock.now()})
        for rule in self.rules:
            matcher, reply, used = rule
            if self._matches(matcher, user):
                if isinstance(reply, list):
                    item = reply[min(used, len(reply) - 1)]
                else:
                    item = reply
                rule[2] += 1
                return self._render(item, user)
        if user.strip() == "ping":
            return "OK"
        return "0"      # unscripted prompt: "none fit" -- never an invented account

    @staticmethod
    def _matches(matcher, user: str) -> bool:
        if callable(matcher):
            return bool(matcher(user))
        if isinstance(matcher, re.Pattern):
            return bool(matcher.search(user))
        return str(matcher) in user

    def _render(self, item, user: str):
        if callable(item) and not isinstance(item, (RateLimit, _Choose)):
            item = item(user)
        if item is FAIL:
            return None
        if isinstance(item, RateLimit):
            raise m._LLMRateLimited(item.retry_after)
        if isinstance(item, _Choose):
            for line in user.splitlines():
                mo = re.match(r"^(\d+)\. (.*)$", line)
                if mo and item.account in mo.group(2):
                    return mo.group(1)
            return INVALID
        return item

    # -- no-network tripwire ---------------------------------------------
    def _install_tripwire(self) -> None:
        import socket
        import urllib.request

        def blocked(*a, **k):
            self.http_attempts.append(str(a[0]) if a else "?")
            raise AssertionError("ScriptedLLM: a real network call was attempted")

        self.mp.setattr(urllib.request, "urlopen", blocked)
        self.mp.setattr(socket, "create_connection", blocked)

    def close(self) -> None:
        lp.reset_registry()
        assert not self.http_attempts, f"real HTTP attempted: {self.http_attempts}"

    # -- convenience -------------------------------------------------------
    def real_calls(self) -> List[dict]:
        """Calls excluding the warm-up ping."""
        return [c for c in self.calls if c["user"].strip() != "ping"]
