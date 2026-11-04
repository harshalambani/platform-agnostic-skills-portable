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
    """book_from is honoured by the UI for 'file' and 'files' inputs only --
    on anything else it is a silent no-op in ui/tabs/_generic.py, so guard
    against that mistake here."""
    for skill in _skills():
        for inp in skill.inputs:
            if inp.book_from:
                assert inp.type in ("file", "files"), (
                    f"{skill.name}: input '{inp.name}' has type '{inp.type}' "
                    f"but declares book_from -- the UI only wires book_from "
                    f"for type: \"file\" and type: \"files\" inputs."
                )


def test_files_book_from_source_is_multiselect():
    """A multi-book field is filled from a MULTI-select entity dropdown.

    `books_update()` resolves every picked entity into one path per line; a
    single-select source would only ever supply one of the two books the
    matrix needs, which looks like the fill quietly failing.
    """
    for skill in _skills():
        by_name = {inp.name: inp for inp in skill.inputs}
        for inp in skill.inputs:
            if inp.type != "files" or not inp.book_from:
                continue
            src = by_name.get(inp.book_from)
            assert src is not None and src.multiselect, (
                f"{skill.name}: '{inp.name}' is a multi-book field fed by "
                f"'{inp.book_from}', which is not multiselect: true."
            )


def test_itr_entities_select_does_not_hijack_output_filename():
    """An entity / AY select never names the output file, wherever it sits.

    ui/tabs/_generic.py's _output_name_source() walks the inputs in skill.yaml
    declaration order and takes the first one that has a value and is consumed
    by run_args -- but it skips itr_entities / itr_ay_years selects outright
    (UI-15: Entity now leads EVERY form, consumed or not). So for each skill
    with such a select, filling in the entity and one other input must name
    the output after that other input.
    """
    from ui.tabs._generic import _output_name_source

    for skill in _skills():
        entity_inputs = [i for i in skill.inputs if i.options_from == "itr_entities"]
        others = [
            i for i in skill.inputs
            if i.options_from not in ("itr_entities", "itr_ay_years")
            and any(f"{{inputs.{i.name}}}" in t for t in skill.run_args.values())
        ]
        if not entity_inputs or not others:
            continue
        values = {i.name: f"value-of-{i.name}" for i in skill.inputs}
        for e in entity_inputs:
            values[e.name] = "test_individual"
        got = _output_name_source(skill, values)
        assert got != "test_individual", (
            f"{skill.name}: the output would be named after the entity select "
            f"'{entity_inputs[0].name}'."
        )
        assert got == values[others[0].name], (
            f"{skill.name}: expected the output to be named after "
            f"'{others[0].name}', got {got!r}."
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


def test_intercompany_matrix_is_entity_wired():
    """The Matrix skill's multi-book input is filled from a multiselect entity
    picker.

    This test previously documented the opposite -- that 'books', being
    type: "files", could not carry book_from at all -- and asked to be updated
    alongside the UI gaining multi-file support. It has, so this is the
    positive form: one dropdown, N entities, N book paths.
    """
    matrix = next(
        (s for s in _skills() if s.name == "gnucash_intercompany_matrix"),
        None,
    )
    assert matrix is not None, "gnucash_intercompany_matrix skill not found"
    books_input = next((i for i in matrix.inputs if i.name == "books"), None)
    assert books_input is not None
    assert books_input.type == "files"
    assert books_input.book_from == "entities"

    entities = next((i for i in matrix.inputs if i.name == "entities"), None)
    assert entities is not None
    assert entities.options_from == "itr_entities"
    assert entities.multiselect is True
    assert entities.required is False
    # UI-only: it must not reach run(), which takes no `entities` kwarg.
    assert not any("{inputs.entities}" in t for t in matrix.run_args.values())
