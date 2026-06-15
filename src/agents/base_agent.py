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

def load_model(config_path: str, model_override: str | None = None):
    """
    Load a LangChain chat model from a legacy-format config.yaml.

    Args:
        config_path:    Path to a config.yaml produced by
                        ui._config.materialize_legacy_config().
        model_override: If provided, overrides the config's default_model.

    Returns:
        A LangChain BaseChatModel (ChatOllama or ChatOpenAI).
    """
    cfg_path = Path(config_path)
    if not cfg_path.is_file():
        raise FileNotFoundError(
            f"Config not found: {config_path}\n"
            "Make sure an endpoint is configured in the Settings tab."
        )

    cfg: dict[str, Any] = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    provider = cfg.get("provider", "ollama")

    if provider == "ollama":
        ep: dict[str, Any] = cfg.get("ollama") or {}
        model = model_override or ep.get("default_model") or "gemma4:12b"
        base_url = ep.get("base_url", "http://localhost:11434")
        temperature = float(ep.get("temperature", 0.0))
        log.info("Loading ChatOllama: base_url=%s model=%s", base_url, model)
        from langchain_ollama import ChatOllama
        return ChatOllama(
            base_url=base_url,
            model=model,
            temperature=temperature,
        )

    elif provider == "openai_compatible":
        ep = cfg.get("openai_compatible") or {}
        model = model_override or ep.get("default_model") or "gpt-4o"
        base_url = ep.get("base_url", "")
        api_key = ep.get("api_key", "not-needed") or "not-needed"
        temperature = float(ep.get("temperature", 0.0))
        log.info("Loading ChatOpenAI: base_url=%s model=%s", base_url, model)
        from langchain_openai import ChatOpenAI
        return ChatOpenAI(
            base_url=base_url or None,
            api_key=api_key,
            model=model,
            temperature=temperature,
        )

    else:
        raise ValueError(
            f"Unknown provider {provider!r} in config. "
            "Expected 'ollama' or 'openai_compatible'."
        )


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
        system_prompt:  System prompt string (baked into tool instructions).
        config_path:    Path to legacy config.yaml for load_model().
        model_override: Optional model name override.

    Returns:
        A compiled LangGraph CompiledGraph (callable with .invoke()).
    """
    from langgraph.prebuilt import create_react_agent

    llm = load_model(config_path, model_override)

    # Create agent with tools; system prompt will be in the user message
    agent = create_react_agent(llm, tools)

    # Store system prompt as an attribute so skill runners can use it
    agent._system_prompt = system_prompt

    return agent


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
    return response.content
