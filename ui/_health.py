"""
ui/_health.py — endpoint health check used by the Home tab and the 26AS
tab's pre-run guard. Matches the contract sketched in spec §8.2 (single GET
against /api/tags or /v1/models with a 2-second timeout).

Also provides LLM capability detection: queries Ollama /api/show per model
to determine tool-calling support (checks for {{ .Tools }} in the model
template). Results are cached to avoid repeated queries.
"""
from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib import error, request
import json

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelInfo:
    """Per-model metadata including capability flags."""
    name: str
    supports_tools: bool = False       # True if template includes .Tools
    parameter_size: str = ""           # e.g. "12B", "3B"
    family: str = ""                   # e.g. "gemma", "llama"
    file_size_bytes: int = 0           # model file size from /api/tags

    @property
    def display_label(self) -> str:
        """Dropdown label: 'model_name (5.1B, 3.2GB, tools)'."""
        tag = "tools" if self.supports_tools else "text-only"
        parts: list[str] = []
        size = self.parameter_size.strip()
        if size:
            parts.append(size)
        if self.file_size_bytes > 0:
            gb = self.file_size_bytes / (1024 ** 3)
            if gb >= 1.0:
                parts.append(f"{gb:.1f}GB")
            else:
                mb = self.file_size_bytes / (1024 ** 2)
                parts.append(f"{mb:.0f}MB")
        parts.append(tag)
        return f"{self.name} ({', '.join(parts)})"


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    status: str   # "ok", "slow", "unreachable"
    detail: str
    models: tuple[str, ...] = ()
    model_infos: tuple[ModelInfo, ...] = ()


# ---------------------------------------------------------------------------
# Capability cache  (survives across tab refreshes within one app session)
# ---------------------------------------------------------------------------

_capability_cache: dict[str, ModelInfo] = {}


def clear_capability_cache() -> None:
    """Reset the cache (e.g. when the user switches endpoints)."""
    _capability_cache.clear()


# ---------------------------------------------------------------------------
# Whole-endpoint result cache + background priming
# ---------------------------------------------------------------------------
#
# A probe costs real wall-clock time: GET /api/tags, then a POST /api/show per
# model, all sequential, each with a multi-second timeout — and an endpoint
# that is simply switched off pays the full timeout. Startup used to spend
# ~11 seconds on this because the Home tab probed every configured endpoint
# while building its status panel, and the skill tabs probed the active one
# again for the model dropdown, all of it synchronous, all of it before the
# window appeared.
#
# Nothing here is needed to *build* the UI, only to fill it in, so the probe
# now runs on a background thread (prime_async) started as early as possible,
# and both consumers read the one shared result through check_cached() when
# the page connects. Callers that mean "go and look again" — the Refresh
# buttons, the Settings tab — keep calling check() and get a fresh probe.

_result_cache: dict[tuple[str, str], HealthResult] = {}
_probe_locks: dict[tuple[str, str], threading.Lock] = {}
_cache_lock = threading.Lock()


def _endpoint_key(endpoint: dict[str, Any]) -> tuple[str, str]:
    """Identity of an endpoint for caching: what it is and where it lives.
    Deliberately excludes the API key — a re-keyed endpoint is still the same
    host, and the user re-probes from Settings after editing it anyway."""
    return (
        endpoint.get("provider") or "",
        (endpoint.get("base_url") or "").rstrip("/"),
    )


def clear_result_cache() -> None:
    """Forget every cached probe, so the next check_cached() goes to the wire."""
    with _cache_lock:
        _result_cache.clear()


def cached_result(endpoint: dict[str, Any]) -> HealthResult | None:
    """The cached probe for *endpoint*, or None if it has never been probed.
    Never touches the network — for callers that want to render immediately
    and say 'checking…' when nothing is known yet."""
    with _cache_lock:
        return _result_cache.get(_endpoint_key(endpoint))


def check_cached(endpoint: dict[str, Any]) -> HealthResult:
    """Return the cached probe for *endpoint*, probing only if there is none.

    Concurrent callers for the same endpoint collapse onto one probe: the
    second caller blocks on the first rather than opening its own sockets.
    That is the point — when the page loads, the Home panel and the model
    dropdowns both ask at once, and the background prime is usually already
    in flight.
    """
    key = _endpoint_key(endpoint)
    with _cache_lock:
        hit = _result_cache.get(key)
        if hit is not None:
            return hit
        lock = _probe_locks.setdefault(key, threading.Lock())

    with lock:
        with _cache_lock:
            hit = _result_cache.get(key)
        if hit is not None:
            return hit
        return check(endpoint)


