"""
tests/skill_itr_workbook/test_itr01_precheck_warn.py -- ITR-01 fix round: the
pre-run AI endpoint check is a loud WARNING for the ITR Workbook only, declared
in skill.yaml (requires.llm_precheck: warn). Every other model skill keeps the
hard stop. Offline; synthetic fixtures; the key is a fake.
"""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
SCRIPTS = SRC / "agents" / "skill_itr_workbook" / "scripts"
AGENT_DIR = SRC / "agents" / "skill_itr_workbook"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
for p in (str(SRC), str(SCRIPTS), str(AGENT_DIR), str(ROOT), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent  # noqa: E402
import fixture_gen  # noqa: E402
import suggest  # noqa: E402
from agents import registry  # noqa: E402
from ui.tabs import _generic as generic_mod  # noqa: E402

FAKE_KEY = "sk-FAKE0000000000000000KEYabcdef1234"
ENTITIES_EXAMPLE = ROOT / "bundling" / "canonical" / "itr" / "entities.example.yaml"
SCRIPS_EXAMPLE = ROOT / "bundling" / "canonical" / "itr" / "scrips.example.yaml"


def _itr():
    sk = registry.get("ITR Workbook")
    assert sk is not None
    return sk


def _with_mode(skill, mode):
    return dataclasses.replace(skill, requires=dataclasses.replace(skill.requires, llm_precheck=mode))


def _drive(monkeypatch, tmp_path, skill, health):
    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        return "done"

    cfg = {"active_endpoint": "ep1",
           "endpoints": {"ep1": {"provider": "openai_compatible", "base_url": "http://127.0.0.1:9/v1", "api_key": FAKE_KEY, "default_model": "m"}}}
    monkeypatch.setattr("agents.registry.load_run_function", lambda s: fake_run)
    monkeypatch.setattr("ui.tabs._generic._health.check", lambda ep: health)
    monkeypatch.setattr("ui.tabs._generic._health.probe_model", lambda ep, model, **k: type("P", (), {"ok": True, "status": "ok", "detail": ""})())
    monkeypatch.setattr("ui._config.load_portable_config", lambda: cfg)
    monkeypatch.setattr("ui._config.output_dir", lambda: tmp_path / "out")
    (tmp_path / "out").mkdir(exist_ok=True)
    values = []
    for inp in skill.inputs:
        if inp.type == "file" and inp.required:
            stub = tmp_path / f"{inp.name}.html"
            stub.write_text("<html></html>", encoding="utf-8")
            values.append(str(stub))
        else:
            values.append("")
    handler = generic_mod._make_run_handler(skill)
    msgs = [m[0] for m in handler(*values, "model-x")]
    return seen, "\n".join(str(m) for m in msgs)


def _down(detail="HTTP 401: Unauthorized"):
    return SimpleNamespace(ok=False, status="unreachable", detail=detail)


# --- the declaration ---------------------------------------------------------

def test_itr_workbook_declares_warn_and_default_is_stop():
    assert _itr().requires.llm_precheck == "warn"
    assert registry.SkillRequires().llm_precheck == "stop"
    others = [s for s in registry.discover() if s.name != "ITR Workbook" and s.requires.llm]
    assert others, "expected at least one other model skill"
    # NEGATIVE: no other skill got the soft check
    assert all(s.requires.llm_precheck == "stop" for s in others)


def test_invalid_precheck_value_is_rejected_loudly(tmp_path):
    d = tmp_path / "skill_bad"
    d.mkdir()
    text = (AGENT_DIR / "skill.yaml").read_text(encoding="utf-8")
    assert "llm_precheck: warn" in text
    (d / "skill.yaml").write_text(text.replace("llm_precheck: warn", "llm_precheck: ignore"),
                                  encoding="utf-8")
    with pytest.raises(ValueError, match="llm_precheck must be 'stop' or 'warn'"):
        registry._parse_manifest(d / "skill.yaml")


# --- the UI check ------------------------------------------------------------

def test_warn_skill_with_failed_check_warns_with_status_and_still_runs(monkeypatch, tmp_path):
    seen, text = _drive(monkeypatch, tmp_path, _itr(), _down("HTTP 401: Unauthorized"))
    assert seen, text                       # the run proceeded
    assert "WARNING" in text and "HTTP 401" in text
    assert "NO AI" in text


def test_default_skill_with_failed_check_still_hard_stops(monkeypatch, tmp_path):
    seen, text = _drive(monkeypatch, tmp_path, _with_mode(_itr(), "stop"), _down())
    # NEGATIVE: the run did not start and the message is the old error
    assert not seen
    assert "Error: endpoint 'ep1' is unreachable" in text
    assert "WARNING" not in text


def test_key_never_appears_in_the_check_output(monkeypatch, tmp_path):
    leaky = _down(f"HTTP 401: Incorrect API key provided: {FAKE_KEY}")
    for mode in ("warn", "stop"):
        _seen, text = _drive(monkeypatch, tmp_path, _with_mode(_itr(), mode), leaky)
        assert FAKE_KEY not in text, mode
        assert "[key hidden]" in text, mode


def test_healthy_endpoint_prints_no_warning(monkeypatch, tmp_path):
    ok = SimpleNamespace(ok=True, status="ok", detail="")
    seen, text = _drive(monkeypatch, tmp_path, _itr(), ok)
    assert seen and "WARNING" not in text and "endpoint OK" in text


# --- the agent side ----------------------------------------------------------

def _html(tmp_path):
    p = tmp_path / "bs.html"
    p.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    return str(p)


def _agent_run(tmp_path, mapping):
    out = tmp_path / "out.xlsx"
    return agent.run(_html(tmp_path), str(out), mapping_file=mapping, entities_path=str(ENTITIES_EXAMPLE),
                     entity_key="SYN-IND", scrips_path=str(SCRIPS_EXAMPLE), config_path="no-such-config.yaml")


def test_unmapped_with_ai_down_shows_notice_and_is_never_clean(tmp_path, monkeypatch):
    def ask(*a):
        raise OSError(f"connection refused {FAKE_KEY}")

    monkeypatch.setattr(suggest, "_ask", ask)
    monkeypatch.setattr(suggest, "_is_cancelled", lambda: False)
    summary = _agent_run(tmp_path, str(FIXTURES / "syn_ind_partial.mapping.yaml"))
    first = summary.splitlines()[0]
    assert first.startswith("WARNING: AI tag suggestions stopped")
    assert "3 of 3 unmapped account(s) have no AI suggestion" in first
    assert "BUILT --" in summary and "REVIEW ITEM(S)" in summary
    assert "Mapping: OK" not in summary
    assert FAKE_KEY not in summary


def test_fully_mapped_run_makes_zero_ai_calls_and_completes(tmp_path, monkeypatch):
    monkeypatch.setattr(suggest, "_ask", lambda *a: pytest.fail("AI called on a fully mapped run"))
    summary = _agent_run(tmp_path, str(FIXTURES / "syn_ind.mapping.yaml"))
    assert "Mapping: OK" in summary and "AI tag suggestions" not in summary
    calls = []
    run = suggest.run_suggestions([], {}, "no-such-config.yaml", ask=lambda *a: calls.append(1))
    assert calls == [] and run.attempted == 0 and run.total == 0
