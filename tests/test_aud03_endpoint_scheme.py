"""AUD-03: endpoint addresses (http only local/LAN, never with a key) and the
send-time guard at every place a key is attached. Synthetic values only."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import endpoint_policy as ep  # noqa: E402

KEY = "synthetic-key-aud03-4471"


# ---- the rule ---------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://localhost:11434", "http://LOCALHOST/v1", "http://127.0.0.1:11434", "http://127.9.9.9",
    "http://[::1]:11434", "http://10.1.2.3", "http://172.16.0.1", "http://172.31.255.255:8000/v1",
    "http://192.168.1.50:11434", "https://api.example.com/v1", "https://8.8.8.8", "HTTPS://Host.example",
])
def test_allowed_addresses_without_a_key(url):
    assert ep.check_save(url, "") == ""


@pytest.mark.parametrize("url", [
    "http://8.8.8.8", "http://example.com/v1", "http://myserver:11434", "http://172.15.0.1",
    "http://172.32.0.1", "http://192.169.1.1", "http://11.0.0.1", "http://[2001:db8::1]",
    "http://localhost.evil.com", "http://127.0.0.1.evil.com", "http://192.168.1.50.nip.io",
])
def test_rejected_http_addresses(url):                                          # NEGATIVE
    assert ep.check_save(url, "") != ""


@pytest.mark.parametrize("url", ["ftp://localhost", "file:///c:/x", "ws://localhost", "localhost:11434",
                                 "javascript:1", "", "http://", "//localhost"])
def test_rejected_schemes_and_junk(url):                                        # NEGATIVE
    assert ep.check_save(url, "") != ""


@pytest.mark.parametrize("url", ["http://127.0.0.1:8000", "http://192.168.1.5", "http://10.0.0.2",
                                 "http://localhost:8000/v1", "http://172.20.1.1"])
def test_a_key_over_http_is_rejected_even_locally(url):                         # NEGATIVE
    msg = ep.check_save(url, KEY)
    assert msg and KEY not in msg


def test_https_may_carry_a_key_to_any_host():
    assert ep.check_save("https://api.example.com/v1", KEY) == ""
    assert ep.check_save("https://127.0.0.1:8443", KEY) == ""


def test_placeholder_keys_are_not_keys():
    assert ep.check_save("http://localhost:8000/v1", "not-needed") == ""
    assert ep.check_save("http://localhost:8000/v1", "") == ""
    assert ep.check_save("http://localhost:8000/v1", None) == ""


def test_no_dns_lookup_is_made(monkeypatch):                                    # NEGATIVE
    import socket

    def boom(*a, **k):
        raise AssertionError("DNS lookup attempted")
    monkeypatch.setattr(socket, "getaddrinfo", boom)
    monkeypatch.setattr(socket, "gethostbyname", boom)
    assert ep.check_save("http://printer.local:11434", "") != ""


def test_messages_never_echo_the_key_or_userinfo():                             # NEGATIVE
    msg = ep.check_save(f"http://user:{KEY}@8.8.8.8", KEY, "e1")
    assert msg and KEY not in msg
    with pytest.raises(ep.EndpointPolicyError) as ei:
        ep.check_send("http://127.0.0.1", KEY, "e1")
    assert KEY not in str(ei.value) and "e1" in str(ei.value)


# ---- Settings save ----------------------------------------------------------

@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    from ui import _config
    p = tmp_path / "settings" / "config.yaml"
    p.parent.mkdir()
    p.write_text(yaml.safe_dump({"active_endpoint": "a", "endpoints": {
        "a": {"provider": "ollama", "base_url": "http://localhost:11434"}}}), encoding="utf-8")
    monkeypatch.setattr(_config, "PORTABLE_CONFIG_PATH", p)
    return p


def _save(**kw):
    from ui.tabs import settings
    args = dict(name="n", provider="ollama", base_url="http://localhost:11434", default_model="m",
                api_key="", temperature=0.0, set_active=False)
    args.update(kw)
    return settings._save_endpoint(**args)


def _endpoints(p):
    return yaml.safe_load(p.read_text(encoding="utf-8"))["endpoints"]


@pytest.mark.parametrize("kw", [
    dict(base_url="http://8.8.8.8:11434"),
    dict(base_url="http://myhost:11434"),
    dict(base_url="ftp://localhost"),
    dict(provider="openai_compatible", base_url="http://127.0.0.1:8000/v1", api_key=KEY),
    dict(provider="openai_compatible", base_url="http://192.168.1.9/v1", api_key=KEY),
    dict(provider="openai_compatible", base_url="http://172.15.0.1/v1"),
    dict(provider="openai_compatible", base_url="http://172.32.0.1/v1"),
])
def test_save_rejects_and_writes_nothing(cfg_file, kw):                         # NEGATIVE
    before = cfg_file.read_text(encoding="utf-8")
    msg = _save(**kw)
    assert msg.startswith("Error:") and KEY not in msg
    assert cfg_file.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("kw", [
    dict(base_url="http://127.0.0.1:11434"), dict(base_url="http://10.0.0.5:11434"),
    dict(base_url="http://172.16.3.3:11434"), dict(base_url="http://172.31.3.3:11434"),
    dict(base_url="http://192.168.0.7:11434"),
    dict(provider="openai_compatible", base_url="https://api.example.com/v1", api_key=KEY),
    dict(provider="openai_compatible", base_url="http://localhost:8000/v1"),
])
def test_save_accepts(cfg_file, kw):
    msg = _save(**kw)
    assert not msg.startswith("Error:"), msg
    assert "n" in _endpoints(cfg_file)


def test_a_stored_key_blocks_a_switch_to_http(cfg_file):                        # NEGATIVE
    assert not _save(name="k", provider="openai_compatible", base_url="https://api.example.com/v1",
                     api_key=KEY).startswith("Error:")
    msg = _save(name="k", provider="openai_compatible", base_url="http://localhost:8000/v1", api_key="")
    assert msg.startswith("Error:") and KEY not in msg
    assert _endpoints(cfg_file)["k"]["base_url"] == "https://api.example.com/v1"


# ---- send sites -------------------------------------------------------------

class _NoWire(Exception):
    pass


def _no_wire(monkeypatch):
    """Any HTTP attempt fails the test."""
    from urllib import request

    def boom(*a, **k):
        raise AssertionError("a request went on the wire")
    monkeypatch.setattr(request, "urlopen", boom)


def test_health_get_json_refuses_http_with_a_key(monkeypatch):                  # NEGATIVE
    from ui import _health
    _no_wire(monkeypatch)
    with pytest.raises(ep.EndpointPolicyError) as ei:
        _health._get_json("http://127.0.0.1:8000/v1/models", api_key=KEY)
    assert KEY not in str(ei.value)


def test_health_probe_reports_it_and_sends_nothing(monkeypatch):                # NEGATIVE
    from ui import _config, _health
    _no_wire(monkeypatch)
    monkeypatch.setattr(_config, "decrypt_api_key", lambda raw: raw)
    res = _health._check_uncached({"provider": "openai_compatible", "base_url": "http://192.168.1.9/v1",
                                   "api_key": KEY})
    assert not res.ok and KEY not in res.detail and "plain http" in res.detail


def test_health_chat_probe_refuses_http_with_a_key(monkeypatch):                # NEGATIVE
    from ui import _config, _health
    _no_wire(monkeypatch)
    monkeypatch.setattr(_config, "decrypt_api_key", lambda raw: raw)
    fn = _health.probe_model
    res = fn({"provider": "openai_compatible", "base_url": "http://localhost:8000/v1", "api_key": KEY}, "m")
    assert not res.ok and KEY not in res.detail


def test_health_still_allows_http_without_a_key(monkeypatch):
    from ui import _health
    seen = {}

    class _R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return b'{"models": []}'

    def fake(req, timeout=None):
        seen["url"] = req.full_url
        seen["auth"] = req.headers.get("Authorization")
        return _R()
    monkeypatch.setattr(_health.request, "urlopen", fake)
    assert _health._get_json("http://127.0.0.1:11434/api/tags") == {"models": []}
    assert seen["auth"] is None


def test_mapper_openai_chat_refuses_http_with_a_key(monkeypatch):               # NEGATIVE
    from urllib import request
    from agents.skill_gnucash_account_mapper import agent as ma
    _no_wire(monkeypatch)
    monkeypatch.setattr(request, "urlopen", lambda *a, **k: (_ for _ in ()).throw(AssertionError("wire")))
    out = ma._openai_compatible_chat("http://127.0.0.1:8000/v1", "m", "s", "u", api_key=KEY, timeout=1)
    assert out is None
    err = ma._take_llm_error()
    assert err.get("fatal") is True and KEY not in err.get("reason", "") and "plain http" in err["reason"]


def test_load_model_refuses_http_with_a_key_and_bad_schemes(tmp_path):         # NEGATIVE
    from agents import base_agent

    def cfg(provider, url, key=None):
        d = {"provider": provider, provider: {"base_url": url, "default_model": "m"}}
        if key:
            d[provider]["api_key"] = key
        p = tmp_path / f"{provider}.yaml"
        p.write_text(yaml.safe_dump(d), encoding="utf-8")
        return str(p)

    with pytest.raises(ep.EndpointPolicyError) as ei:
        base_agent.load_model(cfg("openai_compatible", "http://127.0.0.1:8000/v1", KEY))
    assert KEY not in str(ei.value)
    with pytest.raises(ep.EndpointPolicyError):
        base_agent.load_model(cfg("ollama", "ftp://localhost:11434"))


def test_load_model_still_builds_an_http_model_without_a_key(tmp_path):
    from agents import base_agent
    p = tmp_path / "c.yaml"
    p.write_text(yaml.safe_dump({"provider": "openai_compatible", "openai_compatible": {
        "base_url": "http://localhost:8000/v1", "api_key": "not-needed", "default_model": "m"}}),
        encoding="utf-8")
    assert base_agent.load_model(str(p)) is not None


# ---- shipped default --------------------------------------------------------

def test_shipped_default_has_no_lan_entry_and_every_endpoint_passes_the_rule():
    p = ROOT / "bundling" / "templates" / "DefaultData" / "settings" / "config.yaml"
    cfg = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert "lan_ollama" not in cfg["endpoints"]
    assert cfg["active_endpoint"] in cfg["endpoints"]
    for name, e in cfg["endpoints"].items():
        assert ep.check_save(e["base_url"], e.get("api_key", ""), name) == "", name
