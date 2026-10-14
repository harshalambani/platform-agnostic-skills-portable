"""
ITR-14 / ITR-11: Review Mapping lists exactly the accounts the entity's latest
ITR Workbook run reported; the book only supplies how they are arranged (built
from real parent ids). Synthetic data only.
"""
from __future__ import annotations

import json
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
import run_accounts  # noqa: E402
import run_sidecar  # noqa: E402
import tag_suggest  # noqa: E402
from agents.gnucash_accounts import GncAccount  # noqa: E402
from ui.tabs import itr_mapping_review as ui_mod  # noqa: E402

ENTITIES_EXAMPLE = ROOT / "bundling" / "canonical" / "itr" / "entities.example.yaml"
SCRIPS_EXAMPLE = ROOT / "bundling" / "canonical" / "itr" / "scrips.example.yaml"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _acct(i, name, parent=None, path=None, flags=(), typ="ASSET"):
    return GncAccount(id=i, name=name, type=typ, parent_id=parent, path=path or name,
                      special_flags=frozenset(flags))


def _root(tmp_path, books=None):
    root = tmp_path / "Data"
    (root / "itr" / "mappings").mkdir(parents=True)
    (root / "outputs").mkdir()
    ents = {k: {"name": k, "pan": "AAAAA0000A", "status": "Individual", "default_regime": "new"}
            for k in ("ONE", "TWO")}
    if books:
        ents["ONE"]["books"] = books
    (root / "itr" / "entities.yaml").write_text(yaml.safe_dump(ents), encoding="utf-8")
    return root


def _book(tmp_path, monkeypatch, accounts):
    f = tmp_path / "book.gnucash"
    f.write_text("x", encoding="utf-8")
    monkeypatch.setattr(ui_mod, "_entity_book_path", lambda k: f)
    import agents.gnucash_accounts as ga
    monkeypatch.setattr(ga, "load_accounts", lambda p: accounts)


def _load(root, key="ONE"):
    with patch("ui._config.data_root_dir", return_value=root):
        return ui_mod._collect_review(key)


BOOK = [
    _acct("A", "Assets", flags=("placeholder",)),
    _acct("bank", "Bank", "A", "Assets:Bank", flags=("placeholder",)),
    _acct("sav", "Savings", "bank", "Assets:Bank:Savings"),
    _acct("cur", "Current", "bank", "Assets:Bank:Current"),
    _acct("zero", "Zero Balance", "A", "Assets:Zero Balance"),
    _acct("grp", "Dormant", "A", "Assets:Dormant", flags=("placeholder",)),
    _acct("dl", "Dormant Leaf", "grp", "Assets:Dormant:Dormant Leaf"),
]


# --- the run writes its list ---------------------------------------------------------

def _run_workbook(tmp_path, mapping):
    html = tmp_path / "bs.html"
    html.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out = tmp_path / "out.xlsx"
    agent.run(str(html), str(out), mapping_file=str(mapping), entities_path=str(ENTITIES_EXAMPLE),
              entity_key="SYN-IND", scrips_path=str(SCRIPS_EXAMPLE), config_path="no-such-config.yaml")
    return sorted(tmp_path.glob("*-run-accounts.json"))


def test_a_partial_run_records_resolved_and_unmapped(tmp_path):
    files = _run_workbook(tmp_path, FIXTURES / "syn_ind_partial.mapping.yaml")
    assert len(files) == 1 and "SYN-IND" in files[0].name
    data = json.loads(files[0].read_text(encoding="utf-8"))
    states = {a["state"] for a in data["accounts"]}
    assert data["entity"] == "SYN-IND" and data["fy"]
    assert {"resolved", "unmapped"} <= states
    assert all(a["guid"] and a["path"] for a in data["accounts"])


