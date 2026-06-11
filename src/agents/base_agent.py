"""
base_agent.py — shared model loader, LangGraph agent builder, and direct
(non-agent) chat completion.

All skills import this. To switch models, edit config.yaml or pass
model_override at runtime (used by the --model CLI flag).

Three execution modes:
    build_agent()  — LangGraph ReAct agent with tool-calling (existing).
    run_direct()   — Simple prompt → LLM → response. No tools, no agent
                     loop. For skills that are pure prompt-in/text-out
                     (summarisation, translation, etc.).

Progress streaming (C4):
    When a progress queue is set via set_progress_queue(), build_agent()
    returns a _StreamingAgentWrapper. The wrapper intercepts .invoke()
    and uses .stream() internally, pushing intermediate events (tool calls,
    LLM reasoning steps) to the queue so the UI can display live progress.
    Individual skill files require NO changes — the wrapper is transparent.
"""
from __future__ import annotations

import queue
import threading
from pathlib import Path
from typing import Any

import yaml
from langgraph.prebuilt import create_react_agent


# ---------------------------------------------------------------------------
# Progress queue — per-thread, so concurrent skill runs don't collide.
# ---------------------------------------------------------------------------

_progress = threading.local()


def set_progress_queue(q: queue.Queue | None) -> None:
    """Set a progress queue for the current thread (called by _runner.py)."""
    _progress.queue = q


def get_progress_queue() -> queue.Queue | None:
    """Get the current thread's progress queue, or None."""
    return getattr(_progress, "queue", None)


# ---------------------------------------------------------------------------
# Streaming agent wrapper.
# ---------------------------------------------------------------------------

class _StreamingAgentWrapper:
    """
    Wraps a LangGraph CompiledStateGraph so that .invoke() internally
    uses .stream(), pushing intermediate events to a queue.

    Skills call ``agent.invoke({"messages": [...]})`` as before — this
    wrapper intercepts that call transparently.
    """

    def __init__(self, agent, progress_queue: queue.Queue):
        self._agent = agent
        self._q = progress_queue
        self._step = 0

    # -- Preserve any attribute access the skill might need. ----------------
    def __getattr__(self, name: str):
        return getattr(self._agent, name)

    def invoke(self, inputs: dict, config: dict | None = None, **kwargs) -> dict:
        """
        Stream the agent execution, push progress events to the queue,
        and return the final state dict (same contract as the real
        .invoke()).
        """
        final_state: dict[str, Any] = {}

        for chunk in self._agent.stream(
            inputs, config=config, stream_mode="updates", **kwargs
        ):
            final_state.update(chunk)

            # --- Parse the chunk and push a human-readable event. ----------
            # LangGraph's create_react_agent yields chunks keyed by node
            # name: "agent" for LLM steps, "tools" for tool execution.

            if "agent" in chunk:
                self._step += 1
                messages = chunk["agent"].get("messages", [])
                for msg in messages:
                    # Check if the LLM decided to call tools.
                    tool_calls = getattr(msg, "tool_calls", None)
                    if tool_calls:
                        for tc in tool_calls:
                            name = tc.get("name", "?")
                            args = tc.get("args", {})
                            # Truncate args display for readability.
                            args_str = str(args)
                            if len(args_str) > 200:
                                args_str = args_str[:200] + "…"
                            self._q.put({
                                "type": "tool_call",
                                "step": self._step,
                                "tool": name,
                                "args": args_str,
                            })
                    else:
                        # LLM response without tool calls — final answer
                        # or intermediate reasoning.
                        content = getattr(msg, "content", "")
                        snippet = (content[:150] + "…") if len(content) > 150 else content
                        self._q.put({
                            "type": "llm_response",
                            "step": self._step,
                            "snippet": snippet,
                        })

            elif "tools" in chunk:
                messages = chunk["tools"].get("messages", [])
                for msg in messages:
                    name = getattr(msg, "name", "?")
                    content = getattr(msg, "content", "")
                    snippet = (content[:200] + "…") if len(content) > 200 else content
                    self._q.put({
                        "type": "tool_result",
                        "step": self._step,
                        "tool": name,
                        "snippet": snippet,
                    })

        # Reconstruct the final state in the same shape .invoke() returns.
        # LangGraph's .stream() yields per-node dicts; accumulating them
        # gives us the full state.  But for create_react_agent the
        # canonical final state is {"messages": [...]}.  We need to
        # reconstruct that from the streamed chunks.
        #
        # The safest approach: call .get_state() if available, otherwise
        # fall back to the accumulated dict.  In practice the skill only
        # reads result["messages"][-1].content.
        if hasattr(self._agent, "get_state"):
            try:
                state = self._agent.get_state(config or {})
                if hasattr(state, "values"):
                    return state.values
            except Exception:  # noqa: BLE001 — intentional (finding #9)
                # SECURITY NOTE (finding #9): get_state() is an optional API
                # compatibility shim — its absence or a version mismatch raises
                # here.  The fallback below reconstructs the final state from the
                # accumulated streaming chunks, so no result is lost.
                pass

        # Fallback: re-invoke without streaming (shouldn't normally happen).
        # This is a safety net — if get_state doesn't work, we fall back
        # to the last chunk which for create_react_agent contains the
        # final agent node output with the full message list.
        if "agent" in final_state and "messages" in final_state.get("agent", {}):
            return final_state["agent"]

        # Last resort — return the accumulated state as-is.
        return final_state


