"""UI-35: every entities.yaml field has a control on GnuCash > Entities, and a
save never deletes a key the form does not own. Synthetic data only."""
from __future__ import annotations

import dataclasses
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import configs                                   # noqa: E402
from ui.tabs import itr_entities as ui_mod       # noqa: E402

DRIVERS = {"2025-26": {"firms_tax_rate": 0.25, "target_compensation": 1200000,
                       "remuneration_tds_section": "194T", "remuneration_tds_start_date": "2024-04-01"}}
UNKNOWN_EXTRA = {"future_key": {"a": [1, 2, 3], "b": "kept"}}
PARTNER_ACCTS = {"bank": "Assets:Bank:Firm", "tds_expense": "Expenses:Tax:TDS",
                 "interest_on_capital": "Income:Firm:Interest", "current_account": "Assets:Firm:Current",
                 "capital_contribution": "Assets:Firm:Capital", "medical_expense": "Expenses:Medical",
                 "remuneration_income": "Income:Firm:Remuneration", "share_of_profit_income": "Income:Firm:Share"}


def _entity(**over):
    d = {"name": "Synthetic Partner", "pan": "AAAAA0000A", "status": "Individual", "residency": "Resident",
         "default_regime": "new",
         "bank_service_charge_account": "Expenses:Bank Service Charge",
         "card_emi_interest_account": "Expenses:Card EMI Interest",
         "partner_comp_accounts": dict(PARTNER_ACCTS),
         "foreign_dividends_in_book": True, "foreign_dividends_in_book_by_ay": {"2025-26": False},
         "extra_items": {"bf_losses": ["loss one"], "partner_comp_drivers": DRIVERS, **UNKNOWN_EXTRA}}
    d.update(over)
    return d


def _seed(tmp_path, entities=None):
    root = tmp_path / "Data"
    p = root / "itr" / "entities.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(yaml.safe_dump(entities or {"SYN-P": _entity(), "SYN-O": _entity(name="Other Entity", pan="BBBBB1111B")},
                                sort_keys=True), encoding="utf-8")
    return root, p


def _save(key, **kw):
    """The old-style call: the form's classic fields only, as an older caller made it."""
    e = ui_mod._load_entities()[key]
    return ui_mod._save_entity(
        key, key, e.name, e.pan, e.status, e.residency, "", "", "", "", "", "", "", e.default_regime, "",
        False, "", "\n".join(e.extra_items.get("bf_losses", [])), "", **kw)


# ---- the RED FLAG: a save used to delete every extra_items key it does not own ----

def test_a_form_save_keeps_partner_drivers_and_unknown_extra_keys_byte_for_byte(tmp_path):
    root, p = _seed(tmp_path)
    before = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-P"]["extra_items"]
    with patch("ui._config.data_root_dir", return_value=root):
        assert "Saved" in _save("SYN-P")
    after = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-P"]["extra_items"]
    assert after["partner_comp_drivers"] == before["partner_comp_drivers"]
    assert after["future_key"] == before["future_key"]
    assert after == before


def test_a_save_still_clears_bf_losses_and_notes_when_the_form_empties_them(tmp_path):    # NEGATIVE
    root, p = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        e = ui_mod._load_entities()["SYN-P"]
        ui_mod._save_entity("SYN-P", "SYN-P", e.name, e.pan, e.status, e.residency, "", "", "", "", "", "", "",
                            "new", "", False, "", "", "")
    extra = yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-P"]["extra_items"]
    assert "bf_losses" not in extra and "clubbing_notes" not in extra
    assert extra["partner_comp_drivers"] == DRIVERS and extra["future_key"] == UNKNOWN_EXTRA["future_key"]


# ---- helpers for the rest ----

def _book(tmp_path):
    """Synthetic book: one active account, one hidden, one placeholder, one dormant."""
    from tests.gnc_book_fixture import account_xml, txn_xml, write_book
    accs = [
        account_xml("root", "Root Account", "ROOT", None),
        account_xml("exp", "Expenses", "EXPENSE", "root", ["placeholder"]),
        account_xml("fee", "Bank Fees", "EXPENSE", "exp"),
        account_xml("hid", "Hidden Fees", "EXPENSE", "exp", ["hidden"]),
        account_xml("old", "Dormant Fees", "EXPENSE", "exp"),
        account_xml("bank", "Synthetic Bank", "BANK", "root"),
    ]
    txns = [txn_xml("synthetic fee", "2025-06-01", [("fee", 10000), ("bank", -10000)])]
    return write_book(tmp_path / "SynBook2526.gnucash", accs, txns)


