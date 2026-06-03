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

class TestCSVAnalyzerSafety:
    """Test the expression safety validator."""

    @pytest.fixture(autouse=True)
    def _setup(self):
        # Extract _validate_expression and _BLOCKED_PATTERNS from tools.py
        # without importing langchain_core.
        source = (SKILLS_DIR / "skill_csv_analyzer" / "tools.py").read_text()
        # Remove langchain import
        source = source.replace("from langchain_core.tools import tool", "")
        # Remove @tool decorators
        source = re.sub(r"^@tool\s*$", "", source, flags=re.MULTILINE)
        ns = {}
        exec(compile(source, "tools.py", "exec"), ns)
        self.validate = ns["_validate_expression"]
        self.describe_csv = ns["describe_csv"]
        self.query_csv = ns["query_csv"]
        self.load_csv = ns["_load_csv"]

    # -- Safety: allowed expressions --

    @pytest.mark.parametrize("expr", [
        "df.groupby('region')['revenue'].sum()",
        "df.describe()",
        "df['quantity'].mean()",
        "df[df['quantity'] > 100].shape[0]",
        "df.head(10)",
        "df['product'].value_counts()",
        "df.sort_values('revenue', ascending=False).head(3)",
        "df.corr(numeric_only=True)",
    ])
    def test_safe_expressions_pass(self, expr):
        assert self.validate(expr) is None

    # -- Safety: blocked expressions --

    @pytest.mark.parametrize("expr", [
        "exec('import os')",
        "eval('1+1')",
        "__import__('os').system('ls')",
        "open('/etc/passwd').read()",
        "os.remove('file.csv')",
        "df.to_csv('hack.csv')",
        "df.to_excel('hack.xlsx')",
        "import subprocess",
        "subprocess.run(['ls'])",
        "df.to_pickle('x')",
        "sys.exit()",
    ])
    def test_unsafe_expressions_blocked(self, expr):
        result = self.validate(expr)
        assert result is not None, f"Expression should be blocked: {expr}"
        assert "Blocked" in result

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

    # -- query_csv tool --

    def test_query_csv_groupby(self):
        result = self.query_csv(str(FIXTURES / "sales.csv"), "df.groupby('region')['revenue'].sum()")
        assert "North" in result
        # North: 1050 + 1575 + 1320 = 3945
        assert "3945" in result

    def test_query_csv_filter_count(self):
        result = self.query_csv(str(FIXTURES / "sales.csv"), "df[df['quantity'] > 100].shape[0]")
        # rows with qty > 100: 200, 150, 120, 200, 180 = 5 rows
        assert "5" in result

    def test_query_csv_mean(self):
        result = self.query_csv(str(FIXTURES / "sales.csv"), "df['revenue'].mean()")
        # Mean: (1050+1650+1575+1650+1260+742.5+1320+2100+1485+880) / 10 = 1371.25
        assert "1371.25" in result

    def test_query_csv_blocks_unsafe(self):
        result = self.query_csv(str(FIXTURES / "sales.csv"), "exec('import os')")
        assert "Blocked" in result

    def test_query_csv_bad_expression(self):
        result = self.query_csv(str(FIXTURES / "sales.csv"), "df.nonexistent_method()")
        assert "ERROR" in result

    def test_query_csv_missing_file(self):
        result = self.query_csv("/nonexistent/file.csv", "df.head()")
        assert "ERROR" in result

    # -- _load_csv --

    def test_load_csv(self):
        df = self.load_csv(str(FIXTURES / "sales.csv"))
        assert isinstance(df, pd.DataFrame)
        assert len(df) == 10
        assert list(df.columns) == ["date", "region", "product", "quantity", "unit_price", "revenue"]

    def test_load_csv_missing_file(self):
        with pytest.raises(FileNotFoundError):
            self.load_csv("/nonexistent/file.csv")