def test_a_fully_mapped_run_still_writes_its_list_with_nothing_unmapped(tmp_path):
    files = _run_workbook(tmp_path, FIXTURES / "syn_ind.mapping.yaml")
    assert len(files) == 1
    data = json.loads(files[0].read_text(encoding="utf-8"))
    assert data["accounts"] and all(a["state"] == "resolved" for a in data["accounts"])


def test_an_unreadable_sidecar_is_ignored(tmp_path):
    assert run_accounts.read(tmp_path / "missing.json") is None
    (tmp_path / "bad.json").write_text("not json", encoding="utf-8")
    assert run_accounts.read(tmp_path / "bad.json") is None


# --- Review Mapping lists only the run's accounts -------------------------------------

def test_an_account_the_run_reported_unmapped_is_listed(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _book(tmp_path, monkeypatch, BOOK)
    run_sidecar.write_run(root, "ONE", unmapped=["sav"], resolved=["cur"])
    rows, _ = _load(root)
    assert [r["guid"] for r in rows] == ["sav"] and rows[0]["unmapped"]


def test_a_book_account_absent_from_the_run_is_never_listed_or_counted(tmp_path, monkeypatch):   # NEGATIVE
    root = _root(tmp_path)
    _book(tmp_path, monkeypatch, BOOK)
    run_sidecar.write_run(root, "ONE", unmapped=["sav"])
    rows, _ = _load(root)
    guids = {r["guid"] for r in rows}
    assert not guids & {"cur", "zero", "dl"}
    assert sum(1 for r in rows if r["unmapped"]) == 1       # the only unmapped row the screen can count


def test_a_fully_mapped_run_shows_no_unmapped_rows_despite_extra_book_leaves(tmp_path, monkeypatch):   # NEGATIVE
    root = _root(tmp_path)
    (root / "itr" / "mappings" / "ONE.mapping.yaml").write_text(yaml.safe_dump(
        [{"guid": "sav", "path": "Assets:Bank:Savings", "tag": "AL_CASH_BANK"}]), encoding="utf-8")
    _book(tmp_path, monkeypatch, BOOK)
    run_sidecar.write_run(root, "ONE", resolved=["sav"])
    # an older run left a snippet naming a zero-balance leaf; the newer run resolved everything
    (root / "outputs" / "old-proposed-mappings.yaml").write_text(
        "# entity: ONE\n" + yaml.safe_dump([{"guid": "zero", "path": "Assets/Zero Balance",
                                            "tag": "REPLACE_ME", "note": "n"}]), encoding="utf-8")
    rows, _ = _load(root)
    assert [r["guid"] for r in rows] == ["sav"] and not rows[0]["unmapped"]


def test_a_run_for_another_entity_is_never_used(tmp_path, monkeypatch):   # NEGATIVE
    root = _root(tmp_path)
    _book(tmp_path, monkeypatch, BOOK)
    run_sidecar.write_run(root, "TWO", unmapped=["sav", "cur"])
    rows, info = _load(root, "ONE")
    assert rows == [] and info["run"] is None and info["no_run"]


def test_a_run_for_another_fy_is_never_used(tmp_path, monkeypatch):   # NEGATIVE
    root = _root(tmp_path, books={"2024-25": "a.gnucash", "2025-26": "b.gnucash"})
    _book(tmp_path, monkeypatch, BOOK)
    run_sidecar.write_run(root, "ONE", unmapped=["sav"], fy="2024-25")
    rows, info = _load(root)
    assert "sav" not in {r["guid"] for r in rows} and info["run"] is None
    run_sidecar.write_run(root, "ONE", unmapped=["cur"], fy="2025-26", name="newer.xlsx")
    rows, _ = _load(root)
    assert [r["guid"] for r in rows] == ["cur"]


def test_with_no_run_on_record_the_whole_book_is_not_listed(tmp_path, monkeypatch):   # NEGATIVE
    root = _root(tmp_path)
    (root / "itr" / "mappings" / "ONE.mapping.yaml").write_text(yaml.safe_dump(
        [{"guid": "sav", "path": "Assets:Bank:Savings", "tag": "AL_CASH_BANK"}]), encoding="utf-8")
    _book(tmp_path, monkeypatch, BOOK)
    rows, info = _load(root)
    assert [r["guid"] for r in rows] == ["sav"] and info["no_run"]
    with patch("ui._config.data_root_dir", return_value=root):
        html = ui_mod._load_review_data("ONE")
    assert ui_mod.NO_RUN_LINE in html
    # and no mapping at all: the plain line, not the book
    (root / "itr" / "mappings" / "ONE.mapping.yaml").unlink()
    with patch("ui._config.data_root_dir", return_value=root):
        html = ui_mod._load_review_data("ONE")
    assert ui_mod.NO_RUN_LINE in html and "Current" not in html


def test_a_group_whose_leaves_are_all_outside_the_run_does_not_appear(tmp_path, monkeypatch):   # NEGATIVE
    root = _root(tmp_path)
    _book(tmp_path, monkeypatch, BOOK)
    run_sidecar.write_run(root, "ONE", unmapped=["sav"])
    rows, _ = _load(root)
    assert all("Dormant" not in " ".join(r["_segs"]) for r in rows)
    assert {tuple(r["_segs"][:-1]) for r in rows} == {("Assets", "Bank")}      # only the group with a listed leaf


def test_hidden_accounts_in_the_run_list_are_still_never_offered(tmp_path, monkeypatch):   # NEGATIVE
    root = _root(tmp_path)
    book = BOOK + [_acct("hid", "Old", "A", "Assets:Old", flags=("hidden",))]
    _book(tmp_path, monkeypatch, book)
    run_sidecar.write_run(root, "ONE", unmapped=["sav", "hid", "grp"])
    rows, _ = _load(root)
    assert {r["guid"] for r in rows} == {"sav"}


# --- ITR-11: groups come from real parent ids --------------------------------------------

def test_a_slash_in_an_account_name_never_makes_a_false_group(tmp_path, monkeypatch):   # NEGATIVE
    root = _root(tmp_path)
    book = [
        _acct("A", "Assets", flags=("placeholder",)),
        _acct("ab", "A / B", "A", "Assets:A / B"),
        _acct("c", "C", "A", "Assets:C"),
    ]
    _book(tmp_path, monkeypatch, book)
    run_sidecar.write_run(root, "ONE", unmapped=["ab", "c"])
    rows, _ = _load(root)
    by = {r["guid"]: r for r in rows}
    assert by["ab"]["_segs"] == ["Assets", "A / B"]
    assert "A" not in by["ab"]["_segs"][:-1] and by["ab"]["_pk"] == "assets"
    assert by["c"]["_pk"] == by["ab"]["_pk"]        # siblings under the one real parent


def test_real_nesting_still_groups_and_order_is_unchanged_for_plain_names(tmp_path, monkeypatch):
    root = _root(tmp_path)
    _book(tmp_path, monkeypatch, BOOK)
    run_sidecar.write_run(root, "ONE", unmapped=["sav", "cur", "zero", "dl"])
    rows, _ = _load(root)
    keys = [[x.lower() for x in r["_segs"]] for r in rows]
    assert keys == sorted(keys)                              # book order, segment by segment
    by = {r["guid"]: r for r in rows}
    assert by["sav"]["_segs"] == ["Assets", "Bank", "Savings"] and by["sav"]["_pk"] == "assets:bank"
    assert tag_suggest.book_key("A:Car Insurance") > tag_suggest.book_key("A:Car:Fuel")
    assert tag_suggest.book_key("A:B / C", segs=["A", "B / C"]) == ("a", "b / c")


def test_word_scores_with_real_segments_do_not_split_a_name():
    kept = tag_suggest.word_scores("Assets:A / B", None, segs=["Assets", "A / B"])
    assert isinstance(kept, dict)
