"""
tests/test_health_startup_cache.py — the endpoint probe must stay OFF the
startup path.

Building the UI used to spend ~11 of ~19 seconds probing LLM endpoints: the
Home tab probed every configured endpoint to draw its status dots, and each
skill tab probed the active one for its Model dropdown — all synchronous, all
before a window existed, and an endpoint that is merely switched off pays a
full socket timeout.

The probe now runs on a background thread (ui/_health.prime_async, started
before `import gradio` in ui/webui.py) and both consumers read one shared
result when the browser connects. These tests pin the three properties that
make that safe, because all three are easy to undo by accident:

  1. the shared cache really is shared — one probe serves every reader;
  2. the construction-time paths never open a socket;
  3. "Refresh" still means refresh.

Run with:
    cd src && python -m pytest ../tests/test_health_startup_cache.py -v
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui import _health


EP_A = {"provider": "ollama", "base_url": "http://127.0.0.1:19999"}
EP_B = {"provider": "ollama", "base_url": "http://127.0.0.1:19998"}


@pytest.fixture(autouse=True)
def _clean_cache():
    _health.clear_result_cache()
    _health.clear_capability_cache()
    yield
    _health.clear_result_cache()
    _health.clear_capability_cache()


def _counting_check(delay: float = 0.0):
    """A stand-in for the real probe that records how often it ran."""
    calls: list[dict] = []

    def fake(endpoint):
        calls.append(endpoint)
        if delay:
            time.sleep(delay)
        return _health.HealthResult(True, "ok", "OK — 1 model(s).",
                                    models=("m",),
                                    model_infos=(_health.ModelInfo(name="m"),))

    return fake, calls


# ---------------------------------------------------------------------------
# 1. One probe, many readers
# ---------------------------------------------------------------------------

def test_check_cached_probes_once_per_endpoint():
    fake, calls = _counting_check()
    with patch.object(_health, "_check_uncached", fake):
        first = _health.check_cached(EP_A)
        for _ in range(5):
            _health.check_cached(EP_A)
    assert len(calls) == 1, "every reader after the first must hit the cache"
    assert first.ok


def test_cache_is_keyed_per_endpoint():
    fake, calls = _counting_check()
    with patch.object(_health, "_check_uncached", fake):
        _health.check_cached(EP_A)
        _health.check_cached(EP_B)
        _health.check_cached(EP_A)
    assert len(calls) == 2, "two distinct endpoints, two probes, no more"


def test_concurrent_readers_collapse_onto_one_probe():
    """The load handlers for Home and the model dropdowns fire at the same
    moment; the second must wait on the first rather than open its own
    sockets."""
    fake, calls = _counting_check(delay=0.2)
    results: list = []
    with patch.object(_health, "_check_uncached", fake):
        threads = [
            threading.Thread(target=lambda: results.append(_health.check_cached(EP_A)))
            for _ in range(4)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
    assert len(calls) == 1
    assert len(results) == 4 and all(r.ok for r in results)


def test_prime_async_fills_the_cache_readers_use():
    fake, calls = _counting_check()
    with patch.object(_health, "_check_uncached", fake):
        _health.prime_async([EP_A, EP_B]).join(timeout=10)
        # Both readers now find warm answers.
        assert _health.cached_result(EP_A) is not None
        assert _health.cached_result(EP_B) is not None
        _health.check_cached(EP_A)
        _health.check_cached(EP_B)
    assert len(calls) == 2, "the priming probes are the only probes"


def test_prime_async_survives_a_failing_probe():
    """A probe that raises must not take down the daemon thread mid-list, or
    endpoints after the failing one would never be primed."""
    seen: list[str] = []

    def boom(endpoint):
        seen.append(endpoint["base_url"])
        raise RuntimeError("network on fire")

    with patch.object(_health, "_check_uncached", boom):
        _health.prime_async([EP_A, EP_B]).join(timeout=10)
    assert len(seen) == 2


# ---------------------------------------------------------------------------
# 2. Construction never touches the network
# ---------------------------------------------------------------------------

def test_cached_result_never_probes():
    fake, calls = _counting_check()
    with patch.object(_health, "_check_uncached", fake):
        assert _health.cached_result(EP_A) is None
    assert calls == [], "cached_result is the no-network read"


def test_get_model_choices_if_known_never_probes():
    fake, calls = _counting_check()
    with patch.object(_health, "_check_uncached", fake):
        assert _health.get_model_choices_if_known(EP_A) is None
        _health.check_cached(EP_A)              # now it is known
        known = _health.get_model_choices_if_known(EP_A)
    assert len(calls) == 1
    assert known == [("m (text-only)", "m")]


def test_generic_refresh_models_with_allow_probe_false_stays_offline():
    """The startup path: a cold cache yields the configured default model, and
    that guess is NOT cached — otherwise the deferred load event would find a
    'warm' cache holding one fake entry and never fetch the real list."""
    from ui.tabs import _generic

    _generic._choices_cache = None
    cfg = {
        "active_endpoint": "local",
        "endpoints": {"local": dict(EP_A, default_model="fallback-model")},
    }
    fake, calls = _counting_check()
    with patch.object(_health, "_check_uncached", fake), \
         patch("ui._config.load_portable_config", return_value=cfg), \
         patch("ui._config.default_model_for", return_value="fallback-model"):
        choices = _generic._refresh_models(allow_probe=False)

    assert calls == [], "construction must not open a socket"
    assert choices == [("fallback-model", "fallback-model")]
    assert _generic._choices_cache is None, "the fallback guess must not be cached"


def test_home_status_panel_starts_as_a_placeholder():
    """Home renders a placeholder, not probe output — the real panel arrives
    via wire_deferred_status_loads."""
    from ui.tabs import home

    assert "Checking" in home._PROBING_PLACEHOLDER


# ---------------------------------------------------------------------------
# 3. Refresh still means refresh
# ---------------------------------------------------------------------------

def test_check_always_goes_to_the_wire_and_updates_the_shared_cache():
    fake, calls = _counting_check()
    with patch.object(_health, "_check_uncached", fake):
        _health.check_cached(EP_A)     # probe 1, now cached
        _health.check(EP_A)            # probe 2 — Refresh ignores the cache
        _health.check(EP_A)            # probe 3
        assert len(calls) == 3
        # ...and the fresh answer is what every other reader now sees.
        assert _health.cached_result(EP_A) is not None
        _health.check_cached(EP_A)
    assert len(calls) == 3


def test_home_refresh_button_handler_reprobes():
    from ui.tabs import home

    cfg = {"active_endpoint": "a", "endpoints": {"a": EP_A}}
    fake, calls = _counting_check()
    with patch.object(_health, "_check_uncached", fake), \
         patch("ui._config.load_portable_config", return_value=cfg):
        home._build_status_markdown()          # cached read
        n_after_cached = len(calls)
        home._refresh_status_markdown()        # Refresh button
        home._refresh_status_markdown()

    assert n_after_cached == 1
    assert len(calls) == 3, "each Refresh click is a real probe"


# ---------------------------------------------------------------------------
# 4. The deferred-fill registries
# ---------------------------------------------------------------------------

class _FakeApp:
    """Records what would be attached as Blocks.load."""

    def __init__(self):
        self.loads: list[tuple] = []

    def load(self, fn=None, inputs=None, outputs=None):
        self.loads.append((fn, inputs, outputs))


@pytest.mark.parametrize("mod_name, reset, wire, registry", [
    ("ui.tabs._generic", "reset_deferred_model_dropdowns",
     "wire_deferred_model_loads", "_deferred_model_dropdowns"),
    ("ui.tabs.home", "reset_deferred_status_panels",
     "wire_deferred_status_loads", "_deferred_status_panels"),
])
def test_deferred_registry_wires_once_and_resets(mod_name, reset, wire, registry):
    """build_app runs more than once per process (the suite builds it
    repeatedly). Components from a torn-down Blocks must never be wired into
    the next one's load event."""
    import importlib

    mod = importlib.import_module(mod_name)
    getattr(mod, reset)()

    # Nothing registered → nothing attached (a Blocks.load with no outputs
    # would be an error, not a no-op).
    app = _FakeApp()
    getattr(mod, wire)(app)
    assert app.loads == []

    sentinels = [object(), object()]
    getattr(mod, registry).extend(sentinels)
    app = _FakeApp()
    getattr(mod, wire)(app)
    assert len(app.loads) == 1, "one load event fills them all"
    assert app.loads[0][2] == sentinels

    getattr(mod, reset)()
    app = _FakeApp()
    getattr(mod, wire)(app)
    assert app.loads == [], "a reset must drop the previous build's components"
