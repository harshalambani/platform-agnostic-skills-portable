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
for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_parser_generator import tools  # noqa: E402

@pytest.fixture(autouse=True)
def _agents_root(tmp_path, monkeypatch):
    """Point the tools at a temp agents tree so tests never touch src/agents."""
    monkeypatch.setattr(tools, "AGENTS_ROOT", tmp_path / "agents")
    monkeypatch.setattr(tools, "EXTRA_ARG_ROOTS", [])


@pytest.fixture
def scripts(tmp_path):
    d = tmp_path / "agents" / "skill_t" / "scripts"
    d.mkdir(parents=True)
    return d


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


def test_parser_path_uses_dynamic_picker():
    from agents.registry import discover

    skill = next(s for s in discover(refresh=True) if s.name == "Parser Generator")
    inp = {i.name: i for i in skill.inputs}["parser_path"]
    assert inp.type == "parser_file"


def test_discover_parser_scripts_finds_known_parsers():
    from agents.registry import discover_parser_scripts

    names = {p.name for p in discover_parser_scripts()}
    # A representative sample of the project's embedded parsers.
    assert "parse_krc_ledger.py" in names
    assert "extract_bob_statement.py" in names
    # All results live under a skill's scripts/ dir and match the naming rule.
    for p in discover_parser_scripts():
        assert p.parent.name == "scripts"
        assert p.name.startswith(("parse_", "extract_"))


def test_parser_picker_choices_built_for_ui():
    from ui.tabs._generic import _scan_parser_files

    choices = _scan_parser_files()
    assert choices, "expected at least one parser in the pick-list"
    # (label, path) pairs; label is 'skill_dir / filename'.
    label, path = choices[0]
    assert " / " in label
    assert path.endswith(".py")


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


def test_rewrite_rejects_non_literal_blank_value():
    # A crafted "value" that runs code on splice-and-exec must never reach
    # run_tieout_impl's subprocess execution.
    with pytest.raises(ValueError):
        tools.rewrite_constants(
            SAMPLE, {"BALANCE_X0": "__import__('os').system('x')"}
        )


def test_apply_template_edit_rejects_non_literal_blank(scripts):
    p = scripts / "parse_x.py"
    p.write_text(SAMPLE, encoding="utf-8")
    msg = tools.apply_template_edit_impl(
        str(p), {"BALANCE_X0": "__import__('os').system('x')"}
    )
    assert msg.startswith("ERROR")
    assert p.read_text(encoding="utf-8") == SAMPLE  # unchanged, never reaches disk


def test_create_parser_from_template_rejects_non_literal_blank(tmp_path):
    out = tmp_path / "agents" / "skill_demo" / "scripts" / "parse_demo.py"
    msg = tools.create_parser_from_template_impl(
        str(out), {"FORMAT_NAME": "__import__('os').system('x')"}
    )
    assert msg.startswith("ERROR")
    assert not out.exists()


# --------------------------------------------------------------------------
# validate_parser
# --------------------------------------------------------------------------

def test_validate_good_file(scripts):
    p = scripts / "ok.py"
    p.write_text("X = 1\n", encoding="utf-8")
    assert tools.validate_parser_impl(str(p)).startswith("OK")


def test_validate_syntax_error(scripts):
    p = scripts / "bad.py"
    p.write_text("def broken(:\n", encoding="utf-8")
    assert tools.validate_parser_impl(str(p)).startswith("SYNTAX ERROR")


def test_validate_missing_file(scripts):
    assert tools.validate_parser_impl(str(scripts / "nope.py")).startswith("NOT FOUND")


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


def test_run_tieout_pass(scripts):
    out = tools.run_tieout_impl(str(_fake_parser(scripts, 0)), ["in", "out"])
    assert "TIE-OUT PASS" in out and "exit=0" in out


def test_run_tieout_mismatch(scripts):
    out = tools.run_tieout_impl(str(_fake_parser(scripts, 2)), "in out")
    assert "TIE-OUT MISMATCH" in out and "exit=2" in out
    assert "balance" in out.lower()


