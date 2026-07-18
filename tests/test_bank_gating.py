"""
tests/test_bank_gating.py — regression guard for the "offer-then-reject"
bank-gating bug (2026-07-18-bank-registry-gating-followup-prompt.md).

Context: onboarding Kotak (#89) added it to the skill_gnucash_pipeline `bank`
dropdown (skill.yaml options:) but NOT to agent.py's hardcoded
DEDICATED_BANKS list, so selecting "Kotak" in the UI passed the dropdown but
then failed the SUPPORTED_BANKS guard at runtime. The fix makes BOTH gating
surfaces (the dropdown's options_from resolver, and DEDICATED_BANKS) derive
from the single source of truth, agents.banks.discover(), so they can never
diverge again.

These tests are the permanent guard against this whole bug class recurring
for any future bank (#6, #7, ...).

Run with:
    cd src && python -m pytest ../tests/test_bank_gating.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
UI = ROOT / "ui"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agents import banks
from agents.skill_gnucash_pipeline.agent import (
    CSV_BANKS,
    DEDICATED_BANKS,
    SUPPORTED_BANKS,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    banks._cache = None
    yield
    banks._cache = None


# ---------------------------------------------------------------------------
# Gate 1 (spec acceptance gate 1) — the offer-then-reject bug is dead.
# ---------------------------------------------------------------------------

def test_kotak_passes_the_supported_banks_guard():
    """"Kotak" is offered in the dropdown (skill.yaml) -- it must also pass
    the SUPPORTED_BANKS guard at agent.py:798, or selecting it in the UI
    dead-ends in a runtime error (the exact bug this session fixes)."""
    assert "Kotak" in SUPPORTED_BANKS


def test_dedicated_banks_matches_registry_display_names():
    """DEDICATED_BANKS must be derived from agents.banks.discover(), not a
    hardcoded literal -- this is what makes onboarding bank #6 automatically
    extend the gate without another hand-edit here."""
    assert DEDICATED_BANKS == [b.display_name for b in banks.discover()]


def test_supported_banks_is_dedicated_plus_csv():
    assert SUPPORTED_BANKS == DEDICATED_BANKS + CSV_BANKS


def test_all_five_discovered_banks_pass_the_guard():
    discovered_names = [b.display_name for b in banks.discover()]
    assert len(discovered_names) == 5
    for name in discovered_names:
        assert name in SUPPORTED_BANKS


# ---------------------------------------------------------------------------
# Gate 2 (spec acceptance gate 2) — dropdown == registry conformance.
# This is the permanent guard against the whole bug class returning: if a
# future bank is added to the registry but the dropdown resolver silently
# stops tracking it (or vice versa), this test fails.
# ---------------------------------------------------------------------------

def test_options_from_banks_matches_registry_plus_other_bank_csv():
    from ui.tabs._generic import _resolve_options_from

    resolved = _resolve_options_from("banks")
    discovered_names = [b.display_name for b in banks.discover()]
    expected = [(name, name) for name in discovered_names] + [
        ("Other Bank (CSV)", "Other Bank (CSV)")
    ]
    assert resolved == expected


def test_options_from_banks_puts_other_bank_csv_last():
    from ui.tabs._generic import _resolve_options_from

    resolved = _resolve_options_from("banks")
    assert resolved[-1] == ("Other Bank (CSV)", "Other Bank (CSV)")


def test_banks_option_source_is_registered():
    from ui.tabs._generic import _OPTIONS_FROM_RESOLVERS

    assert "banks" in _OPTIONS_FROM_RESOLVERS


def test_dropdown_offers_exactly_what_the_guard_accepts():
    """The actual regression: every option the dropdown can produce must
    pass the SUPPORTED_BANKS guard, and vice versa (modulo "Other Bank
    (CSV)" which is CSV_BANKS, not a registered bank)."""
    from ui.tabs._generic import _resolve_options_from

    dropdown_values = [value for _, value in _resolve_options_from("banks")]
    assert set(dropdown_values) == set(SUPPORTED_BANKS)


# ---------------------------------------------------------------------------
# Gate 4 (spec acceptance gate 4) — no hardcoded bank display-name list
# remains as a literal in skill.yaml `options:` or in agent.py.
# ---------------------------------------------------------------------------

def test_skill_yaml_has_no_static_bank_options_list():
    import yaml

    skill_yaml = SRC / "agents" / "skill_gnucash_pipeline" / "skill.yaml"
    raw = yaml.safe_load(skill_yaml.read_text(encoding="utf-8"))
    bank_input = next(i for i in raw["inputs"] if i["name"] == "bank")
    assert "options" not in bank_input, (
        "skill.yaml's 'bank' input still has a static options: list -- "
        "this is exactly the divergence-prone pattern being removed."
    )
    assert bank_input.get("options_from") == "banks"
