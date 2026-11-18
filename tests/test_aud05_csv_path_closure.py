"""AUD-05: the CSV Analyzer tools work only on the file chosen for the run.

The five tools are built per file; none takes a path argument, so the model
cannot point them at another file. A fake chat model scripts the tool calls;
nothing here touches the network.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pandas as pd
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import base_agent                                    # noqa: E402
from agents.skill_csv_analyzer import agent as csv_agent         # noqa: E402
from agents.skill_csv_analyzer import tools as csv_tools         # noqa: E402

TOOL_NAMES = ["describe_csv", "aggregate_csv", "value_counts_csv",
              "filter_count_csv", "sort_head_csv"]

# Per-tool arguments that are valid for the synthetic file below.
CALLS = {
    "describe_csv": {},
    "aggregate_csv": {"metric": "amount", "agg": "sum", "group_by": ["category"]},
    "value_counts_csv": {"column": "category"},
    "filter_count_csv": {"filters": [{"column": "amount", "op": ">", "value": 0}]},
    "sort_head_csv": {"sort_by": ["amount"], "ascending": False, "n": 3},
}


@pytest.fixture()
def files(tmp_path):
    bound = tmp_path / "bound.csv"
    other = tmp_path / "other.csv"
    pd.DataFrame({"category": ["alpha", "beta", "alpha"], "amount": [10, 20, 30]}).to_csv(bound, index=False)
    pd.DataFrame({"category": ["ZZSECRETCAT", "ZZSECRETCAT"], "amount": [987654, 123456]}).to_csv(other, index=False)
    return bound, other


def _tools(path):
    return {t.name: t for t in csv_tools._make_tools(str(path))}


def test_each_tool_has_no_path_parameter_in_its_schema(files):
    bound, _ = files
    tools = _tools(bound)
    assert sorted(tools) == sorted(TOOL_NAMES)
    for name, t in tools.items():
        args = set(t.args)
        assert not {"csv_path", "path", "file", "filename"} & args, (name, args)
        assert "csv_path" not in str(t.tool_call_schema.model_json_schema()), name


def test_the_tools_answer_about_the_bound_file(files):
    bound, _ = files
    tools = _tools(bound)
    assert "3 rows" in tools["describe_csv"].invoke({})
    assert "alpha" in tools["value_counts_csv"].invoke({"column": "category"})
    assert "3 of 3" in tools["filter_count_csv"].invoke(CALLS["filter_count_csv"])


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_an_extra_path_argument_is_ignored_and_never_reads_the_other_file(files, name):     # NEGATIVE
    # Chosen behaviour: the extra argument is IGNORED (the tool schema has no
    # such field, so it is dropped before the tool runs) rather than rejected.
    # Rejecting would turn a hostile or confused call into a failed step the
    # model retries; ignoring it still guarantees only the bound file is read.
    bound, other = files
    tools = _tools(bound)
    for extra in ("csv_path", "path"):
        out = tools[name].invoke({**CALLS[name], extra: str(other)})
        assert "ZZSECRETCAT" not in out and "987654" not in out and "123456" not in out, (name, extra)
        assert "ERROR" not in out, (name, extra, out)


@pytest.mark.parametrize("name", TOOL_NAMES)
def test_the_bound_path_is_fixed_when_the_tools_are_built(files, name, monkeypatch):        # NEGATIVE
    bound, other = files
    tools = _tools(bound)
    monkeypatch.chdir(other.parent)                  # a later working-directory change cannot move it
    out = tools[name].invoke(CALLS[name])
    assert "ZZSECRETCAT" not in out
    assert "ERROR" not in out


class ScriptedModel(BaseChatModel):
    script: list = []
    seen: list = []

    @property
    def _llm_type(self) -> str:
        return "scripted-fake"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(list(messages))
        reply = self.script[min(len(self.seen) - 1, len(self.script) - 1)]
        return ChatResult(generations=[ChatGeneration(message=reply)])


def _call(name, args, cid):
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": cid}])


def test_a_model_that_asks_for_another_file_only_ever_opens_the_bound_file(files, tmp_path):     # NEGATIVE
    bound, other = files
    script = [
        _call("describe_csv", {"csv_path": str(other)}, "c1"),
        _call("value_counts_csv", {"csv_path": str(other), "path": str(other), "column": "category"}, "c2"),
        _call("sort_head_csv", {"path": str(other), "sort_by": ["amount"]}, "c3"),
        AIMessage(content="done"),
    ]
    model = ScriptedModel(script=script, seen=[])
    opened: list[str] = []
    real = pd.read_csv

    def spy(path, *a, **k):
        opened.append(str(Path(path).resolve()))
        return real(path, *a, **k)

    out_md = tmp_path / "out.md"
    with patch.object(base_agent, "load_model", return_value=model), \
            patch.object(pd, "read_csv", spy):
        csv_agent.run(str(bound), "what is in the file?", str(out_md), "config.yaml")

    assert opened, "the tools never opened anything"
    assert set(opened) == {str(bound.resolve())}
    results = [str(m.content) for msgs in model.seen for m in msgs if type(m).__name__ == "ToolMessage"]
    assert len(results) >= 3
    assert not any("ZZSECRETCAT" in r or "987654" in r for r in results)
    # the model is no longer told the path at all
    first_user = [m for m in model.seen[0] if type(m).__name__ == "HumanMessage"][0].content
    assert str(bound) not in first_user and bound.name not in first_user
    assert "what is in the file?" in first_user


def test_agent_md_shows_no_csv_path_in_any_tool_signature():                              # NEGATIVE
    text = (ROOT / "src" / "agents" / "skill_csv_analyzer" / "AGENT.md").read_text(encoding="utf-8")
    assert "csv_path" not in text
    for name in TOOL_NAMES:
        assert f"`{name}(csv_path" not in text


def test_the_module_no_longer_exposes_path_taking_tools():                                # NEGATIVE
    for name in TOOL_NAMES:
        assert not hasattr(csv_tools, name), name
    assert not hasattr(csv_agent, "TOOLS")
