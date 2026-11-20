"""
agents/base_agent.py — LLM loading, ReAct agent builder, and direct runner.

This is the PA Skills Portable implementation of the base_agent contract that
all skill agents import from. It replaces the upstream platform-agnostic-skills
version (which is not bundled here) with one that reads from the portable
multi-endpoint config via ui._config.materialize_legacy_config().

Legacy config shape (materialised by ui._config):
    provider: ollama               # or openai_compatible
    ollama:
      base_url: http://localhost:11434
      default_model: gemma4:12b
      temperature: 0.0
    output_dir: ./outputs

Public surface:
    load_model(config_path, model_override)
    build_agent(tools, system_prompt, config_path, model_override) -> LangGraph app
    run_direct(user_message, system_prompt, config_path, model_override) -> str
    set_progress_queue(q)
    get_progress_queue()
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any, Optional, Sequence
import queue as _queue_module

import yaml

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Thread-local progress queue (consumed by ui/_runner.run_with_streaming).
# ---------------------------------------------------------------------------

_thread_local = threading.local()


def set_progress_queue(q: Optional[_queue_module.Queue]) -> None:
    """Install or clear the progress queue for the current worker thread."""
    _thread_local.progress_queue = q


def get_progress_queue() -> Optional[_queue_module.Queue]:
    """Return the current thread's progress queue, or None."""
    return getattr(_thread_local, "progress_queue", None)


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------

def _endpoint_policy():
    try:
        from agents import endpoint_policy
    except ImportError:  # run from inside src/agents
        import endpoint_policy  # type: ignore[no-redef]
    return endpoint_policy


def _params():
    try:
        from agents import llm_model_params
    except ImportError:  # run from inside src/agents
        import llm_model_params  # type: ignore[no-redef]
    return llm_model_params


def _is_length_stop(message) -> bool:
    """True when the model stopped because it hit the reply cap."""
    meta = getattr(message, "response_metadata", None) or {}
    return str(meta.get("done_reason") or meta.get("finish_reason") or "").lower() == "length"


def _with_cap_note(text: str, message) -> str:
    if _is_length_stop(message):
        note = _params().reply_cap_note()
        log.warning("model reply was cut at the reply cap")
        return text + chr(10) + chr(10) + note
    return text


class _LimitedAgent:
    """A compiled agent run with the step limit; a runaway loop stops with a
    plain message, and a reply cut at the reply cap says so."""

    def __init__(self, graph, steps: int | None = None):
        self._graph = graph
        self._steps = steps or _params().AGENT_STEP_LIMIT

    def __getattr__(self, name):
        return getattr(self._graph, name)

    def invoke(self, input, config=None, **kwargs):
        p = _params()
        cfg = dict(config or {})
        cfg["recursion_limit"] = min(int(cfg.get("recursion_limit") or self._steps), self._steps)
        try:
            from langgraph.errors import GraphRecursionError
        except ImportError:  # pragma: no cover
            GraphRecursionError = ()                                  # type: ignore[assignment]
        try:
            result = self._graph.invoke(input, cfg, **kwargs)
        except GraphRecursionError as e:
            raise p.LlmLimitError(p.step_limit_message(self._steps)) from e
        try:
            last = result["messages"][-1]
            if _is_length_stop(last) and isinstance(last.content, str):
                last.content = _with_cap_note(last.content, last)
        except (KeyError, IndexError, TypeError, AttributeError):
            pass
        return result


def load_model(config_path: str, model_override: str | None = None):
    """
    Load a LangChain chat model from a legacy-format config.yaml.

    The model sends every request through the single AI client
    (agents.llm_client), so the key rule, pacing, error words and redaction are
    the same as everywhere else. A missing provider, address or model is an
    error in plain words; nothing is guessed.

    Args:
        config_path:    Path to a config.yaml produced by
                        ui._config.materialize_legacy_config().
        model_override: If provided, overrides the config's default_model.

    Returns:
        A LangChain BaseChatModel (agents.llm_chat_model.ClientChatModel).
    """
    cfg_path = Path(config_path)
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Config not found: {config_path}\n"
            "Make sure an endpoint is configured in the Settings tab."
        )

    import dataclasses  # noqa: PLC0415
    from agents import llm_chat_model, llm_client  # noqa: PLC0415
    p = _params()
    ep = llm_client.resolve_endpoint(config_path, model_override)   # also registers the pacing
    _endpoint_policy().check_send(ep.base_url, ep.api_key if ep.provider == "openai_compatible" else None)  # AUD-03
    ep = dataclasses.replace(
        ep,
        timeout=p.langchain_timeout_seconds(ep.base_url, ep.model),
        max_tokens=p.LANGCHAIN_REPLY_MAX_TOKENS,
    )
    log.info("Loading model: provider=%s base_url=%s model=%s", ep.provider, ep.base_url, ep.model)
    return llm_chat_model.ClientChatModel(endpoint=ep, connect_retries=p.LANGCHAIN_MAX_RETRIES)


# ---------------------------------------------------------------------------
# ReAct agent builder
# ---------------------------------------------------------------------------

def build_agent(
    tools: Sequence,
    system_prompt: str,
    config_path: str,
    model_override: str | None = None,
):
    """
    Build a LangGraph ReAct agent.

    Args:
        tools:          List of LangChain @tool-decorated functions.
        system_prompt:  System prompt sent to the model as the first message
                        of every step. Must not be empty.
        config_path:    Path to legacy config.yaml for load_model().
        model_override: Optional model name override.

    Returns:
        A compiled LangGraph CompiledGraph (callable with .invoke()).

    Raises:
        ValueError: if system_prompt is empty or None. Checked before any
            model is loaded.
    """
    if not isinstance(system_prompt, str) or not system_prompt.strip():
        raise ValueError(
            "build_agent needs a non-empty system prompt; the agent's "
            "instructions would otherwise never reach the model."
        )

    from langchain.agents import create_agent

    llm = load_model(config_path, model_override)

    return _LimitedAgent(create_agent(llm, tools, system_prompt=system_prompt))


# ---------------------------------------------------------------------------
# Direct runner (no tools — prompt → LLM → response)
# ---------------------------------------------------------------------------

def run_direct(
    user_message: str,
    system_prompt: str,
    config_path: str,
    model_override: str | None = None,
) -> str:
    """
    Send a single user message to the LLM (no tools) and return the response.

    Args:
        user_message:   The human turn content.
        system_prompt:  System prompt prepended to the conversation.
        config_path:    Path to legacy config.yaml for load_model().
        model_override: Optional model name override.

    Returns:
        The LLM's reply as a plain string.
    """
    from langchain_core.messages import HumanMessage, SystemMessage

    llm = load_model(config_path, model_override)
    messages = [
        SystemMessage(content=system_prompt),
        HumanMessage(content=user_message),
    ]
    log.info("run_direct: invoking LLM (%d chars user message)", len(user_message))
    response = llm.invoke(messages)
    return _with_cap_note(response.content, response)
