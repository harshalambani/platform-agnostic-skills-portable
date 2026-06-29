"""
Tests for the dev-time Parser Generator skill (v1.1 Session B).

Covers the deterministic tool cores (no LLM): the AST-safe blank rewriter, the
validate/tie-out gate, template creation, and the locked guarantees - the skill
stays UI-hidden, and the oracle / exit contract cannot be edited as "blanks".
"""
from __future__ import annotations

import ast
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_parser_generator import tools  # noqa: E402

PKG = SRC / "agents" / "skill_parser_generator"
TEMPLATE = PKG / "templates" / "parser_template.py"


# --------------------------------------------------------------------------
# UI registration (exposed as the "Parser Generator" dev tab)
# --------------------------------------------------------------------------

def test_skill_is_discovered():
    from agents.registry import discover

    skills = {s.name: s for s in discover(refresh=True)}
    assert "Parser Generator" in skills
    s = skills["Parser Generator"]
    assert s.package == "agents.skill_parser_generator"
    assert s.mode == "agent"
    assert s.entry_point == "agent:run_ui"
    # category "dev" is intentionally outside webui's known groups, so the
    # fallback renders it as a flat top-level tab.
    assert s.category == "dev"


def test_ui_entry_point_loads():
    from agents.registry import discover, load_run_function

    skill = next(s for s in discover(refresh=True) if s.name == "Parser Generator")
    fn = load_run_function(skill)
    assert callable(fn)
    # run_ui guards an empty path without touching the LLM.
    assert fn("Fix a failing parser", "").startswith("Error")


# --------------------------------------------------------------------------
# AST-safe constant rewriting
# --------------------------------------------------------------------------

SAMPLE = textwrap.dedent(
    '''\
    """doc"""
    BALANCE_X0 = (525, 555)
    MARKERS = (
        "Closing Balance",
        "Print Date",
    )
    LOWER_kept = 1


    def oracle(rows):
        return [r for r in rows]
    '''
)


def test_constant_spans_finds_only_upper_constants():
    spans = tools.constant_spans(SAMPLE)
    assert set(spans) == {"BALANCE_X0", "MARKERS"}
    assert SAMPLE[slice(*spans["BALANCE_X0"])] == "(525, 555)"


def test_rewrite_single_line_constant():
    out = tools.rewrite_constants(SAMPLE, {"BALANCE_X0": "(520, 552)"})
    ns: dict = {}
    exec(compile(out, "<t>", "exec"), ns)  # noqa: S102 - trusted test source
    assert ns["BALANCE_X0"] == (520, 552)
    assert ns["MARKERS"] == ("Closing Balance", "Print Date")


def test_rewrite_multiline_constant():
    out = tools.rewrite_constants(SAMPLE, {"MARKERS": '("A", "B", "C")'})
    ns: dict = {}
    exec(compile(out, "<t>", "exec"), ns)  # noqa: S102
    assert ns["MARKERS"] == ("A", "B", "C")
    assert ns["BALANCE_X0"] == (525, 555)


def test_rewrite_rejects_non_blank_names():
    # oracle is a function, LOWER_kept is not a constant - neither is editable.
    with pytest.raises(KeyError):
        tools.rewrite_constants(SAMPLE, {"oracle": "None"})
    with pytest.raises(KeyError):
        tools.rewrite_constants(SAMPLE, {"LOWER_kept": "2"})


def test_rewrite_rejects_syntax_breaking_edit():
    with pytest.raises(SyntaxError):
        tools.rewrite_constants(SAMPLE, {"BALANCE_X0": "(520, "})


# --------------------------------------------------------------------------
# validate_parser
# --------------------------------------------------------------------------

def test_validate_good_file(tmp_path):
    p = tmp_path / "ok.py"
    p.write_text("X = 1\n", encoding="utf-8")
    assert tools.validate_parser_impl(str(p)).startswith("OK")


def test_validate_syntax_error(tmp_path):
    p = tmp_path / "bad.py"
    p.write_text("def broken(:\n", encoding="utf-8")
    assert tools.validate_parser_impl(str(p)).startswith("SYNTAX ERROR")


def test_validate_missing_file(tmp_path):
    assert tools.validate_parser_impl(str(tmp_path / "nope.py")).startswith("NOT FOUND")


# --------------------------------------------------------------------------
# run_tieout exit-code mapping (tiny fake parsers)
# --------------------------------------------------------------------------

