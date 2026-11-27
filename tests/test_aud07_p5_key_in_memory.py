"""AUD-07 P5: the key stays in memory. The per-run config file carries no key;
the client takes it from memory, per config file. Synthetic keys only."""
from __future__ import annotations

import importlib
import io
import logging
import sys
import tempfile
from pathlib import Path
from urllib import error as uerror, request

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import llm_client as lc  # noqa: E402
from ui import _config, _health, _runner  # noqa: E402

KEY_A = "sk-test-SYNTHETIC-0001"
KEY_B = "sk-test-SYNTHETIC-0002"


def _portable(*pairs):
    eps = {name: {"provider": "openai_compatible", "base_url": f"https://{name}.example.test/v1",
                  "default_model": "m", "api_key": key} for name, key in pairs}
    return {"active_endpoint": pairs[0][0], "endpoints": eps}


@pytest.fixture
def portable(monkeypatch):
    cfg = _portable(("e1", KEY_A), ("e2", KEY_B))
    monkeypatch.setattr(_config, "load_portable_config", lambda: cfg)
    monkeypatch.setattr(_config, "decrypt_api_key", lambda s: s)
    made = []
    real = _config.materialize_legacy_config

    def spy(name=None):
        p = real(name)
        made.append(p)
        return p
    monkeypatch.setattr(_config, "materialize_legacy_config", spy)
    yield made
    for p in made:
        _config.discard_legacy_config(p)


def _tree_bytes(folder: Path) -> bytes:
    return b"".join(f.read_bytes() for f in folder.rglob("*") if f.is_file())


def test_the_materialised_file_has_no_key_but_everything_else(portable):      # NEGATIVE
    p = _config.materialize_legacy_config("e1")
    raw = p.read_bytes()
    assert KEY_A.encode() not in raw
    cfg = yaml.safe_load(raw)
    assert "api_key" not in cfg["openai_compatible"]
    assert cfg["openai_compatible"]["base_url"] == "https://e1.example.test/v1"
    assert cfg["openai_compatible"]["default_model"] == "m"


def test_the_client_still_gets_the_key_from_memory(portable):
    p = _config.materialize_legacy_config("e1")
    assert lc.resolve_endpoint(p).api_key == KEY_A
    assert KEY_A not in repr(lc.resolve_endpoint(p))


def test_two_endpoints_in_one_process_each_get_their_own_key(portable):      # NEGATIVE
    p1 = _config.materialize_legacy_config("e1")
    p2 = _config.materialize_legacy_config("e2")
    assert p1 != p2
    e1, e2 = lc.resolve_endpoint(p1), lc.resolve_endpoint(p2)
    assert (e1.api_key, e2.api_key) == (KEY_A, KEY_B)
    assert e1.base_url != e2.base_url


def test_the_key_never_goes_into_the_process_environment(portable, monkeypatch):    # NEGATIVE
    import os
    before = dict(os.environ)
    p = _config.materialize_legacy_config("e1")
    lc.resolve_endpoint(p)
    assert dict(os.environ) == before
    assert not any(KEY_A in v for v in os.environ.values())


def test_discarding_the_run_forgets_the_key(portable):                      # NEGATIVE
    p = _config.materialize_legacy_config("e1")
    assert lc.key_for(p) == KEY_A
    _config.discard_legacy_config(p)
    assert lc.key_for(p) is None
    assert not p.parent.exists()


def test_exit_cleanup_forgets_every_key(portable):
    p = _config.materialize_legacy_config("e2")
    _config._cleanup_legacy_temp_dirs()
    assert lc.key_for(p) is None


def test_a_handwritten_config_with_a_key_still_works(tmp_path):
    f = tmp_path / "config.yaml"
    f.write_text(yaml.safe_dump({"provider": "openai_compatible", "openai_compatible": {
        "base_url": "https://h.example.test/v1", "default_model": "m", "api_key": KEY_A}}),
        encoding="utf-8")
    assert lc.resolve_endpoint(f).api_key == KEY_A


def test_a_blank_or_placeholder_key_is_not_held(tmp_path):
    f = tmp_path / "c.yaml"
    lc.register_key(f, "")
    lc.register_key(f, "not-needed")
    assert lc.key_for(f) is None


