"""
tests/test_gnucash_entity_book_prefill.py -- entity-dropdown prefill wiring
added to the three hand-written GnuCash review tabs (GnuCash book registry
architecture, Phase 5 handover follow-on, 2026-07-30).

Each of ui/tabs/gnucash_review.py, ui/tabs/krc_gnucash_review.py, and
ui/tabs/tds_journal_review.py now renders an optional "Entity (optional --
auto-fills the GnuCash book from the registry)" gr.Dropdown + refresh
button above its existing GnuCash-book gr.File row. That label is the
canonical entity-dropdown wording shared with the skill.yaml-driven tabs.
Selecting an entity calls _entity_book.book_update(entity_key,
None) to prefill the book path from the registry; a registry miss leaves
the field untouched (never blanks a manually-picked path), and using
"Browse..." afterward always wins because it unconditionally overwrites the
value.

Covers:
  1. render() still constructs without error for all three tabs (with the
     new entity dropdown present).
  2. The entity dropdown's .change() handler, captured the same way
     tests/test_gnucash_book_browse.py captures the Browse button's .click()
     handler, returns a value-carrying update on a registry hit and a
     no-change update on a registry miss -- for each of the three tabs.
  3. Where a Reset button/handler exists (gnucash_review.py,
     tds_journal_review.py), its return tuple's arity exactly matches its
     wired `outputs=[...]` list length (the thing the task flagged as easy
     to break by adding a new output without updating the return tuple, or
     vice versa). krc_gnucash_review.py has no Reset handler at all -- see
     test_krc_gnucash_review_has_no_reset_handler below, a deliberate
     structural discrepancy from the other two tabs.

Synthetic entities only (AliceDoe/BobDoe, surname Doe, PAN ABCDE1234X).
Every ".gnucash" path used here is a tmp_path placeholder file (created
empty, never parsed) -- no real book, no real entities.yaml, nothing under
Data/ or C:\\PortableApps is read.

Run with:
    cd "<repo>" && python -m pytest tests/test_gnucash_entity_book_prefill.py -v
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import gradio as gr
import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
SRC = ROOT / "src"
for p in (str(SCRIPTS), str(SRC), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import configs  # noqa: E402

PAN = "ABCDE1234X"
ENTITY_DD_LABEL = "Entity (optional -- auto-fills the GnuCash book from the registry)"

MODULES = [
    "ui.tabs.gnucash_review",
    "ui.tabs.tds_journal_review",
    "ui.tabs.krc_gnucash_review",
]


def _write_entities(tmp_path: Path, entities: dict) -> Path:
    data_root = tmp_path / "Data"
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(configs.dump_entities(entities), encoding="utf-8")
    return data_root


def _capture_entity_change(tmp_path: Path, render_module_path: str, render_kwargs=None):
    """Render `<render_module_path>.render()` inside a gr.Blocks() context
    with output_dir() pointed at an empty tmp_path, and return the exact fn
    registered on the entity dropdown's `.change(...)`."""
    import importlib

    module = importlib.import_module(render_module_path)
    importlib.reload(module)

    captured: dict = {}
    orig_change = gr.Dropdown.change

    def _tracking_change(self, fn=None, **kwargs):
        if self.label == ENTITY_DD_LABEL:
            captured["fn"] = fn
        return orig_change(self, fn=fn, **kwargs)

    with patch.object(gr.Dropdown, "change", _tracking_change):
        with patch.object(module._config_mod, "output_dir", return_value=tmp_path):
            with gr.Blocks():
                module.render(**(render_kwargs or {}))

    assert "fn" in captured, (
        f"{render_module_path}.render() did not wire a .change() handler on "
        f"the {ENTITY_DD_LABEL!r} dropdown"
    )
    return module, captured["fn"]


def _capture_reset(tmp_path: Path, render_module_path: str):
    """Render and return (fn, outputs) registered on the Reset button's
    .click(...), or (None, None) if the tab has no Reset button."""
    import importlib

    module = importlib.import_module(render_module_path)
    importlib.reload(module)

    captured: dict = {}
    orig_click = gr.Button.click

    def _tracking_click(self, fn=None, **kwargs):
        if self.value == "Reset":
            captured["fn"] = fn
            captured["outputs"] = kwargs.get("outputs")
        return orig_click(self, fn=fn, **kwargs)

    with patch.object(gr.Button, "click", _tracking_click):
        with patch.object(module._config_mod, "output_dir", return_value=tmp_path):
            with gr.Blocks():
                module.render()

    return captured.get("fn"), captured.get("outputs")


