"""
tests/test_matrix_multi_book_entities.py -- the Inter-entity Matrix's N-book
entity wiring (GnuCash book registry architecture, Phase 5 remainder).

Before this, the Matrix tab could not complete a single successful run. Its
`books` input is type: "files", which the generic renderer served as a
gr.File; the run handler staged every pick into a temp directory and put THAT
DIRECTORY's path into input_map; run_args substitution turned it into a
string; and `agent.run` then did `[books] if isinstance(books, str)` -- one
path -- and returned "ERROR: select at least two .gnucash books for a matrix."
every time. It also copied live .gnucash files into %TEMP% on the way, and
refused outright any book over the 100 MB upload cap.

The fix makes a multi-book field the plural of the single-book one: a path
textbox, one path per line, filled from a multiselect entity dropdown and
opened in place. These tests cover the three layers of that:

  1. `_entity_book.books_update` / `books_status` -- N entities in, N paths
     out, and the partial-miss case named out loud.
  2. The generic renderer -- the field is not a gr.File, and its Browse
     button ADDS to the list rather than replacing it.
  3. `agent.run` -- splits the newline-separated string it now receives.

Synthetic entities only (AliceDoe/BobDoe/CarolDoe, PAN ABCDE1234X); every
".gnucash" path is an empty tmp_path placeholder that is never parsed.

Run with:
    cd "<repo>" && python -m pytest tests/test_matrix_multi_book_entities.py -v
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

from ui.tabs import _entity_book  # noqa: E402

PAN = "ABCDE1234X"


def _seed(tmp_path: Path, keys_with_books: dict[str, bool]) -> Path:
    """Write a synthetic entities.yaml; True = register a real (empty) book."""
    entities = {}
    for key, has_book in keys_with_books.items():
        books = {}
        if has_book:
            book = tmp_path / f"{key}2526.gnucash"
            book.write_text("", encoding="utf-8")
            books = {"2025-26": str(book)}
        entities[key] = configs.EntityProfile(
            key=key, name=f"{key} Doe", pan=PAN, status="Individual", books=books,
        )
    data_root = tmp_path / "Data"
    path = data_root / "itr" / "entities.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(configs.dump_entities(entities), encoding="utf-8")
    return data_root


# ---------------------------------------------------------------------------
# 1. _entity_book: N entities -> N paths
# ---------------------------------------------------------------------------

def test_books_update_fills_one_path_per_line(tmp_path):
    data_root = _seed(tmp_path, {"AliceDoe": True, "BobDoe": True})
    with patch("ui._config.data_root_dir", return_value=data_root):
        upd = _entity_book.books_update(["AliceDoe", "BobDoe"])
    lines = upd["value"].splitlines()
    assert lines == [
        str(tmp_path / "AliceDoe2526.gnucash"),
        str(tmp_path / "BobDoe2526.gnucash"),
    ]


def test_books_update_skips_unregistered_rather_than_leaving_a_blank_line(tmp_path):
    """A blank line would read as a path the run then fails on. The field only
    ever holds paths that exist; books_status names who was left out."""
    data_root = _seed(tmp_path, {"AliceDoe": True, "BobDoe": False})
    with patch("ui._config.data_root_dir", return_value=data_root):
        upd = _entity_book.books_update(["AliceDoe", "BobDoe"])
        status = _entity_book.books_status(["AliceDoe", "BobDoe"])

    assert upd["value"].splitlines() == [str(tmp_path / "AliceDoe2526.gnucash")]
    # The partial case is the one this exists for: two picked, one filled, and
    # without the line the missing one is invisible.
    assert "1 of 2" in status
    assert "BobDoe" in status
    assert "AliceDoe" not in status


def test_books_update_resolving_nothing_never_blanks_a_manual_pick(tmp_path):
    """Same rule as the single-book `book_update()`: a registry miss returns a
    bare update, so clearing the dropdown cannot wipe hand-picked paths."""
    data_root = _seed(tmp_path, {"AliceDoe": False})
    with patch("ui._config.data_root_dir", return_value=data_root):
        assert _entity_book.books_update(["AliceDoe"]) == {"__type__": "update"}
        assert _entity_book.books_update([]) == {"__type__": "update"}
        assert _entity_book.books_update(None) == {"__type__": "update"}


def test_books_status_is_silent_when_every_pick_resolved(tmp_path):
    data_root = _seed(tmp_path, {"AliceDoe": True, "BobDoe": True})
    with patch("ui._config.data_root_dir", return_value=data_root):
        assert _entity_book.books_status(["AliceDoe", "BobDoe"]) == ""
        assert _entity_book.books_status([]) == ""


def test_as_keys_accepts_the_single_select_shape_too(tmp_path):
    """Gradio hands back a bare string when the component was built without
    multiselect -- the helpers must not iterate that character by character."""
    data_root = _seed(tmp_path, {"AliceDoe": True})
    with patch("ui._config.data_root_dir", return_value=data_root):
        upd = _entity_book.books_update("AliceDoe")
    assert upd["value"] == str(tmp_path / "AliceDoe2526.gnucash")


# ---------------------------------------------------------------------------
# 2. The generic renderer
# ---------------------------------------------------------------------------

def _matrix_skill():
    from agents.registry import discover

    skill = next(
        (s for s in discover(refresh=True) if s.name == "gnucash_intercompany_matrix"),
        None,
    )
    assert skill is not None
    return skill


def _render_matrix(tmp_path: Path):
    """Render the Matrix tab, capturing gr.File labels and the Browse handler."""
    from ui.tabs import _generic

    captured: dict = {}
    file_labels: list[str] = []
    orig_click = gr.Button.click
    orig_file_init = gr.File.__init__

    def _tracking_click(self, fn=None, **kwargs):
        if str(self.value).startswith("Browse"):
            captured["fn"] = fn
            captured["inputs"] = kwargs.get("inputs")
        return orig_click(self, fn=fn, **kwargs)

    def _tracking_file_init(self, *args, **kwargs):
        file_labels.append(str(kwargs.get("label", "")))
        return orig_file_init(self, *args, **kwargs)

    with patch.object(gr.Button, "click", _tracking_click), \
            patch.object(gr.File, "__init__", _tracking_file_init), \
            patch.object(_generic._config, "output_dir", return_value=tmp_path):
        with gr.Blocks():
            _generic.render(_matrix_skill())

    return captured, file_labels


def test_matrix_book_field_is_not_a_served_file_component(tmp_path):
    """Same reason as the single-book fields: Gradio cannot serve a book that
    lives outside allowed_paths, so a gr.File renders an Error box and drops
    the value -- and staging one would copy every live .gnucash into %TEMP%."""
    _captured, file_labels = _render_matrix(tmp_path)
    offenders = [lbl for lbl in file_labels if "gnucash" in lbl.lower()]
    assert not offenders, (
        f"the Matrix book field is a gr.File ({offenders}); it must be a path "
        f"textbox."
    )


def test_matrix_browse_appends_and_deduplicates(tmp_path):
    """Books get gathered a few at a time -- three from one folder, one from
    another -- so a picker that wiped the previous picks would make the field
    unusable. Re-picking one already listed must not list it twice, or the
    matrix would reconcile someone against themselves."""
    from ui import _filedialog

    captured, _labels = _render_matrix(tmp_path)
    assert "fn" in captured, "no Browse... button wired on the Matrix tab"
    # The handler needs the textbox's own value, so the textbox must be wired
    # as an input -- that is what makes appending possible at all.
    assert captured["inputs"], "Browse handler was wired with inputs=[]"

    a, b, c = (str(tmp_path / f"{n}.gnucash") for n in ("A", "B", "C"))

    with patch.object(_filedialog, "pick_files", return_value=([b, c], [])):
        upd = captured["fn"](a)
    assert upd["value"].splitlines() == [a, b, c]

    with patch.object(_filedialog, "pick_files", return_value=([b], [])):
        upd = captured["fn"](f"{a}\n{b}")
    assert upd["value"].splitlines() == [a, b]


def test_matrix_browse_cancel_keeps_the_existing_list(tmp_path):
    from ui import _filedialog

    captured, _labels = _render_matrix(tmp_path)
    a = str(tmp_path / "A.gnucash")
    with patch.object(_filedialog, "pick_files", return_value=([], [])):
        assert captured["fn"](a) == {"__type__": "update"}


def test_matrix_browse_does_not_cap_book_size(tmp_path):
    """The upload cap exists because uploads are copied into a staging dir. A
    book is opened read-only where it lies, so a cap would only mean refusing
    to open a big book -- and real .gnucash files pass 100 MB routinely."""
    from ui import _filedialog

    captured, _labels = _render_matrix(tmp_path)
    seen: dict = {}

    def _spy(box_key, **kwargs):
        seen.update(kwargs)
        return ([], [])

    with patch.object(_filedialog, "pick_files", _spy):
        captured["fn"]("")
    assert seen.get("max_size_bytes") is None
    assert seen.get("multiple") is True


# ---------------------------------------------------------------------------
# 3. agent.run -- the newline-separated string it now receives
# ---------------------------------------------------------------------------

def _matrix_run():
    import importlib

    return importlib.import_module(
        "agents.skill_gnucash_intercompany_matrix.agent"
    ).run


def test_agent_splits_newline_separated_books(tmp_path):
    """run_args substitution makes every kwarg a string, so the multi-book
    field arrives as one newline-separated blob. Two books in it is two books,
    not one -- the bug that made every Matrix run fail."""
    a, b = tmp_path / "A.gnucash", tmp_path / "B.gnucash"
    seen: dict = {}

    def _fake_run_matrix(paths, **kwargs):
        seen["paths"] = list(paths)
        raise RuntimeError("stop here -- path handling is all this test needs")

    # Importing the agent is what puts the skill's scripts/ dir on sys.path,
    # so matrix_recon is only importable after this line.
    run = _matrix_run()
    import matrix_recon  # noqa: PLC0415

    with patch.object(matrix_recon, "run_matrix", _fake_run_matrix):
        out = run(f"{a}\n{b}\n", str(tmp_path / "out.xlsx"))

    assert "at least two" not in out
    assert seen["paths"] == [str(a), str(b)]


def test_agent_still_rejects_a_single_book(tmp_path):
    out = _matrix_run()(str(tmp_path / "A.gnucash"), str(tmp_path / "out.xlsx"))
    assert "at least two" in out


def test_agent_deduplicates_before_counting(tmp_path):
    """The same book twice is one book, not a valid pair -- otherwise the
    matrix would cheerfully reconcile someone against themselves."""
    a = str(tmp_path / "A.gnucash")
    out = _matrix_run()(f"{a}\n{a}", str(tmp_path / "out.xlsx"))
    assert "at least two" in out


def test_agent_still_accepts_a_list(tmp_path):
    """Direct/programmatic callers (and the existing tests) pass a list."""
    a, b = tmp_path / "A.gnucash", tmp_path / "B.gnucash"
    seen: dict = {}

    def _fake_run_matrix(paths, **kwargs):
        seen["paths"] = list(paths)
        raise RuntimeError("stop here")

    run = _matrix_run()
    import matrix_recon  # noqa: PLC0415

    with patch.object(matrix_recon, "run_matrix", _fake_run_matrix):
        run([str(a), str(b)], str(tmp_path / "out.xlsx"))

    assert seen["paths"] == [str(a), str(b)]


@pytest.mark.parametrize("blank", ["", "   ", "\n\n"])
def test_agent_rejects_an_empty_books_value(blank, tmp_path):
    assert "at least two" in _matrix_run()(blank, str(tmp_path / "out.xlsx"))