def prime_async(endpoints: list[dict[str, Any]]) -> threading.Thread:
    """Probe *endpoints* on a daemon thread and return it (tests join on it).

    Errors are swallowed by check() itself, which turns every failure into a
    HealthResult, so this can never take the app down from a background
    thread. Daemon so a probe against a hung host cannot delay quitting.
    """
    def _work() -> None:
        for ep in endpoints:
            try:
                check_cached(ep)
            except Exception:  # noqa: BLE001 - a warm cache is best-effort
                _log.debug("background health prime failed", exc_info=True)

    t = threading.Thread(target=_work, name="health-prime", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: float = 2.0, api_key: str | None = None) -> dict[str, Any]:
    headers = {"User-Agent": "PA-Skills-Portable/health"}
    # Authenticated OpenAI-compatible hosts (OpenAI, Groq, Together, ...) require
    # a Bearer token on GET /models. Without it they answer 401, which the
    # Settings tab would otherwise surface as "unreachable" even for a valid key.
    # The "" / "not-needed" sentinels (local Ollama, keyless gateways) send nothing.
    if api_key and api_key not in ("", "not-needed"):
        headers["Authorization"] = f"Bearer {api_key}"
    req = request.Request(url, headers=headers)
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def _post_json(url: str, payload: dict, timeout: float = 3.0) -> dict[str, Any]:
    """POST JSON and return parsed response."""
    data = json.dumps(payload).encode("utf-8")
    req = request.Request(
        url, data=data, method="POST",
        headers={
            "User-Agent": "PA-Skills-Portable/health",
            "Content-Type": "application/json",
        },
    )
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


# ---------------------------------------------------------------------------
# Tool-calling detection
# ---------------------------------------------------------------------------

def _detect_tool_support(body: dict[str, Any]) -> bool:
    """Detect tool-calling support from an Ollama /api/show response.

    Checks multiple signals in priority order so that every model family
    is covered regardless of Ollama version:

        1. ``capabilities`` list  (Ollama ≥0.6)  — definitive, all models.
        2. ``model_info`` dict    (Ollama ≥0.5)  — some builds expose
           ``general.tools`` or a ``tokenizer.ggml.tokens`` list that
           contains ``<tool_call>`` / ``<|tool▁call|>`` markers.
        3. Template inspection    (all versions)  — scan the Go template
           (``{{ .Tools }}``) or Jinja chat template
           (``{% if tools %}``, ``{%- if tools -%}``, etc.) for tool
           directives.  Covers llama3.x, qwen2/3, gemma3/4, mistral,
           phi3/4, command-r, deepseek-v2/v3, and others.
    """
    # --- Signal 1: capabilities list (most reliable) ---
    capabilities = body.get("capabilities") or []
    if capabilities:
        return "tools" in capabilities

    # --- Signal 2: model_info metadata ---
    model_info = body.get("model_info") or {}
    # Some Ollama builds set "general.tools" = true
    if model_info.get("general.tools"):
        return True
    # Check for tool-call special tokens in the tokenizer vocabulary
    tokens = model_info.get("tokenizer.ggml.tokens") or []
    _TOOL_TOKENS = {"<tool_call>", "<|tool▁call|>", "<|tool_call|>",
                    "<function_call>", "<|plugin|>", "<|tools|>",
                    "<tools>", "</tool_call>", "<|endoftool|>"}
    if isinstance(tokens, list) and any(t in _TOOL_TOKENS for t in tokens):
        return True

    # --- Signal 3: template string inspection ---
    template = body.get("template", "")
    if template:
        # Go template syntax (llama2, older models):
        #   {{ .Tools }}  {{- .Tools }}  {{ if .Tools }}
        if ".Tools" in template:
            return True
        # Jinja template syntax (gemma3/4, qwen2/3, mistral, phi, deepseek, etc.):
        #   {% if tools %}  {%- if tools -%}  {% if tools is defined %}
        if "if tools" in template:
            return True

    return False


def _check_ollama_tool_support(base_url: str, model_name: str,
                               file_size: int = 0) -> ModelInfo:
    """Query /api/show for *model_name* and return a ModelInfo."""
    if model_name in _capability_cache:
        return _capability_cache[model_name]

    supports_tools = False
    param_size = ""
    family = ""

    try:
        body = _post_json(
            f"{base_url}/api/show",
            {"name": model_name},
            timeout=3.0,
        )
        supports_tools = _detect_tool_support(body)

        details = body.get("details") or {}
        param_size = details.get("parameter_size", "")
        family = details.get("family", "")
        families = details.get("families") or []
        if not family and families:
            family = families[0]
    except Exception:  # noqa: BLE001
        _log.debug("Could not query /api/show for %s", model_name)

    info = ModelInfo(
        name=model_name,
        supports_tools=supports_tools,
        parameter_size=param_size,
        family=family,
        file_size_bytes=file_size,
    )
    _capability_cache[model_name] = info
    return info


