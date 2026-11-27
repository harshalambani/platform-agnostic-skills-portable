"""AUD-01: agent-mode skills send their system prompt to the model.

Uses a fake chat model that records every message list it receives -- no network.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

ROOT = Path(__file__).resolve().parent.parent

from agents import base_agent                                  # noqa: E402


class RecordingModel(BaseChatModel):
    """Replies from a script and records the messages of every call."""

    script: list = []
    seen: list = []

    @property
    def _llm_type(self) -> str:
        return "recording-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        reply = self.script[min(len(self.seen) - 1, len(self.script) - 1)]
        return ChatResult(generations=[ChatGeneration(message=reply)])


@tool
def echo(text: str) -> str:
    """Echo the text back."""
    return f"echo:{text}"


def _final(text="done"):
    return AIMessage(content=text)


def _tool_call():
    return AIMessage(content="", tool_calls=[{"name": "echo", "args": {"text": "hi"}, "id": "call-1"}])


def _model(*script):
    return RecordingModel(script=list(script), seen=[])


def _prompts():
    from agents.skill_26as_journal.agent import SYSTEM_PROMPT as journal
    from agents.skill_cc_transactions.agent import SYSTEM_PROMPT as cc
    from agents.skill_csv_analyzer.agent import SYSTEM_PROMPT as csv
    from agents.skill_parser_generator.agent import SYSTEM_PROMPT as parser
    return {"csv_analyzer": csv, "parser_generator": parser, "26as_journal": journal, "cc_transactions": cc}


def _run(model, prompt, tools=None):
    with patch.object(base_agent, "load_model", return_value=model):
        agent = base_agent.build_agent(tools or [echo], prompt, "config.yaml")
        agent.invoke({"messages": [("user", "hello")]})
    return agent


@pytest.mark.parametrize("skill", ["csv_analyzer", "parser_generator", "26as_journal", "cc_transactions"])
def test_the_model_receives_the_skills_real_prompt_as_the_first_message(skill):
    prompt = _prompts()[skill]
    assert prompt.strip()
    model = _model(_final())
    _run(model, prompt)
    first = model.seen[0]
    assert isinstance(first[0], SystemMessage) and first[0].content == prompt
    assert [type(m).__name__ for m in first] == ["SystemMessage", "HumanMessage"]


@pytest.mark.parametrize("skill", ["csv_analyzer", "parser_generator", "26as_journal", "cc_transactions"])
def test_every_step_of_a_tool_loop_carries_the_prompt_exactly_once(skill):          # NEGATIVE
    prompt = _prompts()[skill]
    model = _model(_tool_call(), _final())
    _run(model, prompt)
    assert len(model.seen) == 2                                  # the tool call, then the final answer
    for step, msgs in enumerate(model.seen):
        assert isinstance(msgs[0], SystemMessage) and msgs[0].content == prompt, step
        assert sum(isinstance(m, SystemMessage) for m in msgs) == 1, step      # not duplicated per step
        assert sum(prompt in str(m.content) for m in msgs) == 1, step
    assert any(type(m).__name__ == "ToolMessage" for m in model.seen[1])      # the loop really ran


@pytest.mark.parametrize("bad", ["", None, "   "])
def test_an_empty_or_missing_prompt_is_refused_before_any_model_is_loaded(bad):      # NEGATIVE
    with patch.object(base_agent, "load_model") as load:
        with pytest.raises(ValueError, match="system prompt"):
            base_agent.build_agent([echo], bad, "config.yaml")
    load.assert_not_called()


def test_the_agent_has_no_dead_system_prompt_attribute():                           # NEGATIVE
    agent = _run(_model(_final()), "Be brief.")
    assert not hasattr(agent, "_system_prompt")


def test_run_direct_still_sends_a_system_message():
    model = _model(_final("ok"))
    with patch.object(base_agent, "load_model", return_value=model):
        assert base_agent.run_direct("question", "Be brief.", "config.yaml") == "ok"
    assert isinstance(model.seen[0][0], SystemMessage) and model.seen[0][0].content == "Be brief."
