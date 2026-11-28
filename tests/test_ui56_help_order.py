"""UI-56: the Help tab lists every help page in app order, with its tab path. Real registry,
no user data."""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

from agents import banks  # noqa: E402
from ui import _help, webui  # noqa: E402


@pytest.fixture(scope="module")
def skills_all():
    return webui._discover_skills()


@pytest.fixture(scope="module")
def entries(skills_all):
    return webui.help_page_order(skills_all)


def _helped(s) -> bool:
    return bool(s.help and not s.help.is_empty())


def _label_of(entries, name):
    return next(label for label, s in entries if s.name == name)


def test_every_skill_with_help_appears_exactly_once(skills_all, entries):
    helped = [s for s in skills_all if _helped(s)]
    values = [s.name for _, s in entries]
    assert len(values) == len(set(values)) == len(helped)
    assert set(values) == {s.name for s in helped}
    labels = [label for label, _ in entries]
    assert len(labels) == len(set(labels))          # no two pages read alike


def test_shared_display_names_each_open_their_own_page(skills_all, entries):
    # NEGATIVE: the old dict keyed on display name collapsed these.
    by_disp: dict[str, list] = {}
    for _, s in entries:
        by_disp.setdefault(s.display_name, []).append(s)
    shared = {d: v for d, v in by_disp.items() if len(v) > 1}
    assert {"Convert", "Convert to GnuCash"} <= set(shared)
    for group in shared.values():
        bodies = []
        for s in group:
            md, _html = _help.help_page(entries, s.name)
            assert md == _help._markdown_body(s)    # its own skill's help, not a sibling's
            bodies.append(md)
        assert len(set(bodies)) == len(bodies)


def test_visible_part_equals_app_tab_order(skills_all, entries):
    visible = [s for s in skills_all if not s.hidden]
    expect = [(" > ".join(p), s.name) for s, p in webui.app_tab_order(visible) if _helped(s)]
    got = [(label, s.name) for label, s in entries if not s.hidden]
    assert got == expect


def test_bank_readers_follow_bank_skills_in_picker_order(entries):
    names = [s.name for _, s in entries]
    i = names.index("skill_bank_skills")
    readers = [b.skill_name for b in banks.discover()]
    assert names[i + 1:i + 1 + len(readers)] == readers
    assert [label for label, _ in entries[i + 1:i + 1 + len(readers)]] == [
        f"Bank Skills > {b.display_name}" for b in banks.discover()]


def test_a_swapped_pair_fails_the_order_check(skills_all, entries):
    # NEGATIVE: the order guard really does notice a swap.
    visible = [s for s in skills_all if not s.hidden]
    expect = [(" > ".join(p), s.name) for s, p in webui.app_tab_order(visible) if _helped(s)]
    swapped = [(label, s.name) for label, s in entries if not s.hidden]
    swapped[3], swapped[4] = swapped[4], swapped[3]
    assert swapped != expect


def test_labels_nested_flat_and_bank_reader(entries):
    labels = {s.name: label for label, s in entries}
    assert labels["26AS"] == "GnuCash > 26AS > Convert"
    assert labels["KRC Ledger"] == "GnuCash > KRChoksey > Convert"
    assert labels["CSV Analyzer"] == "Other > CSV Data Analyzer"
    assert labels["Parser Generator"] == "Parser Generator"
    assert labels["skill_bank_skills"] == "Bank Skills"
    assert labels["skill_hdfc"] == "Bank Skills > HDFC"


def test_count_is_24_visible_plus_6_readers(entries):
    assert len(entries) == 30
    assert sum(1 for _, s in entries if s.hidden) == 6


class _S:
    def __init__(self, name, display, hidden=False, help_=True, category="other"):
        self.name, self.display_name, self.hidden, self.category = name, display, hidden, category
        self.help = type("H", (), {"is_empty": lambda self: not help_})() if help_ is not None else None


def test_a_skill_without_help_is_not_listed(skills_all):
    # NEGATIVE
    bare = _S("no_help", "No Help", help_=False)
    out = webui.help_page_order(list(skills_all) + [bare])
    assert "no_help" not in [s.name for _, s in out]
    none = _S("none_help", "None Help", help_=None)
    assert "none_help" not in [s.name for _, s in webui.help_page_order(list(skills_all) + [none])]


def test_an_unknown_value_falls_back_to_the_first_page(entries):
    first = _help.help_page(entries, entries[0][1].name)
    assert _help.help_page(entries, "no_such_skill") == first
    assert _help.help_page(entries, "Convert") == first   # an old display-name value is not a key


def test_an_unknown_hidden_skill_goes_at_the_end_under_its_display_name(skills_all, entries):
    extra = _S("skill_other_hidden", "Other Hidden", hidden=True)
    out = webui.help_page_order(list(skills_all) + [extra])
    assert out[-1] == ("Other Hidden", extra)
    assert out[:-1] == entries
