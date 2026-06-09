"""
tests/test_csv_analyzer_security.py — Security regression tests for MP-01.

Tracker finding #1 (High): `query_csv()` ran LLM-authored strings through
`eval(expression, {"__builtins__": {}}, {"df": df, "pd": pd})`, guarded only
by a regex blocklist. That blocklist was bypassable (string-concat assembly of
blocked names, un-blocked dunders/builtins, object-graph traversal to
os/subprocess), so a malicious CSV could steer the LLM into emitting an escape
expression -> arbitrary code execution / file read-write.

Fix: `query_csv` and the `eval()` call are gone. The skill now exposes only
parameterized, allowlisted operations (`aggregate_csv`, `value_counts_csv`,
`filter_count_csv`, `sort_head_csv`) whose arguments are validated against the
DataFrame's real columns and closed enums before pandas ever runs. No string
supplied by the LLM (however a hostile CSV might shape it) is ever interpreted
as code — there is no `eval`/`exec`/`compile` left to escape from.

These tests feed the exact attack strings from
docs/security/2026-06-08-miniproject-01-csv-eval-rce.md into every string-typed
argument of every tool, and assert each one comes back as a clean validation
error with zero side effects (no file created, no command run, no traceback
leak of internals).

Run with:
    cd src && python -m pytest ../tests/test_csv_analyzer_security.py -v
"""
from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
TOOLS_PATH = SRC / "agents" / "skill_csv_analyzer" / "tools.py"
SALES_CSV = str(FIXTURES / "sales.csv")


# ---------------------------------------------------------------------------
# Load the tool functions without importing langchain_core (fast, isolated).
# ---------------------------------------------------------------------------

@pytest.fixture()
def tools():
    source = TOOLS_PATH.read_text()
    source = source.replace("from langchain_core.tools import tool", "")
    source = re.sub(r"^@tool\s*$", "", source, flags=re.MULTILINE)
    ns: dict = {}
    exec(compile(source, "tools.py", "exec"), ns)
    return ns


# Attack strings drawn directly from the MP-01 spec's test plan. Each one is
# tried as: a column / group_by / sort_by name, an agg name, a filter op, and
# a filter value — i.e. every place an LLM-controlled string can land.
ATTACK_STRINGS = [
    # 1. __class__/__mro__/__subclasses__ walk to a subprocess/os class
    "().__class__.__mro__[1].__subclasses__()",
    "df.__class__.__mro__[1].__subclasses__()[133].__init__.__globals__['system']('id')",
    "{}.__class__.__mro__[1].__subclasses__()",
    # 2. string-concat bypass that defeats a regex looking for the literal '__class__'
    "df.__getattribute__('__cla' + 'ss__')",
    "getattr(df, '__cla' + 'ss__')",
    # 3. IO / persistence attempts
    "pd.read_pickle('x')",
    "pd.read_csv('/etc/passwd')",
    "df.to_csv('/tmp/pwned.csv')",
    "df.to_pickle('/tmp/pwned.pkl')",
    # 4. builtin / getattr escape attempts
    "__import__('os').system('id > /tmp/pwned')",
    "getattr(df, 'to_csv')('/tmp/pwned.csv')",
    "vars(df)",
    "type(df).__mro__",
]


class TestNoCodeExecutionSurface:
    """The module must not contain anything an attack string could reach."""

    def test_no_eval_exec_compile_runpy(self):
        """
        Use AST inspection rather than raw string search so that mentions of
        forbidden names inside the module docstring (historical context) do not
        cause false positives.  The actual constraint is that no *call* to
        eval/exec/compile/__import__ and no *import* of runpy may appear as
        live executable code.
        """
        import ast as _ast
        source = TOOLS_PATH.read_text()
        tree = _ast.parse(source)
        _FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                    pytest.fail(
                        f"Found {node.func.id!r}() call in tools.py at line "
                        f"{getattr(node, 'lineno', '?')} — the module must not "
                        "be able to interpret a string as code."
                    )
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    assert "runpy" not in alias.name, "runpy must not be imported in tools.py"
            elif isinstance(node, _ast.ImportFrom):
                if node.module and "runpy" in node.module:
                    pytest.fail("runpy must not be imported in tools.py")

    def test_query_csv_tool_is_gone(self, tools):
        assert "query_csv" not in tools
        assert "_validate_expression" not in tools
        assert "_BLOCKED_PATTERNS" not in tools


