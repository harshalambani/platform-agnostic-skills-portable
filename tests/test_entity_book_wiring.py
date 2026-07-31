"""
tests/test_entity_book_wiring.py — shape tests for the entity->book UI wiring
(SkillInput.book_from / options_from: "itr_entities").

Guards two things that are easy to get wrong when editing a skill.yaml:

1. Output-filename hazard: ui/tabs/_generic.py derives the output filename
   from the first input (in skill.yaml declaration order) that has a
   non-empty value at run time. A `select` with `options_from: "itr_entities"`
   always renders with a non-empty default (the first entity in the
   registry), even when `required: false` -- so if such a select is placed
   BEFORE the input that currently supplies the output name (typically the
   first `required: true` input), it silently hijacks the output filename.
   This test asserts every itr_entities select comes after the first
   required input in its skill.

2. Dangling book_from: every `book_from` value must name another input that
   actually exists on the same skill (ui/tabs/_generic.py raises a
   ValueError at UI-build time otherwise -- this test catches it at test
   time instead, for every registered skill, without needing to build the
   Gradio app).

Run:
    cd "<repo>/src" && python -m pytest ../tests/test_entity_book_wiring.py -v
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.registry import discover  # noqa: E402


def _skills():
    return discover(refresh=True)


def test_book_from_targets_exist():
    """Every book_from value must name a real input on the same skill."""
    for skill in _skills():
        names = {inp.name for inp in skill.inputs}
        for inp in skill.inputs:
            if not inp.book_from:
                continue
            assert inp.book_from in names, (
                f"{skill.name}: input '{inp.name}' declares book_from="
                f"'{inp.book_from}', but no input with that name exists "
                f"on this skill. Known inputs: {sorted(names)}"
            )


def test_book_from_only_on_file_inputs():
    """book_from is only honoured by the UI for single 'file' inputs, never
    'files' (multi-upload) -- wiring it onto a 'files' input is a silent
    no-op in ui/tabs/_generic.py, so guard against that mistake here."""
    for skill in _skills():
        for inp in skill.inputs:
            if inp.book_from:
                assert inp.type == "file", (
                    f"{skill.name}: input '{inp.name}' has type '{inp.type}' "
                    f"but declares book_from -- the UI only wires book_from "
                    f"for type: \"file\" inputs. (Known exception: skills "
                    f"whose GnuCash input is type \"files\", e.g. Inter-entity "
                    f"Matrix, cannot use book_from at all.)"
                )


def test_itr_entities_select_does_not_hijack_output_filename():
    """No `options_from: "itr_entities"` select may appear before the first
    required input in its skill's declaration order.

    Rationale: ui/tabs/_generic.py picks
        primary_input = next((v for k, v in input_map.items() if v), "output")
    walking inputs in skill.yaml declaration order. A required input is
    guaranteed non-empty by the time this runs (the UI blocks the run
    otherwise). An itr_entities select is non-empty as soon as the user picks
    someone -- and, unless it is a `book_from` source (those render blank on
    purpose), it is non-empty from the very first render because it
    pre-selects the first registered entity. Either way, if it is declared
    earlier than the first required input it silently becomes the
    output-filename source instead.
    """
    for skill in _skills():
        required_idx = next(
            (i for i, inp in enumerate(skill.inputs) if inp.required),
            None,
        )
        if required_idx is None:
            # No required input on this skill -- nothing to protect against
            # being pre-empted; skip.
            continue
        for i, inp in enumerate(skill.inputs):
            if inp.options_from == "itr_entities":
                assert i > required_idx, (
                    f"{skill.name}: input '{inp.name}' (options_from: "
                    f"itr_entities) is declared at index {i}, before the "
                    f"first required input '{skill.inputs[required_idx].name}' "
                    f"at index {required_idx}. This select always has a "
                    f"non-empty default value and would hijack the output "
                    f"filename. Move it after the required input."
                )


def test_intercompany_matrix_has_no_book_from():
    """Documents a known, deliberate gap: the Matrix skill's GnuCash input
    ('books') is type: "files" (2+ books), not type: "file", so it cannot
    receive book_from wiring the way the single-book skills do -- there is
    no per-book entity select for it. If this ever changes (e.g. the UI
    gains multi-file book_from support), update this test alongside it."""
    matrix = next(
        (s for s in _skills() if s.name == "gnucash_intercompany_matrix"),
        None,
    )
    assert matrix is not None, "gnucash_intercompany_matrix skill not found"
    books_input = next((i for i in matrix.inputs if i.name == "books"), None)
    assert books_input is not None
    assert books_input.type == "files"
    assert books_input.book_from == ""
    assert not any(i.options_from == "itr_entities" for i in matrix.inputs)
