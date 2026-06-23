"""
ui/_config.py — adapter between the portable multi-endpoint settings schema
(Data\\settings\\config.yaml) and the legacy single-provider config.yaml
that agents.base_agent.load_model still consumes.

The agents/ tree is mirrored verbatim from the upstream platform-agnostic-skills
project (build-time-pull contract, locked decision §15.1). We must not modify
it, so this adapter writes a transient legacy-shaped file in the user's
%TEMP% / /tmp and hands its path to load_model().

The portable schema lives at Data\\settings\\config.yaml and has the shape:

    active_endpoint: local_ollama
    endpoints:
      local_ollama:
        provider: ollama
        base_url: http://localhost:11434
        default_model: gemma4:12b
        temperature: 0.0
      ...

The legacy schema expected by agents.base_agent.load_model is:

    provider: ollama
    ollama:
      base_url: http://localhost:11434
      default_model: gemma4:12b
      temperature: 0.0
      models: {...}            # optional
    openai_compatible:
      ...                       # if provider is openai_compatible

Public surface:
    PORTABLE_CONFIG_PATH  — Path to the live multi-endpoint config file.
    load_portable_config() -> dict
    write_portable_config(cfg: dict) -> None
    materialize_legacy_config(endpoint_name: str | None = None) -> Path
    available_endpoints() -> list[str]
    active_endpoint_name() -> str
"""
from __future__ import annotations

import atexit
import base64
import ctypes
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Any

import yaml


# ---------------------------------------------------------------------------
# API key encryption (security: finding #4 / MP-04)
#
# API keys are encrypted at rest in config.yaml using Windows DPAPI
# (CryptProtectData / CryptUnprotectData), which binds the ciphertext to the
# current Windows user account.  Even if config.yaml is copied off the
# machine, the blob cannot be decrypted without the same user credential.
#
# Storage format: stored values use a prefix to identify how they are encoded:
#   "dpapi:<base64>"  — DPAPI ciphertext (Windows, new behaviour)
#   "plain:<value>"   — unencrypted fallback (non-Windows dev / CI)
#   "<no prefix>"     — legacy plaintext from before this feature landed;
#                       read as-is; re-encrypted on next save.
#
# The two sentinels "not-needed" and "" are never encrypted; they pass through
# unchanged so Ollama endpoints (which don't use a key) are unaffected.
# ---------------------------------------------------------------------------

_DPAPI_PREFIX = "dpapi:"
_PLAIN_PREFIX = "plain:"
_SENTINELS = {"", "not-needed"}


def _dpapi_encrypt(plaintext: str) -> str:
    """
    Encrypt *plaintext* with Windows DPAPI (user context) and return a
    base64-encoded ciphertext string.  Raises RuntimeError on failure.
    Only call this on Windows (sys.platform == "win32").
    """
    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_char_p)]

    data = plaintext.encode("utf-8")
    blob_in = _BLOB(len(data), data)
    blob_out = _BLOB()

    ok = ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in),
        None, None, None, None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise RuntimeError(
            f"CryptProtectData failed (err={ctypes.GetLastError()}). "
            "The API key could not be encrypted."
        )
    try:
        raw = ctypes.string_at(blob_out.pbData, blob_out.cbData)
        return base64.b64encode(raw).decode("ascii")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_decrypt(b64: str) -> str:
    """
    Decrypt a base64-encoded DPAPI ciphertext back to the original string.
    Raises RuntimeError on failure.  Only call this on Windows.
    """
    class _BLOB(ctypes.Structure):
        _fields_ = [("cbData", ctypes.c_ulong), ("pbData", ctypes.c_char_p)]

    raw = base64.b64decode(b64)
    blob_in = _BLOB(len(raw), raw)
    blob_out = _BLOB()

    ok = ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in),
        None, None, None, None,
        0,
        ctypes.byref(blob_out),
    )
    if not ok:
        raise RuntimeError(
            f"CryptUnprotectData failed (err={ctypes.GetLastError()}). "
            "The API key could not be decrypted — it may have been encrypted by "
            "a different Windows user account."
        )
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData).decode("utf-8")
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def encrypt_api_key(plaintext: str) -> str:
    """
    Encrypt an API key for storage in config.yaml.

    Sentinels ("", "not-needed") are returned unchanged so Ollama endpoints
    are unaffected.  On Windows the key is wrapped with DPAPI and stored as
    "dpapi:<base64>".  On other platforms (dev / CI) it is stored as
    "plain:<value>" — no encryption is available, but the prefix is present
    so the value is round-trip safe through decrypt_api_key().
    """
    if plaintext in _SENTINELS:
        return plaintext
    if sys.platform == "win32":
        return _DPAPI_PREFIX + _dpapi_encrypt(plaintext)
    return _PLAIN_PREFIX + plaintext