def _with_book(tmp_path, **over):
    book = _book(tmp_path)
    e = _entity(books={"2025-26": book}, **over)
    return _seed(tmp_path, {"SYN-P": e, "SYN-O": _entity(name="Other Entity", pan="BBBBB1111B")}), book


def _call(key, root, **kw):
    """Old-style positional call, plus any new keyword fields."""
    with patch("ui._config.data_root_dir", return_value=root):
        e = ui_mod._load_entities()[key]
        return ui_mod._save_entity(
            key, key, e.name, e.pan, e.status, e.residency, "", "", "", "", "", "", "", e.default_regime, "",
            e.audit_case, "", "\n".join(e.extra_items.get("bf_losses", [])), "",
            books_text=ui_mod._format_kv_lines(e.books), **kw)


def _profiles(p):
    return configs.load_entities(p)


# ---- pick-lists ----

def test_pick_list_offers_only_active_postable_accounts(tmp_path):
    (root, p), book = _with_book(tmp_path)
    choices, note = ui_mod._account_choices({"2025-26": book})
    assert "Expenses:Bank Fees" in choices and "Synthetic Bank" in choices
    assert "Expenses:Hidden Fees" not in choices                      # NEGATIVE: hidden
    assert "Expenses" not in choices                                  # NEGATIVE: placeholder
    assert "Expenses:Dormant Fees" not in choices                     # NEGATIVE: dormant
    assert "FY 2025-26" in note


def test_no_book_falls_back_to_typing_and_says_so(tmp_path):
    choices, note = ui_mod._account_choices({})
    assert choices == [] and "typed in" in note
    choices, note = ui_mod._account_choices({"2025-26": str(tmp_path / "missing.gnucash")})
    assert choices == [] and "could not be read" in note


# ---- save: account checks ----

@pytest.mark.parametrize("path,word", [("Expenses:Hidden Fees", "hidden"), ("Expenses", "placeholder")])
def test_a_hidden_or_placeholder_account_is_refused_and_nothing_is_written(tmp_path, path, word):
    (root, p), _book_path = _with_book(tmp_path)
    before = p.read_bytes()
    msg = _call("SYN-P", root, bank_service_charge_account=path)
    assert msg.startswith("**Not saved") and "Bank service charge account" in msg and word in msg
    assert p.read_bytes() == before and not list(p.parent.glob("*.bak-*"))


def test_a_hidden_partner_account_is_refused_with_its_key_named(tmp_path):
    (root, p), _b = _with_book(tmp_path)
    before = p.read_bytes()
    msg = _call("SYN-P", root, partner_comp_accounts={**PARTNER_ACCTS, "bank": "Expenses:Hidden Fees"})
    assert msg.startswith("**Not saved") and "'bank'" in msg
    assert p.read_bytes() == before


def test_a_path_missing_from_the_book_warns_but_saves(tmp_path):
    (root, p), _b = _with_book(tmp_path)
    msg = _call("SYN-P", root, card_emi_interest_account="Expenses:Nowhere")
    assert msg.startswith("**Saved**") and "Warning" in msg and "Expenses:Nowhere" in msg
    assert _profiles(p)["SYN-P"].card_emi_interest_account == "Expenses:Nowhere"


def test_an_unreadable_book_saves_with_an_unchecked_warning(tmp_path):
    (root, p) = _seed(tmp_path, {"SYN-P": _entity(books={"2025-26": str(tmp_path / "gone.gnucash")})})
    msg = _call("SYN-P", root, bank_service_charge_account="Expenses:Anything")
    assert msg.startswith("**Saved**") and "not checked" in msg


# ---- save: drivers ----

@pytest.mark.parametrize("text,needle", [
    ("2025-26: [1, 2", "not valid YAML"),
    ("- a\n- b\n", "expected"),
    ("2025-26:\n  made_up_rate: 0.1\n", "made_up_rate"),
    ("2025-26:\n  firms_tax_rate: lots\n", "firms_tax_rate"),
    ("2025-26:\n  firms_tax_rate: 25\n", "between 0 and 1"),
    ("2025-26:\n  capital_months_total: 4.5\n", "capital_months_total"),
    ("2025-26:\n  remuneration_tds_start_date: not-a-date\n", "remuneration_tds_start_date"),
    ("FY25:\n  firms_tax_rate: 0.1\n", "valid financial year"),
    ("2025-26: 0.25\n", "driver: value"),
])
def test_bad_driver_text_is_refused_and_entities_yaml_is_untouched(tmp_path, text, needle):
    root, p = _seed(tmp_path)
    before = p.read_bytes()
    msg = _call("SYN-P", root, partner_comp_drivers_text=text)
    assert msg.startswith("**Not saved") and needle in msg
    assert p.read_bytes() == before and not list(p.parent.glob("*.bak-*"))


