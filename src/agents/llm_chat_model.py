"""AUD-07 P4: the LangChain chat model that sends through the single AI client.

``ClientChatModel`` is a LangChain ``BaseChatModel`` whose every request goes
through ``agents.llm_client.chat_messages``: the same request builder, key rule,
pacing, error words and redaction as every other caller. The three tool-using
skills (CSV analyzer, parser generator, 26AS journal and the card transactions
agent) reach the model through it.

The limits that used to sit on the LangChain classes live here:
  * a total time limit (the client enforces it on the whole reply);
  * connection failures are retried a fixed number of times, a timeout never;
  * the reply cap and the plain timeout message.

Only agents/base_agent.py imports this module (LangChain is imported here and
nowhere in llm_client.py).
"""
from __future__ import annotations

import logging
import time
from typing import Any, List, Optional

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict

try:
    from agents import llm_client, llm_model_params
except ImportError:  # CLI / bare-import usage
    import llm_client  # type: ignore[no-redef]
    import llm_model_params  # type: ignore[no-redef]

log = logging.getLogger(__name__)


def _retry_pause(seconds: float) -> None:
    time.sleep(seconds)


def _text_of(content: Any) -> str:
    """The text of a message whose content is a string or a list of blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, dict) and block.get("type", "text") == "text":
                parts.append(str(block.get("text", "")))
        return "".join(parts)
    return "" if content is None else str(content)


def to_neutral(message: BaseMessage) -> dict:
    """One LangChain message as the client's neutral dict."""
    kind = getattr(message, "type", "")
    text = _text_of(message.content)
    if kind == "system":
        return {"role": "system", "content": text}
    if kind == "ai":
        out: dict = {"role": "assistant", "content": text}
        calls = getattr(message, "tool_calls", None) or []
        if calls:
            out["tool_calls"] = [{"id": c.get("id") or "", "name": c["name"], "args": c.get("args") or {}}
                                 for c in calls]
        return out
    if kind == "tool":
        return {"role": "tool", "content": text, "tool_call_id": getattr(message, "tool_call_id", "") or "",
                "name": getattr(message, "name", None) or ""}
    return {"role": "user", "content": text}


class ClientChatModel(BaseChatModel):
    """A chat model that sends through ``llm_client.chat_messages``."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    endpoint: llm_client.Endpoint
    connect_retries: int = llm_model_params.LANGCHAIN_MAX_RETRIES

    @property
    def _llm_type(self) -> str:
        return "pask-client"

    @property
    def _identifying_params(self) -> dict:
        return {"provider": self.endpoint.provider, "model": self.endpoint.model,
                "base_url": self.endpoint.base_url}

    def bind_tools(self, tools, *, tool_choice: Optional[str] = None, **kwargs: Any):
        """Tools go to the service as OpenAI-style function schemas, which both
        Ollama and OpenAI-style services accept."""
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(self, messages: List[BaseMessage], stop: Optional[List[str]] = None,
                  run_manager: Any = None, **kwargs: Any) -> ChatResult:
        ep = self.endpoint
        neutral = llm_client.wrap_messages([to_neutral(m) for m in messages])   # AUD-16
        tools = kwargs.get("tools") or None
        attempt = 0
        while True:
            try:
                done = llm_client.chat_messages(ep, neutral, tools, stop=stop, total_limit=ep.timeout,
                                                paced=(attempt == 0))
                break
            except llm_client.RateLimited as e:
                raise llm_client.rate_limit_error(ep, e) from None
            except llm_client.LlmError as e:
                kind, status = e.failure.kind, e.failure.status
                if kind == "timeout" and status is None:           # never retried
                    raise llm_model_params.LlmLimitError(llm_model_params.timeout_message(ep.timeout)) from None
                if kind == "connection" and attempt < self.connect_retries:
                    attempt += 1
                    log.warning("connection to the model failed; retry %d of %d", attempt, self.connect_retries)
                    _retry_pause(1)
                    continue
                raise
        calls = [{"name": c["name"], "args": c["args"], "id": c["id"], "type": "tool_call"}
                 for c in done.tool_calls]
        meta = {"finish_reason": done.finish_reason, "model_name": ep.model}
        message = AIMessage(content=str(done.text), tool_calls=calls, response_metadata=meta)
        return ChatResult(generations=[ChatGeneration(message=message)])
