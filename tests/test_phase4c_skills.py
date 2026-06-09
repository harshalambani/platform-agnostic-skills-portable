"""
tests/test_phase4c_skills.py — Unit tests for Phase 4C skills.

Tests everything that doesn't require an LLM endpoint:
  - Registry discovery
  - File reading and truncation (summarizer)
  - Input validation (translator)
  - CSV tools: describe_csv, query_csv, safety guards

Run with:
    cd src && python -m pytest ../tests/test_phase4c_skills.py -v
"""
from __future__ import annotations

import re
import textwrap
from pathlib import Path

import pandas as pd
import pytest
import yaml

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
SKILLS_DIR = SRC / "agents"


# ===================================================================
# 1. Registry discovery
# ===================================================================

class TestRegistryDiscovery:
    """Verify all three 4C skills are discovered correctly."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import sys
        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))
        from agents.registry import discover
        self.skills = {s.name: s for s in discover(refresh=True)}

    @pytest.mark.parametrize("name,mode,category", [
        ("summarize", "direct", "general"),
        ("translate", "direct", "general"),
        ("CSV Analyzer", "agent", "general"),
    ])
    def test_skill_discovered(self, name, mode, category):
        assert name in self.skills, f"{name} not discovered"
        s = self.skills[name]
        assert s.mode == mode
        assert s.category == category

    def test_summarize_inputs(self):
        s = self.skills["summarize"]
        assert len(s.inputs) == 1
        assert s.inputs[0].name == "file_path"
        assert s.inputs[0].type == "file"
        assert ".pdf" in s.inputs[0].file_types

    def test_translate_inputs(self):
        s = self.skills["translate"]
        assert len(s.inputs) == 3
        names = [i.name for i in s.inputs]
        assert names == ["text", "source_lang", "target_lang"]
        # source_lang is optional
        src_lang = next(i for i in s.inputs if i.name == "source_lang")
        assert src_lang.required is False

    def test_csv_analyzer_inputs(self):
        s = self.skills["CSV Analyzer"]
        assert len(s.inputs) == 2
        names = [i.name for i in s.inputs]
        assert names == ["csv_path", "question"]
        assert s.inputs[0].type == "file"
        assert s.inputs[1].type == "text"

    @pytest.mark.parametrize("name", ["summarize", "translate", "CSV Analyzer"])
    def test_no_native_binaries_required(self, name):
        s = self.skills[name]
        assert s.requires.native_binaries == ()

    @pytest.mark.parametrize("name", ["summarize", "translate", "CSV Analyzer"])
    def test_run_args_match_inputs(self, name):
        """Every {inputs.X} token in run_args must reference a declared input."""
        s = self.skills[name]
        input_names = {i.name for i in s.inputs}
        for param, tmpl in s.run_args.items():
            if "{inputs." in tmpl:
                ref = tmpl.split("{inputs.")[1].rstrip("}")
                assert ref in input_names, (
                    f"run_args.{param} references '{ref}' but inputs are {input_names}"
                )


# ===================================================================
# 2. Manifest YAML validation
# ===================================================================

class TestManifestYAML:
    """Validate skill.yaml files parse and have required fields."""

    REQUIRED_FIELDS = {"name", "display_name", "description", "mode", "entry_point"}

    @pytest.mark.parametrize("skill_dir", [
        "skill_summarize",
        "skill_translate",
        "skill_csv_analyzer",
    ])
    def test_yaml_parses(self, skill_dir):
        path = SKILLS_DIR / skill_dir / "skill.yaml"
        assert path.is_file(), f"{path} does not exist"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert isinstance(raw, dict)
        missing = self.REQUIRED_FIELDS - set(raw.keys())
        assert not missing, f"Missing fields: {missing}"


# ===================================================================
# 3. Summarizer — file reading and truncation
# ===================================================================

class TestSummarizerHelpers:
    """Test _read_text, _read_file, and _truncate without LLM."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        import sys
        if str(SRC) not in sys.path:
            sys.path.insert(0, str(SRC))
        # Import helpers directly — avoid triggering base_agent (needs langgraph).
        # We parse the module file and extract the functions manually.
        self._mod_path = SKILLS_DIR / "skill_summarize" / "agent.py"

    def _exec_module_funcs(self):
        """Load only the helper functions, not the full module (avoids LLM deps)."""
        source = self._mod_path.read_text(encoding="utf-8")
        # Strip the imports that need langgraph
        source = source.replace("from agents.base_agent import run_direct", "")
        # Remove SYSTEM_PROMPT line (needs file read at import)
        source = re.sub(
            r'^SYSTEM_PROMPT\s*=.*$', 'SYSTEM_PROMPT = "test"',
            source, flags=re.MULTILINE,
        )
        ns = {"__file__": str(self._mod_path)}
        exec(compile(source, str(self._mod_path), "exec"), ns)
        return ns

    def test_read_text_file(self):
        ns = self._exec_module_funcs()
        text = ns["_read_text"](FIXTURES / "sample_doc.txt")
        assert "Quarterly Business Review" in text
        assert len(text) > 100

    def test_read_file_dispatches_to_text(self):
        ns = self._exec_module_funcs()
        text = ns["_read_file"](FIXTURES / "sample_doc.txt")
        assert "Widget A" in text

    def test_truncate_no_op_for_short_text(self):
        ns = self._exec_module_funcs()
        text = "Hello world this is a short text"
        result, was_truncated = ns["_truncate"](text, max_words=100)
        assert result == text
        assert was_truncated is False

    def test_truncate_works_for_long_text(self):
        ns = self._exec_module_funcs()
        text = " ".join(["word"] * 200)
        result, was_truncated = ns["_truncate"](text, max_words=50)
        assert len(result.split()) == 50
        assert was_truncated is True

    def test_empty_file_raises(self):
        ns = self._exec_module_funcs()
        text = ns["_read_text"](FIXTURES / "empty.txt")
        assert text.strip() == ""


