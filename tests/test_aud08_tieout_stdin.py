"""AUD-08 follow-up: the Parser Generator tie-out sends the password to a
stdin-capable parser on stdin, never in argv. Synthetic data only."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (SRC, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_parser_generator import tools  # noqa: E402
from test_aud08_krc_password_stdin import (  # noqa: E402
    BILLS_SCRIPT, LEDGER_SCRIPT, _encrypted_pdf, needs_qpdf,
)

PW = "Zq7-made-up-pw-4471"

# Records argv and stdin, then prints the password it was given (a careless parser).
STDIN_PARSER = (
    "# takes --password-stdin\n"
    "import sys, json, pathlib\n"
    "args = sys.argv[1:]\n"
    "pw = sys.stdin.readline().rstrip(chr(13) + chr(10)) if '--password-stdin' in args else ''\n"
    "pathlib.Path('argv.json').write_text(json.dumps(args))\n"
    "print('closing balance ' + pw)\n"
    "sys.stderr.write('bad file ' + pw + chr(10))\n"
)
PLAIN_PARSER = (
    "import sys, json, pathlib\n"
    "pathlib.Path('argv.json').write_text(json.dumps(sys.argv[1:]))\n"
    "print('closing balance ok')\n"
)


@pytest.fixture(autouse=True)
def _agents_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "AGENTS_ROOT", tmp_path / "agents")
    monkeypatch.setattr(tools, "EXTRA_ARG_ROOTS", [])
    monkeypatch.chdir(tmp_path)


def _parser(tmp_path, name, src):
    d = tmp_path / "agents" / "skill_t" / "scripts"
    d.mkdir(parents=True, exist_ok=True)
    p = d / name
    p.write_text(src, encoding="utf-8")
    return p


def _argv(tmp_path):
    return json.loads((tmp_path / "argv.json").read_text())


@pytest.mark.parametrize("typed, expect", [
    (f"in.pdf out.xlsx --password {PW}", ["in.pdf", "out.xlsx"]),
    (f"in.pdf out.xlsx --password={PW}", ["in.pdf", "out.xlsx"]),
    (f"in.pdf out.xlsx password={PW}", ["in.pdf", "out.xlsx"]),
    (f"--pdf-password {PW} in.pdf out.xlsx", ["in.pdf", "out.xlsx"]),
])
def test_stdin_parser_gets_password_on_stdin_not_argv(tmp_path, typed, expect):          # NEGATIVE
    p = _parser(tmp_path, "parse_a.py", STDIN_PARSER)
    with tools.typed_tieout_args(typed):
        masked, _ = tools.mask_secret_args(typed)
        res = tools.run_tieout.func(str(p), masked.split())
    argv = _argv(tmp_path)
    assert argv == expect + ["--password-stdin"]
    assert PW not in " ".join(argv)
    assert "exit=0" in res
    assert PW not in res                      # stdout and stderr both redacted
    assert "***" in res                       # the parser did receive and echo it


def test_gate_command_route_also_uses_stdin(tmp_path):                                   # NEGATIVE
    p = _parser(tmp_path, "parse_a.py", STDIN_PARSER)
    res = tools.run_tieout_impl(str(p), ["in.pdf", "out.xlsx", "--password", PW])
    assert _argv(tmp_path) == ["in.pdf", "out.xlsx", "--password-stdin"]
    assert PW not in res


def test_the_returned_text_never_holds_the_password_even_on_error(tmp_path):             # NEGATIVE
    p = _parser(tmp_path, "parse_e.py", STDIN_PARSER + "sys.exit(1)\n")
    res = tools.run_tieout_impl(str(p), ["in.pdf", "--password", PW])
    assert "exit=1" in res and "stderr:" in res
    assert PW not in res


def test_a_plain_parser_call_is_unchanged(tmp_path):                                     # NEGATIVE
    p = _parser(tmp_path, "parse_p.py", PLAIN_PARSER)
    tools.run_tieout_impl(str(p), ["in.pdf", "--password", PW, "out.xlsx"])
    assert _argv(tmp_path) == ["in.pdf", "--password", PW, "out.xlsx"]
    assert "--password-stdin" not in _argv(tmp_path)


def test_empty_or_absent_password_is_unchanged_for_a_stdin_parser(tmp_path):
    p = _parser(tmp_path, "parse_a.py", STDIN_PARSER)
    tools.run_tieout_impl(str(p), ["in.pdf", "out.xlsx"])
    assert _argv(tmp_path) == ["in.pdf", "out.xlsx"]
    tools.run_tieout_impl(str(p), ["in.pdf", "--password="])
    assert _argv(tmp_path) == ["in.pdf", "--password="]


def test_a_mask_with_no_typed_secret_is_not_turned_into_stdin(tmp_path):                 # NEGATIVE
    p = _parser(tmp_path, "parse_a.py", STDIN_PARSER)
    tools.run_tieout.func(str(p), ["x", "--password", "***"])
    assert "--password-stdin" not in _argv(tmp_path)


def test_every_stdin_capable_parser_in_the_repo_takes_the_flag_last():
    """Layout check: the three parsers that accept --password-stdin all accept
    it as a trailing flag after their positional arguments."""
    cc = SRC / "agents" / "skill_cc_sort" / "scripts" / "extract_sort_cc_pdfs.py"
    for script in (LEDGER_SCRIPT, BILLS_SCRIPT, cc):
        assert tools._supports_password_stdin(script), script.name
    assert not tools._supports_password_stdin(SRC / "agents" / "skill_hsbc" / "agent.py")


@needs_qpdf
def test_end_to_end_ledger_tieout_on_a_synthetic_encrypted_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(tools, "AGENTS_ROOT", SRC / "agents")
    pdf = _encrypted_pdf(tmp_path / "s.pdf", PW)
    out = tmp_path / "o.xlsx"
    res = tools.run_tieout_impl(
        str(LEDGER_SCRIPT), [str(pdf), str(out), "--password", PW])
    assert "decryption failed" not in res and "no ledger rows" in res
    assert PW not in res
    bad = tools.run_tieout_impl(
        str(LEDGER_SCRIPT), [str(pdf), str(out), "--password", "wrong-pw-9"])
    assert "decryption failed" in bad and "wrong-pw-9" not in bad
