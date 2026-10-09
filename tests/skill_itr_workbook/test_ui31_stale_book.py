"""
tests/skill_itr_workbook/test_ui31_stale_book.py -- a book left in a book box
by one entity or FY must never reach a run for another.

Rule on a registry miss (the box's present value is passed in):
  * a registered book of some other entity or FY (or whatever this box last
    filled) -> the box is cleared;
  * a path that is not a registered book at all (picked by hand) -> kept, with
    a line telling the user to check it.
Multi-book boxes drop exactly the registered lines of entities no longer
selected and keep hand-picked lines.

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
from ui import _book_registry  # noqa: E402
from ui.tabs import _entity_book  # noqa: E402
from ui.tabs import _generic as generic_mod  # noqa: E402


def _setup(tmp_path: Path):
    a2526 = tmp_path / "AliceDoe2526.gnucash"
    a2425 = tmp_path / "AliceDoe2425.gnucash"
    c2526 = tmp_path / "CarolDoe2526.gnucash"
    hand = tmp_path / "picked-by-hand.gnucash"
    for f in (a2526, a2425, c2526, hand):
        f.write_text("", encoding="utf-8")
    ents = {
        "AliceDoe": configs.EntityProfile(
            key="AliceDoe", name="Alice Doe", pan="ABCDE1234X", status="Individual",
            books={"2025-26": str(a2526), "2024-25": str(a2425)}),
        "BobDoe": configs.EntityProfile(
            key="BobDoe", name="Bob Doe", pan="ABCDE1235X", status="Individual"),
        "CarolDoe": configs.EntityProfile(
            key="CarolDoe", name="Carol Doe", pan="ABCDE1236X", status="Individual",
            books={"2025-26": str(c2526)}),
    }
    root = tmp_path / "Data"
    path = root / "itr" / "entities.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(configs.dump_entities(ents), encoding="utf-8")
    return root, a2526, a2425, c2526, hand


def _val(update):
    return update.get("value", "<unchanged>")


def _skill(name):
    for s in registry.discover():
        if s.name == name:
            return s
    raise AssertionError(name)


def _render(monkeypatch, skill):
    monkeypatch.setattr(generic_mod, "_refresh_models", lambda **kw: [("m", "m")])
    monkeypatch.setattr(generic_mod, "_default_model_value", lambda c: "m")
    with gr.Blocks() as demo:
        with gr.Tab("T"):
            generic_mod.render(skill)
    return demo


def _handler(demo):
    fns = [f for f in demo.fns.values() if getattr(f.fn, "__name__", "") == "_handler"]
    assert fns
    return fns[0]


# ---------------------------------------------------------------------------
# helper: is this path a registered book (read-only)
# ---------------------------------------------------------------------------

def test_is_registered_book_is_a_read_only_string_match(tmp_path):
    root, a, _a2, _c, hand = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        assert _book_registry.is_registered_book(str(a))
        assert _book_registry.is_registered_book(f'  "{a}" ')
        assert not _book_registry.is_registered_book(str(hand))
        assert not _book_registry.is_registered_book("")
        assert not _book_registry.is_registered_book(None)


def test_is_registered_book_matches_a_path_that_no_longer_exists(tmp_path):
    root, a, *_ = _setup(tmp_path)
    a.unlink()
    with patch("ui._config.data_root_dir", return_value=root):
        assert _book_registry.is_registered_book(str(a))


# ---------------------------------------------------------------------------
# single-book box: _entity_book level
# ---------------------------------------------------------------------------

def test_hit_still_fills(tmp_path):
    root, a, *_ = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        assert _val(_entity_book.book_update("AliceDoe", "2025-26", "")) == str(a)
        assert _val(_entity_book.book_update("AliceDoe", "2025-26", str(tmp_path / "x"))) == str(a)


def test_entity_with_no_book_clears_the_previous_entitys_book(tmp_path):
    root, a, *_ = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        upd = _entity_book.book_update("BobDoe", "2025-26", str(a))
        msg = _entity_book.book_status("BobDoe", "2025-26", str(a))
    assert _val(upd) == ""
    assert msg == "No registered book for BobDoe 2025-26; book box cleared."


def test_fy_with_no_book_clears_and_never_falls_back_to_another_fy(tmp_path):
    root, a, a2425, *_ = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        # NEGATIVE: an FY the entity has no book for must not hand over its newest book
        assert _entity_book.resolve_for_ui("AliceDoe", "2023-24") == ""
        upd = _entity_book.book_update("AliceDoe", "2023-24", str(a))
        msg = _entity_book.book_status("AliceDoe", "2023-24", str(a))
        assert _val(_entity_book.book_update("AliceDoe", "2024-25", str(a))) == str(a2425)
    assert _val(upd) == ""
    assert "book box cleared" in msg


def test_registered_path_typed_by_hand_is_still_cleared_on_a_miss(tmp_path):
    root, a, *_ = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        upd = _entity_book.book_update("BobDoe", "2025-26", f"  {a}  ")
    assert _val(upd) == ""


def test_hand_picked_unregistered_path_survives_with_a_check_line(tmp_path):
    root, _a, _a2, _c, hand = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        upd = _entity_book.book_update("BobDoe", "2025-26", str(hand))
        msg = _entity_book.book_status("BobDoe", "2025-26", str(hand))
    assert _val(upd) == "<unchanged>"          # NEGATIVE: not blanked
    assert msg == "Book picked by hand is kept. Check that it belongs to BobDoe 2025-26."


def test_empty_entity_clears_a_registered_book_and_keeps_a_hand_picked_one(tmp_path):
    root, a, _a2, _c, hand = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        assert _val(_entity_book.book_update(None, "2025-26", str(a))) == ""
        assert _val(_entity_book.book_update(None, "2025-26", str(hand))) == "<unchanged>"


def test_callers_that_do_not_pass_the_box_value_are_unchanged(tmp_path):
    root, a, *_ = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        assert _val(_entity_book.book_update("BobDoe")) == "<unchanged>"
        assert _val(_entity_book.book_update("BobDoe", "2025-26")) == "<unchanged>"
        assert "**BobDoe**" in _entity_book.book_status("BobDoe")


# ---------------------------------------------------------------------------
# through the rendered ITR Workbook tab and the run
# ---------------------------------------------------------------------------

def _run(monkeypatch, tmp_path, skill, entity, ay, book_value):
    seen = {}
    monkeypatch.setattr("agents.registry.load_run_function",
                        lambda s: (lambda **kw: seen.update(kw) or "done"))
    monkeypatch.setattr("ui._config.output_dir", lambda: tmp_path / "out")
    (tmp_path / "out").mkdir(exist_ok=True)
    values = []
    for inp in skill.inputs:
        if inp.name == "entity":
            values.append(entity)
        elif inp.name == "ay":
            values.append(ay)
        elif inp.name == "book_file":
            values.append(book_value)
        elif inp.type == "file" and inp.required:
            stub = tmp_path / f"{inp.name}.html"
            stub.write_text("<html></html>", encoding="utf-8")
            values.append(str(stub))
        else:
            values.append("")
    list(generic_mod._make_run_handler(skill)(*values, "m"))
    return seen


def test_itr_entity_a_then_b_leaves_an_empty_box_and_the_run_has_no_book(monkeypatch, tmp_path):
    root, a, *_ = _setup(tmp_path)
    skill = _skill("ITR Workbook")
    fn = _handler(_render(monkeypatch, skill)).fn
    with patch("ui._config.data_root_dir", return_value=root):
        box = fn("AliceDoe", "2025-26", "")[0]["value"]
        assert box == str(a)
        upd, status = fn("BobDoe", "2025-26", box)
        box = upd["value"]
        assert box == ""
        assert "book box cleared" in status["value"]
        seen = _run(monkeypatch, tmp_path, skill, "BobDoe", "2025-26", box)
    assert seen, "the skill should have run"
    assert seen.get("book_file", "") == ""
    assert str(a) not in [str(v) for v in seen.values()]   # NEGATIVE: A's path never reaches B's run


def test_itr_fy_with_a_book_then_fy_without_clears_the_box(monkeypatch, tmp_path):
    root, a, *_ = _setup(tmp_path)
    fn = _handler(_render(monkeypatch, _skill("ITR Workbook"))).fn
    with patch("ui._config.data_root_dir", return_value=root):
        box = fn("AliceDoe", "2025-26", "")[0]["value"]
        assert box == str(a)
        assert fn("AliceDoe", "2023-24", box)[0]["value"] == ""


def test_itr_hand_picked_path_survives_an_entity_change_with_the_warning(monkeypatch, tmp_path):
    root, _a, _a2, _c, hand = _setup(tmp_path)
    fn = _handler(_render(monkeypatch, _skill("ITR Workbook"))).fn
    with patch("ui._config.data_root_dir", return_value=root):
        upd, status = fn("BobDoe", "2025-26", str(hand))
    assert "value" not in upd
    assert "Check that it belongs to BobDoe 2025-26" in status["value"]


def test_itr_hit_still_fills_through_the_wired_handler(monkeypatch, tmp_path):
    root, a, _a2, c, _h = _setup(tmp_path)
    fn = _handler(_render(monkeypatch, _skill("ITR Workbook"))).fn
    with patch("ui._config.data_root_dir", return_value=root):
        assert fn("CarolDoe", "2025-26", str(a))[0]["value"] == str(c)


# ---------------------------------------------------------------------------
# another book_from skill (not ITR)
# ---------------------------------------------------------------------------

def test_generic_book_from_skill_clears_a_to_b(monkeypatch, tmp_path):
    root, a, *_ = _setup(tmp_path)
    skill = _skill("gnucash_pipeline")
    fn = _handler(_render(monkeypatch, skill)).fn
    with patch("ui._config.data_root_dir", return_value=root):
        box = fn("AliceDoe", "")[0]["value"]
        assert box == str(a)
        upd, status = fn("BobDoe", box)
    assert upd["value"] == ""
    assert "book box cleared" in status["value"]


# ---------------------------------------------------------------------------
# multi-book box
# ---------------------------------------------------------------------------

def test_matrix_deselecting_an_entity_removes_its_line_only(tmp_path):
    root, a, _a2, c, hand = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        box = _val(_entity_book.books_update(["AliceDoe", "CarolDoe"], "2025-26", ""))
        assert box.splitlines() == [str(a), str(c)]
        upd = _entity_book.books_update(["CarolDoe"], "2025-26", box)
        msg = _entity_book.books_status(["CarolDoe"], "2025-26", box)
    assert _val(upd).splitlines() == [str(c)]
    assert str(a) not in _val(upd)             # NEGATIVE: no line for a deselected entity
    assert "Removed 1 book line" in msg


def test_matrix_deselecting_everything_empties_the_box_but_keeps_hand_lines(tmp_path):
    root, a, _a2, c, hand = _setup(tmp_path)
    text = f"{a}\n{hand}\n{c}"
    with patch("ui._config.data_root_dir", return_value=root):
        upd = _entity_book.books_update([], "2025-26", text)
        msg = _entity_book.books_status([], "2025-26", text)
        none_left = _entity_book.books_update([], "2025-26", f"{a}\n{c}")
    assert _val(upd) == str(hand)
    assert "kept" in msg
    assert _val(none_left) == ""


def test_matrix_without_current_behaves_as_before(tmp_path):
    root, a, _a2, c, _h = _setup(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        assert _val(_entity_book.books_update(["AliceDoe", "CarolDoe"], "2025-26")).splitlines() == [str(a), str(c)]
        assert _val(_entity_book.books_update(["BobDoe"], "2025-26")) == "<unchanged>"


def test_rendered_matrix_handler_takes_the_box_value(monkeypatch, tmp_path):
    root, a, _a2, c, _h = _setup(tmp_path)
    skill = _skill("gnucash_intercompany_matrix")
    fn = _handler(_render(monkeypatch, skill)).fn
    with patch("ui._config.data_root_dir", return_value=root):
        box = fn(["AliceDoe", "CarolDoe"], "")[0]["value"]
        assert box.splitlines() == [str(a), str(c)]
        after = fn(["CarolDoe"], box)[0]["value"]
    assert after.splitlines() == [str(c)]


# ---------------------------------------------------------------------------
# hand-written tabs: the entity change passes the box value too
# ---------------------------------------------------------------------------

import importlib  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.parametrize("module_path", [
    "ui.tabs.gnucash_review", "ui.tabs.krc_gnucash_review", "ui.tabs.tds_journal_review",
])
def test_hand_written_tab_clears_a_registered_book_on_entity_change(module_path, tmp_path):
    root, a, *_ = _setup(tmp_path)
    mod = importlib.import_module(module_path)
    with gr.Blocks() as demo:
        mod.render()
    fns = [f for f in demo.fns.values()
           if getattr(f.fn, "__name__", "") == "<lambda>" and len(f.inputs) == 2]
    assert fns, "entity change handler should take the entity and the box value"
    with patch("ui._config.data_root_dir", return_value=root):
        book, status = fns[0].fn("BobDoe", str(a))
    assert book["value"] == ""                 # NEGATIVE: A's book is not left for B
    assert "book box cleared" in status["value"]