def test_run_tieout_error(scripts):
    out = tools.run_tieout_impl(str(_fake_parser(scripts, 1)), [])
    assert "exit=1" in out


# --------------------------------------------------------------------------
# apply_template_edit (round-trip on a temp parser)
# --------------------------------------------------------------------------

def test_apply_template_edit_changes_blank(scripts):
    p = scripts / "parse_x.py"
    p.write_text(SAMPLE, encoding="utf-8")
    msg = tools.apply_template_edit_impl(str(p), {"BALANCE_X0": "(1, 2)"})
    assert "Edited 1 blank" in msg
    spans = tools.constant_spans(p.read_text(encoding="utf-8"))
    src = p.read_text(encoding="utf-8")
    assert src[slice(*spans["BALANCE_X0"])] == "(1, 2)"


def test_apply_template_edit_rejects_unknown_blank(scripts):
    p = scripts / "parse_x.py"
    p.write_text(SAMPLE, encoding="utf-8")
    msg = tools.apply_template_edit_impl(str(p), {"NOPE": "1"})
    assert msg.startswith("ERROR")
    assert p.read_text(encoding="utf-8") == SAMPLE  # unchanged


def test_apply_template_edit_accepts_json_string(scripts):
    p = scripts / "parse_x.py"
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


# --------------------------------------------------------------------------
# File paths stay inside the parser folders (negative tests)
# --------------------------------------------------------------------------

def _tree(tmp_path):
    """Return (scripts_dir, outside_file, sibling_file, sibling_dir)."""
    scripts = tmp_path / "agents" / "skill_t" / "scripts"
    scripts.mkdir(parents=True)
    outside = tmp_path / "outside" / "parse_evil.py"
    outside.parent.mkdir()
    outside.write_text(SAMPLE, encoding="utf-8")
    # A sibling whose name shares the prefix of the allowed folder.
    sib_dir = tmp_path / "agents" / "skill_t" / "scripts_evil"
    sib_dir.mkdir()
    sib = sib_dir / "parse_evil.py"
    sib.write_text(SAMPLE, encoding="utf-8")
    return scripts, outside, sib, sib_dir


def _bad_paths(tmp_path):
    scripts, outside, sib, _ = _tree(tmp_path)
    return {
        "absolute": str(outside),
        "traversal": str(scripts / ".." / ".." / ".." / "outside" / "parse_evil.py"),
        "prefix-sibling": str(sib),
    }


@pytest.mark.parametrize("which", ["absolute", "traversal", "prefix-sibling"])
def test_edit_refuses_paths_outside_scripts_and_writes_nothing(tmp_path, which):
    path = _bad_paths(tmp_path)[which]
    before = Path(path).resolve().read_text(encoding="utf-8")
    msg = tools.apply_template_edit_impl(path, {"BALANCE_X0": "(1, 2)"})
    assert msg.startswith("ERROR: refusing")
    assert Path(path).resolve().read_text(encoding="utf-8") == before


@pytest.mark.parametrize("which", ["absolute", "traversal", "prefix-sibling"])
def test_read_and_run_refuse_paths_outside_scripts(tmp_path, which):
    path = _bad_paths(tmp_path)[which]
    assert tools.extract_blanks_impl(path).startswith("ERROR: refusing")
    assert tools.validate_parser_impl(path).startswith("ERROR: refusing")
    out = tools.run_tieout_impl(path, [])
    assert out.startswith("ERROR: refusing")
    assert "exit=" not in out  # the file was never executed


@pytest.mark.parametrize("which", ["absolute", "traversal", "prefix-sibling"])
def test_create_refuses_paths_outside_scripts_and_writes_nothing(tmp_path, which):
    scripts, outside, sib, sib_dir = _tree(tmp_path)
    target = {
        "absolute": tmp_path / "elsewhere" / "agents" / "skill_x" / "scripts" / "parse_n.py",
        "traversal": scripts / ".." / ".." / ".." / "made" / "agents" / "skill_x" / "scripts" / "parse_n.py",
        "prefix-sibling": sib_dir / "parse_new.py",
    }[which]
    msg = tools.create_parser_from_template_impl(str(target), {"FORMAT_NAME": '"x"'})
    assert msg.startswith("ERROR")
    assert not target.resolve().exists()
    assert not (tmp_path / "elsewhere").exists()
    assert not (tmp_path / "made").exists()


