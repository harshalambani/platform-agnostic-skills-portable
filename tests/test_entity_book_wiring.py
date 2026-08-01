"""
tests/test_entity_book_wiring.py — shape tests for the entity->book UI wiring
(SkillInput.book_from / options_from: "itr_entities").

Guards two things that are easy to get wrong when editing a skill.yaml:

1. Output-filename hazard: ui/tabs/_generic.py derives the output filename
   from the first input (in skill.yaml declaration order) that has a
   non-empty value at run time AND is consumed by the skill (referenced by a
   `{inputs.<name>}` token in run_args). The book_from entity selects are
   UI-only, which is what lets them lead the form without hijacking the
   filename -- these tests pin that: a consumed entity select is never the
   first consumed input (ITR Workbook is the one skill that does consume
   its entity, and `bs_html` leads it), and each entity select is declared
   above the book field it fills.

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
    """A *consumed* entity select may never be the first consumed input.

    ui/tabs/_generic.py picks

        consumed = {inputs referenced by a {inputs.<name>} token in run_args}
        primary_input = next((v for k, v in input_map.items()
                              if v and k in consumed), ...)

    walking inputs in skill.yaml declaration order. An itr_entities select is
    non-empty as soon as the user picks someone, so a select that is both
    consumed and declared first would silently become the output-filename
    source -- files would come out named after the taxpayer instead of the
    statement.

    Two ways to be safe, and both are in use:

      - UI-only (no `{inputs.<name>}` token anywhere in run_args). This is
        what lets the select sit at the TOP of the form, where it belongs --
        it answers for the book field below it, so the user should meet it
        first. All five book_from skills work this way.
      - Consumed, but declared after an input that already supplies the
        output name. ITR Workbook does this: it genuinely passes the entity
        key through to run(), and `bs_html` leads the form.
    """
    for skill in _skills():
        consumed = [
            inp.name for inp in skill.inputs
            if any(f"{{inputs.{inp.name}}}" in t for t in skill.run_args.values())
        ]
        for inp in skill.inputs:
            if inp.options_from != "itr_entities" or inp.name not in consumed:
                continue
            assert consumed[0] != inp.name, (
                f"{skill.name}: input '{inp.name}' (options_from: "
                f"itr_entities) is consumed by run_args AND is the first "
                f"consumed input, so it would name the output file. Either "
                f"drop the run_args reference (making it UI-only, the usual "
                f"choice) or declare it after the input that supplies the "
                f"output name."
            )


def test_book_from_source_precedes_its_book_field():
    """Each entity select is declared before the book input it fills.

    Cosmetic but the whole point of the placement change: a control that
    fills a field belongs above that field, not buried at the bottom of the
    form where it reads as an afterthought.
    """
    for skill in _skills():
        order = {inp.name: i for i, inp in enumerate(skill.inputs)}
        for inp in skill.inputs:
            if not inp.book_from or inp.book_from not in order:
                continue
            assert order[inp.book_from] < order[inp.name], (
                f"{skill.name}: entity select '{inp.book_from}' is declared "
                f"at index {order[inp.book_from]}, AFTER the book input "
                f"'{inp.name}' it fills (index {order[inp.name]}). Move the "
                f"select above the book field."
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
