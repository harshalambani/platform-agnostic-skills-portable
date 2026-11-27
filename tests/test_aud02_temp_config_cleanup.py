"""AUD-02: the per-run legacy config (and its folder) is deleted as soon as the
run ends, on success, error, cancel and generator close. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ui import _config  # noqa: E402
from ui import _runner  # noqa: E402

FAKE_KEY = "synthetic-not-a-key"
PORTABLE = {
    "active_endpoint": "e1",
    "endpoints": {"e1": {"provider": "openai_compatible", "base_url": "http://localhost:1/v1",
                         "default_model": "m", "api_key": FAKE_KEY}},
}


@pytest.fixture
def made(monkeypatch):
    """Spy on materialize: records every path it returned."""
    paths: list[Path] = []
    real = _config.materialize_legacy_config

    def spy(name=None):
        p = real(name)
        paths.append(p)
        return p

    monkeypatch.setattr(_config, "load_portable_config", lambda: PORTABLE)
    monkeypatch.setattr(_config, "materialize_legacy_config", spy)
    return paths


def _gone(paths):
    assert paths, "the run never made a config"
    for p in paths:
        assert not p.exists() and not p.parent.exists(), p


# ---- the helper -------------------------------------------------------------

def test_discard_removes_file_and_folder_and_is_safe_twice(made):
    p = _config.materialize_legacy_config("e1")
    assert p.is_file()
    _config.discard_legacy_config(p)
    assert not p.parent.exists()
    _config.discard_legacy_config(p)          # second call: no error
    _config.discard_legacy_config(None)
    _config.discard_legacy_config("")
    _config.discard_legacy_config(p.parent / "nope" / "config.yaml")


def test_discard_never_touches_a_folder_it_did_not_make(tmp_path):          # NEGATIVE
    f = tmp_path / "config.yaml"
    f.write_text("keep", encoding="utf-8")
    _config.discard_legacy_config(f)
    assert f.is_file() and tmp_path.is_dir()


def test_exit_cleanup_still_works_after_a_per_run_delete(made):
    gone = _config.materialize_legacy_config("e1")
    _config.discard_legacy_config(gone)
    live = _config.materialize_legacy_config("e1")       # a second run, not yet discarded
    _config.run_exit_cleanups()                          # must not raise on the gone one
    assert not live.parent.exists() and not gone.parent.exists()
    _config._cleanup_legacy_temp_dirs()                  # and is safe to run twice


# ---- generic tab ------------------------------------------------------------

def _generic_handler(monkeypatch, tmp_path, run_fn, extras=False):
    from ui.tabs import _generic
    out = tmp_path / "outputs"
    out.mkdir(exist_ok=True)
    monkeypatch.setattr(_config, "output_dir", lambda: out)
    monkeypatch.setattr(_config, "data_root_dir", lambda: tmp_path)
    monkeypatch.setattr(_config, "download_staging_dir", lambda: tmp_path)
    import agents.registry as reg
    monkeypatch.setattr(reg, "load_run_function", lambda skill: run_fn)
    monkeypatch.setattr(_generic, "skill_uses_llm", lambda n: False, raising=False)
    skill = SimpleNamespace(
        name="aud02_test", display_name="AUD02 test", description="", inputs=[],
        output=SimpleNamespace(type="file", suffix="t", extension=".csv", download_label="Download",
                               extra_outputs=(SimpleNamespace(),) if extras else ()),
        requires=SimpleNamespace(native_binaries=[], external_tools=[], llm=False),
        run_args={"config_path": "{config_path}", "output_path": "{output_path}"},
        mode="direct", entry_point="")
    return _generic._make_run_handler(skill)


@pytest.mark.parametrize("how", ["success", "error", "cancel", "close"])
def test_generic_tab_leaves_no_config_folder(how, made, monkeypatch, tmp_path):          # NEGATIVE
    seen = {}

    def run_fn(**kw):
        seen["during"] = Path(kw["config_path"]).is_file()
        if how == "error":
            raise RuntimeError("synthetic failure")
        return "ok"

    h = _generic_handler(monkeypatch, tmp_path, run_fn)
    if how == "cancel":
        def fake(work, tick_factory, **kw):
            seen["during"] = True
            return "__CANCELLED__"
            yield  # pragma: no cover
        monkeypatch.setattr(_runner, "run_with_progress", fake)
    elif how == "close":
        def fake(work, tick_factory, **kw):
            yield tick_factory(1)
            yield tick_factory(2)
        monkeypatch.setattr(_runner, "run_with_progress", fake)

    gen = h("", "m")
    if how == "close":
        for _ in gen:
            if made:
                break
        assert made[0].is_file()
        gen.close()
    else:
        list(gen)
        assert seen.get("during") is True
    _gone(made)


def test_generic_tab_with_extra_outputs_wrapper_also_cleans_up(made, monkeypatch, tmp_path):   # NEGATIVE
    h = _generic_handler(monkeypatch, tmp_path, lambda **kw: "ok", extras=True)
    list(h("", "m"))
    _gone(made)
    made.clear()
    h2 = _generic_handler(monkeypatch, tmp_path, lambda **kw: (_ for _ in ()).throw(RuntimeError("x")),
                          extras=True)
    list(h2("", "m"))
    _gone(made)


@pytest.mark.parametrize("skill_name", ["skill_bob", "skill_hsbc"])
def test_a_run_that_fails_before_the_config_makes_none(skill_name, made, monkeypatch, tmp_path):
    """The real BoB and HSBC skills, through the generic tab: no upload, no config."""
    from ui.tabs import _generic
    import agents.registry as reg
    out = tmp_path / "outputs"
    out.mkdir(exist_ok=True)
    monkeypatch.setattr(_config, "output_dir", lambda: out)
    h = _generic._make_run_handler(reg.get(skill_name))
    list(h(None, "m"))
    assert made == []
