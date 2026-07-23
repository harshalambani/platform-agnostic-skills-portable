"""
tests/test_gnucash_review.py — regression guards for the migrated Review &
Edit Account Mappings tab (ui/tabs/gnucash_review.py), now built on the
shared ui._review_engine.

Covers what test_contra_review.py doesn't:
  - the full render path (a render-path test is required precisely because a
    prior migration passed 17 unit tests while the screen crashed with a
    NameError the moment anyone clicked Load — nothing exercised the render
    path);
  - hostile bank-description content never reaching the DOM as live markup;
  - _save_changes reading the engine's NEW payload shape (built the way the
    engine's syncPayload() actually builds it, not the old bespoke shape);
  - the Suspense-account override-skip rule;
  - contra row _tags/_badges/_rowclass.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from ui.tabs import gnucash_review as gr_review  # noqa: E402


_HEADER = "Date,Description,Account,Deposit,Withdrawal,Balance,Confidence,MatchReason\n"


def _write_csv(tmp_path: Path, rows: list[str], name: str = "GnuCash_import_ready.csv") -> Path:
    csv_p = tmp_path / name
    csv_p.write_text(_HEADER + "".join(rows), encoding="utf-8")
    return csv_p


# ---------------------------------------------------------------------------
# Render path
# ---------------------------------------------------------------------------

def test_load_review_data_render_path_has_no_residual_tokens(tmp_path):
    csv_p = _write_csv(tmp_path, [
        "2025-05-01,COFFEE,Expense:Food,,100.00,900.00,high,rule\n",
    ])
    html = gr_review._load_review_data(str(csv_p), str(csv_p))
    assert re.findall(r"%%[A-Z_]*%%", html) == []
    assert "<table" in html


def test_load_review_data_hostile_description_not_live_markup(tmp_path):
    csv_p = _write_csv(tmp_path, [
        "2025-05-01,<img src=x onerror=alert(1)>,Expense:Food,,100.00,900.00,high,rule\n",
    ])
    html = gr_review._load_review_data(str(csv_p), str(csv_p))
    assert "<img src=x onerror=alert(1)>" not in html


def test_load_review_data_missing_both_inputs_is_safe():
    html = gr_review._load_review_data("", "")
    assert "Select both" in html


def test_load_review_data_falls_back_to_row_accounts_when_no_book(tmp_path):
    # Same trick as test_contra_review.py: pass the CSV itself as the
    # "gnucash" arg so the file-exists guard passes but account-tree parsing
    # fails gracefully and falls back to the accounts already in the rows.
    csv_p = _write_csv(tmp_path, [
        "2025-05-01,COFFEE,Expense:Food,,100.00,900.00,high,rule\n",
    ])
    html = gr_review._load_review_data(str(csv_p), str(csv_p))
    assert "Expense:Food" in html


# ---------------------------------------------------------------------------
# _row_presentation — tags / badges / rowclass
# ---------------------------------------------------------------------------

def test_row_presentation_confirmed_contra_gets_green_tone_and_transfer_badge():
    row = {"Confidence": "smart"}
    gr_review._row_presentation(row, {"status": "confirmed", "confidence": "high", "reason": "matched ref X"})
    assert "contra" in row["_tags"]
    assert row["_rowclass"] == "tone-green"
    assert row["_badges"]["Date"]["text"] == "TRANSFER"
    assert row["_note"] == "matched ref X"


def test_row_presentation_possible_contra_gets_amber_tone_and_possible_badge():
    row = {"Confidence": "smart"}
    gr_review._row_presentation(row, {"confidence": "medium", "reason": "amount+date hint"})
    assert "contra" in row["_tags"]
    assert row["_rowclass"] == "tone-amber"
    assert row["_badges"]["Date"]["text"] == "POSSIBLE"


def test_row_presentation_suspense_confidence_gets_suspense_badge_on_account():
    row = {"Confidence": "suspense"}
    gr_review._row_presentation(row, None)
    assert row["_tags"] == ["suspense"]
    assert row["_badges"]["Account"]["text"] == "SUSPENSE"
    assert row["_badges"]["Account"]["cls"] == "red"


def test_row_presentation_non_contra_row_has_empty_rowclass_and_no_badges():
    row = {"Confidence": "high"}
    gr_review._row_presentation(row, None)
    assert row["_rowclass"] == ""
    assert row["_tags"] == ["high"]
    assert "_badges" not in row


def test_row_presentation_blank_confidence_tags_as_none():
    row = {}
    gr_review._row_presentation(row, None)
    assert row["_tags"] == ["none"]


# ---------------------------------------------------------------------------
# _save_changes — the NEW engine payload shape (built the way syncPayload()
# in ui/_review_engine.py actually builds it: {_idx, _orig, <Column keys>}).
# ---------------------------------------------------------------------------

def _engine_change(orig_account: str, **overrides) -> dict:
    """Build a change dict shaped exactly like the engine's syncPayload()."""
    ch = {
        "_idx": 0, "_orig": orig_account,
        "Date": "2025-05-01", "Description": "ACME RENT 123456",
        "Account": orig_account, "Transfer Account": "",
        "Deposit": "", "Withdrawal": "5000.00", "Balance": "10000.00",
        "Confidence": "override", "MatchReason": "User override (review)",
    }
    ch.update(overrides)
    return ch


