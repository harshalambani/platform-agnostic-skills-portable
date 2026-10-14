"""
tests/skill_itr_workbook/test_itr04_review_mapping.py -- ITR-04: Review
Mapping finds the accounts a workbook run produced. The workbook records the
entity inside the snippet (and in its name); Review Mapping attributes by that
key, lists a registered book's own leaf accounts, says which source was empty,
and shows an applied tag as "changed, not saved". Synthetic data only.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path
from unittest.mock import patch

import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
SCRIPTS = SRC / "agents" / "skill_itr_workbook" / "scripts"
for p in (str(ROOT), str(SRC), str(SCRIPTS), str(SRC / "agents" / "skill_itr_workbook"), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import agent  # noqa: E402
import fixture_gen  # noqa: E402
import mapping  # noqa: E402
from agents.gnucash_accounts import GncAccount  # noqa: E402
from ui import _review_engine  # noqa: E402
from ui.tabs import itr_mapping_review as ui_mod  # noqa: E402

ENTITIES_EXAMPLE = ROOT / "bundling" / "canonical" / "itr" / "entities.example.yaml"
SCRIPS_EXAMPLE = ROOT / "bundling" / "canonical" / "itr" / "scrips.example.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _root(tmp_path, match=None):
    data_root = tmp_path / "Data"
    (data_root / "itr" / "mappings").mkdir(parents=True)
    (data_root / "outputs").mkdir()
    ents = {
        "ONE": {"name": "One", "pan": "AAAAA0000A", "status": "Individual", "default_regime": "new"},
        "TWO": {"name": "Two", "pan": "BBBBB0000B", "status": "Individual", "default_regime": "new"},
    }
    for k, v in (match or {}).items():
        ents[k]["workbook_match"] = v
    (data_root / "itr" / "entities.yaml").write_text(yaml.safe_dump(ents), encoding="utf-8")
    return data_root


def _snip(data_root, name, entity, guids):
    body = yaml.safe_dump([{"guid": g, "path": f"Assets/{g}", "tag": "REPLACE_ME", "note": "n"} for g in guids])
    text = (f"# entity: {entity}\n" if entity else "") + body
    p = data_root / "outputs" / name
    p.write_text(text, encoding="utf-8")
    return p


def _load(data_root, key):
    with patch("ui._config.data_root_dir", return_value=data_root):
        return ui_mod._collect_review(key)


# --- attribution by the recorded key ------------------------------------------

def test_recorded_key_attributes_snippet_without_any_config(tmp_path):
    root = _root(tmp_path)
    _snip(root, "x-proposed-mappings.yaml", "ONE", ["a1", "a2"])
    rows, _ = _load(root, "ONE")
    assert {r["guid"] for r in rows} == {"a1", "a2"}


def test_other_entitys_snippet_never_shown_even_if_filename_has_this_name(tmp_path):
    root = _root(tmp_path, match={"ONE": "One", "TWO": "Two"})
    _snip(root, "run-One-proposed-mappings.yaml", "TWO", ["foreign"])
    rows, info = _load(root, "ONE")
    assert rows == [] and info["snippet"] is None


def test_snippet_with_no_recorded_entity_and_no_token_is_never_shown(tmp_path):
    root = _root(tmp_path)
    _snip(root, "run-One-proposed-mappings.yaml", None, ["orphan"])
    rows, info = _load(root, "ONE")
    assert rows == [] and info["snippet"] is None


def test_old_snippet_still_attributed_by_filename_token(tmp_path):
    root = _root(tmp_path, match={"ONE": "One"})
    _snip(root, "run-One-proposed-mappings.yaml", None, ["old"])
    rows, _ = _load(root, "ONE")
    assert [r["guid"] for r in rows] == ["old"]


def test_workbook_run_records_entity_inside_snippet_and_in_name(tmp_path):
    html = tmp_path / "bs.html"
    html.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out = tmp_path / "out.xlsx"
    agent.run(str(html), str(out), mapping_file=str(FIXTURES / "syn_ind_partial.mapping.yaml"),
              entities_path=str(ENTITIES_EXAMPLE), entity_key="SYN-IND", scrips_path=str(SCRIPS_EXAMPLE),
              config_path="no-such-config.yaml")
    snip = fixture_gen.snippet_for(out)
    assert "SYN-IND" in snip.name
    text = snip.read_text(encoding="utf-8")
    assert mapping.snippet_recorded_entity(text) == "SYN-IND"
    assert isinstance(yaml.safe_load(text), list)            # still the plain list
    assert ui_mod._snippet_recorded_entity(snip) == "SYN-IND"


# --- a registered book's leaves -------------------------------------------------

def _acct(i, name, parent=None, path=None, flags=(), typ="ASSET"):
    return GncAccount(id=i, name=name, type=typ, parent_id=parent, path=path or name,
                      special_flags=frozenset(flags))


def _book(tmp_path, monkeypatch, accounts):
    f = tmp_path / "book.gnucash"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ui_mod, "_entity_book_path", lambda k: f)
    import agents.gnucash_accounts as ga
    monkeypatch.setattr(ga, "load_accounts", lambda p: accounts)


def test_book_leaves_listed_when_no_snippet_and_no_mapping(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _book(tmp_path, monkeypatch, [
        _acct("p", "Assets", flags=("placeholder",)),
        _acct("l1", "Bank", "p", "Assets:Bank"),
        _acct("h", "Old", "p", "Assets:Old", flags=("hidden",)),
    ])
    rows, info = _load(root, "ONE")
    assert [r["guid"] for r in rows] == ["l1"]                # hidden/placeholder never offered
    assert all(r["unmapped"] for r in rows) and info["book"] is not None


def test_mapped_account_or_mapped_ancestor_never_listed_as_unmapped(tmp_path, monkeypatch):
    root = _root(tmp_path)
    (root / "itr" / "mappings" / "ONE.mapping.yaml").write_text(yaml.safe_dump([
        {"guid": "par", "path": "Assets:Funds", "tag": "AL_SECURITIES"},
        {"guid": "own", "path": "Assets:Bank", "tag": "AL_CASH_BANK"},
    ]), encoding="utf-8")
    _book(tmp_path, monkeypatch, [
        _acct("par", "Funds", path="Assets:Funds"),
        _acct("kid", "Fund A", "par", "Assets:Funds:Fund A"),
        _acct("own", "Bank", path="Assets:Bank"),
        _acct("free", "Cash", path="Assets:Cash"),
    ])
    rows, _ = _load(root, "ONE")
    unmapped = {r["guid"] for r in rows if r["unmapped"]}
    assert unmapped == {"free"}
    assert "kid" not in unmapped and "own" not in unmapped


def test_snippet_row_not_duplicated_by_book_row(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _snip(root, "s-proposed-mappings.yaml", "ONE", ["l1"])
    _book(tmp_path, monkeypatch, [_acct("l1", "Bank", path="Assets:Bank")])
    rows, _ = _load(root, "ONE")
    assert [r["guid"] for r in rows] == ["l1"]


# --- the empty state ----------------------------------------------------------------

def _empty(root, key="ONE"):
    with patch("ui._config.data_root_dir", return_value=root):
        return ui_mod._load_review_data(key)


def test_empty_state_names_each_empty_source(tmp_path):
    html = _empty(_root(tmp_path))
    assert "no mapping entries saved yet" in html
    assert "no proposed-mappings snippet is attributable" in html
    assert "no book registered" in html


def test_empty_state_never_says_run_workbook_when_snippet_exists(tmp_path):
    root = _root(tmp_path)
    (root / "outputs" / "s-proposed-mappings.yaml").write_text("# entity: ONE\n[]\n", encoding="utf-8")
    html = _empty(root)
    assert "attributable to this entity" in html
    assert "Run the ITR Workbook" not in html


# --- changed, not saved ---------------------------------------------------------------

def _html_for_rows(tmp_path):
    root = _root(tmp_path)
    _snip(root, "s-proposed-mappings.yaml", "ONE", ["a1"])
    return _empty(root)


def test_spec_declares_changed_not_saved_badge(tmp_path):
    html = _html_for_rows(tmp_path)
    assert "CHANGED, NOT SAVED" in html
    assert re.findall(r"%%[A-Z_]*%%", html) == []


def test_assign_js_replaces_the_old_badge(tmp_path):
    html = _html_for_rows(tmp_path)
    assert "r._badges[TARGET] = { text: ASSIGNED_BADGE.text" in html
    spec = _review_engine.ReviewSpec(
        app_id="zz", columns=[_review_engine.Column("a", "A")], target_col="a", payload_var="zzP",
        picker_label="x", picker_placeholder="x", picker_items=[], status_options=[], status_label="s",
        default_sort="a")
    assert "CHANGED, NOT SAVED" not in _review_engine.build_html(spec, [])


def _mixed_rows(tmp_path):
    root = _root(tmp_path)
    _snip(root, "s-proposed-mappings.yaml", "ONE", ["a1"])
    (root / "itr" / "mappings" / "ONE.mapping.yaml").write_text(
        yaml.safe_dump([{"guid": "m1", "path": "Assets:M", "tag": "AL_CASH_BANK"}]), encoding="utf-8")
    rows, _ = _load(root, "ONE")
    return {r["guid"]: r for r in rows}


def test_loaded_rows_never_carry_the_unsaved_marker(tmp_path):
    for r in _mixed_rows(tmp_path).values():
        ui_mod._row_presentation(r, {})
        assert "CHANGED" not in r["_badges"][ui_mod.TARGET_COL]["text"]
        assert "changed" not in r["_tags"]


def test_unmapped_row_shows_unmapped_and_mapped_never_does(tmp_path):
    by = _mixed_rows(tmp_path)
    ui_mod._row_presentation(by["a1"], {})
    ui_mod._row_presentation(by["m1"], {})
    assert by["a1"]["_badges"][ui_mod.TARGET_COL]["text"] == "UNMAPPED"
    assert by["m1"]["_badges"][ui_mod.TARGET_COL]["text"] != "UNMAPPED"