# ===================================================================
# 4. Translator — input validation
# ===================================================================

class TestTranslatorValidation:
    """Test that run() rejects missing inputs before calling the LLM."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        self._mod_path = SKILLS_DIR / "skill_translate" / "agent.py"

    def _exec_module_funcs(self):
        source = self._mod_path.read_text(encoding="utf-8")
        source = source.replace("from agents.base_agent import run_direct", "")
        source = re.sub(
            r'^SYSTEM_PROMPT\s*=.*$', 'SYSTEM_PROMPT = "test"',
            source, flags=re.MULTILINE,
        )
        ns = {"__file__": str(self._mod_path)}
        exec(compile(source, str(self._mod_path), "exec"), ns)
        return ns

    def test_empty_text_raises(self):
        ns = self._exec_module_funcs()
        with pytest.raises(ValueError, match="No text provided"):
            ns["run"](
                text="   ", source_lang="English", target_lang="Hindi",
                output_path="/tmp/out.txt",
            )

    def test_empty_target_lang_raises(self):
        ns = self._exec_module_funcs()
        with pytest.raises(ValueError, match="Target language is required"):
            ns["run"](
                text="Hello world", source_lang="English", target_lang="",
                output_path="/tmp/out.txt",
            )


# ===================================================================
# 5. CSV Analyzer — tools
# ===================================================================
#
# NOTE (2026-06-08, Tracker finding #1 / MP-01): the old `query_csv` tool ran
# LLM-authored strings through `eval()` guarded only by a regex blocklist —
# a sandbox-escape RCE (see docs/security/2026-06-08-miniproject-01-csv-eval-rce.md).
# It has been replaced with a fixed set of parameterized, allowlisted
# operations (`aggregate_csv`, `value_counts_csv`, `filter_count_csv`,
# `sort_head_csv`). These tests cover that surface; deeper security/parity
# coverage lives in tests/test_csv_analyzer_security.py.

class TestCSVAnalyzerSafety:
    """Test the structured CSV analyzer tools (no eval, no free-form code)."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        # Extract the tool functions from tools.py without importing
        # langchain_core (keeps this test fast and dependency-light).
        source = (SKILLS_DIR / "skill_csv_analyzer" / "tools.py").read_text()
        # Remove langchain import
        source = source.replace("from langchain_core.tools import tool", "")
        # Remove @tool decorators
        source = re.sub(r"^@tool\s*$", "", source, flags=re.MULTILINE)
        ns = {}
        exec(compile(source, "tools.py", "exec"), ns)
        self.describe_csv = ns["describe_csv"]
        self.aggregate_csv = ns["aggregate_csv"]
        self.value_counts_csv = ns["value_counts_csv"]
        self.filter_count_csv = ns["filter_count_csv"]
        self.sort_head_csv = ns["sort_head_csv"]
        self.load_csv = ns["_load_csv"]
        self.ns = ns

    # -- No eval/exec surface remains --

    def test_no_eval_exec_compile_in_module(self):
        """
        Use AST inspection rather than raw string search so that mentions of
        removed symbols in the module docstring (historical context) do not
        produce false positives.  The real constraint is: no live call to
        eval/exec/compile/__import__, no import of runpy, and no function
        definition named query_csv/_validate_expression.
        """
        import ast as _ast
        source = (SKILLS_DIR / "skill_csv_analyzer" / "tools.py").read_text()
        tree = _ast.parse(source)

        _FORBIDDEN_CALLS = {"eval", "exec", "compile", "__import__"}
        for node in _ast.walk(tree):
            if isinstance(node, _ast.Call):
                if isinstance(node.func, _ast.Name) and node.func.id in _FORBIDDEN_CALLS:
                    raise AssertionError(
                        f"{node.func.id!r}() call found in tools.py at line "
                        f"{getattr(node, 'lineno', '?')} — must not execute code strings"
                    )
            elif isinstance(node, _ast.Import):
                for alias in node.names:
                    assert "runpy" not in alias.name, "runpy must not be imported"
            elif isinstance(node, _ast.ImportFrom):
                if node.module and "runpy" in node.module:
                    raise AssertionError("runpy must not be imported")
            elif isinstance(node, _ast.FunctionDef):
                assert node.name != "query_csv", "query_csv function must not exist in tools.py"
                assert node.name != "_validate_expression", "_validate_expression must not exist"

        assert "_BLOCKED_PATTERNS" not in source, "_BLOCKED_PATTERNS must not exist in tools.py"

    # -- describe_csv tool --

    def test_describe_csv_returns_shape(self):
        result = self.describe_csv(str(FIXTURES / "sales.csv"))
        assert "10 rows" in result
        assert "6 columns" in result

    def test_describe_csv_lists_columns(self):
        result = self.describe_csv(str(FIXTURES / "sales.csv"))
        for col in ["date", "region", "product", "quantity", "unit_price", "revenue"]:
            assert col in result

    def test_describe_csv_shows_head(self):
        result = self.describe_csv(str(FIXTURES / "sales.csv"))
        assert "Widget A" in result

    def test_describe_csv_missing_file(self):
        result = self.describe_csv("/nonexistent/file.csv")
        assert "ERROR" in result

    # -- aggregate_csv tool --

    def test_aggregate_groupby_sum(self):
        result = self.aggregate_csv(str(FIXTURES / "sales.csv"), metric="revenue", agg="sum", group_by=["region"])
        assert "North" in result
        # North: 1050 + 1575 + 1320 = 3945
        assert "3945" in result

    def test_aggregate_overall_mean(self):
        result = self.aggregate_csv(str(FIXTURES / "sales.csv"), metric="revenue", agg="mean")
        # Mean: (1050+1650+1575+1650+1260+742.5+1320+2100+1485+880) / 10 = 1371.25
        assert "1371.25" in result

    def test_aggregate_with_filter(self):
        result = self.aggregate_csv(
            str(FIXTURES / "sales.csv"), metric="revenue", agg="sum",
            filters=[{"column": "region", "op": "==", "value": "North"}],
        )
        assert "3945" in result

    def test_aggregate_unknown_column(self):
        result = self.aggregate_csv(str(FIXTURES / "sales.csv"), metric="nope", agg="sum")
        assert "ERROR" in result
        assert "unknown column" in result.lower()

    def test_aggregate_bad_agg(self):
        result = self.aggregate_csv(str(FIXTURES / "sales.csv"), metric="revenue", agg="hack")
        assert "ERROR" in result
        assert "unsupported agg" in result.lower()

    def test_aggregate_missing_file(self):
        result = self.aggregate_csv("/nonexistent/file.csv", metric="revenue", agg="sum")
        assert "ERROR" in result

    # -- value_counts_csv tool --

    def test_value_counts_top_n(self):
        result = self.value_counts_csv(str(FIXTURES / "sales.csv"), column="product", top_n=1)
        assert "Widget A" in result
        assert "4" in result  # Widget A appears 4 times

    def test_value_counts_unknown_column(self):
        result = self.value_counts_csv(str(FIXTURES / "sales.csv"), column="nope")
        assert "ERROR" in result

    def test_value_counts_caps_at_max_rows(self):
        result = self.value_counts_csv(str(FIXTURES / "sales.csv"), column="product", top_n=999)
        assert "ERROR" not in result

    # -- filter_count_csv tool --

    def test_filter_count_basic(self):
        result = self.filter_count_csv(
            str(FIXTURES / "sales.csv"),
            filters=[{"column": "quantity", "op": ">", "value": 100}],
        )
        # rows with qty > 100: 200, 150, 120, 200, 180 = 5 rows
        assert "5 of 10" in result

    def test_filter_count_combines_with_and(self):
        result = self.filter_count_csv(
            str(FIXTURES / "sales.csv"),
            filters=[
                {"column": "quantity", "op": ">", "value": 100},
                {"column": "region", "op": "==", "value": "North"},
            ],
        )
        # qty>100 AND region==North: row 3 only (North, Widget A, qty 150)
        assert "1 of 10" in result

    def test_filter_count_in_op(self):
        result = self.filter_count_csv(
            str(FIXTURES / "sales.csv"),
            filters=[{"column": "region", "op": "in", "value": ["North", "South"]}],
        )
        assert "5 of 10" in result

    def test_filter_count_empty_filters_rejected(self):
        result = self.filter_count_csv(str(FIXTURES / "sales.csv"), filters=[])
        assert "ERROR" in result

    def test_filter_count_unknown_column(self):
        result = self.filter_count_csv(
            str(FIXTURES / "sales.csv"),
            filters=[{"column": "nope", "op": "==", "value": 1}],
        )
        assert "ERROR" in result

    def test_filter_count_bad_op(self):
        result = self.filter_count_csv(
            str(FIXTURES / "sales.csv"),
            filters=[{"column": "quantity", "op": "~~", "value": 1}],
        )
        assert "ERROR" in result
        assert "unsupported op" in result.lower()

    # -- sort_head_csv tool --

    def test_sort_head_descending(self):
        result = self.sort_head_csv(str(FIXTURES / "sales.csv"), sort_by=["revenue"], n=1, ascending=False)
        assert "2100" in result
        assert "East" in result

    def test_sort_head_caps_n(self):
        result = self.sort_head_csv(str(FIXTURES / "sales.csv"), sort_by=["revenue"], n=999)
        assert "ERROR" not in result

    def test_sort_head_unknown_sort_column(self):
        result = self.sort_head_csv(str(FIXTURES / "sales.csv"), sort_by=["nope"], n=5)
        assert "ERROR" in result

    def test_sort_head_column_subset(self):
        result = self.sort_head_csv(
            str(FIXTURES / "sales.csv"), sort_by=["revenue"], n=2,
            ascending=False, columns=["region", "revenue"],
        )
        assert "ERROR" not in result
        assert "product" not in result.lower()

    # -- _load_csv --

    def test_load_csv(self):
        df = self.load_csv(str(FIXTURES / "sales.csv"))
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 10
        assert list(df.columns) == ["date", "region", "product", "quantity", "unit_price", "revenue"]

    def test_load_csv_missing_file(self):
        with pytest.raises(FileNotFoundError):
            self.load_csv("/nonexistent/file.csv")