# ---------------------------------------------------------------------------
# render() still constructs without error
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_path", MODULES)
def test_render_constructs_without_error(module_path, tmp_path):
    import importlib

    module = importlib.import_module(module_path)
    importlib.reload(module)
    with patch.object(module._config_mod, "output_dir", return_value=tmp_path):
        with gr.Blocks():
            module.render()


# ---------------------------------------------------------------------------
# entity dropdown .change() handler -- registry hit / miss
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("module_path", MODULES)
def test_entity_dropdown_wired_and_present(module_path, tmp_path):
    """Every tab renders the optional entity dropdown and wires its
    .change() to a handler (proves the dropdown + wiring exist at all)."""
    _capture_entity_change(tmp_path, module_path)


@pytest.mark.parametrize("module_path", MODULES)
def test_entity_change_registry_hit_carries_book_path(module_path, tmp_path):
    module, fn = _capture_entity_change(tmp_path, module_path)

    book_path = tmp_path / "AliceDoe2526.gnucash"
    book_path.write_text("", encoding="utf-8")
    entities = {
        "AliceDoe": configs.EntityProfile(
            key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual",
            books={"2025-26": str(book_path)},
        ),
    }
    data_root = _write_entities(tmp_path, entities)

    with patch("ui._config.data_root_dir", return_value=data_root):
        book_upd, status_upd = fn("AliceDoe")

    assert book_upd == {"value": str(book_path), "__type__": "update"}
    # ...and the user is told the field below is already answered for, since
    # a silently-filled box is otherwise indistinguishable from a stale one.
    assert status_upd["visible"] is True
    assert "do not need to pick" in status_upd["value"]


@pytest.mark.parametrize("module_path", MODULES)
def test_entity_change_registry_miss_is_no_change_not_blank(module_path, tmp_path):
    """A registry miss must not blank a path the user already picked
    manually via Browse -- book_update() returns a bare gr.update()."""
    module, fn = _capture_entity_change(tmp_path, module_path)

    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        book_upd, status_upd = fn("NoSuchEntity")

    assert book_upd == {"__type__": "update"}
    # An untouched field looks identical to a filled one that happens to be
    # empty, so the miss has to be said out loud.
    assert status_upd["visible"] is True
    assert "No registered book" in status_upd["value"]


@pytest.mark.parametrize("module_path", MODULES)
def test_entity_change_empty_selection_is_no_change(module_path, tmp_path):
    """An empty/blank entity selection (dropdown cleared) must also be a
    no-op update -- the dropdown is optional and must never force a value."""
    module, fn = _capture_entity_change(tmp_path, module_path)

    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        book_upd, status_upd = fn("")

    assert book_upd == {"__type__": "update"}
    # Nothing picked, nothing to say -- the status line stays hidden rather
    # than nagging about a book for an entity the user never chose.
    assert status_upd["visible"] is False
    assert status_upd["value"] == ""


# ---------------------------------------------------------------------------
# Reset-handler arity -- gnucash_review.py, tds_journal_review.py
# ---------------------------------------------------------------------------

RESET_MODULES = [
    "ui.tabs.gnucash_review",
    "ui.tabs.tds_journal_review",
]


@pytest.mark.parametrize("module_path", RESET_MODULES)
def test_reset_handler_return_arity_matches_outputs_list(module_path, tmp_path):
    fn, outputs = _capture_reset(tmp_path, module_path)
    assert fn is not None, f"{module_path}.render() has no Reset button wired"
    assert outputs is not None

    result = fn()
    assert isinstance(result, tuple)
    assert len(result) == len(outputs), (
        f"{module_path}: reset handler returned {len(result)} values but "
        f"outputs=[...] lists {len(outputs)} components"
    )


def test_krc_gnucash_review_has_no_reset_handler(tmp_path):
    """Deviation from the other two tabs, called out explicitly: this tab
    has no Reset button/handler at all, so there is nothing to keep in sync
    with the new entity dropdown on reset here."""
    fn, outputs = _capture_reset(tmp_path, "ui.tabs.krc_gnucash_review")
    assert fn is None
    assert outputs is None