def test_good_driver_text_replaces_drivers_and_keeps_the_rest(tmp_path):
    root, p = _seed(tmp_path)
    before = _profiles(p)
    text = "2025-26:\n  firms_tax_rate: 0.3\n  remuneration_tds_start_date: '2024-04-01'\n"
    assert _call("SYN-P", root, partner_comp_drivers_text=text).startswith("**Saved**")
    after = _profiles(p)
    assert after["SYN-P"].extra_items["partner_comp_drivers"]["2025-26"]["firms_tax_rate"] == 0.3
    assert after["SYN-P"].extra_items["future_key"] == UNKNOWN_EXTRA["future_key"]
    assert after["SYN-O"] == before["SYN-O"]


def test_blank_driver_text_removes_the_drivers_only(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, partner_comp_drivers_text="").startswith("**Saved**")
    extra = _profiles(p)["SYN-P"].extra_items
    assert "partner_comp_drivers" not in extra and extra["future_key"] == UNKNOWN_EXTRA["future_key"]


def test_the_form_text_round_trips_the_stored_drivers(tmp_path):
    root, p = _seed(tmp_path)
    before = _profiles(p)["SYN-P"].extra_items["partner_comp_drivers"]
    text = ui_mod._format_drivers(before)
    assert ui_mod._parse_drivers(text) == (before, [])
    assert _call("SYN-P", root, partner_comp_drivers_text=text).startswith("**Saved**")
    assert _profiles(p)["SYN-P"].extra_items["partner_comp_drivers"] == before


# ---- save: the other new fields ----

def test_foreign_dividend_switches_are_saved_and_a_bad_line_is_refused(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, foreign_dividends_in_book=False,
                 foreign_dividends_in_book_by_ay_text="2026-27 = true\n2025-26 = no").startswith("**Saved**")
    e = _profiles(p)["SYN-P"]
    assert e.foreign_dividends_in_book is False
    assert e.foreign_dividends_in_book_by_ay == {"2026-27": True, "2025-26": False}
    before = p.read_bytes()
    msg = _call("SYN-P", root, foreign_dividends_in_book_by_ay_text="2026-27 = maybe")
    assert msg.startswith("**Not saved") and "must be true/false" in msg and p.read_bytes() == before


def test_partner_accounts_set_clear_and_keep_unknown_keys(tmp_path):
    accts = {**PARTNER_ACCTS, "future_account": "Assets:Kept"}
    root, p = _seed(tmp_path, {"SYN-P": _entity(partner_comp_accounts=accts)})
    new = {**PARTNER_ACCTS, "bank": "Assets:Bank:New", "medical_expense": ""}
    assert _call("SYN-P", root, partner_comp_accounts=new).startswith("**Saved**")
    got = _profiles(p)["SYN-P"].partner_comp_accounts
    assert got["bank"] == "Assets:Bank:New" and "medical_expense" not in got
    assert got["future_account"] == "Assets:Kept"
    assert got["tds_expense"] == PARTNER_ACCTS["tds_expense"]


def test_an_unknown_partner_account_key_is_refused(tmp_path):
    root, p = _seed(tmp_path)
    before = p.read_bytes()
    msg = _call("SYN-P", root, partner_comp_accounts={"bnak": "Assets:Typo"})
    assert msg.startswith("**Not saved") and "'bnak'" in msg and p.read_bytes() == before


# ---- isolation and the old-caller contract ----

def test_saving_one_new_field_changes_no_other_field_or_entity(tmp_path):
    root, p = _seed(tmp_path)
    before = _profiles(p)
    assert _call("SYN-P", root, bank_service_charge_account="Expenses:Changed").startswith("**Saved**")
    after = _profiles(p)
    assert after["SYN-O"] == before["SYN-O"]
    a, b = dataclasses.asdict(after["SYN-P"]), dataclasses.asdict(before["SYN-P"])
    assert a.pop("bank_service_charge_account") == "Expenses:Changed"
    b.pop("bank_service_charge_account")
    assert a == b


def test_an_old_style_caller_passing_none_preserves_every_new_field(tmp_path):
    root, p = _seed(tmp_path)
    before = _profiles(p)["SYN-P"]
    assert _call("SYN-P", root).startswith("**Saved**")
    after = _profiles(p)["SYN-P"]
    for f in ("bank_service_charge_account", "card_emi_interest_account", "partner_comp_accounts",
              "foreign_dividends_in_book", "foreign_dividends_in_book_by_ay", "extra_items"):
        assert getattr(after, f) == getattr(before, f), f
    assert dataclasses.asdict(after) == dataclasses.asdict(before)


