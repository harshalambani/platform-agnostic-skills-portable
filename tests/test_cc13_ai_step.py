"""CC-13 (design B): Credit Card - Book Spends declares its AI step, so it gets the Model
dropdown and the pre-run AI check; the chosen model reaches the mapper; the descriptions no
longer say 'nothing is guessed' / 'no LLM'; AI matches still pass the CC-10 guard.
Synthetic data only; no network."""
from __future__ import annotations

import csv
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import registry                                              # noqa: E402
from agents.skill_cc_spend_booking import agent as ccsb                  # noqa: E402
from tests import test_cc_spend_booking as T                             # noqa: E402
from tests.test_cc_spend_booking import APR, MAY, Guard, book_pay, go, may_rows, row, stmt, tie   # noqa: E402
from tests.test_cc10_impossible_targets import INC, LAND, SUSP           # noqa: E402

SKILL = "CC Book Spends"
FAKE_KEY = "sk-FAKE0000000000000000KEYabcdef1234"


def _skill():
    sk = registry.get(SKILL)
    assert sk is not None
    return sk


def test_the_skill_declares_its_ai_step_and_keeps_the_hard_stop_precheck():
    sk = _skill()
    assert sk.requires.llm is True
    assert sk.requires.llm_precheck == "stop"          # a dead endpoint is caught BEFORE the run


def test_the_descriptions_no_longer_claim_nothing_is_guessed_or_no_llm():     # NEGATIVE
    sk = _skill()
    assert "Nothing is guessed" not in sk.description
    assert "AI step" in sk.description
    mapper = (ROOT / "src/agents/skill_gnucash_account_mapper/skill.yaml").read_text(encoding="utf-8")
    assert "No LLM required" not in mapper and "fully deterministic" not in mapper
    code = (ROOT / "src/agents/skill_gnucash_account_mapper/agent.py").read_text(encoding="utf-8")
    assert "Unused (no LLM required)" not in code


def test_the_chosen_model_is_the_one_the_mapper_is_given(tmp_path, monkeypatch):
    seen = {}

    def mapper(gnucash, cin, cout, config, drawings, default, model=None):
        seen["model"] = model
        return T.fake_mapper_factory([])(gnucash, cin, cout, config, drawings, default, model)

    s1, s2 = stmt(APR, T.april_rows()), stmt(MAY, may_rows())
    go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=1900.0), tie(s2, printed=300.0)],
       [book_pay("p1", 1900, date(2025, 5, 12))], mapper=mapper, model="synthetic-model-b")
    assert seen["model"] == "synthetic-model-b"


def test_run_mapper_passes_model_override_to_the_real_mapper(monkeypatch, tmp_path):
    got = {}
    if str(ccsb._AGENTS_ROOT) not in sys.path:
        sys.path.insert(0, str(ccsb._AGENTS_ROOT))
    import skill_gnucash_account_mapper.agent as mm        # the module _run_mapper imports
    monkeypatch.setattr(mm, "run", lambda *a, **k: got.update(k))
    ccsb._run_mapper("b.gnucash", "in.csv", "out.csv", "cfg.yaml", [], "", "model-x")
    assert got["model_override"] == "model-x"
    ccsb._run_mapper("b.gnucash", "in.csv", "out.csv", "cfg.yaml", [], "")
    assert got["model_override"] is None                 # no choice: the endpoint default, never a stale one


def _drive(monkeypatch, tmp_path, health):
    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        return "done"

    cfg = {"active_endpoint": "ep1", "endpoints": {"ep1": {"provider": "openai_compatible",
           "base_url": "http://127.0.0.1:9/v1", "api_key": FAKE_KEY, "default_model": "m"}}}
    monkeypatch.setattr("agents.registry.load_run_function", lambda s: fake_run)
    monkeypatch.setattr("ui.tabs._generic._health.check", lambda ep: health)
    monkeypatch.setattr("ui.tabs._generic._health.probe_model", lambda ep, model, **k: type("P", (), {"ok": True, "status": "ok", "detail": ""})())
    monkeypatch.setattr("ui._config.load_portable_config", lambda: cfg)
    monkeypatch.setattr("ui._config.output_dir", lambda: tmp_path / "out")
    (tmp_path / "out").mkdir(exist_ok=True)
    skill = _skill()
    values = []
    for inp in skill.inputs:
        if inp.type == "file" and inp.required:
            stub = tmp_path / f"{inp.name}.gnucash"
            stub.write_text("x", encoding="utf-8")
            values.append(str(stub))
        elif inp.type == "directory" and inp.required:
            d = tmp_path / inp.name
            d.mkdir(exist_ok=True)
            values.append(str(d))
        else:
            values.append("x" if inp.required else "")
    from ui.tabs import _generic as generic_mod
    handler = generic_mod._make_run_handler(skill)
    msgs = [m[0] for m in handler(*values, "model-x")]
    return seen, "\n".join(str(m) for m in msgs)


def test_a_dead_endpoint_is_caught_before_the_run_not_mid_run(monkeypatch, tmp_path):       # NEGATIVE
    seen, text = _drive(monkeypatch, tmp_path, SimpleNamespace(ok=False, status="unreachable", detail="HTTP 401"))
    assert not seen, "the run must not start"
    assert "Error: endpoint 'ep1' is unreachable" in text and FAKE_KEY not in text


def test_a_healthy_endpoint_lets_the_run_start_with_the_chosen_model(monkeypatch, tmp_path):
    seen, text = _drive(monkeypatch, tmp_path, SimpleNamespace(ok=True, status="ok", detail=""))
    assert seen.get("model_override") == "model-x", (seen, text)


def _ai_run(tmp_path, monkeypatch, rows, target, ttype):
    monkeypatch.setattr(T, "MAP_OVERRIDE", {"UPI": (target, "llm", "LLM: synthetic pick")})
    monkeypatch.setattr(Guard, "types", {target: ttype})
    monkeypatch.setattr(T, "PATHS", T.PATHS | {INC, LAND, SUSP})
    s1 = stmt(APR, rows)
    total = sum(r.amount for r in rows)
    s2 = stmt(MAY, may_rows(pay=total))
    return go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=total), tie(s2, printed=300.0)],
              [book_pay("p1", total, date(2025, 5, 12))])


def test_an_ai_match_to_an_income_account_is_never_ready(tmp_path, monkeypatch):             # NEGATIVE
    reply, rows, *_ = _ai_run(tmp_path, monkeypatch, [row("UPI-SYNTH PERSON", 700, (4, 5))], INC, "INCOME")
    assert INC not in {r["Account"] for r in rows}
    assert "income account" in str(reply)


def test_an_ai_match_to_a_weak_asset_account_is_never_ready(tmp_path, monkeypatch):          # NEGATIVE
    reply, rows, *_ = _ai_run(tmp_path, monkeypatch, [row("UPI-SYNTH PERSON", 900, (4, 6))], LAND, "ASSET")
    assert LAND not in {r["Account"] for r in rows}
