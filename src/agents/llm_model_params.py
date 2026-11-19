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


# ---------------------------------------------------------------------------
# AUD-06: limits on the LangChain path (base_agent.load_model / build_agent).
# One place for the values. The mapper's own client above keeps its own numbers.
# ---------------------------------------------------------------------------

LANGCHAIN_TIMEOUT_ONPREM_SECONDS = 600   # model on this computer or a private network
LANGCHAIN_TIMEOUT_HOSTED_SECONDS = 120   # any other host, and Ollama cloud models
LANGCHAIN_MAX_RETRIES = 2                # connection failures only; a timeout is never retried
LANGCHAIN_REPLY_MAX_TOKENS = 4096
AGENT_STEP_LIMIT = 15                    # langgraph recursion_limit

_CLOUD_SUFFIXES = (":cloud", "-cloud")   # Ollama cloud models served through a local daemon


class LlmLimitError(RuntimeError):
    """A model call or agent run hit one of the limits above. The first line
    names the limit in plain words."""


def is_cloud_model(model: str) -> bool:
    return (model or "").strip().lower().endswith(_CLOUD_SUFFIXES)


def langchain_timeout_seconds(base_url: str, model: str = "") -> int:
    """600 s when the model runs on this computer or a private network (the
    AUD-03 classifier), else 120 s. A cloud model is hosted even when its
    daemon is on localhost."""
    if is_cloud_model(model):
        return LANGCHAIN_TIMEOUT_HOSTED_SECONDS
    try:
        from agents import endpoint_policy
    except ImportError:  # run from inside src/agents
        import endpoint_policy  # type: ignore[no-redef]
    host = endpoint_policy._parts(base_url)[1]
    if host and endpoint_policy.is_local_or_lan_host(host):
        return LANGCHAIN_TIMEOUT_ONPREM_SECONDS
    return LANGCHAIN_TIMEOUT_HOSTED_SECONDS


def timeout_message(seconds) -> str:
    return (f"The model did not answer within {int(seconds)} s (the time limit for this "
            f"endpoint). Check that the model is running, or use a smaller model.")


def step_limit_message(steps: int = AGENT_STEP_LIMIT) -> str:
    return (f"The run stopped after {steps} steps without finishing (the step limit). "
            f"Try a simpler request.")


def reply_cap_note(tokens: int = LANGCHAIN_REPLY_MAX_TOKENS) -> str:
    return f"[The reply was cut off at {tokens:,} tokens (the reply limit) and may be incomplete.]"
