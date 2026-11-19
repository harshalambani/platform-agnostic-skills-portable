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
import time
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


def _retry_pause(seconds: float) -> None:
    time.sleep(seconds)


_LIMITED_CLASSES: dict = {}


def _limited_ollama_class():
    """ChatOllama with a total time limit and connection retries (the
    langchain class has neither). Built once; langchain_ollama is imported late."""
    if "ollama" in _LIMITED_CLASSES:
        return _LIMITED_CLASSES["ollama"]
    import httpx
    from langchain_ollama import ChatOllama

    class LimitedChatOllama(ChatOllama):
        limit_seconds: float = 120
        connect_retries: int = 2

        def _create_chat_stream(self, messages, stop=None, **kwargs):
            p = _params()
            deadline = time.monotonic() + self.limit_seconds
            attempt = 0
            while True:
                started = False
                try:
                    for item in super()._create_chat_stream(messages, stop, **kwargs):
                        started = True
                        if time.monotonic() > deadline:
                            raise p.LlmLimitError(p.timeout_message(self.limit_seconds))
                        yield item
                    return
                except httpx.TimeoutException as e:      # never retried
                    raise p.LlmLimitError(p.timeout_message(self.limit_seconds)) from e
                except (ConnectionError, httpx.ConnectError):
                    if started or attempt >= self.connect_retries:
                        raise
                    attempt += 1
                    log.warning("connection to the model failed; retry %d of %d", attempt, self.connect_retries)
                    _retry_pause(1)

    _LIMITED_CLASSES["ollama"] = LimitedChatOllama
    return LimitedChatOllama


def _limited_openai_class():
    """ChatOpenAI with the plain timeout message and our own retries: the SDK is
    built with max_retries=0 (it would retry a timeout), and a connection failure
    is retried here, a timeout never."""
    if "openai" in _LIMITED_CLASSES:
        return _LIMITED_CLASSES["openai"]
    import openai
    from langchain_openai import ChatOpenAI

    class LimitedChatOpenAI(ChatOpenAI):
        connect_retries: int = 2

        def _timeout_error(self):
            p = _params()
            return p.LlmLimitError(p.timeout_message(self.request_timeout or 0))

        def _generate(self, *args, **kwargs):
            attempt = 0
            while True:
                try:
                    return super()._generate(*args, **kwargs)
                except openai.APITimeoutError as e:            # a subclass of the next one: first
                    raise self._timeout_error() from e
                except openai.APIConnectionError:
                    if attempt >= self.connect_retries:
                        raise
                    attempt += 1
                    log.warning("connection to the model failed; retry %d of %d", attempt, self.connect_retries)
                    _retry_pause(1)

        def _stream(self, *args, **kwargs):
            attempt = 0
            while True:
                started = False
                try:
                    for chunk in super()._stream(*args, **kwargs):
                        started = True
                        yield chunk
                    return
                except openai.APITimeoutError as e:
                    raise self._timeout_error() from e
                except openai.APIConnectionError:
                    if started or attempt >= self.connect_retries:
                        raise
                    attempt += 1
                    log.warning("connection to the model failed; retry %d of %d", attempt, self.connect_retries)
                    _retry_pause(1)

    _LIMITED_CLASSES["openai"] = LimitedChatOpenAI
    return LimitedChatOpenAI


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

    # MAP-20: one shared pacer per endpoint (None when pacing is off, so an
    # unpaced endpoint is built exactly as before).
    from agents import llm_pacing  # noqa: PLC0415
    _limiter = llm_pacing.langchain_rate_limiter(llm_pacing.configure_from_legacy(cfg))
    _pace_kw: dict[str, Any] = {"rate_limiter": _limiter} if _limiter is not None else {}

    if provider == "ollama":
        ep: dict[str, Any] = cfg.get("ollama") or {}
        model = model_override or ep.get("default_model") or "gemma4:12b"
        base_url = ep.get("base_url", "http://localhost:11434")
        temperature = float(ep.get("temperature", 0.0))
        _endpoint_policy().check_send(base_url, None)   # AUD-03
        log.info("Loading ChatOllama: base_url=%s model=%s", base_url, model)
        from langchain_ollama import ChatOllama
        p = _params()
        seconds = p.langchain_timeout_seconds(base_url, model)
        return _limited_ollama_class()(
            base_url=base_url,
            model=model,
            temperature=temperature,
            num_predict=p.LANGCHAIN_REPLY_MAX_TOKENS,
            client_kwargs={"timeout": seconds},
            limit_seconds=seconds,
            connect_retries=p.LANGCHAIN_MAX_RETRIES,
            **_pace_kw,
        )

    elif provider == "openai_compatible":
        ep = cfg.get("openai_compatible") or {}
        model = model_override or ep.get("default_model") or "gpt-4o"
        base_url = ep.get("base_url", "")
        api_key = ep.get("api_key", "not-needed") or "not-needed"
        temperature = float(ep.get("temperature", 0.0))
        _endpoint_policy().check_send(base_url, api_key)   # AUD-03: no key over http
        log.info("Loading ChatOpenAI: base_url=%s model=%s", base_url, model)
        from langchain_openai import ChatOpenAI
        p = _params()
        return _limited_openai_class()(
            base_url=base_url or None,
            api_key=api_key,
            model=model,
            temperature=temperature,
            request_timeout=p.langchain_timeout_seconds(base_url, model),
            max_retries=0,                            # the SDK would retry a timeout; see LimitedChatOpenAI
            connect_retries=p.LANGCHAIN_MAX_RETRIES,
            max_tokens=p.LANGCHAIN_REPLY_MAX_TOKENS,
            **_pace_kw,
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

    from langgraph.prebuilt import create_react_agent

    llm = load_model(config_path, model_override)

    return _LimitedAgent(create_react_agent(llm, tools, prompt=system_prompt))


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