def test_create_refuses_non_parser_name_inside_scripts(scripts):
    target = scripts / "helper.py"
    msg = tools.create_parser_from_template_impl(str(target), {"FORMAT_NAME": '"x"'})
    assert msg.startswith("ERROR")
    assert not target.exists()


def test_symlink_out_of_scripts_is_refused(tmp_path):
    scripts, outside, _, _ = _tree(tmp_path)
    link = scripts / "parse_link.py"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks not permitted on this platform")
    before = outside.read_text(encoding="utf-8")
    msg = tools.apply_template_edit_impl(str(link), {"BALANCE_X0": "(1, 2)"})
    assert msg.startswith("ERROR: refusing")
    assert outside.read_text(encoding="utf-8") == before
    assert tools.run_tieout_impl(str(link), []).startswith("ERROR: refusing")


def test_symlinked_folder_out_of_scripts_blocks_create(tmp_path):
    scripts, outside, _, _ = _tree(tmp_path)
    target_dir = tmp_path / "linked_target"
    target_dir.mkdir()
    link = scripts / "sub"
    try:
        link.symlink_to(target_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        # Windows without symlink rights: a directory junction needs none.
        import subprocess
        if sys.platform != "win32" or subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target_dir)],
            capture_output=True,
        ).returncode != 0:
            pytest.skip("symlinks not permitted on this platform")
    msg = tools.create_parser_from_template_impl(
        str(link / "parse_n.py"), {"FORMAT_NAME": '"x"'}
    )
    assert msg.startswith("ERROR")
    assert not (target_dir / "parse_n.py").exists()


def _writer_parser(scripts, started):
    """Parser that records that it started, then writes its last argument."""
    p = scripts / "parse_ok.py"
    p.write_text(
        "import sys, pathlib\n"
        f"pathlib.Path({str(started)!r}).write_text('started')\n"
        "pathlib.Path(sys.argv[-1]).write_text('x')\n"
        "print('closing balance 1')\n",
        encoding="utf-8",
    )
    return p


def test_tieout_refuses_untyped_path_arguments_and_parser_never_starts(tmp_path, scripts):
    started = tmp_path / "started.txt"
    p = _writer_parser(scripts, started)
    victim = tmp_path / "outside" / "dropped.txt"
    victim.parent.mkdir()
    for arg in (str(victim), "--out=" + str(victim)):
        out = tools.run_tieout_impl(str(p), ["in", arg], check_args=True)
        assert out.startswith("ERROR: refusing tie-out argument 2"), out
    assert not victim.exists()
    assert not started.exists()


def test_tieout_refusal_names_the_position_not_the_value(tmp_path, scripts):
    p = _writer_parser(scripts, tmp_path / "started.txt")
    secret = str(tmp_path / "outside" / "SecretValue99.pdf")
    out = tools.run_tieout_impl(str(p), ["in", secret], check_args=True)
    assert out.startswith("ERROR: refusing")
    assert "SecretValue99" not in out and str(tmp_path) not in out


def test_tieout_refuses_glued_short_option_and_relative_traversal(tmp_path, scripts, monkeypatch):
    started = tmp_path / "started.txt"
    p = _writer_parser(scripts, started)
    outside = tmp_path / "outside"
    outside.mkdir()
    monkeypatch.chdir(scripts)
    for arg in ("-o" + str(outside / "x.txt"), "../../../outside/x.txt"):
        out = tools.run_tieout_impl(str(p), ["in", arg], check_args=True)
        assert out.startswith("ERROR: refusing tie-out argument 2"), out
    assert not (outside / "x.txt").exists()
    assert not started.exists()