def _openai_model_info(model_id: str) -> ModelInfo:
    """OpenAI-compatible endpoints: assume tool support (most do)."""
    if model_id in _capability_cache:
        return _capability_cache[model_id]
    info = ModelInfo(name=model_id, supports_tools=True)
    _capability_cache[model_id] = info
    return info


# ---------------------------------------------------------------------------
# Public: enriched model list
# ---------------------------------------------------------------------------

def get_model_choices(endpoint: dict[str, Any]) -> list[tuple[str, str]]:
    """Return Gradio-compatible (label, value) pairs with capability badges.

    Each entry is (display_label, raw_model_name) so the dropdown shows
    'model (tools)' but the value passed to the runner is the plain name.
    """
    return _choices_from(check(endpoint), endpoint)


def get_model_choices_cached(endpoint: dict[str, Any]) -> list[tuple[str, str]]:
    """As get_model_choices, but served from the shared probe cache — this is
    the startup path, where re-probing an endpoint the prime thread has
    already answered for would just re-spend the seconds prime_async exists
    to hide."""
    return _choices_from(check_cached(endpoint), endpoint)


def get_model_choices_if_known(endpoint: dict[str, Any]) -> list[tuple[str, str]] | None:
    """Choices for an endpoint that has already been probed, else None.

    Never opens a socket, so it is safe on the startup path: a None answer
    means 'not known yet', which the caller renders as a placeholder rather
    than waiting for the wire.
    """
    result = cached_result(endpoint)
    return None if result is None else _choices_from(result, endpoint)


def _choices_from(result: HealthResult, endpoint: dict[str, Any]) -> list[tuple[str, str]]:
    if not result.ok or not result.model_infos:
        # Fallback — no enrichment available
        fallback = endpoint.get("default_model")
        if fallback:
            return [(fallback, fallback)]
        return []
    return [(mi.display_label, mi.name) for mi in result.model_infos]


# ---------------------------------------------------------------------------
# Main health check
# ---------------------------------------------------------------------------

def check(endpoint: dict[str, Any]) -> HealthResult:
    """
    Probe a single endpoint and return a HealthResult.

    For Ollama: GET <base_url>/api/tags, then POST /api/show per model.
    For OpenAI-compatible: GET <base_url>/models.

    Always goes to the wire — this is what a Refresh button means. The result
    is stored in the shared cache on the way out, so a fresh probe here also
    updates what check_cached() will hand to everyone else.
    """
    result = _check_uncached(endpoint)
    with _cache_lock:
        _result_cache[_endpoint_key(endpoint)] = result
    return result


def _check_uncached(endpoint: dict[str, Any]) -> HealthResult:
    provider = endpoint.get("provider")
    base = (endpoint.get("base_url") or "").rstrip("/")
    if not base:
        return HealthResult(False, "unreachable", "Empty base_url.")

    if provider == "ollama":
        url = f"{base}/api/tags"
    elif provider == "openai_compatible":
        url = f"{base}/models"
    else:
        return HealthResult(False, "unreachable", f"Unknown provider '{provider}'.")

    try:
        if provider == "openai_compatible":
            # Decrypt the stored key (config holds "dpapi:"/"plain:" ciphertext)
            # so the health probe authenticates the same way generation does.
            from . import _config
            raw_key = endpoint.get("api_key", "")
            key = _config.decrypt_api_key(raw_key) if raw_key else ""
            body = _get_json(url, timeout=2.0, api_key=key)
        else:
            body = _get_json(url, timeout=2.0)
    except error.URLError as e:
        return HealthResult(False, "unreachable", f"{type(e).__name__}: {e.reason}")
    except TimeoutError:
        return HealthResult(False, "slow", "Timed out after 2s.")
    except Exception as e:  # noqa: BLE001 — intentional broad catch (finding #9)
        # SECURITY NOTE (finding #9): this broad handler is intentional.  Every
        # exception — including TLS/SSL errors, certificate failures, and network
        # errors — is captured in HealthResult.detail and surfaced to the user in
        # the Settings tab.  Nothing is silently swallowed.
        return HealthResult(False, "unreachable", f"{type(e).__name__}: {e}")

    # Extract model list and enrich with capabilities.
    if provider == "ollama":
        tags = body.get("models") or []
        model_sizes: dict[str, int] = {}
        for m in tags:
            if isinstance(m, dict):
                model_sizes[m.get("name", "")] = m.get("size", 0)
        names = tuple(model_sizes.keys())
        infos = tuple(
            _check_ollama_tool_support(base, n, file_size=model_sizes.get(n, 0))
            for n in names
        )
    else:
        data = body.get("data") or []
        names = tuple(m.get("id", "") for m in data if isinstance(m, dict))
        infos = tuple(_openai_model_info(n) for n in names)

    return HealthResult(
        True, "ok", f"OK — {len(names)} model(s).", models=names, model_infos=infos,
    )
