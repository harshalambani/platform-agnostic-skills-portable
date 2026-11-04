"""
tests/test_config.py — Unit tests for ui/_config.py (Phase 6, task 6-1).

Tests cover:
  - load/write round-trip with temp YAML files
  - _legacy_from_endpoint() for ollama, openai_compatible, and unknown providers
  - materialize_legacy_config() — writes valid YAML, picks active endpoint,
    errors on empty/missing endpoints
  - available_endpoints() and active_endpoint_name()
  - _resolve_config_path() resolution order
  - output_dir() resolution

Run with:
    cd src && python -m pytest ../tests/test_config.py -v
"""
from __future__ import annotations

import os
import sys
import textwrap
from pathlib import Path

import pytest
import yaml

# ---------------------------------------------------------------------------
# Path setup — make src/ importable.
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ui._config import (
    _legacy_from_endpoint,
    load_portable_config,
    write_portable_config,
    materialize_legacy_config,
    available_endpoints,
    active_endpoint_name,
    _resolve_config_path,
    output_dir,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SAMPLE_CONFIG = {
    "active_endpoint": "local_ollama",
    "endpoints": {
        "local_ollama": {
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "default_model": "gemma4",
            "temperature": 0.0,
        },
        "remote_openai": {
            "provider": "openai_compatible",
            "base_url": "https://api.example.com/v1",
            "default_model": "gpt-4o",
            "api_key": "sk-test-123",
            "temperature": 0.7,
        },
    },
}


def _write_yaml(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# load / write round-trip
# ---------------------------------------------------------------------------

class TestLoadWriteRoundTrip:
    def test_write_then_read(self, tmp_path):
        p = tmp_path / "cfg.yaml"
        write_portable_config(SAMPLE_CONFIG, path=p)
        loaded = load_portable_config(path=p)
        assert loaded == SAMPLE_CONFIG

    def test_load_missing_file_returns_empty(self, tmp_path):
        p = tmp_path / "nonexistent.yaml"
        assert load_portable_config(path=p) == {}

    def test_load_empty_file_returns_empty(self, tmp_path):
        p = tmp_path / "empty.yaml"
        p.write_text("", encoding="utf-8")
        assert load_portable_config(path=p) == {}

    def test_write_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "cfg.yaml"
        write_portable_config({"x": 1}, path=p)
        assert p.is_file()
        assert load_portable_config(path=p) == {"x": 1}


# ---------------------------------------------------------------------------
# _legacy_from_endpoint()
# ---------------------------------------------------------------------------

class TestLegacyFromEndpoint:
    def test_ollama_shape(self):
        ep = SAMPLE_CONFIG["endpoints"]["local_ollama"]
        legacy = _legacy_from_endpoint(ep)
        assert legacy["provider"] == "ollama"
        assert legacy["ollama"]["base_url"] == "http://localhost:11434"
        assert legacy["ollama"]["default_model"] == "gemma4"
        assert legacy["ollama"]["temperature"] == 0.0
        # No api_key for ollama
        assert "api_key" not in legacy["ollama"]

    def test_openai_compatible_shape(self):
        ep = SAMPLE_CONFIG["endpoints"]["remote_openai"]
        legacy = _legacy_from_endpoint(ep)
        assert legacy["provider"] == "openai_compatible"
        assert legacy["openai_compatible"]["base_url"] == "https://api.example.com/v1"
        assert legacy["openai_compatible"]["api_key"] == "sk-test-123"
        assert legacy["openai_compatible"]["temperature"] == 0.7

    def test_unknown_provider_raises(self):
        with pytest.raises(ValueError, match="Unknown provider"):
            _legacy_from_endpoint({"provider": "anthropic", "base_url": "http://x"})

    def test_output_dir_present(self):
        ep = SAMPLE_CONFIG["endpoints"]["local_ollama"]
        legacy = _legacy_from_endpoint(ep)
        assert "output_dir" in legacy

    def test_temperature_coerced_to_float(self):
        ep = {
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "temperature": "0.5",
        }
        legacy = _legacy_from_endpoint(ep)
        assert isinstance(legacy["ollama"]["temperature"], float)
        assert legacy["ollama"]["temperature"] == 0.5


# ---------------------------------------------------------------------------
# materialize_legacy_config()
# ---------------------------------------------------------------------------

class TestMaterializeLegacyConfig:
    def test_writes_valid_yaml(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "settings" / "config.yaml"
        _write_yaml(cfg_path, SAMPLE_CONFIG)
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        legacy_path = materialize_legacy_config()
        assert legacy_path.is_file()
        legacy = yaml.safe_load(legacy_path.read_text(encoding="utf-8"))
        assert legacy["provider"] == "ollama"
        assert legacy["ollama"]["base_url"] == "http://localhost:11434"

    def test_uses_active_endpoint_by_default(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        _write_yaml(cfg_path, SAMPLE_CONFIG)
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        legacy_path = materialize_legacy_config()
        legacy = yaml.safe_load(legacy_path.read_text(encoding="utf-8"))
        # active_endpoint is local_ollama
        assert legacy["provider"] == "ollama"

    def test_explicit_endpoint_name(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        _write_yaml(cfg_path, SAMPLE_CONFIG)
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        legacy_path = materialize_legacy_config(endpoint_name="remote_openai")
        legacy = yaml.safe_load(legacy_path.read_text(encoding="utf-8"))
        assert legacy["provider"] == "openai_compatible"

    def test_missing_endpoint_name_raises(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        _write_yaml(cfg_path, SAMPLE_CONFIG)
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        with pytest.raises(KeyError, match="no_such"):
            materialize_legacy_config(endpoint_name="no_such")

    def test_empty_endpoints_raises(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        _write_yaml(cfg_path, {"active_endpoint": "x", "endpoints": {}})
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        with pytest.raises(RuntimeError, match="No endpoints defined"):
            materialize_legacy_config()

    def test_no_active_falls_back_to_first(self, tmp_path, monkeypatch):
        """If active_endpoint is missing, use the first endpoint."""
        cfg = {
            "endpoints": {
                "only_one": {
                    "provider": "ollama",
                    "base_url": "http://localhost:11434",
                },
            },
        }
        cfg_path = tmp_path / "config.yaml"
        _write_yaml(cfg_path, cfg)
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        legacy_path = materialize_legacy_config()
        legacy = yaml.safe_load(legacy_path.read_text(encoding="utf-8"))
        assert legacy["provider"] == "ollama"


# ---------------------------------------------------------------------------
# available_endpoints() / active_endpoint_name()
# ---------------------------------------------------------------------------

class TestEndpointHelpers:
    def test_available_endpoints(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        _write_yaml(cfg_path, SAMPLE_CONFIG)
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        eps = available_endpoints()
        assert set(eps) == {"local_ollama", "remote_openai"}

    def test_active_endpoint_name(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        _write_yaml(cfg_path, SAMPLE_CONFIG)
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        assert active_endpoint_name() == "local_ollama"

    def test_no_endpoints_returns_empty_list(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        _write_yaml(cfg_path, {})
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        assert available_endpoints() == []

    def test_no_active_returns_empty_string(self, tmp_path, monkeypatch):
        cfg_path = tmp_path / "config.yaml"
        _write_yaml(cfg_path, {"endpoints": {"a": {}}})
        monkeypatch.setattr("ui._config.PORTABLE_CONFIG_PATH", cfg_path)

        assert active_endpoint_name() == ""


# ---------------------------------------------------------------------------
# _resolve_config_path() — resolution order
# ---------------------------------------------------------------------------

class TestResolveConfigPath:
    def test_env_var_takes_priority(self, tmp_path, monkeypatch):
        env_cfg = tmp_path / "env_config.yaml"
        _write_yaml(env_cfg, {"from": "env"})
        monkeypatch.setenv("PA_SKILLS_CONFIG", str(env_cfg))

        result = _resolve_config_path()
        assert result == env_cfg

    def test_env_var_ignored_if_file_missing(self, tmp_path, monkeypatch):
        monkeypatch.setenv("PA_SKILLS_CONFIG", str(tmp_path / "nope.yaml"))
        # Should fall through to other candidates (won't be env path)
        result = _resolve_config_path()
        assert result != tmp_path / "nope.yaml"

    def test_cwd_candidate(self, tmp_path, monkeypatch):
        monkeypatch.delenv("PA_SKILLS_CONFIG", raising=False)
        cwd_cfg = tmp_path / "settings" / "config.yaml"
        _write_yaml(cwd_cfg, {"from": "cwd"})
        monkeypatch.chdir(tmp_path)

        result = _resolve_config_path()
        assert result == cwd_cfg
