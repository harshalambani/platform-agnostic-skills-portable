"""DBT-01: BoB and HSBC are run through the generic tab. The two hand-written tab
files are gone; the skills are not. In the app they are hidden bank readers that
the generic "Bank Skills" tab runs through the bank picker (agents.banks), and
each keeps its help page. Synthetic, no user data."""
from __future__ import annotations

import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

from agents import banks, registry  # noqa: E402
from ui import webui  # noqa: E402
from ui.tabs import _generic  # noqa: E402


def test_the_old_hand_written_tab_modules_are_gone():
    assert importlib.util.find_spec("ui.tabs.skill_bob") is None
    assert importlib.util.find_spec("ui.tabs.skill_hsbc") is None


def test_bob_and_hsbc_are_still_registered_and_reachable_from_the_bank_picker():   # NEGATIVE
    by_skill = {b.skill_name: b.display_name for b in banks.discover()}
    assert by_skill.get("skill_bob") == "Bank of Baroda"
    assert by_skill.get("skill_hsbc") == "HSBC"
    for name in ("skill_bob", "skill_hsbc"):
        s = registry.get(name)
        assert s is not None and s.hidden          # a reader the Bank Skills tab runs
        assert registry.load_run_function(s)       # and its run function still loads


def test_the_bank_skills_tab_is_still_built_once_through_the_generic_tab():      # NEGATIVE
    calls = []
    orig = _generic.render

    def spy(skill, *a, **k):
        calls.append(skill.name)
        return orig(skill, *a, **k)

    _generic.render = spy
    try:
        webui.build_app(launch=False, lazy=False)
    finally:
        _generic.render = orig
    assert calls.count("skill_bank_skills") == 1


def test_bob_and_hsbc_keep_their_help_pages():                                   # NEGATIVE
    labels = [label for label, _s in webui.help_page_order(registry.discover(refresh=True))]
    assert "Bank Skills > Bank of Baroda" in labels
    assert "Bank Skills > HSBC" in labels


def test_the_generic_run_handler_builds_for_both_skills():
    for name in ("skill_bob", "skill_hsbc"):
        assert callable(_generic._make_run_handler(registry.get(name)))