def decrypt_api_key(stored: str) -> str:
    """
    Decrypt a stored API key value from config.yaml.

    Handles all three storage formats:
      • "dpapi:<base64>"  — DPAPI ciphertext (Windows only)
      • "plain:<value>"   — cleartext with explicit prefix
      • "<no prefix>"     — legacy cleartext; returned as-is for compatibility

    Raises RuntimeError if a DPAPI blob is encountered on a non-Windows host.
    """
    if not stored or stored in _SENTINELS:
        return stored
    if stored.startswith(_DPAPI_PREFIX):
        if sys.platform != "win32":
            raise RuntimeError(
                "Cannot decrypt a DPAPI-encrypted API key on a non-Windows platform. "
                "Please re-enter the key in the Settings tab."
            )
        return _dpapi_decrypt(stored[len(_DPAPI_PREFIX):])
    if stored.startswith(_PLAIN_PREFIX):
        return stored[len(_PLAIN_PREFIX):]
    # Legacy plaintext (no prefix) — return as-is; will be encrypted on next save.
    return stored


# ---------------------------------------------------------------------------
# Temp-dir registry: every legacy config dir created by
# materialize_legacy_config() is tracked here and wiped at process exit.
# This prevents API keys lingering in %TEMP% after the app closes.
# ---------------------------------------------------------------------------

_LEGACY_TEMP_DIRS: list[Path] = []


def _cleanup_legacy_temp_dirs() -> None:
    """atexit handler — silently removes all legacy config temp dirs."""
    for d in _LEGACY_TEMP_DIRS:
        try:
            shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass


atexit.register(_cleanup_legacy_temp_dirs)


# ---------------------------------------------------------------------------
# Resolving the live config path.
# ---------------------------------------------------------------------------
#
# In a frozen PA-Skills build the PortableApps Launcher sets %PAL:DataDir%
# as the working directory and copies DefaultData/ into Data/ on first run,
# so the live config lives at .\settings\config.yaml relative to cwd.
#
# In source mode we fall back to bundling/templates/DefaultData/settings/
# config.yaml (read-only); in frozen mode the spec bundles that file under
# _internal/DefaultData/, exposed at runtime via sys._MEIPASS.
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_default_template() -> Path:
    """Locate the read-only fallback config — different layout in source vs frozen."""
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        return Path(meipass) / "DefaultData" / "settings" / "config.yaml"
    return PROJECT_ROOT / "bundling" / "templates" / "DefaultData" / "settings" / "config.yaml"


_DEFAULT_TEMPLATE = _resolve_default_template()


def _resolve_config_path() -> Path:
    """
    Resolution order:
      1. PA_SKILLS_CONFIG  env var, if set and the file exists.
      2. <cwd>/settings/config.yaml         — frozen build, PAL sets cwd to Data\\.
      3. <project>/Data/settings/config.yaml — dev convenience for source mode.
      4. bundling/templates/DefaultData/settings/config.yaml — read-only fallback.
    """
    env_path = os.environ.get("PA_SKILLS_CONFIG")
    if env_path and Path(env_path).is_file():
        return Path(env_path)

    cwd_candidate = Path.cwd() / "settings" / "config.yaml"
    if cwd_candidate.is_file():
        return cwd_candidate

    dev_candidate = PROJECT_ROOT / "Data" / "settings" / "config.yaml"
    if dev_candidate.is_file():
        return dev_candidate

    return _DEFAULT_TEMPLATE


PORTABLE_CONFIG_PATH: Path = _resolve_config_path()


# ---------------------------------------------------------------------------
# Read / write the portable config.
# ---------------------------------------------------------------------------

def load_portable_config(path: Path | None = None) -> dict[str, Any]:
    """Load the portable multi-endpoint config. Returns an empty dict if missing."""
    path = path or PORTABLE_CONFIG_PATH
    if not path.is_file():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def write_portable_config(cfg: dict[str, Any], path: Path | None = None) -> None:
    """Persist the portable multi-endpoint config back to disk."""
    path = path or PORTABLE_CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")


def available_endpoints() -> list[str]:
    cfg = load_portable_config()
    return list((cfg.get("endpoints") or {}).keys())


def active_endpoint_name() -> str:
    cfg = load_portable_config()
    return cfg.get("active_endpoint", "")


# ---------------------------------------------------------------------------
# Bridge to the legacy load_model() contract.
# ---------------------------------------------------------------------------

def default_model_for(endpoint: dict[str, Any], cfg: dict[str, Any] | None = None) -> str:
    """Resolve an endpoint's effective default model.

    Single knob: a top-level ``default_model`` in config.yaml is the default for
    every endpoint. An endpoint may override it with its own ``default_model``.
    So you change the default LLM in ONE place (top-level) and it applies to
    every endpoint that doesn't override it.
    """
    own = (endpoint.get("default_model") or "").strip()
    if own:
        return own
    cfg = cfg if cfg is not None else load_portable_config()
    return (cfg.get("default_model") or "").strip()


