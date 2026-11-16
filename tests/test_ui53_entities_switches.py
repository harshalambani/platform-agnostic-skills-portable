"""UI-53: Entities fields are shown by switch; hiding never deletes. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import gradio as gr
import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import configs                                   # noqa: E402,F401
from ui.tabs import itr_entities as ui          # noqa: E402

G = ui._GATE_RULES
DOB_GROUP = {"dob", "father_name", "aadhaar"}
PARTNER_GROUP = {n for n, r in G.items() if r == "partner"}
BOOKS_GROUP = {n for n, r in G.items() if r == "books"}
BOOKS_ACCORDIONS = {"fdb_acc", "card_acc", "ye_acc"}
BOOKS = "2025-26 = C:\\synthetic\\book.gnucash"
NAMES = [n for n in ui._compute_gates("Individual", False, "", False, "", {}) if n in G]


def visible(status="Individual", tick=False, books="", audit=False, by_ay="", values=None):
    g = ui._compute_gates(status, tick, books, audit, by_ay, values or {})
    return {n for n, (v, _h) in g.items() if v}


def notes(status="Individual", tick=False, books="", audit=False, by_ay="", values=None):
    g = ui._compute_gates(status, tick, books, audit, by_ay, values or {})
    return {n for n, (_v, h) in g.items() if h}


# ---- each switch shows / hides exactly its own list ----

def test_individual_default_shows_dob_group_and_tick_and_hides_the_rest():
    assert visible() == DOB_GROUP | {"partner_tick"}


def test_status_huf_hides_exactly_dob_father_aadhaar_and_the_tick_and_shows_doi():
    before, after = visible(), visible("HUF")
    assert before - after == DOB_GROUP | {"partner_tick"}
    assert after - before == {"doi"}


def test_status_huf_hides_the_whole_partner_block_even_if_ticked():           # NEGATIVE
    assert visible("Individual", tick=True) & PARTNER_GROUP == PARTNER_GROUP
    assert visible("HUF", tick=True) & PARTNER_GROUP == set()
    assert "partner_acc" not in visible("HUF", tick=True)


def test_the_partner_tick_shows_exactly_the_partner_block_and_turning_it_back_hides_it():
    off, on = visible(), visible(tick=True)
    assert on - off == PARTNER_GROUP | {"partner_acc"}
    assert off - on == set()
    assert visible(tick=False) == off


def test_keeping_books_shows_exactly_the_books_group_and_its_accordions():
    off, on = visible(), visible(books=BOOKS)
    assert on - off == BOOKS_GROUP | BOOKS_ACCORDIONS
    assert off - on == set()
    assert visible(books="") == off


def test_an_extended_due_date_shows_only_the_reason():
    off = visible()
    assert visible(audit=True) - off == {"audit_basis"}
    assert visible(by_ay="2025-26 = true") - off == {"audit_basis"}
    assert visible(by_ay="2025-26 = false") == off                           # NEGATIVE: all false is not extended


# ---- a value keeps its field visible, with the note ----

@pytest.mark.parametrize("name", sorted(G))
def test_a_hidden_field_holding_a_value_is_shown_with_the_note_and_nothing_else_appears(name):
    status = "HUF" if G[name] == "not_huf" else "Individual"
    value = True if name == "fdb" else "x"
    base = visible(status)
    got = visible(status, values={name: value})
    assert name in got and name in notes(status, values={name: value})
    accordions = {a for a, kids in ui._GATE_ACCORDIONS.items() if name in kids}
    assert got - base == {name} | accordions                                 # nothing else appears


def test_a_blank_or_false_value_does_not_keep_a_field_visible():           # NEGATIVE
    assert not ui._holds_value("") and not ui._holds_value("  ") and not ui._holds_value(False)
    assert not ui._holds_value(None) and not ui._holds_value(ui._AUDIT_BASIS_BLANK)
    assert visible("HUF", values={"dob": "", "aadhaar": None}) == {"doi"}


def test_a_field_whose_switch_is_on_has_no_note():                         # NEGATIVE
    assert notes(values={"dob": "1980-01-01"}) == set()


# ---- the audited-firm reason ----

def test_an_huf_is_not_offered_the_audited_firm_partner_reason():
    assert "partner_of_audited_firm" not in ui._basis_choices("HUF", "")
    assert "partner_of_audited_firm" not in ui._basis_choices("HUF", "self_44ab")
    assert "partner_of_audited_firm" in ui._basis_choices("Individual", "")


def test_an_huf_that_already_holds_the_audited_firm_reason_keeps_it_selectable():
    assert "partner_of_audited_firm" in ui._basis_choices("HUF", "partner_of_audited_firm")
    assert "audit_basis" in visible("HUF", values={"audit_basis": "partner_of_audited_firm"})


# ---- the partner tick on load ----

def _profile(**over):
    d = {"name": "Syn", "pan": "AAAAA0000A", "status": "Individual", "residency": "Resident", "default_regime": "new"}
    d.update(over)
    return d


def _load(tmp_path, ents):
    root = tmp_path / "Data"
    p = root / "itr" / "entities.yaml"
    p.parent.mkdir(parents=True)
    p.write_text(yaml.safe_dump(ents, sort_keys=True), encoding="utf-8")
    return root, p


@pytest.mark.parametrize("over", [
    {"partner_admission_date": "2020-04-01"},
    {"partner_comp_accounts": {"bank": "Assets:Firm"}},
    {"extra_items": {"partner_comp_drivers": {"2025-26": {"firms_tax_rate": 0.25}}}},
])
def test_the_tick_is_on_when_the_entity_holds_any_partner_value(tmp_path, over):
    root, _ = _load(tmp_path, {"E": _profile(**over)})
    with patch("ui._config.data_root_dir", return_value=root):
        assert ui._entity_holds_partner(ui._load_entities()["E"]) is True


def test_the_tick_is_off_with_no_partner_value_and_a_firm_pan_alone_does_not_tick_it(tmp_path):
    root, _ = _load(tmp_path, {"E": _profile(partner_firm_pan="BBBBB1111B")})
    with patch("ui._config.data_root_dir", return_value=root):
        assert ui._entity_holds_partner(ui._load_entities()["E"]) is False
    assert ui._entity_holds_partner(None) is False


# ---- the page ----

def _blocks():
    with gr.Blocks() as demo:
        ui.render()
    return demo


def _by_label(demo, label_start):
    return [c for c in demo.blocks.values() if str(getattr(c, "label", "") or "").startswith(label_start)]


def _fn(demo, name):
    found = [f for f in demo.fns.values() if getattr(f.fn, "__name__", "") == name]
    return max(found, key=lambda f: len(f.inputs))      # the Entities form, not a same-named card handler


def test_firm_pan_is_relabelled_sits_right_after_the_entity_pan_and_is_never_gated():
    demo = _blocks()
    firm = _by_label(demo, "PAN of the firm or AOP this entity holds an interest in")
    assert len(firm) == 1 and "not this entity's own PAN" in firm[0].info
    own = [c for c in demo.blocks.values() if getattr(c, "label", None) == "PAN"]
    assert len(own) == 1                                                    # the entity's own label is unchanged
    seq = [c for c in demo.blocks.values() if c is own[0] or c is firm[0]]
    assert seq == [own[0], firm[0]]
    assert firm[0].visible is True
    assert not _by_label(demo, "Firm PAN")                                  # the old label is gone
    assert firm[0] not in _fn(demo, "_apply_gates").outputs                 # no switch touches it
    assert ui._FIELD_CONTROLS["partner_firm_pan"] in firm[0].label


def test_the_blank_form_starts_with_the_gated_controls_hidden():
    demo = _blocks()
    assert _by_label(demo, "Date of birth")[0].visible is True
    assert _by_label(demo, "Date of incorporation")[0].visible == "hidden"
    assert _by_label(demo, "Drawings accounts")[0].visible == "hidden"
    assert _by_label(demo, "Partner of a firm or LLP")[0].visible is True


def test_the_apply_handler_hides_not_removes_and_adds_the_note_with_no_prior_info_line():
    demo = _blocks()
    f = _fn(demo, "_apply_gates")
    vals = [""] * len(NAMES)
    vals[NAMES.index("aadhaar")] = "123412341234"
    out = f.fn("HUF", False, "", False, "", *vals)
    by_name = dict(zip(list(ui._compute_gates("Individual", False, "", False, "", {})), out))
    assert by_name["aadhaar"]["visible"] is True
    assert by_name["aadhaar"]["info"] == ui.HELD_NOTE
    assert by_name["dob"]["visible"] == "hidden"                            # hidden, not removed
    assert by_name["audit_basis"]["visible"] == "hidden"
    assert "partner_of_audited_firm" not in by_name["audit_basis"]["choices"]


def test_the_note_is_appended_to_an_existing_info_line_and_only_to_that_field():
    demo = _blocks()
    f = _fn(demo, "_apply_gates")
    vals = [""] * len(NAMES)
    vals[NAMES.index("drawings")] = "Equity:Drawings"
    out = dict(zip(list(ui._compute_gates("Individual", False, "", False, "", {})),
                   f.fn("Individual", False, "", False, "", *vals)))
    assert out["drawings"]["info"].endswith(ui.HELD_NOTE) and out["drawings"]["info"] != ui.HELD_NOTE
    assert ui.HELD_NOTE not in out["business_subtree"]["info"]               # NEGATIVE


# ---- saving with fields hidden never clears anything ----

FULL = dict(
    name="Syn Hidden", pan="AAAAA0000A", residency="Resident", default_regime="new",
    dob="1980-01-01", doi="2001-04-01", father_name="Syn Father", aadhaar="123412341234",
    business_subtree="Income:Business", drawings_accounts=["Equity:Drawings"],
    card_spend_default_account="Equity:Drawings", reimbursement_markers=["ZZRB"],
    partner_admission_date="2020-04-01", partner_firm_pan="BBBBB1111B",
    audit_case_basis="partner_of_audited_firm",
    bank_service_charge_account="Expenses:Bank", card_emi_interest_account="Expenses:Emi",
    foreign_dividends_in_book=True, foreign_dividend_cash_account="Assets:Cash",
    foreign_dividend_tax_account="Expenses:Tax", foreign_dividend_income_account="Income:Div",
    cc_large_spend_threshold=25000, cc_cred_max_overage=60, folder_name="Syn Folder",
    partner_comp_accounts={"bank": "Assets:Firm", "tds_expense": "Expenses:Tds"},
    extra_items={"bf_losses": ["loss"], "partner_comp_drivers": {"2025-26": {"firms_tax_rate": 0.25,
                                                                            "capital_rate": 0.4}}},
)


def _roundtrip(tmp_path, status, books):
    ent = dict(FULL, status=status)
    if books:
        ent["books"] = {"2025-26": str(tmp_path / "gone.gnucash")}
    root, p = _load(tmp_path, {"SYN-H": ent, "SYN-O": _profile(name="Other")})
    before = yaml.safe_load(p.read_text(encoding="utf-8"))
    demo = _blocks()
    sel, save = _fn(demo, "_on_select"), _fn(demo, "_on_save")
    with patch("ui._config.data_root_dir", return_value=root):
        out = sel.fn("SYN-H")
        # a pick-list comes back as gr.update(choices=..., value=...); the browser keeps its value
        vals = {id(c): (v["value"] if isinstance(v, dict) and "value" in v else v) for c, v in zip(sel.outputs, out)}
        msg = save.fn(*[vals[id(c)] for c in save.inputs])[0]
    return msg, before, yaml.safe_load(p.read_text(encoding="utf-8"))


@pytest.mark.parametrize("status,books", [("HUF", False), ("Individual", False), ("HUF", True), ("Individual", True)])
def test_a_save_with_the_hidden_fields_as_loaded_changes_no_stored_value(tmp_path, status, books):
    msg, before, after = _roundtrip(tmp_path, status, books)
    assert msg.startswith("**Saved**"), msg
    diff = {k: (before["SYN-H"].get(k), after["SYN-H"].get(k))
            for k in set(before["SYN-H"]) | set(after["SYN-H"]) if before["SYN-H"].get(k) != after["SYN-H"].get(k)}
    assert diff == {}
    assert after["SYN-O"] == before["SYN-O"]


def test_every_gated_control_is_still_a_save_input_so_a_hide_never_blanks_it():
    demo = _blocks()
    save, gate = _fn(demo, "_on_save"), _fn(demo, "_apply_gates")
    in_ids = {id(c) for c in save.inputs}
    gated = gate.inputs[5:]
    assert len(gated) == len(NAMES)
    missing = [n for n, c in zip(NAMES, gated) if id(c) not in in_ids]
    assert missing == []


# ---- help quotes tied to the page ----

def test_every_name_quoted_in_the_tab_help_is_a_real_control_label_or_the_note():
    import re
    demo = _blocks()
    labels = [str(getattr(c, "label", "") or "") for c in demo.blocks.values()]
    quotes = re.findall(r'"([^"]+)"', ui._SWITCH_HELP)
    assert len(quotes) >= 6
    for q in quotes:
        if q == ui.HELD_NOTE.rstrip(".") or q == ui.HELD_NOTE:
            continue
        assert any(lab.startswith(q) for lab in labels), q


def test_a_wrong_quote_would_fail_the_help_check():                          # NEGATIVE
    labels = [str(getattr(c, "label", "") or "") for c in _blocks().blocks.values()]
    assert not any(lab.startswith("Date of Birth") for lab in labels)       # wrong case is not a label


def test_the_intro_markdown_carries_the_switch_help():
    demo = _blocks()
    assert any(ui._SWITCH_HELP in str(getattr(c, "value", "")) for c in demo.blocks.values())
