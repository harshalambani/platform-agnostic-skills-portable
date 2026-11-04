"""UI-20: the heading over a run's text follows the skill's manifest
(requires.llm): "Agent reply" only where a model answers. And the custom
BoB / HSBC tabs health-check the LLM endpoint only for skills that use one."""
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.registry import discover  # noqa: E402
from ui.tabs import _generic, skill_bob, skill_hsbc  # noqa: E402

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
    assert {"BoB", "HSBC"} <= names
    assert skill_bob._reply_label() == "Result"
    assert skill_hsbc._reply_label() == "Result"


def test_no_hardcoded_agent_reply_heading_left_in_tabs():            # NEGATIVE
    for f in (ROOT / "ui").rglob("*.py"):
        text = f.read_text(encoding="utf-8")
        assert "**Agent reply:**" not in text, f


# ---- health-check gate -------------------------------------------------------

def _drive(gen):
    out = []
    for item in gen:
        out.append(item[0])
    return "\n".join(out)


@pytest.fixture
def wired(tmp_path, monkeypatch):
    calls = []
    bad = SimpleNamespace(ok=False, status="down", detail="synthetic")
    for mod in (skill_bob, skill_hsbc):
        monkeypatch.setattr(mod._health, "check", lambda ep, _c=calls: (_c.append(1), bad)[1])
        monkeypatch.setattr(mod._config, "load_portable_config",
                            lambda: {"endpoints": {"e": {}}, "active_endpoint": "e"})
        monkeypatch.setattr(mod._config, "output_dir", lambda: tmp_path)

        def boom(active):
            raise RuntimeError("stop here")
        monkeypatch.setattr(mod._config, "materialize_legacy_config", boom)
    monkeypatch.setattr(skill_hsbc, "_native_warning_or_none", lambda: None)
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")
    return calls, pdf


def test_bob_does_not_health_check_the_llm_endpoint(wired):          # NEGATIVE
    calls, pdf = wired
    text = _drive(skill_bob._run_bob(str(pdf), ""))
    assert calls == []
    assert "endpoint" not in text.lower().replace("no llm endpoint needed", "")
    assert "stop here" in text          # it carried on to the run


def test_hsbc_does_not_health_check_the_llm_endpoint(wired):         # NEGATIVE
    calls, pdf = wired
    text = _drive(skill_hsbc._run_hsbc(str(pdf), ""))
    assert calls == []
    assert "stop here" in text


def test_a_skill_that_uses_a_model_is_still_health_checked(wired, monkeypatch):
    calls, pdf = wired
    monkeypatch.setattr(skill_bob, "skill_uses_llm", lambda name: True)
    text = _drive(skill_bob._run_bob(str(pdf), ""))
    assert calls == [1]
    assert "is down" in text