def _fake_config_path(tmp_path: Path) -> Path:
    # Any path under tmp_path is fine here — persistent_rules.rules_path()
    # only reads its .parent as a fallback settings dir. Not on disk yet.
    return tmp_path / "settings" / "config.yaml"


def test_save_changes_reads_new_engine_payload_shape_and_saves_override(tmp_path, monkeypatch):
    csv_p = _write_csv(tmp_path, [
        "2025-05-01,ACME RENT 123456,Liabilities:Suspense,,5000.00,10000.00,suspense,Suspense — review\n",
    ])
    monkeypatch.setattr(gr_review._config_mod, "PORTABLE_CONFIG_PATH", _fake_config_path(tmp_path))

    gnucash_file = str(tmp_path / "book.gnucash")
    payload = json.dumps({
        "context": {"csv_path": str(csv_p), "gnucash_file": gnucash_file},
        "changes": [_engine_change("Liabilities:Suspense", Account="Expense:Rent")],
        "all_rows": [{
            "Date": "2025-05-01", "Description": "ACME RENT 123456", "Account": "Expense:Rent",
            "Transfer Account": "", "Deposit": "", "Withdrawal": "5000.00",
            "Balance": "10000.00", "Confidence": "override",
            "MatchReason": "User override (review)",
        }],
    })

    status, download_update = gr_review._save_changes(payload)

    assert "Saved" in status
    assert "Saved 1 new override" in status

    # Re-exported CSV picked up the new Account value.
    saved_rows = list(csv.DictReader(open(csv_p, encoding="utf-8")))
    assert saved_rows[0]["Account"] == "Expense:Rent"

    # Override was actually persisted, with the ref number generalized away
    # (not saved verbatim) — _generalize_pattern runs at save time.
    from agents.skill_gnucash_account_mapper.persistent_rules import load_overrides
    overrides = load_overrides(gnucash_file, config_path=str(gr_review._config_mod.PORTABLE_CONFIG_PATH))
    assert len(overrides) == 1
    assert overrides[0]["account"] == "Expense:Rent"
    saved_pattern = overrides[0]["patterns"][0]
    assert "123456" not in saved_pattern


def test_save_changes_never_persists_suspense_account_as_override(tmp_path, monkeypatch):
    csv_p = _write_csv(tmp_path, [
        "2025-05-01,WEIRD TXN,Liabilities:Suspense,,10.00,10.00,suspense,Suspense\n",
    ])
    monkeypatch.setattr(gr_review._config_mod, "PORTABLE_CONFIG_PATH", _fake_config_path(tmp_path))

    gnucash_file = str(tmp_path / "book2.gnucash")
    payload = json.dumps({
        "context": {"csv_path": str(csv_p), "gnucash_file": gnucash_file},
        "changes": [_engine_change("Liabilities:Suspense", Account="Liabilities:Suspense:Uncleared",
                                    Description="WEIRD TXN")],
        "all_rows": [{
            "Date": "2025-05-01", "Description": "WEIRD TXN",
            "Account": "Liabilities:Suspense:Uncleared",
            "Transfer Account": "", "Deposit": "", "Withdrawal": "10.00",
            "Balance": "10.00", "Confidence": "override",
            "MatchReason": "User override (review)",
        }],
    })

    status, download_update = gr_review._save_changes(payload)

    assert "No new overrides needed" in status

    from agents.skill_gnucash_account_mapper.persistent_rules import load_overrides
    overrides = load_overrides(gnucash_file, config_path=str(gr_review._config_mod.PORTABLE_CONFIG_PATH))
    assert overrides == []


def test_save_changes_no_changes_is_a_noop():
    status, download_update = gr_review._save_changes("")
    assert "No changes to save" in status


def test_save_changes_malformed_json_reports_error():
    status, download_update = gr_review._save_changes("{not valid json")
    assert "Error parsing changes" in status
