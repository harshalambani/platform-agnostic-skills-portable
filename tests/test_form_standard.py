"""
tests/test_form_standard.py -- UI-15, the app-wide form standard.

Every skill form that has an entity picker shows it FIRST. The standard order
is: 1 Entity (+ its FY/AY), 2 source picker, 3 source documents, 4 password,
5 GnuCash book, 6 output file, 7 advanced options. Only rule 1 is enforced
here for every skill; rules 2-7 are pinned on the four forms that were
reordered.

Reordering must be cosmetic: inputs reach run() by NAME through run_args, so
no run argument may change, and a skill with no entity picker must not grow
one.
"""
from __future__ import annotations

import random
import re

from agents.registry import discover

# Skills that have an entity picker today. A skill with no entity picker must
# not be forced to grow one, so this set is pinned: adding a picker to another
# skill is a deliberate act that updates this list.
ENTITY_SKILLS = {
    "skill_ais_reconcile",
    "skill_gnucash_coverage",
    "skill_gnucash_intercompany",
    "skill_gnucash_intercompany_matrix",
    "skill_krc_gnucash",
    "skill_26as_journal",
    "skill_itr_workbook",
    "skill_gnucash_pipeline",
    "skill_partner_comp_recon",
}

REORDERED = {
    "skill_partner_comp_recon",
    "skill_26as_journal",
    "skill_itr_workbook",
    "skill_gnucash_pipeline",
}


def _skills():
    return discover(refresh=True)


def _pkg(skill) -> str:
    """The skill's folder name (src/agents/<folder>/skill.yaml)."""
    return skill.manifest_path.parent.name


def _by_name():
    """Skills keyed by folder name, which is stable (display names are not)."""
    return {_pkg(s): s for s in _skills()}


def _entity_inputs(skill):
    return [i for i in skill.inputs if i.options_from == "itr_entities"]


def test_every_skill_with_an_entity_picker_shows_it_first():
    for skill in _skills():
        ents = _entity_inputs(skill)
        if not ents:
            continue
        assert skill.inputs[0].name == ents[0].name, (
            f"{skill.name}: the entity picker '{ents[0].name}' must be the first "
            f"input (form standard, UI-15); the form opens on "
            f"'{skill.inputs[0].name}' instead."
        )


def test_skills_without_an_entity_picker_do_not_grow_one():
    """Negative: the standard is not a licence to add an entity picker everywhere."""
    have = {_pkg(s) for s in _skills() if _entity_inputs(s)}
    assert have == ENTITY_SKILLS, (
        f"entity-picker skills changed: added {sorted(have - ENTITY_SKILLS)}, "
        f"removed {sorted(ENTITY_SKILLS - have)}. If deliberate, update "
        f"ENTITY_SKILLS in this test."
    )
    for plain in ("skill_26as", "skill_bob", "skill_hdfc", "skill_icici", "skill_mf_cas"):
        s = _by_name().get(plain)
        if s is not None:
            assert not _entity_inputs(s), f"{plain} must not have an entity picker"


def test_reordered_forms_follow_the_standard_order():
    by = _by_name()
    for name in REORDERED:
        skill = by[name]
        # Entity (and AY) block first.
        names = [i.name for i in skill.inputs]
        first_non_entity = next(
            idx for idx, i in enumerate(skill.inputs)
            if i.options_from not in ("itr_entities", "itr_ay_years")
        )
        assert all(
            i.options_from in ("itr_entities", "itr_ay_years")
            for i in skill.inputs[:first_non_entity]
        ), name
        # Password never precedes a source document; book never precedes a password.
        pw = [i for i, inp in enumerate(skill.inputs) if inp.type == "password"]
        docs = [i for i, inp in enumerate(skill.inputs)
                if inp.type in ("file", "files") and not inp.book_from]
        books = [i for i, inp in enumerate(skill.inputs) if inp.book_from]
        if pw and docs:
            assert min(pw) > min(docs), f"{name}: password before any source document: {names}"
        if pw and books:
            assert max(pw) < min(books), f"{name}: GnuCash book before the password: {names}"


def _substitute(skill, values: dict[str, str]) -> dict[str, str]:
    """The run_args substitution the form handler does, by input NAME."""
    kwargs = {}
    for param, template in skill.run_args.items():
        val = template
        for inp_name, inp_val in values.items():
            val = val.replace(f"{{inputs.{inp_name}}}", inp_val)
        kwargs[param] = val
    return kwargs


def test_reordering_changes_no_run_argument():
    """Each input still reaches the skill by name: the same values give the
    same kwargs whatever order they are collected in, and every token names a
    real input."""
    for name in REORDERED:
        skill = _by_name()[name]
        declared = {i.name for i in skill.inputs}
        tokens = set()
        for t in skill.run_args.values():
            tokens.update(re.findall(r"\{inputs\.([A-Za-z0-9_]+)\}", t))
        assert tokens <= declared, f"{name}: run_args names unknown inputs {tokens - declared}"
        values = {i.name: f"<<{i.name}>>" for i in skill.inputs}
        baseline = _substitute(skill, values)
        for _ in range(5):
            items = list(values.items())
            random.shuffle(items)
            assert _substitute(skill, dict(items)) == baseline
        # Every consumed input lands in exactly the run argument that names it.
        for param, template in skill.run_args.items():
            for tok in re.findall(r"\{inputs\.([A-Za-z0-9_]+)\}", template):
                assert baseline[param].count(f"<<{tok}>>") >= 1


def test_output_name_does_not_follow_entity_to_the_top():
    """Negative: moving the entity to the top must not rename any output."""
    from ui.tabs._generic import _output_name_source

    expect_first = {
        "skill_partner_comp_recon": "firm_documents",
        "skill_26as_journal": "xlsx_path",
        "skill_itr_workbook": "bs_html",
        "skill_gnucash_pipeline": "bank",
    }
    for name, first in expect_first.items():
        skill = _by_name()[name]
        values = {i.name: f"v-{i.name}" for i in skill.inputs}
        values["entity"] = "test_individual"
        got = _output_name_source(skill, values)
        assert got == f"v-{first}", f"{name}: output named after {got!r}, expected input {first}"
        assert got != "test_individual"


def test_scaffolder_template_documents_the_standard():
    from pathlib import Path
    tmpl = (Path(__file__).resolve().parent.parent / "src" / "agents" / "skill_scaffold"
            / "templates" / "skill.yaml.tmpl").read_text(encoding="utf-8")
    assert "Form standard" in tmpl
    for step in ("Entity", "Source picker", "Source documents", "Password",
                 "GnuCash book", "Output file", "Advanced options"):
        assert step in tmpl, f"scaffolder template does not mention '{step}'"
