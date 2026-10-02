"""
UI-06 -- editable Description in Review.

The reworded text REPLACES Description in the exported CSV, but the original
narration is kept (Original Description + Notes), so nothing is learned from or
matched on the reworded text, and a re-import still finds the row.

Fixtures are synthetic; account names use the real book's shape.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "src"))

import _review_js as js  # noqa: E402
from ui.tabs import gnucash_review as rv  # noqa: E402

_HEADER = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"
ORIG = "NEFT-AXISP0001234-ACME LANDLORD-RENT JUNE-013065XXXX"
BANK = "Root Account:Assets:Current Assets:Bank:013065XXXX"


def _row(desc=ORIG, edited=None, **kw):
    r = {"Date": "2025-06-01", "Description": desc, "Account": "Expenses:Rent",
         "Deposit": "", "Withdrawal": "1000.00", "Balance": "9000.00",
         "Confidence": "override", "MatchReason": "x"}
    r.update(kw)
    if edited is not None:
        r[rv.EDIT_KEY] = edited
    return r


def _save(tmp_path, rows, changes=None):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER, encoding="utf-8")
    payload = {"context": {"csv_path": str(p), "gnucash_file": str(p)},
               "changes": changes if changes is not None else [], "all_rows": rows}
    msg, _ = rv._save_changes(json.dumps(payload))
    with open(p, newline="", encoding="utf-8") as f:
        return msg, list(csv.DictReader(f)), p.read_bytes()


# ---- pure export helper ---------------------------------------------------

def test_edit_replaces_description_and_keeps_original_in_notes_and_orig():
    out, n = rv._apply_description_edits([_row(edited="Rent - June")])
    r = out[0]
    assert n == 1
    assert r["Description"] == "Rent - June"
    assert r[rv.ORIG_KEY] == ORIG
    assert r[rv.NOTES_KEY] == ORIG          # bank reference stays on the txn
    assert rv.EDIT_KEY not in r


@pytest.mark.parametrize("blank", ["", "   ", "\t \n", None])
def test_empty_or_whitespace_edit_exports_original_unchanged(blank):
    """NEGATIVE: a blank edit must not blank the Description or add Notes."""
    src = _row(edited=blank if blank is not None else "")
    out, n = rv._apply_description_edits([src])
    assert n == 0
    assert out[0]["Description"] == ORIG
    assert rv.NOTES_KEY not in out[0] and rv.ORIG_KEY not in out[0]


def test_edit_equal_to_original_is_not_an_edit():
    """NEGATIVE: retyping the same text (whitespace aside) adds no Notes."""
    out, n = rv._apply_description_edits([_row(edited="  " + ORIG.replace("-", "-") + " ")])
    assert n == 0 and rv.NOTES_KEY not in out[0]


def test_unedited_rows_are_untouched_and_input_not_mutated():
    src = [_row(), _row(desc="OTHER", edited="Renamed")]
    snap = json.loads(json.dumps(src))
    out, n = rv._apply_description_edits(src)
    assert src == snap, "helper must not mutate its input"
    assert out[0] == _row(), "an unedited row exports exactly as before"
    assert n == 1


def test_existing_notes_are_kept_and_not_duplicated():
    out, _ = rv._apply_description_edits([_row(edited="Rent", Notes="manual note")])
    assert "manual note" in out[0]["Notes"] and ORIG in out[0]["Notes"]
    again, _ = rv._apply_description_edits([dict(out[0], **{rv.EDIT_KEY: "Rent"})])
    assert again[0]["Notes"].count(ORIG) == 1, "idempotent: original not appended twice"


# ---- save / CSV -----------------------------------------------------------

def test_unedited_export_is_byte_identical_to_pre_ui06(tmp_path):
    rows = [_row(), _row(desc="OTHER")]
    # what the pre-UI-06 writer produced for the same rows
    import io
    from agents.canonical_io import order_import_ready_headers
    buf = io.StringIO(newline="")
    w = csv.DictWriter(buf, fieldnames=order_import_ready_headers(rows[0].keys()),
                       extrasaction="ignore")
    w.writeheader()
    w.writerows(rows)
    change = [{"_idx": 0, "Description": ORIG, "Account": "Expenses:Rent", "_orig": "Y"}]
    _msg, _rows, raw = _save(tmp_path, rows, change)
    assert raw == buf.getvalue().encode("utf-8")


def test_edited_export_writes_reworded_text_and_notes(tmp_path):
    msg, rows, _ = _save(tmp_path, [_row(edited="Rent - June")])
    assert "Saved" in msg
    assert rows[0]["Description"] == "Rent - June"
    assert rows[0]["Notes"] == ORIG and rows[0]["Original Description"] == ORIG
    assert rv.EDIT_KEY not in rows[0]


def test_edit_only_save_proceeds_without_account_changes(tmp_path):
    """NEGATIVE: an edit with no account change must not be dropped as
    'No changes to save'."""
    msg, rows, _ = _save(tmp_path, [_row(edited="Rent - June")], changes=[])
    assert "No changes" not in msg
    assert rows[0]["Description"] == "Rent - June"


def test_no_changes_and_no_edits_still_saves_nothing(tmp_path):
    """NEGATIVE: unchanged behaviour when nothing was done."""
    msg, rows, _ = _save(tmp_path, [_row()], changes=[])
    assert "No changes" in msg and rows == []


def test_notes_column_present_when_only_a_later_row_is_edited(tmp_path):
    """NEGATIVE: headers come from every row, so Notes is not lost when only
    the second row carries it."""
    _m, rows, _ = _save(tmp_path, [_row(desc="A"), _row(desc="B", edited="Bee")])
    assert rows[1]["Notes"] == "B" and rows[0].get("Notes", "") == ""


def test_override_is_keyed_on_original_narration(tmp_path, monkeypatch):
    """NEGATIVE: the saved override pattern comes from the ORIGINAL text, never
    the reworded one."""
    seen = []
    import agents.skill_gnucash_account_mapper.persistent_rules as pr
    monkeypatch.setattr(pr, "load_overrides", lambda *a, **k: [])
    monkeypatch.setattr(pr, "save_overrides_batch",
                        lambda gf, allo, config_path=None: seen.extend(allo))
    monkeypatch.setattr(pr, "rules_path", lambda *a, **k: "x")
    change = [{"_idx": 0, "Description": ORIG, "Account": "Expenses:Rent", "_orig": "Y"}]
    _save(tmp_path, [_row(edited="Rent - June")], change)
    assert seen, "override should have been saved"
    joined = " ".join(o["pattern"] for o in seen).lower()
    assert "june" in joined and "rent" in joined  # original text carries these too
    assert r"rent\ \-\ june" not in joined and "rent - june" not in joined
    assert "acme" in joined and "landlord" in joined


# ---- reload / re-import ---------------------------------------------------

def test_reload_restores_original_for_learning_and_shows_edit():
    exported, _ = rv._apply_description_edits([_row(edited="Rent - June")])
    row = dict(exported[0])
    rv._restore_description_edit(row)
    assert row["Description"] == ORIG, "learning key is the original again"
    assert row[rv.EDIT_KEY] == "Rent - June"
    assert row[rv.NOTES_KEY] == "" and rv.ORIG_KEY not in row
    again, n = rv._apply_description_edits([row])
    assert n == 1 and again[0]["Description"] == "Rent - June"
    assert again[0][rv.NOTES_KEY] == ORIG


def test_restore_is_a_noop_for_rows_never_edited():
    """NEGATIVE: a normal row (no Original Description) is not modified."""
    row = _row(Notes="keep me")
    before = dict(row)
    rv._restore_description_edit(row)
    assert row == before


def test_reimport_after_edited_export_is_a_detected_duplicate():
    """NEGATIVE (double-booking guard): a transaction already booked under the
    REWORDED description is still detected when the same statement line comes
    back with its ORIGINAL narration -- the duplicate check keys on date +
    amount (scoped to the bank account), never on the description."""
    from agents.skill_gnucash_reconciler.agent import reconcile
    booked = {"transactions": [
        {"date": "2025-06-01", "amount": -1000.0, "account": BANK,
         "description": "Rent - June"}]}
    stmt = [{"row_num": 1, "date": "2025-06-01", "deposit": 0.0,
             "withdrawal": 1000.0, "description": ORIG}]
    report, summary = reconcile(stmt, booked)
    assert report[0]["status"] != "New", "reworded booking must not look new"
    assert summary["new"] == 0
    # and the mirror image: reworded line vs original booking
    booked["transactions"][0]["description"] = ORIG
    stmt[0]["description"] = "Rent - June"
    _r, s2 = reconcile(stmt, booked)
    assert s2["new"] == 0


# ---- client (node) --------------------------------------------------------

def _html(tmp_path, csv_rows):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + csv_rows, encoding="utf-8")
    return rv._load_review_data(str(p), str(p))


_JS_EDIT = """
  const tr = $id(APP + '-tbody').children[0];
  const di = tr.children.findIndex(td => td.innerHTML.indexOf(%s) >= 0);
  // UI-06: a real double-click (click, click, dblclick on the node under the pointer),
  // not a direct ondblclick() call -- that call is what hid the real-browser bug.
  realDblClick(APP, 0, 'Description');
  const inp = tr.children[di].children[0];
  inp.value = %s;
  inp.onkeydown({key: 'Enter', preventDefault(){}});
