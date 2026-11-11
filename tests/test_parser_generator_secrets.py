"""A typed password never reaches the model prompt or the written report, while
the local tie-out run still receives it. Synthetic values only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_parser_generator import agent as pg_agent  # noqa: E402
from agents.skill_parser_generator import tools  # noqa: E402

SECRET = "Zq7-synthetic-secret"


@pytest.fixture(autouse=True)
def _agents_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "AGENTS_ROOT", tmp_path / "agents")
    monkeypatch.setattr(tools, "EXTRA_ARG_ROOTS", [])


@pytest.fixture
def parser(tmp_path):
    d = tmp_path / "agents" / "skill_t" / "scripts"
    d.mkdir(parents=True)
    p = d / "parse_echo.py"
    # Records its argv and prints the secret, as a careless parser might.
    p.write_text(
        "import sys, json, pathlib\n"
        "pathlib.Path('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
        f"print('closing balance 1 ' + {SECRET!r})\n",
        encoding="utf-8",
    )
    return p


# ---- masking ---------------------------------------------------------------

@pytest.mark.parametrize("typed, shown", [
    (f"in.pdf --password {SECRET} out.xlsx", "in.pdf --password *** out.xlsx"),
    (f"in.pdf --password={SECRET} out.xlsx", "in.pdf --password=*** out.xlsx"),
    (f"in.pdf password={SECRET} out.xlsx", "in.pdf password=*** out.xlsx"),
    (f"in.pdf --pwd {SECRET}", "in.pdf --pwd ***"),
    (f"in.pdf --pass={SECRET}", "in.pdf --pass=***"),
    (f"in.pdf passphrase={SECRET}", "in.pdf passphrase=***"),
    (f"in.pdf --pdf-password {SECRET}", "in.pdf --pdf-password ***"),
    (f"in.pdf --PASSWORD {SECRET}", "in.pdf --PASSWORD ***"),
])
def test_password_values_are_masked(typed, shown):
    masked, secrets = tools.mask_secret_args(typed)
    assert masked == shown
    assert SECRET not in masked
    assert secrets == [SECRET]


def test_non_secret_arguments_are_unchanged():
    typed = "in.pdf out.xlsx --year 2025 --bypass x compass=7 --verbose"
    masked, secrets = tools.mask_secret_args(typed)
    assert masked == typed
    assert secrets == []
    assert tools.mask_secret_args("") == ("", [])


def test_a_secret_name_does_not_swallow_the_wrong_value():
    masked, _ = tools.mask_secret_args(f"--password {SECRET} --year 2025")
    assert masked == "--password *** --year 2025"


# ---- the prompt and the report ---------------------------------------------

def test_prompt_and_report_never_hold_the_password_but_tieout_gets_it(
        tmp_path, parser, monkeypatch):
    work = parser.parent.parent
    monkeypatch.chdir(work)
    seen = {}

    def fake_run(instruction, config_path="config.yaml", model_override=None):
        seen["prompt"] = instruction
        # The model repeats what it was shown, mask included.
        seen["tool"] = tools.run_tieout.func(
            str(parser), ["in.pdf", "--password", "***", "out.xlsx"])
        return f"Ran the tie-out. Result:\n{seen['tool']}\nUsed {SECRET} as typed."

    monkeypatch.setattr(pg_agent, "run", fake_run)
    out_dir = tmp_path / "out"
    report = pg_agent.run_ui(
        "fix", str(parser), parser_args=f"in.pdf --password {SECRET} out.xlsx",
        output_path=str(out_dir))

    assert SECRET not in seen["prompt"]
    assert "--password ***" in seen["prompt"]
    # the local run received the real value
    assert json.loads((work / "argv.json").read_text()) == [
        "in.pdf", "--password", SECRET, "out.xlsx"]
    assert "exit=0" in seen["tool"]
    # nothing the model saw or wrote carries it
    assert SECRET not in seen["tool"]
    assert SECRET not in report
    assert SECRET not in (out_dir / "parser-generator-report.md").read_text(encoding="utf-8")
    assert tools._SECRETS == []


def test_equals_form_is_restored_for_the_tieout(tmp_path, parser, monkeypatch):
    monkeypatch.chdir(parser.parent.parent)

    def fake_run(instruction, config_path="config.yaml", model_override=None):
        assert SECRET not in instruction
        tools.run_tieout.func(str(parser), "in.pdf password=*** out.xlsx")
        return "ok"

    monkeypatch.setattr(pg_agent, "run", fake_run)
    pg_agent.run_ui("fix", str(parser), parser_args=f"in.pdf password={SECRET} out.xlsx")
    assert json.loads((parser.parent.parent / "argv.json").read_text()) == [
        "in.pdf", f"password={SECRET}", "out.xlsx"]


def test_non_secret_args_reach_the_prompt_unchanged(tmp_path, parser, monkeypatch):
    seen = {}

    def fake_run(instruction, config_path="config.yaml", model_override=None):
        seen["prompt"] = instruction
        return "ok"

    monkeypatch.setattr(pg_agent, "run", fake_run)
    pg_agent.run_ui("fix", str(parser), parser_args="in.pdf out.xlsx --year 2025")
    assert "(args: in.pdf out.xlsx --year 2025)" in seen["prompt"]
    assert "***" not in seen["prompt"]


def test_a_mask_with_no_typed_secret_is_left_alone(parser, monkeypatch):
    """The model cannot conjure a password: with nothing typed, *** stays ***."""
    monkeypatch.chdir(parser.parent.parent)
    tools.run_tieout.func(str(parser), ["in.pdf", "***"])
    assert json.loads((parser.parent.parent / "argv.json").read_text()) == ["in.pdf", "***"]


def test_fix_command_prompt_hides_the_password(parser, monkeypatch, capsys):
    from agents.skill_parser_generator import __main__ as pg_main
    seen = {}

    def fake_run(instruction, config_path="config.yaml", model_override=None):
        seen["prompt"] = instruction
        return f"done {SECRET}"

    monkeypatch.setattr(pg_agent, "run", fake_run)
    pg_main.main(["fix", str(parser), "--", "in.pdf", "--password", SECRET, "out.xlsx"])
    assert SECRET not in seen["prompt"]
    assert "--password ***" in seen["prompt"]
    assert SECRET not in capsys.readouterr().out


# ---- the field label and guide no longer suggest typing one ------------------

def test_label_and_guide_do_not_suggest_typing_a_password():
    root = Path(__file__).resolve().parent.parent
    yaml_text = (root / "src/agents/skill_parser_generator/skill.yaml").read_text(encoding="utf-8")
    guide = (root / "docs/guides/Parser-Generator-Guide.md").read_text(encoding="utf-8")
    assert "statement.pdf PASSWORD" not in yaml_text
    assert "MYPASSWORD" not in guide and "hunter2" not in guide


# ---- changed forms of a typed value are scrubbed too ------------------------

def _forms(secret):
    import base64
    import urllib.parse
    b = base64.b64encode(secret.encode()).decode()
    return {
        "percent": urllib.parse.quote(secret, safe=""),
        "percent_plus": urllib.parse.quote_plus(secret),
        "base64": b,
        "base64_unpadded": b.rstrip("="),
        "urlsafe": base64.urlsafe_b64encode(secret.encode()).decode(),
        "split_space": " ".join(secret),
        "split_newline": "\n".join([secret[:6], secret[6:12], secret[12:]]),
        "split_crlf": secret[:5] + "\r\n  " + secret[5:],
    }


_ODD = "Zq7/synth+secret=ab&c d"  # characters that change under encoding


@pytest.mark.parametrize("value", [SECRET, _ODD])
def test_encoded_and_split_forms_are_scrubbed(value):
    for name, form in _forms(value).items():
        shown = tools.scrub_secrets(f"line one\nvalue was {form} end", [value])
        assert form not in shown, name
        assert tools.MASK in shown, name
        assert "end" in shown and "line one" in shown, name


def test_scrub_leaves_unrelated_text_alone():
    text = "closing balance 100.00 tie-out PASS\nsecond line"
    assert tools.scrub_secrets(text, [SECRET]) == text
    assert tools.scrub_secrets(text, []) == text
    assert tools.scrub_secrets(text, [""]) == text


def test_encoded_form_never_reaches_the_report(tmp_path, parser, monkeypatch):
    forms = _forms(SECRET)
    monkeypatch.chdir(parser.parent.parent)

    def fake_run(instruction, config_path="config.yaml", model_override=None):
        return "Echo:\n" + "\n".join(forms.values())

    monkeypatch.setattr(pg_agent, "run", fake_run)
    out_dir = tmp_path / "out"
    report = pg_agent.run_ui(
        "fix", str(parser), parser_args=f"in.pdf --password {SECRET}",
        output_path=str(out_dir))
    written = (out_dir / "parser-generator-report.md").read_text(encoding="utf-8")
    for name, form in forms.items():
        assert form not in report, name
        assert form not in written, name


def test_encoded_form_never_reaches_the_screen(parser, monkeypatch, capsys):
    from agents.skill_parser_generator import __main__ as pg_main
    forms = _forms(SECRET)

    def fake_run(instruction, config_path="config.yaml", model_override=None):
        return "\n".join(forms.values())

    monkeypatch.setattr(pg_agent, "run", fake_run)
    pg_main.main(["fix", str(parser), "--", "in.pdf", "--password", SECRET])
    out = capsys.readouterr().out
    for name, form in forms.items():
        assert form not in out, name


# ---- a value goes back only into the slot it was typed for -------------------

def _run_with(parser, monkeypatch, typed, model_args):
    work = parser.parent.parent
    monkeypatch.chdir(work)

    def fake_run(instruction, config_path="config.yaml", model_override=None):
        tools.run_tieout.func(str(parser), model_args)
        return "ok"

    monkeypatch.setattr(pg_agent, "run", fake_run)
    pg_agent.run_ui("fix", str(parser), parser_args=typed)
    return json.loads((work / "argv.json").read_text())


def test_same_slot_round_trip_still_works(parser, monkeypatch):
    argv = _run_with(parser, monkeypatch, f"in.pdf --password {SECRET} out.xlsx",
                     ["in.pdf", "--password", "***", "out.xlsx"])
    assert argv == ["in.pdf", "--password", SECRET, "out.xlsx"]


def test_two_secrets_each_go_to_their_own_slot(parser, monkeypatch):
    other = "Qw9-other-synthetic"
    argv = _run_with(
        parser, monkeypatch,
        f"in.pdf --pdf-password {SECRET} pwd={other}",
        ["in.pdf", "pwd=***", "--pdf-password", "***"])
    assert argv == ["in.pdf", f"pwd={other}", "--pdf-password", SECRET]


def test_mask_moved_to_another_option_gets_no_value(parser, monkeypatch):
    argv = _run_with(parser, monkeypatch, f"in.pdf --password {SECRET} out.xlsx",
                     ["in.pdf", "--password", "x", "--output", "***"])
    assert SECRET not in json.dumps(argv)
    assert argv == ["in.pdf", "--password", "x", "--output", "***"]


def test_mask_moved_to_a_bare_argument_gets_no_value(parser, monkeypatch):
    argv = _run_with(parser, monkeypatch, f"in.pdf --password {SECRET} out.xlsx",
                     ["***", "in.pdf", "out.xlsx"])
    assert SECRET not in json.dumps(argv)
    assert argv[0] == "***"


def test_mask_in_a_differently_named_key_gets_no_value(parser, monkeypatch):
    argv = _run_with(parser, monkeypatch, f"in.pdf password={SECRET}",
                     ["in.pdf", "output=***", "--out=***"])
    assert SECRET not in json.dumps(argv)
    assert argv == ["in.pdf", "output=***", "--out=***"]


def test_one_value_is_not_reused_for_a_second_mask(parser, monkeypatch):
    argv = _run_with(parser, monkeypatch, f"in.pdf --password {SECRET}",
                     ["--password", "***", "--password", "***"])
    assert argv.count(SECRET) == 1
    assert argv == ["--password", SECRET, "--password", "***"]
