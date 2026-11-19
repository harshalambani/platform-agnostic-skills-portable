"""AUD-07 P1: the single AI client.

One place that knows how to talk to an AI service over plain HTTP: how to read
the endpoint from a config, how to build the request for each provider, how to
send the key, how to pace calls, and how to turn a failure into plain words.

Built by copying the account mapper's transport code
(skill_gnucash_account_mapper/agent.py) and the health check's request code
(ui/_health.py). Nothing calls this module yet; the callers move over in later
steps. It imports nothing from ui/ and nothing from LangChain.

Public surface:
    Endpoint        frozen settings for one endpoint (repr hides the key)
    Reply           a str (the message text) with .finish_reason
    RateLimited     the service answered HTTP 429; .retry_after seconds or None
    LlmError        any other failure; .failure is a llm_errors.LlmFailure
    resolve_endpoint(config_path, model_override=None) -> Endpoint
    chat(ep, system, user, *, timeout=None) -> Reply
    probe(ep, *, timeout=None) -> None
    list_models(ep, *, timeout=2.0) -> list[dict]

chat() and probe() never retry on their own. A 429 is raised as RateLimited and
the CALLER decides whether to wait.
"""
from __future__ import annotations

import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Optional
from urllib import error as _uerror
from urllib import request as _req

import yaml

try:
    from agents import endpoint_policy, llm_errors, llm_model_params, llm_pacing
except ImportError:  # CLI / bare-import usage
    import endpoint_policy  # type: ignore[no-redef]
    import llm_errors  # type: ignore[no-redef]
    import llm_model_params  # type: ignore[no-redef]
    import llm_pacing  # type: ignore[no-redef]

PacingStopped = llm_pacing.PacingStopped

PROVIDERS = ("ollama", "openai_compatible")

# Copied from the mapper (_LLM_TIMEOUT_SECONDS / _LLM_WARMUP_TIMEOUT).
DEFAULT_TIMEOUT = 60.0
DEFAULT_WARMUP_TIMEOUT = 180.0
# Copied from ui/_health.py probe_model (a cold local model has to load first).
PROBE_TIMEOUT_OLLAMA = 120.0
PROBE_TIMEOUT_HOSTED = 20.0

_USER_AGENT = "PA-Skills/llm-client"


@dataclass(frozen=True)
class Endpoint:
    provider: str
    base_url: str
    model: str
    api_key: Optional[str] = None
    temperature: float = 0.0
    timeout: float = DEFAULT_TIMEOUT
    warmup_timeout: float = DEFAULT_WARMUP_TIMEOUT
    max_tokens: Optional[int] = None     # None -> llm_model_params.reply_cap_for(model)

    def __repr__(self) -> str:
        key = "***" if self.api_key else "None"
        return (f"Endpoint(provider={self.provider!r}, base_url={self.base_url!r}, "
                f"model={self.model!r}, api_key={key}, temperature={self.temperature!r}, "
                f"timeout={self.timeout!r}, warmup_timeout={self.warmup_timeout!r}, "
                f"max_tokens={self.max_tokens!r})")

    __str__ = __repr__


class Reply(str):
    """The message CONTENT of an AI reply (never a reasoning field), plus why
    the model stopped (``finish_reason``: "stop", "length", ... or None when the
    server did not say)."""
    finish_reason: Optional[str] = None

    def __new__(cls, text: str, finish_reason: Optional[str] = None):
        obj = super().__new__(cls, text)
        obj.finish_reason = finish_reason
        return obj


class RateLimited(Exception):
    """The provider answered HTTP 429 (and it is not an out-of-credits refusal).
    ``retry_after`` is the seconds the server asked for, or None."""

    def __init__(self, retry_after: Optional[float] = None, original: Optional[BaseException] = None,
                 detail: str = ""):
        super().__init__("HTTP 429 rate limited")
        self.retry_after = retry_after
        self.original = original      # the urllib exception, for callers that word it themselves
        self.detail = detail          # the service's own message (key removed)


class LlmError(Exception):
    """A failed AI call. ``failure`` is the classified, redacted reason;
    ``str(error)`` is ``failure.reason``. ``original`` is the exception that was
    caught (urllib's HTTPError / URLError, a timeout, ...) and ``detail`` the
    service's own message from a JSON error body, uncapped, with the key
    removed; both exist for callers that word the failure themselves (the
    health check)."""

    def __init__(self, failure: "llm_errors.LlmFailure", original: Optional[BaseException] = None,
                 detail: str = ""):
        super().__init__(failure.reason)
        self.failure = failure
        self.original = original
        self.detail = detail


class _HttpFailure(Exception):
    """An HTTP error answer in the shape llm_errors.classify reads."""

    def __init__(self, status: int, body: Any, text: str):
        super().__init__(f"HTTP {status}: {text}")
        self.status_code = status
        self.body = body
        self.message = text


