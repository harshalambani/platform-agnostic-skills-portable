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

    @property
    def display_label(self) -> str:
        """Dropdown label: 'model_name (tools)' or 'model_name (text-only)'."""
        tag = "tools" if self.supports_tools else "text-only"
        return f"{self.name} ({tag})"


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
# HTTP helpers
# ---------------------------------------------------------------------------

def _get_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    req = request.Request(url, headers={"User-Agent": "PA-Skills-Portable/health"})
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


def _check_ollama_tool_support(base_url: str, model_name: str) -> ModelInfo:
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
    result = check(endpoint)
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
    """
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
        names = tuple(m.get("name", "") for m in tags if isinstance(m, dict))
        infos = tuple(_check_ollama_tool_support(base, n) for n in names)
    else:
        data = body.get("data") or []
        names = tuple(m.get("id", "") for m in data if isinstance(m, dict))
        infos = tuple(_openai_model_info(n) for n in names)

    return HealthResult(
        True, "ok", f"OK — {len(names)} model(s).", models=names, model_infos=infos,
    )
