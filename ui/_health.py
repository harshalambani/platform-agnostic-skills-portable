"""
ui/_health.py — endpoint health check used by the Home tab and the 26AS
tab's pre-run guard. Matches the contract sketched in spec §8.2 (single GET
against /api/tags or /v1/models with a 2-second timeout).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib import error, request
import json


@dataclass(frozen=True)
class HealthResult:
    ok: bool
    status: str   # "ok", "slow", "unreachable"
    detail: str
    models: tuple[str, ...] = ()


def _get_json(url: str, timeout: float = 2.0) -> dict[str, Any]:
    req = request.Request(url, headers={"User-Agent": "PA-Skills-Portable/health"})
    with request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", errors="replace"))


def check(endpoint: dict[str, Any]) -> HealthResult:
    """
    Probe a single endpoint and return a HealthResult.

    For Ollama: GET <base_url>/api/tags.
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

    # Extract model list for the picker.
    if provider == "ollama":
        tags = body.get("models") or []
        names = tuple(m.get("name", "") for m in tags if isinstance(m, dict))
    else:
        data = body.get("data") or []
        names = tuple(m.get("id", "") for m in data if isinstance(m, dict))

    return HealthResult(True, "ok", f"OK — {len(names)} model(s).", models=names)