@pytest.mark.parametrize("how", ["success", "error"])
def test_no_file_under_the_run_tree_has_the_key_after_a_run(how, portable, monkeypatch, tmp_path):    # NEGATIVE
    from ui.tabs import skill_bob as mod
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(_config, "output_dir", lambda: out)
    monkeypatch.setattr(mod, "skill_uses_llm", lambda n: False)
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4 synthetic")
    seen = {}

    def work(**kw):
        cfg = Path(kw["config_path"])
        seen["bytes"] = _tree_bytes(cfg.parent)       # mid-run: the folder has no key
        if how == "error":
            raise RuntimeError("synthetic failure")
        return "ok"
    monkeypatch.setattr(importlib.import_module("agents.skill_bob.agent"), "run", work)
    msgs = [str(m) for m in mod._run_bob(str(pdf), "m")]
    assert seen["bytes"] and KEY_A.encode() not in seen["bytes"]
    assert portable and not portable[0].parent.exists()
    assert lc.key_for(portable[0]) is None
    assert KEY_A not in " ".join(msgs)
    assert KEY_A.encode() not in b"".join(f.read_bytes() for f in tmp_path.rglob("*") if f.is_file())


@pytest.mark.parametrize("how", ["success", "error"])
def test_generic_tab_run_leaves_no_file_with_the_key(how, portable, monkeypatch, tmp_path):    # NEGATIVE
    """The same run-level check as above, through the tab every skill really uses:
    ui/tabs/_generic.py _make_run_handler (not the retired per-skill tab)."""
    from types import SimpleNamespace

    from ui.tabs import _generic
    out = tmp_path / "outputs"
    out.mkdir()
    monkeypatch.setattr(_config, "output_dir", lambda: out)
    monkeypatch.setattr(_config, "data_root_dir", lambda: tmp_path)
    monkeypatch.setattr(_config, "download_staging_dir", lambda: tmp_path)
    seen = {}

    def work(**kw):
        cfg = Path(kw["config_path"])
        seen["bytes"] = _tree_bytes(cfg.parent)       # mid-run: the folder has no key
        if how == "error":
            raise RuntimeError("synthetic failure")
        return "ok"

    import agents.registry as reg
    monkeypatch.setattr(reg, "load_run_function", lambda skill: work)
    monkeypatch.setattr(_generic, "skill_uses_llm", lambda n: False, raising=False)
    skill = SimpleNamespace(
        name="aud07_generic_test", display_name="AUD07 generic test", description="", inputs=[],
        output=SimpleNamespace(type="file", suffix="t", extension=".csv", download_label="Download",
                               extra_outputs=()),
        requires=SimpleNamespace(native_binaries=[], external_tools=[], llm=False),
        run_args={"config_path": "{config_path}", "output_path": "{output_path}"},
        mode="direct", entry_point="")
    msgs = [str(m) for m in _generic._make_run_handler(skill)("", "m")]
    assert seen["bytes"] and KEY_A.encode() not in seen["bytes"]
    assert portable and not portable[0].parent.exists()
    assert lc.key_for(portable[0]) is None
    assert KEY_A not in " ".join(msgs)
    assert KEY_A.encode() not in b"".join(f.read_bytes() for f in tmp_path.rglob("*") if f.is_file())


def test_a_failed_send_does_not_put_the_key_in_the_error_or_the_log(portable, monkeypatch, caplog):   # NEGATIVE
    p = _config.materialize_legacy_config("e1")
    ep = lc.resolve_endpoint(p)

    def boom(req, timeout=None):
        raise uerror.HTTPError(req.full_url, 401, "denied", {}, io.BytesIO(
            f'{{"error":"bad key {KEY_A}"}}'.encode()))
    monkeypatch.setattr(request, "urlopen", boom)
    caplog.set_level(logging.DEBUG)
    with pytest.raises(lc.LlmError) as ei:
        lc.chat(ep, "sys", "hi")
    assert KEY_A not in str(ei.value) and KEY_A not in (ei.value.detail or "")
    assert KEY_A not in caplog.text


def test_suggest_still_scrubs_the_in_memory_key(portable, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"))
    import suggest
    p = _config.materialize_legacy_config("e1")
    assert KEY_A in suggest._config_secrets(p)


def test_the_tool_support_probe_goes_through_the_client(monkeypatch):      # NEGATIVE
    seen = []

    def fake_post(url, payload, *, timeout=3.0):
        seen.append((url, payload))
        return {"capabilities": ["tools"]}
    monkeypatch.setattr(lc, "post_json", fake_post)
    # the old direct wire must not be reachable from the health module
    monkeypatch.setattr(request, "urlopen", lambda *a, **k: pytest.fail("direct urlopen"))
    out = _health._post_json("http://127.0.0.1:11434/api/show", {"model": "m"})
    assert out == {"capabilities": ["tools"]}
    assert seen == [("http://127.0.0.1:11434/api/show", {"model": "m"})]


def test_health_module_has_no_wire_of_its_own():                           # NEGATIVE
    assert "urlopen" not in (ROOT / "ui" / "_health.py").read_text(encoding="utf-8")
