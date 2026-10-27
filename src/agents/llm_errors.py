"""
agents/llm_errors.py -- generic, skill-independent classification of a failed
AI-service call into a plain-words reason.

Any skill that makes many AI calls in a loop uses this to decide "stop on the
first failure" and to say WHY in words the user can act on. It never returns
or logs a credential: every text it produces passes through redact().

Public surface:
    redact(text, secrets=())            -> text with key-like tokens removed
    classify(exc, model=None)           -> LlmFailure
    LlmFailure.reason                   -> one plain sentence (safe to show/log)
    LlmFailure.kind                     -> auth | model_not_found | timeout |
                                           rate_limit | out_of_credits | connection |
                                           no_endpoint | stopped | other
    is_out_of_credits(status, text)     -> True when the provider says the
                                           account has no credit / quota left
"""
from __future__ import annotations

import re
from dataclasses import dataclass

# Anything that looks like an API key / bearer token. Deliberately broad:
# provider error messages echo a masked copy of the key ("sk-abc***xyz").
_KEY_LIKE = re.compile(
    r"(?:Bearer\s+\S+)"
    r"|(?:\b(?:sk|gsk|pk|rk|xai|key|tok)[-_][A-Za-z0-9_\-\*\.]{4,})"
    r"|(?:\b[A-Za-z0-9_\-]{32,}\b)",
    re.IGNORECASE,
)

_MAX_MESSAGE = 200

# Provider wordings for "the account has no credit or quota left". Anthropic:
# "credit balance is too low"; OpenAI-style: code "insufficient_quota";
# others: "out of credits", "billing" refusals. These always mean billing.
_CREDIT_PHRASES = (
    "credit balance", "insufficient_quota", "insufficient quota",
    "out of credit", "no credits", "insufficient credit", "insufficient balance",
    "insufficient funds", "payment required", "billing hard limit",
)
# Wordings that are ambiguous: "quota" can be a billing limit or a per-minute /
# per-day rate limit (Gemini says "exceeded your current quota" for both). They
# count as out-of-credits ONLY when the text carries no rate-limit marker.
_QUOTA_PHRASES = (
    "exceeded your current quota", "quota exhausted", "quota is exhausted",
    "quota exceeded",
)
# UI-49: a rate-limit marker in a 429 body (Gemini RESOURCE_EXHAUSTED, or
# per-minute / per-day wording). Such a refusal is waited out, not final.
_RATE_LIMIT_MARKERS = (
    "resource_exhausted", "per minute", "per day", "per second", "perminute",
    "perday", "rate limit", "rate_limit", "rate-limit", "requests per",
    "retry in", "retrydelay", "try again in", "too many requests",
)


def is_rate_limit_text(text: object) -> bool:
    """True when the text carries a rate-limit marker (see _RATE_LIMIT_MARKERS)."""
    low = ("" if text is None else str(text)).lower()
    return any(m in low for m in _RATE_LIMIT_MARKERS)


def is_out_of_credits(status, text: object) -> bool:
    """True when a failed call means the account is out of credit/quota (more
    calls cannot help until it is topped up). A plain rate limit (HTTP 429
    with no such wording), or a quota message that carries a rate-limit marker
    (Gemini RESOURCE_EXHAUSTED, "per minute", "per day"), is NOT this."""
    if status == 402:
        return True
    low = ("" if text is None else str(text)).lower()
    if any(p in low for p in _CREDIT_PHRASES):
        return True
    if any(p in low for p in _QUOTA_PHRASES):
        return not is_rate_limit_text(low)
    return False


def message_from_body(body: object) -> str:
    """The provider's message from a decoded JSON error body, in either shape:
    an object ({"error": {...}} / {"message": ...}) or, as Gemini's
    OpenAI-compatible address sometimes sends, a LIST wrapping it
    ([{"error": {...}}]). An unusable body (empty list, other types) gives "".
    A status string such as RESOURCE_EXHAUSTED is appended so rate-limit
    wording is not lost."""
    if isinstance(body, list):
        for item in body:
            msg = message_from_body(item)
            if msg:
                return msg
        return ""
    if not isinstance(body, dict):
        return ""
    inner = body.get("error")
    if isinstance(inner, dict):
        msg = str(inner.get("message") or "")
        status = inner.get("status")
        if isinstance(status, str) and status and status.lower() not in msg.lower():
            msg = f"{msg} ({status})" if msg else status
        return msg
    if isinstance(inner, str) and inner:
        return inner
    return str(body.get("message") or "")


def redact(text: object, secrets: tuple = ()) -> str:
    """Return ``text`` with key-like tokens (and any explicit ``secrets``)
    replaced by ``[redacted]``."""
    out = "" if text is None else str(text)
    for s in secrets:
        if s and len(str(s)) >= 4:
            out = out.replace(str(s), "[redacted]")
    return _KEY_LIKE.sub("[redacted]", out)


@dataclass
class LlmFailure:
    kind: str
    status: int | None
    provider_message: str
    reason: str


def _status_of(exc: BaseException) -> int | None:
    for obj in (exc, getattr(exc, "response", None)):
        code = getattr(obj, "status_code", None)
        if isinstance(code, int):
            return code
    m = re.search(r"\b(?:HTTP|Error code:|status(?: code)?)\s*[:=]?\s*(\d{3})\b", str(exc), re.IGNORECASE)
    return int(m.group(1)) if m else None


def _provider_message(exc: BaseException, secrets: tuple) -> str:
    msg = ""
    body = getattr(exc, "body", None)
    if isinstance(body, (dict, list)):
        msg = message_from_body(body)
    if not msg:
        msg = getattr(exc, "message", None) or str(exc)
    msg = " ".join(str(msg).split())
    return redact(msg, secrets)[:_MAX_MESSAGE]


def classify(exc: BaseException, model: str | None = None, secrets: tuple = ()) -> LlmFailure:
    """Classify one failed call. Never raises; never includes a credential."""
    status = _status_of(exc)
    provider = _provider_message(exc, secrets)
    tname = type(exc).__name__
    low = f"{tname} {provider}".lower()
    model_txt = f" '{model}'" if model else ""

    if type(exc).__name__.startswith("Pacing") or tname == "DailyCapReached":
        kind, why = "stopped", f"the AI call was stopped before it was sent ({provider})"
    elif isinstance(exc, FileNotFoundError):
        kind, why = "no_endpoint", "no AI endpoint is configured (set one up in Settings)"
    elif status in (401, 403):
        kind, why = "auth", "the AI service refused the key"
    elif status == 404 or "model" in low and ("not found" in low or "does not exist" in low or "unknown model" in low):
        kind, why = "model_not_found", f"the AI service does not have the model{model_txt}"
    elif status == 402 or is_out_of_credits(status, f"{provider} {exc}"):
        kind, why = "out_of_credits", "the AI service says the account is out of credits"
    elif status == 429:
        kind, why = "rate_limit", "the AI service is rate-limiting this key"
    elif "timeout" in low or "timed out" in low or isinstance(exc, TimeoutError):
        kind, why = "timeout", "the AI service did not answer in time"
    elif "connect" in low or isinstance(exc, ConnectionError):
        kind, why = "connection", "the AI service could not be reached"
    else:
        kind, why = "other", "the AI service call failed"

    parts = [why]
    if status is not None:
        parts.append(f"HTTP {status}")
    detail = ", ".join(parts)
    if provider and kind not in ("stopped",):
        detail += f"; provider said: {provider}"
    return LlmFailure(kind=kind, status=status, provider_message=provider, reason=detail)