def load_model(config_path: str = "config.yaml", model_override: str = None):
    """
    Load the LLM specified in config.yaml.

    Args:
        config_path:    Path to config.yaml.
        model_override: If set, overrides the default_model in config.yaml.
                        Pass the model name exactly as Ollama/vLLM knows it
                        (e.g. 'llama3.1', 'qwen3', 'phi4-mini').
    """
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8"))
    provider = cfg["provider"]

    if provider == "ollama":
        from langchain_ollama import ChatOllama
        oc = cfg["ollama"]
        # Resolve model: CLI override > config default_model
        model_name = model_override or oc.get("default_model", "llama3.1")
        print(f"[base_agent] Using Ollama model: {model_name}")
        return ChatOllama(
            base_url=oc["base_url"],
            model=model_name,
            temperature=oc["temperature"],
        )

    elif provider == "openai_compatible":
        from langchain_openai import ChatOpenAI
        oc = cfg["openai_compatible"]
        model_name = model_override or oc.get("default_model", "gemma-4")
        print(f"[base_agent] Using OpenAI-compatible model: {model_name}")
        return ChatOpenAI(
            base_url=oc["base_url"],
            api_key=oc["api_key"],
            model=model_name,
            temperature=oc["temperature"],
        )

    else:
        raise ValueError(
            f"Unknown provider '{provider}' in config.yaml. "
            "Use 'ollama' or 'openai_compatible'."
        )


def build_agent(
    tools: list,
    system_prompt: str,
    config_path: str = "config.yaml",
    model_override: str = None,
):
    """
    Build a LangGraph ReAct agent with the given tools and system prompt.

    If a progress queue has been set (via set_progress_queue), the returned
    object is a _StreamingAgentWrapper whose .invoke() streams intermediate
    events to the queue.  Otherwise returns the raw LangGraph agent.

    Args:
        tools:          List of @tool-decorated functions.
        system_prompt:  The agent's system prompt (loaded from AGENT.md).
        config_path:    Path to config.yaml.
        model_override: Optional model name to use instead of config default.
                        e.g. 'llama3.1', 'qwen3', 'phi4-mini'
    """
    model = load_model(config_path, model_override)

    # 'prompt' replaced 'state_modifier' in LangGraph >= 0.2.x
    try:
        agent = create_react_agent(
            model=model,
            tools=tools,
            prompt=system_prompt,
        )
    except TypeError:
        # Fallback for older LangGraph installs
        agent = create_react_agent(
            model=model,
            tools=tools,
            state_modifier=system_prompt,
        )

    # Wrap with streaming if a progress queue is active.
    q = get_progress_queue()
    if q is not None:
        return _StreamingAgentWrapper(agent, q)
    return agent


def run_direct(
    user_message: str,
    system_prompt: str | None = None,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    Simple prompt → LLM → response.  No tools, no agent loop.

    Use this for skills whose mode is "direct" — they don't need tool-calling
    and benefit from lower latency and broader model compatibility (any model
    that supports chat completions works, no function-calling required).

    Args:
        user_message:   The user's input (may include file contents, etc.).
        system_prompt:  Optional system prompt (loaded from AGENT.md).
        config_path:    Path to config.yaml.
        model_override: Optional model name override.

    Returns:
        The model's text response.
    """
    q = get_progress_queue()
    if q is not None:
        q.put({"type": "llm_start", "step": 1, "snippet": "LLM is thinking…"})

    model = load_model(config_path, model_override)
    messages = []
    if system_prompt:
        messages.append(("system", system_prompt))
    messages.append(("user", user_message))
    response = model.invoke(messages)

    if q is not None:
        snippet = response.content[:150] + "…" if len(response.content) > 150 else response.content
        q.put({"type": "llm_response", "step": 1, "snippet": snippet})

    return response.content