def parse_retry_after(value) -> Optional[float]:
    """Retry-After is either delta-seconds or an HTTP date. None if unusable.
    (Copied from the mapper's _parse_retry_after.)"""
    if value is None:
        return None
    text = str(value).strip()
    try:
        secs = float(text)
        return secs if secs >= 0 else None
    except ValueError:
        pass
    try:
        from datetime import datetime, timezone  # noqa: PLC0415
        from email.utils import parsedate_to_datetime  # noqa: PLC0415
        when = parsedate_to_datetime(text)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Endpoint lookup (the mapper's strict version: nothing is guessed)
# ---------------------------------------------------------------------------

def resolve_endpoint(config_path, model_override: Optional[str] = None) -> Endpoint:
    """Read the materialised legacy LLM config and return the Endpoint.

    A missing or unknown provider, base_url or model is a ValueError; there is
    no fallback address and no fallback model. Registers the endpoint's pacing
    (min gap / daily cap) with llm_pacing, as the mapper does.
    """
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    provider = cfg.get("provider")
    if provider not in PROVIDERS:
        raise ValueError(
            f"Unknown or missing LLM provider {provider!r} in {config_path} "
            "-- refusing to guess an endpoint."
        )
    ep = cfg.get(provider) or {}
    base_url = (ep.get("base_url") or "").rstrip("/")
    if not base_url:
        raise ValueError(f"No base_url configured for provider '{provider}' in {config_path}.")
    llm_pacing.configure_from_legacy(cfg)
    model = model_override or ep.get("default_model")
    if not model:
        raise ValueError(f"No model configured for provider '{provider}' in {config_path}.")
    api_key = ep.get("api_key") if provider == "openai_compatible" else None
    return Endpoint(
        provider=provider,
        base_url=base_url,
        model=model,
        api_key=api_key or None,
        temperature=float(ep.get("temperature", 0.0)),
    )


# ---------------------------------------------------------------------------
# Request builders, one per provider (the AUD-09 registry)
# ---------------------------------------------------------------------------

def _cap(ep: Endpoint) -> int:
    return ep.max_tokens if ep.max_tokens else llm_model_params.reply_cap_for(ep.model)


def _ollama_chat_request(ep: Endpoint, messages: list, cap: int):
    body = {
        "model": ep.model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": ep.temperature, "num_predict": cap},
    }
    return f"{ep.base_url}/api/chat", body


def _openai_chat_request(ep: Endpoint, messages: list, cap: int):
    body: Dict[str, Any] = {
        "model": llm_model_params.chat_model_name(ep.model),
        "messages": messages,
        "temperature": ep.temperature,
        "max_tokens": cap,
        "stream": False,
    }
    effort = llm_model_params.reasoning_effort_for(ep.model)
    if effort:
        body["reasoning_effort"] = effort
    return f"{ep.base_url}/chat/completions", body


def _ollama_parse(body: dict) -> Reply:
    # Only message CONTENT counts; a "thinking" field is never the answer.
    return Reply((body.get("message") or {}).get("content") or "", body.get("done_reason"))


def _openai_parse(body: dict) -> Reply:
    choices = body.get("choices") or []
    if not choices:
        return Reply("")
    # Only message CONTENT counts; a "reasoning" field is never the answer.
    return Reply((choices[0].get("message") or {}).get("content") or "",
                 choices[0].get("finish_reason"))


def _ollama_list_url(ep: Endpoint) -> str:
    return f"{ep.base_url}/api/tags"


def _openai_list_url(ep: Endpoint) -> str:
    return f"{ep.base_url}/models"


def _ollama_list_parse(body: dict) -> list:
    return [m for m in (body.get("models") or []) if isinstance(m, dict)]


def _openai_list_parse(body: dict) -> list:
    return [m for m in (body.get("data") or []) if isinstance(m, dict)]


# provider -> (build request, parse reply, list url, parse list, probe timeout)
_PROVIDERS: Dict[str, dict] = {
    "ollama": {
        "request": _ollama_chat_request, "parse": _ollama_parse,
        "list_url": _ollama_list_url, "list_parse": _ollama_list_parse,
        "probe_timeout": PROBE_TIMEOUT_OLLAMA,
    },
    "openai_compatible": {
        "request": _openai_chat_request, "parse": _openai_parse,
        "list_url": _openai_list_url, "list_parse": _openai_list_parse,
        "probe_timeout": PROBE_TIMEOUT_HOSTED,
    },
}


# ---------------------------------------------------------------------------
# Sending
# ---------------------------------------------------------------------------

def _secrets(ep: Endpoint) -> tuple:
    return (ep.api_key,) if ep.api_key else ()


def _fail(ep: Endpoint, exc: BaseException, original: Optional[BaseException] = None,
          detail: str = "") -> LlmError:
    return LlmError(llm_errors.classify(exc, ep.model, _secrets(ep)),
                    original=original if original is not None else exc, detail=detail)


def _spec(ep: Endpoint) -> dict:
    spec = _PROVIDERS.get(ep.provider)
    if spec is None:
        raise ValueError(f"Unknown LLM provider {ep.provider!r} -- no chat dispatch available.")
    return spec


def _guard_address(ep: Endpoint, url: str) -> None:
    """The one key rule: a key is never sent over plain http to a public address
    (agents.endpoint_policy). Nothing is sent when it is broken."""
    key = ep.api_key if _sends_key(ep) else None
    try:
        endpoint_policy.check_send(url, key)
    except ValueError as e:
        raise _fail(ep, e) from None


def _sends_key(ep: Endpoint) -> bool:
    # Only the OpenAI-style route carries a key; "" / "not-needed" send nothing.
    return ep.provider == "openai_compatible" and endpoint_policy.has_key(ep.api_key)


def _headers(ep: Endpoint, json_body: bool) -> dict:
    headers = {"User-Agent": _USER_AGENT}
    if json_body:
        headers["Content-Type"] = "application/json"
    if _sends_key(ep):
        headers["Authorization"] = f"Bearer {ep.api_key}"
    return headers


def _send(ep: Endpoint, request: "_req.Request", timeout: float, parse: bool = True) -> dict:
    """Send one request and return the parsed JSON body. 429 -> RateLimited
    (unless the text says the account is out of credits); anything else ->
    LlmError. Never retries. ``parse=False`` accepts any 2xx answer unread."""
    try:
        with _req.urlopen(request, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        if not parse:
            return {}
        try:
            out = json.loads(raw)
        except ValueError as e:
            raise _fail(ep, ValueError(f"the reply was not JSON ({type(e).__name__})"), original=e) from None
        return out if isinstance(out, dict) else {}
    except _uerror.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            raw = ""
        body: Any = None
        text = ""
        detail = ""
        try:
            body = json.loads(raw)
            text = detail = llm_errors.message_from_body(body)
        except Exception:  # noqa: BLE001
            text = raw if len(raw) < 200 else ""
        detail = llm_errors.redact(detail, _secrets(ep)) if detail else ""
        if e.code == 429 and not llm_errors.is_out_of_credits(429, text):
            raise RateLimited(parse_retry_after(e.headers.get("Retry-After") if e.headers else None),
                              original=e, detail=detail) from None
        raise _fail(ep, _HttpFailure(e.code, body, text), original=e, detail=detail) from None
    except LlmError:
        raise
    except _uerror.URLError as e:
        raise _fail(ep, e.reason if isinstance(e.reason, BaseException) else e, original=e) from None
    except (socket.timeout, TimeoutError) as e:
        raise _fail(ep, TimeoutError("timed out"), original=e) from None
    except Exception as e:  # noqa: BLE001
        raise _fail(ep, e) from None


def chat(ep: Endpoint, system: str, user: str, *, timeout: Optional[float] = None) -> Reply:
    """One system+user message in, the reply text out.

    Raises LlmError (a failure, already in plain redacted words), RateLimited
    (HTTP 429; not retried here) or llm_pacing.PacingStopped (Stop, or the daily
    cap). Waits for this endpoint's slot on the shared pacer first.
    """
    spec = _spec(ep)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    url, body = spec["request"](ep, messages, _cap(ep))
    _guard_address(ep, url)
    pacer = llm_pacing.pacer_for(ep.provider, ep.base_url)
    if pacer is not None:
        pacer.acquire()
    request = _req.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                           headers=_headers(ep, True))
    return spec["parse"](_send(ep, request, timeout or ep.timeout))


def probe(ep: Endpoint, *, timeout: Optional[float] = None) -> None:
    """The health check's one-message "ping" with the chosen model. Returns None
    on a 2xx answer; raises LlmError / RateLimited otherwise. Not paced."""
    spec = _spec(ep)
    messages = [{"role": "user", "content": "ping"}]
    if ep.provider == "ollama":
        url, body = spec["request"](ep, messages, 1)
    else:
        url, body = spec["request"](ep, messages, _cap(ep))
    _guard_address(ep, url)
    request = _req.Request(url, data=json.dumps(body).encode("utf-8"), method="POST",
                           headers=_headers(ep, True))
    _send(ep, request, timeout or spec["probe_timeout"], parse=False)


def list_models(ep: Endpoint, *, timeout: float = 2.0) -> list:
    """The models the endpoint says it serves: Ollama's /api/tags "models" or an
    OpenAI-style /models "data", as the raw dicts the service sent."""
    spec = _spec(ep)
    return spec["list_parse"](_get(ep, spec["list_url"](ep), timeout))


def _get(ep: Endpoint, url: str, timeout: float) -> dict:
    _guard_address(ep, url)
    return _send(ep, _req.Request(url, headers=_headers(ep, False)), timeout)


def get_json(url: str, *, api_key: Optional[str] = None, timeout: float = 2.0) -> dict:
    """GET one URL and return its JSON body, with the same key rule and error
    handling as the rest of the client. The key, when real, is sent as a Bearer
    token whatever the provider (the health check's request)."""
    return _get(Endpoint("openai_compatible", "", "", api_key or None), url, timeout)
