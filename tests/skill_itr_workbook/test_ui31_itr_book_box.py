"""
tests/skill_itr_workbook/test_ui31_itr_book_box.py -- UI-31.

The ITR Workbook's GnuCash book box is a path textbox filled from the chosen
entity and FY (like the other book-taking skills), not a file-upload box.
Picking the book is never a copy: nothing is staged, nothing is refilled at
Run time.

Synthetic names only; every ".gnucash" path is an empty tmp_path placeholder.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
for p in (str(SRC / "agents" / "skill_itr_workbook" / "scripts"), str(SRC), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

import gradio as gr  # noqa: E402

import configs  # noqa: E402
from agents import registry  # noqa: E402
from ui.tabs import _entity_book  # noqa: E402
from ui.tabs import _generic as generic_mod  # noqa: E402

PAN = "ABCDE1234X"
BOOK_LABEL_PREFIX = "GnuCash book"


def _itr_skill():
    skill = registry.get("ITR Workbook")
    assert skill is not None
    return skill


def _entities(tmp_path: Path):
    a = tmp_path / "AliceDoe2526.gnucash"
    b = tmp_path / "AliceDoe2425.gnucash"
    c = tmp_path / "BobDoe2526.gnucash"
    for f in (a, b, c):
        f.write_text("", encoding="utf-8")
    ents = {
        "AliceDoe": configs.EntityProfile(
            key="AliceDoe", name="Alice Doe", pan=PAN, status="Individual",
            books={"2025-26": str(a), "2024-25": str(b)}),
        "BobDoe": configs.EntityProfile(
            key="BobDoe", name="Bob Doe", pan="ABCDE1235X", status="Individual",
            books={"2025-26": str(c)}),
        "CarolDoe": configs.EntityProfile(
            key="CarolDoe", name="Carol Doe", pan="ABCDE1236X", status="Individual"),
    }
    root = tmp_path / "Data"
    path = root / "itr" / "entities.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(configs.dump_entities(ents), encoding="utf-8")
    return root, a, b, c


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def test_book_file_is_wired_to_entity_and_ay():
    inp = next(i for i in _itr_skill().inputs if i.name == "book_file")
    assert inp.book_from == "entity"
    assert inp.fy_from == "ay"
    assert inp.required is False
    assert inp.label.startswith(BOOK_LABEL_PREFIX)
    assert "optional" in inp.label


def test_ay_select_is_the_bare_fy_list():
    ay = next(i for i in _itr_skill().inputs if i.name == "ay")
    assert ay.options_from == "itr_ay_years"


# ---------------------------------------------------------------------------
# fill behaviour
# ---------------------------------------------------------------------------

def test_entity_and_fy_fill_the_registered_book(tmp_path):
    root, a, _b, _c = _entities(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        upd = _entity_book.book_update("AliceDoe", "2025-26")
    assert upd.get("value") == str(a)


def test_no_registered_book_shows_the_line_and_fills_nothing(tmp_path):
    root, *_ = _entities(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        msg = _entity_book.book_status("CarolDoe", "2025-26")
        upd = _entity_book.book_update("CarolDoe", "2025-26")
    assert "No registered book for **CarolDoe**" in msg
    # NEGATIVE: no value is set (and the box is not blanked either)
    assert "value" not in upd or upd["value"] in (None,) or upd["value"] is gr.update()


def test_fy_picks_that_fys_book_and_never_another_entitys(tmp_path):
    root, a, b, c = _entities(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        assert _entity_book.resolve_for_ui("AliceDoe", "2024-25") == str(b)
        assert _entity_book.resolve_for_ui("AliceDoe", "2025-26") == str(a)
        assert _entity_book.resolve_for_ui("BobDoe", "2025-26") == str(c)
        # NEGATIVE: never the other entity's book, never the other FY's
        assert _entity_book.resolve_for_ui("BobDoe", "2025-26") != str(a)
        assert _entity_book.resolve_for_ui("AliceDoe", "2025-26") != str(b)
        assert _entity_book.resolve_for_ui("CarolDoe", "2025-26") in ("", None)


# ---------------------------------------------------------------------------
# rendering: a textbox, never an upload
# ---------------------------------------------------------------------------

def _rendered_blocks(monkeypatch):
    monkeypatch.setattr(generic_mod, "_refresh_models", lambda **kw: [("model-x", "model-x")])
    monkeypatch.setattr(generic_mod, "_default_model_value", lambda choices: "model-x")
    with gr.Blocks() as demo:
        with gr.Tab("Test"):
            generic_mod.render(_itr_skill())
    return demo


def test_book_file_never_renders_as_a_file_upload(monkeypatch):
    demo = _rendered_blocks(monkeypatch)
    book_comps = [b for b in demo.blocks.values()
                  if str(getattr(b, "label", "") or "").startswith(BOOK_LABEL_PREFIX)]
    assert book_comps, "the book box should have rendered"
    assert all(isinstance(b, gr.Textbox) for b in book_comps)
    assert not any(isinstance(b, gr.File) for b in book_comps)


def test_entity_select_starts_blank(monkeypatch):
    demo = _rendered_blocks(monkeypatch)
    ent = [b for b in demo.blocks.values() if getattr(b, "label", "") == "Entity"]
    assert ent and all(b.value in (None, "", []) for b in ent)


# ---------------------------------------------------------------------------
# run time
# ---------------------------------------------------------------------------

def _run(monkeypatch, tmp_path, book_value):
    skill = _itr_skill()
    seen = {}

    def fake_run(**kw):
        seen.update(kw)
        return "done"

    monkeypatch.setattr("agents.registry.load_run_function", lambda s: fake_run)
    monkeypatch.setattr("ui._config.output_dir", lambda: tmp_path / "out")
    (tmp_path / "out").mkdir(exist_ok=True)
    values = []
    for inp in skill.inputs:
        if inp.name == "entity":
            values.append("AliceDoe")
        elif inp.name == "ay":
            values.append("2025-26")
        elif inp.name == "book_file":
            values.append(book_value)
        elif inp.type == "file" and inp.required:
            stub = tmp_path / f"{inp.name}.html"
            stub.write_text("<html></html>", encoding="utf-8")
            values.append(str(stub))
        else:
            values.append("")
    handler = generic_mod._make_run_handler(skill)
    msgs = [m[0] for m in handler(*values, "model-x")]
    return seen, msgs


def test_empty_book_box_passes_no_book_and_is_not_refilled(monkeypatch, tmp_path):
    root, *_ = _entities(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        seen, msgs = _run(monkeypatch, tmp_path, "")
    assert seen, msgs               # the skill did run
    assert seen.get("book_file", "") == ""


def test_picked_book_path_is_passed_as_is_never_copied(monkeypatch, tmp_path):
    root, a, *_ = _entities(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        seen, msgs = _run(monkeypatch, tmp_path, str(a))
    assert seen, msgs
    assert seen["book_file"] == str(a)
    assert not list((tmp_path / "out").glob("*.gnucash"))