"""


@pytest.mark.skipif(not js.have_node(), reason="node not installed")
def test_double_click_edit_keeps_original_and_marks_cell(tmp_path):
    html = _html(tmp_path, f"2025-06-01,{ORIG},Expenses:Rent,,1000.00,9000.00,override,x\n")
    out = js.run_js(html, "rv",
                    _JS_EDIT % (json.dumps(ORIG[:12]), json.dumps("Rent - June"))
                    + "return {cells: view(APP)[0].cells, titles: $id(APP + '-tbody').children[0].children.map(t => t.title)};", tmp_path)
    joined = " ".join(out["cells"]) + " " + " ".join(out["titles"])
    assert "Rent - June" in joined
    assert "Edited. Original:" in joined


@pytest.mark.skipif(not js.have_node(), reason="node not installed")
def test_blank_edit_in_ui_clears_it(tmp_path):
    """NEGATIVE: committing a blank value leaves the original, unmarked."""
    html = _html(tmp_path, f"2025-06-01,{ORIG},Expenses:Rent,,1000.00,9000.00,override,x\n")
    out = js.run_js(html, "rv",
                    _JS_EDIT % (json.dumps(ORIG[:12]), json.dumps("   "))
                    + "return {cells: view(APP)[0].cells, titles: $id(APP + '-tbody').children[0].children.map(t => t.title)};", tmp_path)
    joined = " ".join(out["cells"]) + " " + " ".join(out["titles"])
    assert "Edited. Original:" not in joined and ORIG[:12] in joined


@pytest.mark.skipif(not js.have_node(), reason="node not installed")
def test_edit_does_not_change_what_apply_to_matching_uses(tmp_path):
    """NEGATIVE: two rows share one narration; editing one row's text must not
    stop 'apply to matching' from matching both on the ORIGINAL."""
    html = _html(tmp_path,
                 f"2025-06-01,{ORIG},Liabilities:Suspense,,1.00,9.00,suspense,x\n"
                 f"2025-07-01,{ORIG},Liabilities:Suspense,,1.00,8.00,suspense,x\n")
    out = js.run_js(html, "rv",
                    _JS_EDIT % (json.dumps(ORIG[:12]), json.dumps("Rent - June"))
                    + """
      select(APP, 0); pick(APP, 0);
      $id(APP + '-apply-match').onclick();
      return view(APP).map(r => r.cls.join(' '));
    """, tmp_path)
    assert all("accent-blue" in c for c in out), out


def test_description_column_declares_edit_key():
    spec = rv._spec([], "c", "g", "Deposit", "Withdrawal")
    col = next(c for c in spec.columns if c.key == "Description")
    assert col.edit_key == rv.EDIT_KEY
    others = [c.key for c in spec.columns if c.edit_key]
    assert others == ["Description"], "only Description is editable"
