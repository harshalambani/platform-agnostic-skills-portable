"""UI-20: the heading over a run's text follows the skill's manifest
(requires.llm): "Agent reply" only where a model answers. And the custom
generic tab (which BoB and HSBC use) health-checks the LLM endpoint only for skills
that use one."""
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent

from agents.registry import discover  # noqa: E402
from ui.tabs import _generic  # noqa: E402

SKILLS = discover(refresh=True)
DETERMINISTIC = [s for s in SKILLS if not s.requires.llm]
LLM = [s for s in SKILLS if s.requires.llm]


@pytest.mark.parametrize("skill", DETERMINISTIC, ids=lambda s: s.name)
def test_deterministic_skill_never_renders_agent_reply(skill):       # NEGATIVE
    assert _generic.reply_label(skill) != "Agent reply"
    assert _generic.reply_label(skill) == "Result"
    assert _generic.reply_label_for(skill.name) == "Result"


def test_a_model_skill_still_renders_agent_reply():
    assert LLM, "registry has no LLM skill to check"
    for s in LLM:
        assert _generic.reply_label(s) == "Agent reply"


def test_bob_and_hsbc_are_deterministic_and_decide_from_their_own_flag():
    names = {s.name for s in DETERMINISTIC}
    assert {"skill_bob", "skill_hsbc"} <= names
    from agents import registry
    assert registry.get("BoB").name == "skill_bob"       # old names still resolve
    assert registry.get("HSBC").name == "skill_hsbc"
    assert registry.get("BoB") in DETERMINISTIC
    assert registry.get("HSBC") in DETERMINISTIC
    assert _generic.reply_label(registry.get("BoB")) == "Result"
    assert _generic.reply_label(registry.get("HSBC")) == "Result"


def test_no_hardcoded_agent_reply_heading_left_in_tabs():            # NEGATIVE
    for f in (ROOT / "ui").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "**Agent reply:**" not in text, f


# ---- health-check gate -------------------------------------------------------

def _drive(gen):
    out = []
    for item in gen:
        out.append(item[0])
    return " ".join(out)


def _args(skill, pdf):
    """One value per declared input: a folder for a directory input, else a file list."""
    return [str(pdf.parent) if i.type == "directory"
            else [str(pdf)] if i.type == "files" else str(pdf) for i in skill.inputs]


@pytest.fixture
def wired(tmp_path, monkeypatch):
    calls = []
    bad = SimpleNamespace(ok=False, status="down", detail="synthetic")
    monkeypatch.setattr(_generic._health, "check", lambda ep, _c=calls: (_c.append(1), bad)[1])
    monkeypatch.setattr(_generic._health, "check_with_model",
                        lambda ep, model, active, _c=calls: (_c.append(1), bad, model)[1:])
    monkeypatch.setattr(_generic._config, "load_portable_config",
                        lambda: {"endpoints": {"e": {}}, "active_endpoint": "e"})
    monkeypatch.setattr(_generic._config, "output_dir", lambda: tmp_path)
    monkeypatch.setattr(_generic, "_check_native_binaries", lambda skill: None)
    monkeypatch.setattr(_generic, "_check_external_tools", lambda skill: None)

    def boom(active):
        raise RuntimeError("stop here")
    monkeypatch.setattr(_generic._config, "materialize_legacy_config", boom)
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    return calls, pdf


@pytest.mark.parametrize("skill_name", ["skill_bob", "skill_hsbc"])
def test_bob_and_hsbc_do_not_health_check_the_llm_endpoint(skill_name, wired):   # NEGATIVE
    from agents import registry
    calls, pdf = wired
    skill = registry.get(skill_name)
    text = _drive(_generic._make_run_handler(skill)(*_args(skill, pdf), ""))
    assert calls == []
    assert "is down" not in text
    assert "stop here" in text          # it carried on to the run


def test_a_skill_that_uses_a_model_is_still_health_checked(wired):
    calls, pdf = wired
    llm_skill = next(s for s in SKILLS if s.requires.llm and len(s.inputs) >= 1)
    text = _drive(_generic._make_run_handler(llm_skill)(*_args(llm_skill, pdf), ""))
    assert calls, "an LLM skill must be health-checked"
