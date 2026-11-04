"""
UI-12 -- the Review tab no longer loses unsaved work silently, and its save
message tells the truth.

(a) Load / Reset ask before discarding unsaved overrides, exclusions or
    description edits (in-page confirm, second click; never window.confirm).
(b) _save_changes reports received / learned / not learned, by reason.
(c) a same-pattern, different-account correction UPDATES the rule; a repeat
    is not duplicated; a multi-pattern rule has the pattern split off.
(d) "original kept in Notes" is only claimed when there was an original.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _review_js as js  # noqa: E402
from ui.tabs import gnucash_review as rv  # noqa: E402

needs_node = pytest.mark.skipif(not js.have_node(), reason="node not installed")

DIRTY = "_rvSavePayloadDirty"
_HEADER = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"
_ROWS = (
    "2025-05-01,UNKNOWN PAYEE ONE,Liabilities:Suspense,,100.00,900.00,suspense,no match\n"
    "2025-05-02,COFFEE SHOP,Expense:Food,,20.00,880.00,high,rule\n"
)


def _html(tmp_path):
    p = tmp_path / "GnuCash_import_ready.csv"
    p.write_text(_HEADER + _ROWS, encoding="utf-8")
    return rv._load_review_data(str(p), str(p))


def _run(tmp_path, scenario, pre=""):
    return js.run_js(_html(tmp_path), "rv", scenario, tmp_path, pre=pre)


# ---- (a) dirty flag in the engine ------------------------------------------------

@needs_node
def test_dirty_flag_is_not_set_by_init_alone(tmp_path):
    """NEGATIVE: syncPayload() runs once at init; that must not mark the page dirty."""
    out = _run(tmp_path, "return {d: globalThis.%s};" % DIRTY)
    assert out["d"] is False


@needs_node
def test_assign_marks_dirty(tmp_path):
    out = _run(tmp_path, """
        const before = globalThis.%s;
        select(APP, 0); pick(APP, 0); applySel(APP);
        return {before, after: globalThis.%s};
    """ % (DIRTY, DIRTY))
    assert out == {"before": False, "after": True}


@needs_node
def test_selecting_rows_alone_does_not_mark_dirty(tmp_path):
    """NEGATIVE: browsing (select / sort / filter) is not unsaved work."""
    out = _run(tmp_path, """
        select(APP, 0); select(APP, 1);
        return {d: globalThis.%s};
    """ % DIRTY)
    assert out["d"] is False


@needs_node
def test_description_edit_marks_dirty_but_a_noop_edit_does_not(tmp_path):
    out = _run(tmp_path, """
        const col = colIdx(APP, 'Description');
        let tr = $id(APP + '-tbody').children[0];
        realDblClick(APP, 0, 'Description');
        let inp = tr.children[col].children[0];
        inp.value = inp.value;
        inp.onkeydown({key: 'Enter', preventDefault(){}});
        const afterNoop = globalThis.%s;
        tr = $id(APP + '-tbody').children[0];
        realDblClick(APP, 0, 'Description');
        inp = tr.children[col].children[0];
        inp.value = 'Reworded narration';
        inp.onkeydown({key: 'Enter', preventDefault(){}});
        return {afterNoop, afterEdit: globalThis.%s};
    """ % (DIRTY, DIRTY))
    assert out == {"afterNoop": False, "afterEdit": True}


@needs_node
def test_exclusion_toggle_marks_dirty(tmp_path):
    out = _run(tmp_path, """
        select(APP, 0);
        $id(APP + '-exclude-sel').onclick();
        return {d: globalThis.%s, p: payload('_rvSavePayload').excluded_dirty};
    """ % DIRTY)
    assert out == {"d": True, "p": True}


@needs_node
def test_beforeunload_guard_registered_and_only_blocks_when_dirty(tmp_path):
    pre = """
      const _wl = {};
      globalThis.addEventListener = (t, f) => { (_wl[t] = _wl[t] || []).push(f); };
    """
    out = _run(tmp_path, """
        const fire = () => { const ev = {prevented: false, preventDefault() { this.prevented = true; }};
                             for (const f of (_wl.beforeunload || [])) f(ev); return ev.prevented; };
        const clean = fire();
        select(APP, 0); pick(APP, 0); applySel(APP);
        const dirty = fire();
        globalThis.%s = false;
        return {n: (_wl.beforeunload || []).length, clean, dirty, afterSave: fire()};
    """ % DIRTY, pre=pre)
    assert out == {"n": 1, "clean": False, "dirty": True, "afterSave": False}


# ---- (a) Load / Reset guard ------------------------------------------------------

def test_guard_js_never_calls_window_confirm():
    for action in ("load", "reset"):
        assert "confirm(" not in rv._guard_js(action)


def _guard(action, dirty, armed=""):
    """Run the page-side guard js in node; returns {r: return value, d, a}."""
    fn = rv._guard_js(action)
    src = (
        "globalThis.window = globalThis; window.%s = %s; window.%s = %s; window.%s = 'x';"
        "const f = %s; const r = f('c', 'g', '');"
        "console.log(JSON.stringify({r, d: window.%s, a: window.%s}));"
    ) % (rv.DIRTY_VAR, str(dirty).lower(), rv._ARM_VAR, json.dumps(armed),
         rv.PAYLOAD_VAR, fn, rv.DIRTY_VAR, rv._ARM_VAR)
    out = subprocess.run([js.NODE, "-e", src], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@needs_node
def test_load_with_no_unsaved_changes_does_not_prompt():
    """NEGATIVE: a clean page loads on the first click."""
    r = _guard("load", dirty=False)
    assert r["r"][2] == "go" and r["a"] == ""


@needs_node
def test_load_with_unsaved_changes_prompts_first_then_goes_on_second_click():
    first = _guard("load", dirty=True)
    assert first["r"][2] == "confirm" and first["a"] == "load" and first["d"] is True
    second = _guard("load", dirty=True, armed="load")
    assert second["r"][2] == "go" and second["d"] is False


@needs_node
def test_reset_prompts_when_dirty_and_clears_on_confirm():
    assert _guard("reset", dirty=True)["r"] == "confirm"
    assert _guard("reset", dirty=True, armed="reset")["r"] == "go"
    assert _guard("reset", dirty=False)["r"] == "go"


@needs_node
def test_arming_load_does_not_arm_reset():
    """NEGATIVE: a pending Load confirmation is not a Reset confirmation."""
    assert _guard("reset", dirty=True, armed="load")["r"] == "confirm"


def test_load_guarded_confirm_loads_nothing(monkeypatch):
    called = []
    monkeypatch.setattr(rv, "_load_review_data", lambda *a: called.append(a) or "HTML")
    out = rv._load_guarded("c.csv", "b.gnucash", "confirm")
    assert called == [] and "Unsaved changes" in out[1]


def test_load_guarded_go_and_clean_load_as_before(monkeypatch):
    monkeypatch.setattr(rv, "_load_review_data", lambda *a: "HTML")
    for mode in ("go", ""):
        out = rv._load_guarded("c.csv", "b.gnucash", mode)
        assert out[0] == "HTML" and out[1] == ""


# ---- (b)/(c) the learn accounting --------------------------------------------------

def _ch(desc, account):
    return {"Description": desc, "Account": account}


def test_learn_new_rule_counts_as_learned():
    rules, st = rv._learn_overrides([_ch("ACME RENT", "Expense:Rent")], [])
    assert st["learned"] == 1 and st["received"] == 1
    assert rules[0]["account"] == "Expense:Rent"


def test_same_pattern_same_account_is_not_duplicated():
    existing = [{"patterns": [rv._generalize_pattern("ACME RENT")], "account": "Expense:Rent"}]
    rules, st = rv._learn_overrides([_ch("ACME RENT", "Expense:Rent")], existing)
    assert len(rules) == 1 and st["same"] == 1 and st["learned"] == 0 and st["updated"] == 0


def test_same_pattern_different_account_updates_the_rule():
    pat = rv._generalize_pattern("ACME RENT")
    existing = [{"patterns": [pat], "account": "Expense:Old"}]
    rules, st = rv._learn_overrides([_ch("ACME RENT", "Expense:Rent")], existing)
    assert len(rules) == 1 and rules[0]["account"] == "Expense:Rent"
    assert st["updated"] == 1 and st["learned"] == 0
    assert existing[0]["account"] == "Expense:Old"       # input not mutated


def test_multi_pattern_rule_has_only_the_pattern_split_off():
    pat = rv._generalize_pattern("ACME RENT")
    existing = [{"patterns": [pat, "OTHER"], "account": "Expense:Old"}]
    rules, st = rv._learn_overrides([_ch("ACME RENT", "Expense:Rent")], existing)
    by_acct = {r["account"]: r["patterns"] for r in rules}
    assert by_acct["Expense:Old"] == ["OTHER"]            # untouched sibling
    assert by_acct["Expense:Rent"] == [pat]
    assert st["updated"] == 1


def test_suspense_empty_narration_and_no_account_are_never_learned():
    rules, st = rv._learn_overrides(
        [_ch("X", "Liabilities:Suspense:Uncleared"), _ch("", "Expense:A"), _ch("Y", "")], [])
    assert rules == [] and st["suspense"] == 1 and st["empty"] == 2 and st["learned"] == 0


def test_accounting_line_names_every_reason():
    _, st = rv._learn_overrides(
        [_ch("X", "Liabilities:Suspense"), _ch("", "Expense:A")], [])
    line = rv._override_accounting(st)
    assert "received: 2" in line and "learned: 0" in line and "not learned: 2" in line
    assert "Suspense" in line and "never learned, by design" in line and "empty narration" in line


# ---- _save_changes end to end ------------------------------------------------------

def _payload(tmp_path, changes, rows=None, **extra):
    csv_p = tmp_path / "GnuCash_import_ready.csv"
    csv_p.write_text(_HEADER + _ROWS, encoding="utf-8")
    p = {"context": {"csv_path": str(csv_p), "gnucash_file": str(tmp_path / "bk.gnucash")},
         "changes": changes,
         "all_rows": rows or [{"Date": "2025-05-01", "Description": "ACME RENT 1",
                               "Account": "Expense:Rent", "Transfer Account": "", "Deposit": "",
                               "Withdrawal": "5.00", "Balance": "1.00",
                               "Confidence": "override", "MatchReason": "x"}]}
    p.update(extra)
    return json.dumps(p), csv_p


def _change(desc, account, orig="Liabilities:Suspense"):
    return {"_idx": 0, "_orig": orig, "_deleted": False, "Date": "2025-05-01",
            "Description": desc, "Account": account, "Transfer Account": "",
            "Deposit": "", "Withdrawal": "5.00", "Balance": "1.00",
            "Confidence": "override", "MatchReason": "x"}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.setattr(rv._config_mod, "PORTABLE_CONFIG_PATH", tmp_path / "settings" / "config.yaml")
    return str(tmp_path / "settings" / "config.yaml")


def _rules(tmp_path, cfg):
    from agents.skill_gnucash_account_mapper.persistent_rules import load_overrides
    return load_overrides(str(tmp_path / "bk.gnucash"), config_path=cfg)


def test_save_reports_received_learned_and_wording_kept(tmp_path, cfg):
    payload, _ = _payload(tmp_path, [_change("ACME RENT 1", "Expense:Rent")])
    status, _ = rv._save_changes(payload)
    assert "Saved 1 new override" in status
    assert "Account changes received: 1; learned: 1" in status
    assert "No new overrides needed" not in status


def test_suspense_only_save_writes_no_rules_file(tmp_path, cfg):
    """NEGATIVE: nothing learned -> no rules file on disk."""
    from agents.skill_gnucash_account_mapper.persistent_rules import rules_path
    payload, _ = _payload(tmp_path, [_change("ACME RENT 1", "Liabilities:Suspense:X")])
    status, _ = rv._save_changes(payload)
    assert not rules_path(str(tmp_path / "bk.gnucash"), cfg).exists()
    assert "No override rules were written" in status and "Saved 0 new" not in status


def test_same_account_repeat_save_adds_no_duplicate(tmp_path, cfg):
    payload, _ = _payload(tmp_path, [_change("ACME RENT 1", "Expense:Rent")])
    rv._save_changes(payload)
    status, _ = rv._save_changes(payload)
    assert len(_rules(tmp_path, cfg)) == 1
    assert "already saved to the same account" in status


def test_correction_to_a_different_account_updates_not_duplicates(tmp_path, cfg):
    rv._save_changes(_payload(tmp_path, [_change("ACME RENT 1", "Expense:Old")])[0])
    status, _ = rv._save_changes(_payload(tmp_path, [_change("ACME RENT 1", "Expense:Rent")])[0])
    rules = _rules(tmp_path, cfg)
    assert len(rules) == 1 and rules[0]["account"] == "Expense:Rent"
    assert "updated to a different account" in status


# ---- (d) the Notes claim -----------------------------------------------------------

def _edited_row(desc, edit):
    return {"Date": "2025-05-01", "Description": desc, "Edited Description": edit,
            "Account": "Expense:Rent", "Transfer Account": "", "Deposit": "",
            "Withdrawal": "5.00", "Balance": "1.00", "Confidence": "high", "MatchReason": "x"}


def test_notes_claim_made_only_when_an_original_exists(tmp_path, cfg):
    payload, _ = _payload(tmp_path, [], rows=[_edited_row("ORIGINAL TEXT", "Reworded")])
    status, _ = rv._save_changes(payload)
    assert "1 description(s) reworded" in status and "original kept in Notes for 1" in status


def test_empty_original_does_not_claim_kept_in_notes(tmp_path, cfg):
    """NEGATIVE: nothing was kept, so the message must not say it was."""
    payload, _ = _payload(tmp_path, [], rows=[_edited_row("", "Reworded")])
    status, _ = rv._save_changes(payload)
    assert "1 description(s) reworded" in status
    assert "original kept in Notes" not in status
    assert "no original description to keep" in status


# ---- unchanged behaviour -----------------------------------------------------------

def test_ui06_export_unchanged_for_a_normal_edit(tmp_path, cfg):
    import csv
    payload, csv_p = _payload(tmp_path, [], rows=[_edited_row("ORIGINAL TEXT", "Reworded")])
    rv._save_changes(payload)
    row = list(csv.DictReader(open(csv_p, encoding="utf-8")))[0]
    assert row["Description"] == "Reworded" and row["Notes"] == "ORIGINAL TEXT"
    assert row["Original Description"] == "ORIGINAL TEXT"


def test_imp11_exclusion_still_written_to_sidecar(tmp_path, cfg):
    booked = {"Date": "2025-05-09", "Description": "BOOKED", "Account": "Assets:X",
              "Withdrawal": "1.00", "Deposit": "", "Balance": "", "Transfer Account": ""}
    payload, csv_p = _payload(tmp_path, [], excluded=[booked], excluded_dirty=True)
    status, _ = rv._save_changes(payload)
    assert "1 row(s) left out of the import" in status
    assert csv_p.with_suffix(".matched.json").exists()