def _legacy_from_endpoint(endpoint: dict[str, Any],
                          default_model_fallback: str = "") -> dict[str, Any]:
    """
    Convert a single endpoint block from the portable schema into the
    flat shape that base_agent.load_model expects.
    """
    provider = endpoint.get("provider")
    if provider not in ("ollama", "openai_compatible"):
        raise ValueError(f"Unknown provider '{provider}' in endpoint config.")

    common = {
        "base_url": endpoint["base_url"],
        "default_model": (endpoint.get("default_model") or "").strip() or default_model_fallback,
        "temperature": float(endpoint.get("temperature", 0.0)),
    }
    if provider == "openai_compatible":
        stored_key = endpoint.get("api_key", "not-needed")
        common["api_key"] = decrypt_api_key(stored_key)

    return {
        "provider": provider,
        provider: common,
        # output_dir is honoured by agents that read the legacy file directly;
        # we keep it harmlessly present so the file is a strict superset.
        "output_dir": "./outputs",
    }


def materialize_legacy_config(endpoint_name: str | None = None) -> Path:
    """
    Materialise a transient legacy-shaped config.yaml in a temp dir and
    return its path. Caller passes this path to base_agent.load_model
    (or to any skill's run() function).

    If endpoint_name is None, the active endpoint from the portable config
    is used.
    """
    cfg = load_portable_config()
    endpoints = cfg.get("endpoints") or {}
    if not endpoints:
        raise RuntimeError(
            "No endpoints defined in the portable config "
            f"({PORTABLE_CONFIG_PATH}). Set up at least one in the Settings tab."
        )

    name = endpoint_name or cfg.get("active_endpoint") or next(iter(endpoints))
    if name not in endpoints:
        raise KeyError(f"Endpoint '{name}' not found in portable config.")

    legacy = _legacy_from_endpoint(endpoints[name], cfg.get("default_model", ""))

    # Write into a per-process temp dir so concurrent skill runs don't collide.
    # The dir is registered for deletion on process exit so the API key does
    # not linger in %TEMP% after the app closes (security: finding #4).
    tmp_dir = Path(tempfile.mkdtemp(prefix="pa-skills-cfg-"))
    _LEGACY_TEMP_DIRS.append(tmp_dir)
    legacy_path = tmp_dir / "config.yaml"
    legacy_path.write_text(yaml.safe_dump(legacy, sort_keys=False), encoding="utf-8")
    return legacy_path


def output_dir() -> Path:
    """Resolve the outputs folder; create it if absent."""
    cfg = load_portable_config()
    raw = cfg.get("output_dir") or "./outputs"
    p = Path(raw)
    if not p.is_absolute():
        # Anchor to the live config's parent's parent (Data\) when possible,
        # else fall back to cwd.
        if PORTABLE_CONFIG_PATH.is_file() and PORTABLE_CONFIG_PATH != _DEFAULT_TEMPLATE:
            p = PORTABLE_CONFIG_PATH.parent.parent / raw
    p.mkdir(parents=True, exist_ok=True)
    return p


# Module-level singleton for the per-session download staging directory.
_DOWNLOAD_STAGING_DIR: Path | None = None


def download_staging_dir() -> Path:
    """
    Return a per-session temp directory used exclusively for staging files
    that the Gradio UI needs to serve for download.

    WHY: Gradio's ``allowed_paths`` in ``app.launch()`` makes every file
    under the listed directories fetchable via the local HTTP server.  Using
    ``output_dir()`` there exposes the entire outputs folder to any local
    process or browser tab.  This function returns a narrower staging dir
    — only the one output file the current run just produced is copied here
    before being handed to ``gr.DownloadButton``.  The durable copy in
    ``output_dir()`` is not reachable through the Gradio file route.

    The directory is created lazily on first call and registered for deletion
    on process exit (security: finding #5 / MP-05).
    """
    global _DOWNLOAD_STAGING_DIR
    if _DOWNLOAD_STAGING_DIR is None:
        _DOWNLOAD_STAGING_DIR = Path(tempfile.mkdtemp(prefix="pa-skills-dl-"))
        _dl_dir = _DOWNLOAD_STAGING_DIR  # capture for closure

        def _cleanup_dl() -> None:
            shutil.rmtree(_dl_dir, ignore_errors=True)

        atexit.register(_cleanup_dl)
    return _DOWNLOAD_STAGING_DIR


def open_in_file_manager(path) -> bool:
    """
    Open a file or folder in the OS file manager. Server-side: in the portable
    app the Gradio server runs on the user's own machine, so this opens the
    location locally. If a file path is given, its containing folder's file
    manager is opened. Best-effort; returns True if a launch was attempted.
    """
    import os
    import sys
    import subprocess
    from pathlib import Path as _P
    p = _P(path)
    target = p if p.exists() else p.parent
    try:
        if sys.platform.startswith("win"):
            os.startfile(str(target))  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        else:
            subprocess.run(["xdg-open", str(target)], check=False)
        return True
    except Exception:
        return False