def _fake_parser(tmp_path: Path, exit_code: int) -> Path:
    p = tmp_path / f"parse_fake{exit_code}.py"
    p.write_text(
        textwrap.dedent(
            f"""\
            import sys
            print("recomputed closing balance 10.00 vs printed 10.00")
            raise SystemExit({exit_code})
            """
        ),
        encoding="utf-8",
    )
    return p


def test_run_tieout_pass(tmp_path):
    out = tools.run_tieout_impl(str(_fake_parser(tmp_path, 0)), ["in", "out"])
    assert "TIE-OUT PASS" in out and "exit=0" in out


def test_run_tieout_mismatch(tmp_path):
    out = tools.run_tieout_impl(str(_fake_parser(tmp_path, 2)), "in out")
    assert "TIE-OUT MISMATCH" in out and "exit=2" in out
    assert "balance" in out.lower()


def test_run_tieout_error(tmp_path):
    out = tools.run_tieout_impl(str(_fake_parser(tmp_path, 1)), [])
    assert "exit=1" in out


# --------------------------------------------------------------------------
# apply_template_edit (round-trip on a temp parser)
# --------------------------------------------------------------------------

def test_apply_template_edit_changes_blank(tmp_path):
    p = tmp_path / "parse_x.py"
    p.write_text(SAMPLE, encoding="utf-8")
    msg = tools.apply_template_edit_impl(str(p), {"BALANCE_X0": "(1, 2)"})
    assert "Edited 1 blank" in msg
    spans = tools.constant_spans(p.read_text(encoding="utf-8"))
    src = p.read_text(encoding="utf-8")
    assert src[slice(*spans["BALANCE_X0"])] == "(1, 2)"


def test_apply_template_edit_rejects_unknown_blank(tmp_path):
    p = tmp_path / "parse_x.py"
    p.write_text(SAMPLE, encoding="utf-8")
    msg = tools.apply_template_edit_impl(str(p), {"NOPE": "1"})
    assert msg.startswith("ERROR")
    assert p.read_text(encoding="utf-8") == SAMPLE  # unchanged


def test_apply_template_edit_accepts_json_string(tmp_path):
    p = tmp_path / "parse_x.py"
    p.write_text(SAMPLE, encoding="utf-8")
    msg = tools.apply_template_edit_impl(str(p), '{"BALANCE_X0": "(9, 9)"}')
    assert "Edited 1 blank" in msg


# --------------------------------------------------------------------------
# Template + create_parser_from_template
# --------------------------------------------------------------------------

def test_template_is_valid_and_has_blanks():
    src = TEMPLATE.read_text(encoding="utf-8")
    ast.parse(src)
    spans = tools.constant_spans(src)
    assert {"FORMAT_NAME", "COLUMN_X0", "BOILERPLATE_MARKERS",
            "INTERNAL_TRANSFER_MARKER", "DATE_RE"} <= set(spans)


def test_template_oracle_is_not_a_blank():
    # The oracle is a def, so it must never appear as an editable constant.
    spans = tools.constant_spans(TEMPLATE.read_text(encoding="utf-8"))
    assert "verify_balance_invariant" not in spans


def test_create_enforces_locked_location(tmp_path):
    bad = tmp_path / "parser.py"
    msg = tools.create_parser_from_template_impl(str(bad), {"FORMAT_NAME": '"x"'})
    assert msg.startswith("ERROR")
    assert not bad.exists()


def test_create_writes_valid_parser(tmp_path):
    out = tmp_path / "agents" / "skill_demo" / "scripts" / "parse_demo.py"
    msg = tools.create_parser_from_template_impl(
        str(out), {"FORMAT_NAME": '"demo"'}
    )
    assert "Created parse_demo.py" in msg
    assert out.is_file()
    ast.parse(out.read_text(encoding="utf-8"))
    ns: dict = {}
    # Exec only the module-level constants by parsing - just confirm it imports
    # cleanly as far as syntax; full import needs pdfplumber at runtime.
    assert 'FORMAT_NAME = "demo"' in out.read_text(encoding="utf-8")
    del ns


def test_create_refuses_overwrite(tmp_path):
    out = tmp_path / "agents" / "skill_demo" / "scripts" / "parse_demo.py"
    tools.create_parser_from_template_impl(str(out), {"FORMAT_NAME": '"demo"'})
    msg = tools.create_parser_from_template_impl(str(out), {"FORMAT_NAME": '"demo"'})
    assert "refusing to overwrite" in msg