def test_a_new_entity_save_starts_with_empty_new_fields(tmp_path):
    root, p = _seed(tmp_path)
    with patch("ui._config.data_root_dir", return_value=root):
        msg = ui_mod._save_entity("", "SYN-N", "New One", "EEEEE5555E", "Individual", "Resident",
                                  "", "", "", "", "", "", "", "new", "", False, "", "", "")
    assert msg.startswith("**Saved**")
    n = _profiles(p)["SYN-N"]
    assert n.extra_items == {} and n.partner_comp_accounts == {} and n.bank_service_charge_account == ""


# ---- every EntityProfile field has a control ----

def _labels():
    import gradio as gr
    with gr.Blocks() as demo:
        ui_mod.render()
    return [str(getattr(c, "label", "") or "") for c in demo.blocks.values()]


def test_every_entityprofile_field_has_a_control_on_the_tab():
    fields = {f.name for f in dataclasses.fields(configs.EntityProfile)}
    covered = set(ui_mod._FIELD_CONTROLS) | set(ui_mod._FIELD_CONTROL_ALLOW_LIST)
    assert fields - covered == set(), f"fields with no control: {sorted(fields - covered)}"
    assert covered - fields == set()                                   # NEGATIVE: no stale entries
    labels = _labels()
    for field_name, fragment in ui_mod._FIELD_CONTROLS.items():
        assert any(fragment in lab for lab in labels), f"{field_name}: no control labelled '{fragment}'"


def test_the_allow_list_and_hidden_list_start_empty():
    assert ui_mod._FIELD_CONTROL_ALLOW_LIST == () and ui_mod._FORM_HIDDEN_FIELDS == ()


def test_partner_and_driver_keys_match_the_partner_skill():
    from agents.skill_partner_comp_recon import jv_emitter
    assert ui_mod._PARTNER_ACCOUNT_KEYS == (*jv_emitter.ACCOUNT_KEYS, jv_emitter.ROUNDING_KEY)
    assert len(set(ui_mod._DRIVER_KEYS)) == len(ui_mod._DRIVER_KEYS)


def test_the_example_file_documents_every_entityprofile_field():
    example = ROOT / "bundling" / "canonical" / "itr" / "entities.example.yaml"
    raw = yaml.safe_load(example.read_text(encoding="utf-8"))
    used = {k for block in raw.values() for k in block}
    fields = {f.name for f in dataclasses.fields(configs.EntityProfile)} - {"key"}
    assert fields - used == set(), f"not shown in entities.example.yaml: {sorted(fields - used)}"
    prof = configs.load_entities(example)["SYN-PARTNER"]
    assert ui_mod._parse_drivers(ui_mod._format_drivers(prof.extra_items["partner_comp_drivers"]))[1] == []
    assert set(prof.partner_comp_accounts) == set(ui_mod._PARTNER_ACCOUNT_KEYS)


# ---- partner_firm_pan (ITR-05 field) has its own control and survives a save ----

def test_the_firm_pan_is_saved_upper_cased_and_kept_by_an_old_style_save(tmp_path):
    root, p = _seed(tmp_path, {"SYN-P": _entity(partner_firm_pan="FFFFF1234F")})
    assert _profiles(p)["SYN-P"].partner_firm_pan == "FFFFF1234F"
    assert _call("SYN-P", root).startswith("**Saved**")                       # older caller: None
    assert _profiles(p)["SYN-P"].partner_firm_pan == "FFFFF1234F"
    assert _call("SYN-P", root, partner_firm_pan=" ggggg4321g ").startswith("**Saved**")
    assert _profiles(p)["SYN-P"].partner_firm_pan == "GGGGG4321G"


def test_a_malformed_firm_pan_is_refused_and_nothing_is_written(tmp_path):        # NEGATIVE
    root, p = _seed(tmp_path, {"SYN-P": _entity(partner_firm_pan="FFFFF1234F")})
    before = p.read_bytes()
    msg = _call("SYN-P", root, partner_firm_pan="NOTAPAN")
    assert msg.startswith("**Not saved") and "Firm PAN" in msg and p.read_bytes() == before


def test_a_blank_firm_pan_clears_it_and_changes_no_other_entity(tmp_path):
    root, p = _seed(tmp_path, {"SYN-P": _entity(partner_firm_pan="FFFFF1234F"),
                               "SYN-O": _entity(name="Other", pan="BBBBB1111B", partner_firm_pan="HHHHH5555H")})
    assert _call("SYN-P", root, partner_firm_pan="").startswith("**Saved**")
    got = _profiles(p)
    assert got["SYN-P"].partner_firm_pan == "" and got["SYN-O"].partner_firm_pan == "HHHHH5555H"
