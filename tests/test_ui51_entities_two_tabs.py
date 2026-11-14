"""tests/test_ui51_entities_two_tabs.py -- UI-51: the Entities screen in two tabs (This entity / All entities).

Cards follow the selected entity; year-end roots, the card-payment split settings, the no-holder
count and the dropdown data live on All entities and never read the Entity picker.
Synthetic entities and cards only (tmp_path); the real settings folder is never used."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import gradio as gr
import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from ui.tabs import _entity_cards as ec                      # noqa: E402
from ui.tabs import itr_entities as ui_mod                   # noqa: E402

KEYS = ["entity_a", "entity_b"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    p = tmp_path / "settings" / "cc_cards.yaml"
    monkeypatch.setattr(ec, "cards_file", lambda: p)
    monkeypatch.setattr(ec, "_holder_keys", lambda: list(KEYS))
    ks = set(KEYS)
    assert ec.save_card(p, ks, None, "ICICI-Amazon", "1111", "entity_a", True).startswith("**Saved")
    assert ec.save_card(p, ks, None, "ICICI-Sapphiro", "2222", "entity_a", False).startswith("**Saved")
    assert ec.save_card(p, ks, None, "HSBC-Premier", "3333", "entity_b", True).startswith("**Saved")
    assert ec.save_card(p, ks, None, "HDFC-Regalia", "4444", "", True).startswith("**Saved")
    return p


def _snap(path: Path) -> dict:
    return {f.name: f.read_bytes() for f in sorted(path.parent.glob("*"))}


def _blocks(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    return [b for b in re.split(r"(?m)^(?=  - bank:)", text.split("cards:", 1)[1]) if "bank:" in b]


def _val(u):
    return u.get("value") if isinstance(u, dict) else getattr(u, "value", None)


def _choices(u):
    return u.get("choices") if isinstance(u, dict) else getattr(u, "choices", None)


# ------------------------------------------------------------------ cards follow the entity

def test_only_the_selected_entitys_cards_are_listed(env):
    rows, _ = ec._snapshot()
    mine = ec.rows_for_entity(rows, "entity_a")
    assert [r["last4"] for _i, r in mine] == [["1111"], ["2222"]]
    assert [r["last4"] for _i, r in ec.rows_for_entity(rows, "entity_b")] == [["3333"]]


def test_another_holders_card_never_shows_under_the_selected_entity(env):          # NEGATIVE
    md, card_dd, *_ = ec._refresh_entity("entity_a")
    assert "1111" in md and "2222" in md
    assert "3333" not in md and "4444" not in md and "HSBC" not in md and "HDFC" not in md
    labels = " | ".join(c[0] for c in _choices(card_dd))
    assert "3333" not in labels and "4444" not in labels and "HSBC" not in labels


def test_other_holders_are_a_bare_count_naming_neither_cards_nor_holders(env):      # NEGATIVE
    md, *_ = ec._refresh_entity("entity_a")
    assert "1 card under other holders" in md
    assert "entity_b" not in md
    md_b, *_ = ec._refresh_entity("entity_b")
    assert "2 cards under other holders" in md_b and "entity_a" not in md_b


def test_new_entity_shows_no_cards_and_offers_only_a_new_card(env):                # NEGATIVE
    md, card_dd, holder_dd, *_ = ec._refresh_entity("")
    assert "1111" not in md and "2222" not in md and "3333" not in md and "4444" not in md
    assert _choices(card_dd) == [("(new card)", ec._NEW)]
    assert _val(holder_dd) == ""


def test_the_holder_defaults_to_the_selected_entity(env):
    _md, card_dd, holder_dd, folder, last4, active, account = ec._refresh_entity("entity_b")
    assert _val(holder_dd) == "entity_b" and _val(card_dd) == ec._NEW
    assert last4 == "" and active is True and account == ""


def test_switching_entity_writes_nothing_to_disk(env):                             # NEGATIVE
    before = _snap(env)
    for k in ("entity_a", "entity_b", "", "entity_a"):
        ec._refresh_entity(k)
        ec._pick(ec._NEW, k)
    assert _snap(env) == before


def test_picking_a_card_loads_it(env):
    rows, _ = ec._snapshot()
    i = next(i for i, r in ec.rows_for_entity(rows, "entity_a") if r["last4"] == ["2222"])
    folder, last4, holder, active, _acct = ec._pick(str(i), "entity_a")
    assert _val(folder) == "ICICI-Sapphiro" and last4 == "2222" and _val(holder) == "entity_a" and active is False


def test_a_card_of_another_holder_cannot_be_picked_edited_or_deleted_by_index(env):   # NEGATIVE
    rows, _ = ec._snapshot()
    j = next(i for i, r in enumerate(rows) if r["holder"] == "entity_b")
    before = _snap(env)
    folder, last4, *_ = ec._pick(str(j), "entity_a")
    assert _val(folder) is None and last4 == ""
    msg = ec._on_save("entity_a", str(j), "ICICI-Amazon", "9999", "entity_a", True, "")[0]
    assert "Not saved" in msg
    msg = ec._on_delete("entity_a", str(j), True)[0]
    assert "Not deleted" in msg
    assert _snap(env) == before


def test_a_new_entity_cannot_save_a_card(env):                                      # NEGATIVE
    before = _snap(env)
    msg = ec._on_save("", ec._NEW, "ICICI-Amazon", "9999", "", True, "")[0]
    assert "Not saved" in msg
    assert _snap(env) == before


def test_a_new_card_gets_the_selected_entity_as_holder(env):
    msg = ec._on_save("entity_b", ec._NEW, "ICICI-Amazon", "5555", "", True, "")[0]
    assert msg.startswith("**Saved")
    rows, _ = ec._snapshot()
    assert [r["holder"] for r in rows if r["last4"] == ["5555"]] == ["entity_b"]


def test_a_save_for_one_entity_leaves_other_holders_rows_byte_identical(env):      # NEGATIVE
    other_before = [b for b in _blocks(env) if 'holder: "entity_b"' in b or 'holder: ""' in b]
    rows, _ = ec._snapshot()
    i = next(i for i, r in ec.rows_for_entity(rows, "entity_a") if r["last4"] == ["1111"])
    assert ec._on_save("entity_a", str(i), "ICICI-Amazon", "1111, 6666", "entity_a", True, "")[0].startswith("**Saved")
    assert ec._on_save("entity_a", ec._NEW, "SBM-Global", "7777", "", True, "")[0].startswith("**Saved")
    other_after = [b for b in _blocks(env) if 'holder: "entity_b"' in b or 'holder: ""' in b]
    assert other_after == other_before
    assert any("6666" in b for b in _blocks(env))


def test_delete_for_one_entity_leaves_other_holders_rows_byte_identical(env):      # NEGATIVE
    other_before = [b for b in _blocks(env) if 'holder: "entity_a"' not in b]
    rows, _ = ec._snapshot()
    i = next(i for i, r in ec.rows_for_entity(rows, "entity_a") if r["last4"] == ["2222"])
    assert ec._on_delete("entity_a", str(i), True)[0].startswith("**Deleted")
    assert [b for b in _blocks(env) if 'holder: "entity_a"' not in b] == other_before
    assert not any("2222" in b for b in _blocks(env))


# ------------------------------------------------------------------ All entities (global)

def test_the_no_holder_cards_are_a_count_with_a_hint(env):
    md, split, prepay = ec._refresh_global()
    assert "1 card with no holder" in md and "set its holder" in md
    assert "HDFC" not in md and "4444" not in md


def test_the_split_settings_stay_app_wide_in_the_same_file(env):
    msg = ec._on_save_top("2026-04-01", "")[0]
    assert msg.startswith("**Saved")
    sf, pa = ec.read_top(env, set(KEYS))
    assert sf == "2026-04-01"
    assert len(ec._snapshot()[0]) == 4                       # the cards are untouched


def test_the_global_refresh_takes_no_entity_input():                                   # NEGATIVE
    import inspect
    assert list(inspect.signature(ec._refresh_global).parameters) == []
    assert list(inspect.signature(ec._on_save_top).parameters) == ["split_from", "prepayment_account"]


# ------------------------------------------------------------------ the page layout

def _ancestors(c):
    out = []
    p = getattr(c, "parent", None)
    while p is not None:
        out.append(p)
        p = getattr(p, "parent", None)
    return out


def _in_tab(c, label):
    return any(isinstance(a, gr.Tab) and a.label == label for a in _ancestors(c))


@pytest.fixture(scope="module")
def page():
    with gr.Blocks() as demo:
        with gr.Tab("Entities") as t:
            ui_mod.render(container_tab=t)
    return demo


def _by_label(demo, label):
    return next(c for c in demo.blocks.values() if getattr(c, "label", "") == label)


def test_there_are_two_tabs_with_the_expected_names(page):
    tabs = [c.label for c in page.blocks.values() if isinstance(c, gr.Tab) and c.label in ("This entity", "All entities")]
    assert tabs == ["This entity", "All entities"]


def test_the_picker_form_and_cards_are_on_this_entity(page):
    picker = _by_label(page, "Entity")
    assert _in_tab(picker, "This entity") and not _in_tab(picker, "All entities")
    for lab in ("Entity key", "Name", "PAN", "Card", "Last 4 digits"):
        assert _in_tab(_by_label(page, lab), "This entity"), lab
    for btn in ("Save Entity", "Delete Entity", "Save card"):
        b = next(c for c in page.blocks.values() if isinstance(c, gr.Button) and c.value == btn)
        assert _in_tab(b, "This entity"), btn


def test_roots_split_settings_and_dropdown_data_are_on_all_entities(page):
    for lab in ("Drive Accounts root", "GnuCash profile root", "Split from date (ccp_split_from)",
                "Corporate card prepayment account (prepayment_account)"):
        c = _by_label(page, lab)
        assert _in_tab(c, "All entities") and not _in_tab(c, "This entity"), lab
    assert any(isinstance(c, gr.Button) and c.value == "Save split settings" and _in_tab(c, "All entities")
               for c in page.blocks.values())


def test_nothing_on_all_entities_reads_or_follows_the_entity_picker(page):          # NEGATIVE
    picker = _by_label(page, "Entity")
    glob = {c for c in page.blocks.values() if _in_tab(c, "All entities")}
    assert glob
    for fn in page.fns.values():
        ins, outs = set(fn.inputs), set(fn.outputs)
        if picker in ins:
            assert not (outs & glob), "an entity-driven handler writes to the All entities tab"
        if outs & glob or (ins & glob):
            assert picker not in ins, "an All entities handler reads the Entity picker"
    # and nothing there is the target of the picker's own change event
    changers = [fn for fn in page.fns.values() if picker in fn.inputs]
    assert changers


def test_the_header_names_the_selected_entity(monkeypatch):
    class E:
        name = "Synthetic Person"
    monkeypatch.setattr(ui_mod, "_load_entities", lambda: {"SYN-IND": E()})
    assert ui_mod._entity_header("SYN-IND") == "### SYN-IND - Synthetic Person"
    assert "New entity" in ui_mod._entity_header("")
    assert "SYN-IND" not in ui_mod._entity_header("")


def test_the_header_is_read_only(tmp_path, monkeypatch):                              # NEGATIVE
    monkeypatch.setattr(ui_mod, "_load_entities", lambda: {})
    before = sorted(p.name for p in tmp_path.iterdir())
    ui_mod._entity_header("anything")
    assert sorted(p.name for p in tmp_path.iterdir()) == before


def test_every_card_handler_returns_as_many_values_as_it_has_outputs(env, page):
    """A Gradio handler that returns the wrong number of values fails at click time, not in a unit test."""
    n_out = {}
    for fn in page.fns.values():
        if getattr(fn.fn, "__module__", "").endswith("_entity_cards"):
            n_out[fn.fn.__name__] = len(fn.outputs)
    assert len(ec._refresh_entity("entity_a")) == n_out["_refresh_entity"]
    assert len(ec._on_save("entity_a", ec._NEW, "ICICI-Amazon", "bad", "", True, "")) == n_out["_on_save"]
    assert len(ec._on_delete("entity_a", ec._NEW, False)) == n_out["_on_delete"]
    assert len(ec._pick(ec._NEW, "entity_a")) == n_out["_pick"]
    assert len(ec._refresh_global()) == n_out["_refresh_global"]
    assert len(ec._on_save_top("", "")) == n_out["_on_save_top"]