class TestAttackStringsAreInertEverywhere:
    """
    Every attack string, in every argument position, must come back as a
    plain validation error — never raise, never touch the filesystem, never
    spawn a process, never leak a traceback.
    """

    @pytest.fixture(autouse=True)
    def _watch_for_side_effects(self, tools, monkeypatch, tmp_path):
        self.tools = tools
        self.commands_run: list[str] = []
        self.files_written: list[str] = []

        # If anything ever reaches a process-spawning primitive, record it
        # (and don't actually run it).
        monkeypatch.setattr(os, "system", lambda cmd: self.commands_run.append(cmd) or 0)
        monkeypatch.setattr(os, "popen", lambda cmd, *a, **kw: (_ for _ in ()).throw(AssertionError(f"os.popen called: {cmd}")))

        # Track file creation: nothing the tools do should write anywhere.
        self._tmp_path = tmp_path
        self._before = set(tmp_path.iterdir()) if tmp_path.exists() else set()

    def _assert_clean(self, result: str):
        assert isinstance(result, str)
        assert "ERROR" in result
        # No leaking of internal exception types/tracebacks that could hint
        # at exploitable internals.
        assert "Traceback" not in result
        assert self.commands_run == [], f"A command was executed: {self.commands_run}"
        after = set(self._tmp_path.iterdir()) if self._tmp_path.exists() else set()
        assert after == self._before, "A file was created as a side effect"

    @pytest.mark.parametrize("attack", ATTACK_STRINGS)
    def test_as_metric_and_agg(self, attack):
        result = self.tools["aggregate_csv"](SALES_CSV, metric=attack, agg="sum")
        self._assert_clean(result)
        result = self.tools["aggregate_csv"](SALES_CSV, metric="revenue", agg=attack)
        self._assert_clean(result)

    @pytest.mark.parametrize("attack", ATTACK_STRINGS)
    def test_as_group_by(self, attack):
        result = self.tools["aggregate_csv"](SALES_CSV, metric="revenue", agg="sum", group_by=[attack])
        self._assert_clean(result)

    @pytest.mark.parametrize("attack", ATTACK_STRINGS)
    def test_as_value_counts_column(self, attack):
        result = self.tools["value_counts_csv"](SALES_CSV, column=attack)
        self._assert_clean(result)

    @pytest.mark.parametrize("attack", ATTACK_STRINGS)
    def test_as_filter_column(self, attack):
        result = self.tools["filter_count_csv"](SALES_CSV, filters=[{"column": attack, "op": "==", "value": 1}])
        self._assert_clean(result)

    @pytest.mark.parametrize("attack", ATTACK_STRINGS)
    def test_as_filter_op(self, attack):
        result = self.tools["filter_count_csv"](SALES_CSV, filters=[{"column": "quantity", "op": attack, "value": 1}])
        self._assert_clean(result)

    @pytest.mark.parametrize("attack", ATTACK_STRINGS)
    def test_as_filter_value(self, attack):
        # A filter *value* is data, never code — it should either fail to
        # match (valid op, just no rows) or be handled as an opaque string.
        # It must never be evaluated.
        result = self.tools["filter_count_csv"](
            SALES_CSV, filters=[{"column": "region", "op": "==", "value": attack}]
        )
        assert isinstance(result, str)
        assert "Traceback" not in result
        assert self.commands_run == [], f"A command was executed: {self.commands_run}"
        # A simple equality filter against a non-matching string is valid
        # input — it returns "0 of 10 rows match", not an ERROR. Either a
        # clean match-count or a clean ERROR is acceptable; an exception or
        # side effect is not.
        assert ("of 10 rows match" in result) or ("ERROR" in result)

    @pytest.mark.parametrize("attack", ATTACK_STRINGS)
    def test_as_sort_by_and_columns(self, attack):
        result = self.tools["sort_head_csv"](SALES_CSV, sort_by=[attack])
        self._assert_clean(result)
        result = self.tools["sort_head_csv"](SALES_CSV, sort_by=["revenue"], columns=[attack])
        self._assert_clean(result)

    @pytest.mark.parametrize("attack", ATTACK_STRINGS)
    def test_as_csv_path(self, attack):
        # Even as the path argument, an attack string is just a (nonexistent)
        # path — _load_csv raises FileNotFoundError, caught and reported.
        for fn, kwargs in [
            (self.tools["describe_csv"], {}),
            (self.tools["aggregate_csv"], {"metric": "revenue", "agg": "sum"}),
            (self.tools["value_counts_csv"], {"column": "region"}),
            (self.tools["filter_count_csv"], {"filters": [{"column": "region", "op": "==", "value": "x"}]}),
            (self.tools["sort_head_csv"], {"sort_by": ["revenue"]}),
        ]:
            result = fn(attack, **kwargs)
            self._assert_clean(result)


class TestFunctionalParityStillWorks:
    """The legitimate analytical use cases from the old query_csv examples."""

    def test_groupby_sum(self, tools):
        result = tools["aggregate_csv"](SALES_CSV, metric="revenue", agg="sum", group_by=["region"])
        assert "North" in result and "3945" in result

    def test_groupby_mean(self, tools):
        result = tools["aggregate_csv"](SALES_CSV, metric="revenue", agg="mean", group_by=["region"])
        assert "ERROR" not in result

    def test_filtered_row_count(self, tools):
        result = tools["filter_count_csv"](SALES_CSV, filters=[{"column": "quantity", "op": ">", "value": 100}])
        assert "5 of 10" in result

    def test_value_counts_top_n(self, tools):
        result = tools["value_counts_csv"](SALES_CSV, column="product", top_n=3)
        assert "Widget A" in result

    def test_sort_and_head(self, tools):
        result = tools["sort_head_csv"](SALES_CSV, sort_by=["revenue"], n=3, ascending=False)
        assert "2100" in result

    def test_describe(self, tools):
        result = tools["describe_csv"](SALES_CSV)
        assert "10 rows" in result

    def test_unknown_column_clean_error(self, tools):
        result = tools["aggregate_csv"](SALES_CSV, metric="not_a_column", agg="sum")
        assert "ERROR" in result and "Traceback" not in result

    def test_unknown_agg_clean_error(self, tools):
        result = tools["aggregate_csv"](SALES_CSV, metric="revenue", agg="not_an_agg")
        assert "ERROR" in result and "Traceback" not in result

    def test_unknown_op_clean_error(self, tools):
        result = tools["filter_count_csv"](SALES_CSV, filters=[{"column": "revenue", "op": "not_an_op", "value": 1}])
        assert "ERROR" in result and "Traceback" not in result

    def test_row_cap_holds(self, tools):
        # sales.csv only has 10 rows, but ask for far more than the 50-row cap
        # and confirm the cap logic doesn't error and reports a sane count.
        result = tools["sort_head_csv"](SALES_CSV, sort_by=["revenue"], n=10_000)
        assert "ERROR" not in result
        # 10 data rows -> at most 10 lines of output, well under the 50 cap.
        assert result.count("\n") < 60