def test_tieout_allows_plain_tokens_and_in_folder_paths_on_the_model_route(tmp_path, scripts, monkeypatch):
    p = _writer_parser(scripts, tmp_path / "started.txt")
    monkeypatch.chdir(scripts.parent)
    out = tools.run_tieout_impl(str(p), ["sample", "PASSWORD", "result.txt"], check_args=True)
    assert "exit=0" in out
    assert (scripts.parent / "result.txt").read_text() == "x"


def test_typed_token_is_trusted_but_not_its_siblings(tmp_path, scripts):
    started = tmp_path / "started.txt"
    p = _writer_parser(scripts, started)
    d = tmp_path / "statements"
    d.mkdir()
    a, b = d / "a.pdf", d / "b.pdf"
    with tools.typed_tieout_args(f"{a}"):
        ok = tools.run_tieout_impl(str(p), [str(a)], check_args=True)
        assert "exit=0" in ok
        started.unlink()
        refused = tools.run_tieout_impl(str(p), [str(b)], check_args=True)
        assert refused.startswith("ERROR: refusing")
        assert not started.exists() and not b.exists()
        # nor files inside a typed folder
        refused = tools.run_tieout_impl(str(p), [str(d / "c.txt")], check_args=True)
        assert refused.startswith("ERROR: refusing")


def test_typed_set_is_cleared_after_a_run_and_after_an_exception():
    with tools.typed_tieout_args("a.pdf pw"):
        assert tools._TYPED_ARGS == {"a.pdf", "pw"}
    assert tools._TYPED_ARGS == set()
    with pytest.raises(RuntimeError):
        with tools.typed_tieout_args("a.pdf"):
            raise RuntimeError("boom")
    assert tools._TYPED_ARGS == set()


def test_relative_output_lands_in_the_process_cwd_not_the_skill_folder(tmp_path, scripts, monkeypatch):
    p = _writer_parser(scripts, tmp_path / "started.txt")
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    out = tools.run_tieout_impl(str(p), ["in.pdf", "rel-out.txt"])
    assert "exit=0" in out
    assert (work / "rel-out.txt").read_text() == "x"
    assert not (scripts.parent / "rel-out.txt").exists()
    assert not (scripts / "rel-out.txt").exists()


def test_gate_trusts_what_the_developer_typed(tmp_path, scripts, capsys):
    from agents.skill_parser_generator import __main__ as pg_main
    started = tmp_path / "started.txt"
    p = _writer_parser(scripts, started)
    target = tmp_path / "outside" / "typed.txt"
    target.parent.mkdir()
    pg_main.main(["gate", str(p), "--", "in.pdf", str(target)])
    assert started.exists()
    assert target.read_text() == "x"


def test_fix_route_trusts_typed_args_but_not_other_paths(tmp_path, scripts, monkeypatch):
    from agents.skill_parser_generator import agent as pg_agent
    started = tmp_path / "started.txt"
    p = _writer_parser(scripts, started)
    typed = tmp_path / "outside" / "typed.txt"
    other = tmp_path / "outside" / "model-chose.txt"
    typed.parent.mkdir()
    seen = {}

    def fake_run(instruction, config_path="config.yaml", model_override=None):
        seen["typed"] = tools.run_tieout_impl(str(p), ["in.pdf", str(typed)], check_args=True)
        started.unlink()
        seen["other"] = tools.run_tieout_impl(str(p), ["in.pdf", str(other)], check_args=True)
        return "REPORT"

    monkeypatch.setattr(pg_agent, "run", fake_run)
    pg_agent.run_ui("fix", str(p), parser_args=f"in.pdf {typed}")
    assert "exit=0" in seen["typed"] and typed.read_text() == "x"
    assert seen["other"].startswith("ERROR: refusing")
    assert not started.exists() and not other.exists()
    assert tools._TYPED_ARGS == set()


def test_valid_path_inside_scripts_still_works(scripts):
    p = scripts / "nested.py"
    p.write_text(SAMPLE, encoding="utf-8")
    assert "Edited 1 blank" in tools.apply_template_edit_impl(
        str(p), {"BALANCE_X0": "(1, 2)"}
    )
