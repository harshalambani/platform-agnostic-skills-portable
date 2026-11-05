"""Parser Generator is a source-checkout-only dev tool: refused when frozen."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from agents.skill_parser_generator import __main__ as pg_main  # noqa: E402
from agents.skill_parser_generator import agent as pg_agent  # noqa: E402

PARSER = "def f():\n    pass\n\nFORMAT_NAME = 'X'\n"


def _simulate_frozen(monkeypatch, how):
    if how == "frozen":
        monkeypatch.setattr(sys, "frozen", True, raising=False)
    else:
        monkeypatch.setattr(sys, "_MEIPASS", "/nowhere", raising=False)


@pytest.fixture
def no_agent(monkeypatch):
    """Fail loudly if the guard lets the agent get built."""
    import agents.base_agent as base

    def boom(*a, **k):
        raise AssertionError("agent must not be built")
    monkeypatch.setattr(base, "build_agent", boom)


@pytest.mark.parametrize("how", ["frozen", "meipass"])
def test_run_ui_refused_when_frozen_writes_nothing(tmp_path, monkeypatch, no_agent, how):
    _simulate_frozen(monkeypatch, how)
    out = tmp_path / "out"
    parser = tmp_path / "parse_x.py"
    parser.write_text(PARSER, encoding="utf-8")
    msg = pg_agent.run_ui("fix", str(parser), output_path=str(out))
    assert msg == pg_agent.FROZEN_MESSAGE
    assert "source checkout" in msg
    assert not out.exists()
    assert parser.read_text(encoding="utf-8") == PARSER
    assert sorted(p.name for p in tmp_path.iterdir()) == ["parse_x.py"]


def test_run_refused_when_frozen(monkeypatch, no_agent):
    _simulate_frozen(monkeypatch, "frozen")
    assert pg_agent.run("fix it") == pg_agent.FROZEN_MESSAGE


@pytest.mark.parametrize("argv", [
    ["blanks", "x.py"],
    ["gate", "x.py", "--", "a"],
    ["fix", "x.py"],
    ["create", "y.py", "--format", "F"],
])
def test_cli_exits_2_when_frozen_before_touching_files(tmp_path, monkeypatch, capsys, no_agent, argv):
    _simulate_frozen(monkeypatch, "frozen")
    monkeypatch.chdir(tmp_path)
    rc = pg_main.main(argv)
    assert rc == 2
    err = capsys.readouterr().err
    assert "source-checkout-only" in err
    assert list(tmp_path.iterdir()) == []


def test_unfrozen_cli_behaviour_unchanged(tmp_path, monkeypatch, capsys):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    parser = tmp_path / "parse_x.py"
    parser.write_text(PARSER, encoding="utf-8")
    rc = pg_main.main(["blanks", str(parser)])
    assert rc == 0
    assert "source-checkout-only" not in capsys.readouterr().err


def test_unfrozen_run_ui_reaches_agent(tmp_path, monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delattr(sys, "_MEIPASS", raising=False)
    monkeypatch.setattr(pg_agent, "run", lambda *a, **k: "REPORT")
    out = tmp_path / "out"
    assert pg_agent.run_ui("fix", "parse_x.py", output_path=str(out)) == "REPORT"
    assert (out / "parser-generator-report.md").read_text(encoding="utf-8") == "REPORT"
