"""
tests/skill_itr_workbook/test_entity_book.py -- Phase 5 core: shared entity ->
GnuCash book prefill plumbing (GnuCash book registry architecture, 2026-07-30
handover).

Covers:
  1. ui/tabs/_entity_book.py's three functions (entity_choices,
     resolve_for_ui, book_update) directly.
  2. ui/tabs/_generic.py's `_options_from_itr_entities()` back-compat wrapper
     and the `options_from: "itr_entities"` resolver, both now delegating to
     _entity_book.entity_choices().
  3. ui/tabs/_generic.py's render()-time book_from/fy_from wiring: the clear
     error when book_from names a nonexistent input.

Synthetic names only (AliceDoe/BobDoe/CarolDoe/DaveDoe), synthetic PAN
ABCDE1234X. Every ".gnucash" path used here is a tmp_path placeholder file
(created empty, never parsed) -- no real book, no real entities.yaml,
nothing under Data/ or C:\\PortableApps is read. Follows the
patch("ui._config.data_root_dir") monkeypatch pattern established in
test_itr_entities_ui.py / test_registry_wiring.py.
"""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
SRC = ROOT / "src"

for p in (str(SCRIPTS), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import configs  # noqa: E402

from ui.tabs import _entity_book  # noqa: E402
from ui.tabs import _generic as generic_mod  # noqa: E402

PAN = "ABCDE1234X"


def _write_entities(tmp_path: Path, entities: dict) -> Path:
    data_root = tmp_path / "Data"
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(configs.dump_entities(entities), encoding="utf-8")
    return data_root


# ---------------------------------------------------------------------------
# resolve_for_ui
# ---------------------------------------------------------------------------

def test_resolve_for_ui_registered_book_matching_fy(tmp_path):
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
        result = _entity_book.resolve_for_ui("AliceDoe", "2025-26")
    assert result == str(book_path)


def test_resolve_for_ui_fy_omitted_uses_newest_fy(tmp_path):
    older = tmp_path / "BobDoe2425.gnucash"
    newer = tmp_path / "BobDoe2526.gnucash"
    older.write_text("", encoding="utf-8")
    newer.write_text("", encoding="utf-8")
    entities = {
        "BobDoe": configs.EntityProfile(
            key="BobDoe", name="Bob Doe", pan=PAN, status="Individual",
            books={"2024-25": str(older), "2025-26": str(newer)},
        ),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = _entity_book.resolve_for_ui("BobDoe")
    assert result == str(newer)


def test_resolve_for_ui_legacy_book_only(tmp_path):
    book_path = tmp_path / "CarolDoeLegacy.gnucash"
    book_path.write_text("", encoding="utf-8")
    data_root = tmp_path / "Data"
    entities_path = data_root / "itr" / "entities.yaml"
    entities_path.parent.mkdir(parents=True, exist_ok=True)
    entities_path.write_text(
        "CarolDoe:\n"
        "  name: Carol Doe\n"
        f"  pan: {PAN}\n"
        "  status: Individual\n"
        f"  book: {book_path.as_posix()}\n",
        encoding="utf-8",
    )
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = _entity_book.resolve_for_ui("CarolDoe")
    assert result == str(book_path)


def test_resolve_for_ui_unknown_entity_returns_empty(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = _entity_book.resolve_for_ui("NoSuchEntity")
    assert result == ""


def test_resolve_for_ui_empty_entity_returns_empty(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        assert _entity_book.resolve_for_ui("") == ""
        assert _entity_book.resolve_for_ui(None) == ""


def test_resolve_for_ui_registered_path_missing_on_disk_returns_empty(tmp_path):
    entities = {
        "DaveDoe": configs.EntityProfile(
            key="DaveDoe", name="Dave Doe", pan=PAN, status="Individual",
            books={"2025-26": str(tmp_path / "DaveDoe2526.gnucash")},
        ),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = _entity_book.resolve_for_ui("DaveDoe", "2025-26")
    assert result == ""


def test_resolve_for_ui_swallows_resolve_book_exception(tmp_path):
    with patch("ui._config.data_root_dir", return_value=tmp_path / "Data"), \
         patch("ui._book_registry.resolve_book", side_effect=RuntimeError("boom")):
        result = _entity_book.resolve_for_ui("AliceDoe", "2025-26")
    assert result == ""


# ---------------------------------------------------------------------------
# book_update
# ---------------------------------------------------------------------------

def test_book_update_hit_carries_path(tmp_path):
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
        update = _entity_book.book_update("AliceDoe", "2025-26")
    assert isinstance(update, dict)
    assert update.get("value") == str(book_path)


def test_book_update_miss_is_no_change_not_blank(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        update = _entity_book.book_update("NoSuchEntity")
    assert isinstance(update, dict)
    # A miss must NOT carry value="" / None -- that would blank out a path
    # the user already picked manually via Browse. gr.update() with no
    # value= kwarg either omits "value" entirely or uses a sentinel; either
    # way it must not equal "" or None.
    if "value" in update:
        assert update["value"] not in ("", None)


# ---------------------------------------------------------------------------
# entity_choices / backward-compat wrapper / options_from resolver
# ---------------------------------------------------------------------------

def test_entity_choices_shape(tmp_path):
    entities = {
        "AliceDoe": configs.EntityProfile(key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual"),
        "BobDoe": configs.EntityProfile(key="BobDoe", name="Bob Doe", pan=PAN, status="Individual"),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = _entity_book.entity_choices()
    assert result == sorted(result)
    keys = [key for _label, key in result]
    assert "AliceDoe" in keys
    assert "BobDoe" in keys
    for label, key in result:
        assert key in label


def test_entity_choices_empty_when_entities_file_absent(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        assert _entity_book.entity_choices() == []


def test_generic_options_from_itr_entities_wrapper_delegates(tmp_path):
    entities = {
        "CarolDoe": configs.EntityProfile(key="CarolDoe", name="Carol Doe", pan=PAN, status="Individual"),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        via_wrapper = generic_mod._options_from_itr_entities()
        via_new_module = _entity_book.entity_choices()
    assert via_wrapper == via_new_module


def test_generic_options_from_resolvers_itr_entities_resolves_through_entity_book(tmp_path):
    entities = {
        "DaveDoe": configs.EntityProfile(key="DaveDoe", name="Dave Doe", pan=PAN, status="Individual"),
    }
    data_root = _write_entities(tmp_path, entities)
    with patch("ui._config.data_root_dir", return_value=data_root):
        result = generic_mod._resolve_options_from("itr_entities")
    keys = [key for _label, key in result]
    assert "DaveDoe" in keys


# ---------------------------------------------------------------------------
# render()-time book_from wiring error
# ---------------------------------------------------------------------------

def _make_book_from_skill(book_from: str, fy_from: str = ""):
    entity_inp = SimpleNamespace(
        name="entity", type="select", label="Entity",
        required=True, file_types=None, options=[], match="",
        options_from="", book_from="", fy_from="", multiselect=False,
    )
    file_inp = SimpleNamespace(
        name="book_file", type="file", label="GnuCash Book",
        required=False, file_types=(".gnucash",), options=[], match="",
        options_from="", book_from=book_from, fy_from=fy_from, multiselect=False,
    )
    output = SimpleNamespace(type="directory", suffix="out", extension=".txt", download_label="Download")
    requires = SimpleNamespace(native_binaries=[], external_tools=[], llm=False, network=False)
    return SimpleNamespace(
        name="test_book_from_skill",
        display_name="Test Book From Skill",
        description="test",
        inputs=[entity_inp, file_inp],
        output=output,
        requires=requires,
        run_args={},
        mode="direct",
        entry_point="agent:run",
        help=None,
    )


def test_render_raises_clear_error_when_book_from_names_missing_input(monkeypatch):
    import gradio as gr

    monkeypatch.setattr(generic_mod, "_refresh_models", lambda **kw: [("model-x", "model-x")])
    monkeypatch.setattr(generic_mod, "_default_model_value", lambda choices: "model-x")

    skill = _make_book_from_skill(book_from="no_such_input")

    with gr.Blocks():
        with gr.Tab("Test"):
            try:
                generic_mod.render(skill)
                raised = None
            except ValueError as exc:
                raised = exc

    assert raised is not None, "render() should raise when book_from names a nonexistent input"
    msg = str(raised)
    assert "test_book_from_skill" in msg or skill.name in msg
    assert "book_file" in msg
    assert "no_such_input" in msg


def test_render_raises_clear_error_when_fy_from_names_missing_input(monkeypatch):
    import gradio as gr

    monkeypatch.setattr(generic_mod, "_refresh_models", lambda **kw: [("model-x", "model-x")])
    monkeypatch.setattr(generic_mod, "_default_model_value", lambda choices: "model-x")

    skill = _make_book_from_skill(book_from="entity", fy_from="no_such_fy_input")

    with gr.Blocks():
        with gr.Tab("Test"):
            try:
                generic_mod.render(skill)
                raised = None
            except ValueError as exc:
                raised = exc

    assert raised is not None, "render() should raise when fy_from names a nonexistent input"
    msg = str(raised)
    assert "book_file" in msg
    assert "no_such_fy_input" in msg


def test_render_wires_book_from_without_error_when_inputs_exist(monkeypatch):
    """Sanity check: a valid book_from (naming a real input) does not raise,
    and the entity select's .change() gets wired at least once."""
    import gradio as gr

    monkeypatch.setattr(generic_mod, "_refresh_models", lambda **kw: [("model-x", "model-x")])
    monkeypatch.setattr(generic_mod, "_default_model_value", lambda choices: "model-x")

    skill = _make_book_from_skill(book_from="entity")

    changes = []
    original_change = gr.Dropdown.change

    def _tracking_change(self, *args, **kwargs):
        changes.append(kwargs.get("outputs"))
        return original_change(self, *args, **kwargs)

    monkeypatch.setattr(gr.Dropdown, "change", _tracking_change)

    with gr.Blocks():
        with gr.Tab("Test"):
            generic_mod.render(skill)

    assert changes, "entity dropdown .change() should be wired for book_from"


# ---------------------------------------------------------------------------
# Blank default for book_from-source selects
# ---------------------------------------------------------------------------

_SYNTHETIC_CHOICES = [("AliceDoe (Individual)", "AliceDoe"), ("BobDoe (HUF)", "BobDoe")]


def _make_options_from_skill(*, wire_book_from: bool):
    """A skill with one `options_from: itr_entities` select and one file input.
    When wire_book_from is True the file input names that select via book_from,
    which is what should suppress the select's pre-selected first choice."""
    entity_inp = SimpleNamespace(
        name="entity", type="select", label="Entity",
        required=False, file_types=None, options=[], match="",
        options_from="itr_entities", book_from="", fy_from="", multiselect=False,
    )
    file_inp = SimpleNamespace(
        name="book_file", type="file", label="GnuCash Book",
        required=False, file_types=(".gnucash",), options=[], match="",
        options_from="", book_from=("entity" if wire_book_from else ""), fy_from="", multiselect=False,
    )
    output = SimpleNamespace(type="directory", suffix="out", extension=".txt", download_label="Download")
    requires = SimpleNamespace(native_binaries=[], external_tools=[], llm=False, network=False)
    return SimpleNamespace(
        name="test_options_from_skill",
        display_name="Test Options From Skill",
        description="test",
        inputs=[entity_inp, file_inp],
        output=output,
        requires=requires,
        run_args={},
        mode="direct",
        entry_point="agent:run",
        help=None,
    )


def _render_and_capture_entity_dropdown(monkeypatch, skill):
    import gradio as gr

    monkeypatch.setattr(generic_mod, "_refresh_models", lambda **kw: [("model-x", "model-x")])
    monkeypatch.setattr(generic_mod, "_default_model_value", lambda choices: "model-x")
    monkeypatch.setattr(generic_mod, "_resolve_options_from", lambda key: list(_SYNTHETIC_CHOICES))

    captured = {}
    original_dropdown = gr.Dropdown

    def _tracking_dropdown(*args, **kwargs):
        if kwargs.get("label") == "Entity":
            captured["kwargs"] = kwargs
        return original_dropdown(*args, **kwargs)

    monkeypatch.setattr(gr, "Dropdown", _tracking_dropdown)

    with gr.Blocks():
        with gr.Tab("Test"):
            generic_mod.render(skill)

    assert captured, "the entity select should have rendered as a gr.Dropdown"
    return captured["kwargs"]


def test_book_from_source_select_renders_blank(monkeypatch):
    """A select that DRIVES a book_from prefill must start blank.

    Gradio's .change() does not fire for an initial value, so a pre-selected
    entity name would sit above an empty book field and read as "this book
    belongs to that person" when nothing was actually resolved.
    """
    kwargs = _render_and_capture_entity_dropdown(
        monkeypatch, _make_options_from_skill(wire_book_from=True)
    )
    assert kwargs["choices"] == _SYNTHETIC_CHOICES
    assert kwargs["value"] is None, (
        "an options_from select that is a book_from source must not pre-select "
        "an entity -- the book field below it would stay empty"
    )


def test_non_book_from_select_keeps_preselect(monkeypatch):
    """The blank default is targeted: a plain `options_from: itr_entities`
    select that no file input points at (e.g. ITR Workbook's `entity`) keeps
    its existing pre-select-first-choice behaviour."""
    kwargs = _render_and_capture_entity_dropdown(
        monkeypatch, _make_options_from_skill(wire_book_from=False)
    )
    assert kwargs["value"] == _SYNTHETIC_CHOICES[0][1]
