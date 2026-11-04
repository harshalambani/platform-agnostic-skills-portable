"""Per-model request settings for OpenAI-compatible chat calls (one source of
truth for the mapper's real calls and the UI's pre-run model check)."""
from __future__ import annotations

from typing import Optional

LLM_REPLY_MAX_TOKENS = 300
LLM_REPLY_MAX_TOKENS_THINKING = 1500


def chat_model_name(model: str) -> str:
    """Gemini lists models as "models/gemini-..." but the chat call needs the
    bare name. Other providers' names are left unchanged."""
    m = model or ""
    return m[len("models/"):] if m.startswith("models/") else m


def reasoning_effort_for(model: str) -> Optional[str]:
    """gpt-oss: "low". Gemini 2.5 Flash/Flash-Lite: "none". Gemini 2.5 Pro and
    3.x (thinking cannot be turned off): "low". Anything else: None (not sent)."""
    low = chat_model_name(model).lower()
    if "gpt-oss" in low:
        return "low"
    if low.startswith("gemini-2.5-flash"):
        return "none"
    if low.startswith("gemini-2.5-pro") or low.startswith("gemini-3"):
        return "low"
    return None


def reply_cap_for(model: str) -> int:
    low = chat_model_name(model).lower()
    if low.startswith("gemini-2.5-pro") or low.startswith("gemini-3"):
        return LLM_REPLY_MAX_TOKENS_THINKING
    return LLM_REPLY_MAX_TOKENS
