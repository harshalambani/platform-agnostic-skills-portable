"""PERF-04 C2 review fix: lazily built Model dropdowns read the shared choices
cache; they probe only when it is missing. Synthetic only."""
from __future__ import annotations

import json

import pytest

from tests.test_perf04c2_lazy_groups import _free_port, fire_page_load  # noqa: E402

from ui import _health, webui  # noqa: E402
from ui.tabs import _generic  # noqa: E402

REAL = [("real (tools)", "real")]


@pytest.fixture(scope="module")
def app_(request):
    app = webui.build_app(launch=False, lazy=True)
    port = _free_port()
    app.launch(server_name="127.0.0.1", server_port=port, prevent_thread_lock=True, quiet=True)
    yield app, f"http://127.0.0.1:{port}", app.get_config_file()
    app.close()


def _lazy_model_dropdowns(outs):
    return [c for o in outs.values() for c in o["render_config"]["components"]
            if c["type"] == "dropdown" and c["props"].get("label") == "Model"
            and c["props"].get("visible", True)]


def test_valid_cache_means_zero_probes_in_a_lazy_page_load(app_, monkeypatch):  # NEGATIVE
    _app, base, cfg = app_
    calls = []

    def counting(*a, **k):
        calls.append(1)
        raise AssertionError("a lazy group probed the endpoint with a valid cache")
    monkeypatch.setattr(_health, "get_model_choices", counting)
    monkeypatch.setattr(_health, "check", counting)
    monkeypatch.setattr(_generic, "_choices_cache", list(REAL))
    monkeypatch.setattr(_generic, "_choices_key", _generic._active_endpoint_key())
    outs, _ = fire_page_load(base, cfg)
    dds = _lazy_model_dropdowns(outs)
    assert dds and calls == []
    assert all(d["props"]["choices"] == [list(c) for c in REAL] for d in dds)


def test_no_cache_still_gives_the_real_list_and_probes_at_most_once_per_dropdown(app_, monkeypatch):
    _app, base, cfg = app_
    calls = []

    def probe(ep):
        calls.append(1)
        return list(REAL)
    monkeypatch.setattr(_health, "get_model_choices", probe)
    monkeypatch.setattr(_generic, "_choices_cache", None)
    monkeypatch.setattr(_generic, "_choices_key", None)
    outs, _ = fire_page_load(base, cfg)
    dds = _lazy_model_dropdowns(outs)
    assert dds
    assert 1 <= len(calls) <= len(dds)
    for d in dds:
        assert d["props"]["choices"] == [list(c) for c in REAL]
        assert "Loading models" not in json.dumps(d["props"]["choices"])
