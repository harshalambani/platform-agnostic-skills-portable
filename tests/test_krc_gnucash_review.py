"""
tests/test_krc_gnucash_review.py — regression guards for the new KRChoksey
GnuCash Import Review tab (ui/tabs/krc_gnucash_review.py), built on the
shared ui._review_engine.

The critical behavioural requirement this file exists to prove: only
account_mapping rows (Reason: "no security account match ...") may be edited
from this screen. data_value rows ("no net amount" / "sale quantity could
not be read ...") and judgment rows ("insufficient FIFO lots ...") must be
LOCKED, and an attempted edit to one must be REJECTED server-side at save
time — not merely hidden/greyed in the client. All data here is synthetic —
no content from the gitignored Data/ directory appears in this file.
"""
from __future__ import annotations

import csv
import json
import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ui"))

from ui.tabs import krc_gnucash_review as m  # noqa: E402


_HEADER = "CN No,Type,Security,Net,Reason\n"


def _write_review_csv(tmp_path: Path, rows: list[str], name: str = "Review.csv") -> Path:
    p = tmp_path / name
    p.write_text(_HEADER + "".join(rows), encoding="utf-8")
    return p


# A row of each of the four real Reason strings the build script emits.
ROW_ACCOUNT_MAPPING = 'CN1,Sale,SYNCORP LTD,1000.00,"no security account match (fuzzy:0.30) for \'SYNCORP LTD\'"\n'
ROW_DATA_VALUE_NET = 'CN2,Sale,SYNBANK LTD,,"no net amount"\n'
ROW_DATA_VALUE_QTY = 'CN3,Sale,SYNBANK LTD,500.00,"sale quantity could not be read from the contract note for SYNBANK LTD (CN CN3) — cannot book a FIFO sale; re-run Reconcile or fix the Quantity in the Bills workbook"\n'
ROW_JUDGMENT = 'CN4,Sale,SYNCORP LTD,2000.00,"insufficient FIFO lots for sale of SYNCORP LTD (need 100.0, have 40.0)"\n'
ROW_UNKNOWN_REASON = 'CN5,Sale,SYNCORP LTD,300.00,"some brand new reason nobody classified yet"\n'


# ---------------------------------------------------------------------------
# _classify_reason
# ---------------------------------------------------------------------------

def test_classify_no_security_account_match_is_account_mapping():
    assert m._classify_reason("no security account match (fuzzy:0.30) for 'X'") == "account_mapping"


def test_classify_no_net_amount_is_data_value():
    assert m._classify_reason("no net amount") == "data_value"


def test_classify_sale_quantity_unreadable_is_data_value():
    assert m._classify_reason(
        "sale quantity could not be read from the contract note for X (CN 1)"
    ) == "data_value"


def test_classify_insufficient_fifo_lots_is_judgment():
    assert m._classify_reason("insufficient FIFO lots for sale of X (need 1, have 0)") == "judgment"


def test_classify_unknown_reason_defaults_to_locked_judgment():
    """Honesty requirement: an unrecognised reason must never default to
    editable — the safe default is locked."""
    assert m._classify_reason("something nobody has seen before") == "judgment"


def test_classify_blank_reason_defaults_to_locked_judgment():
    assert m._classify_reason("") == "judgment"


# ---------------------------------------------------------------------------
# Render path — mandatory: must actually build the HTML, not just call
# helper functions in isolation.
# ---------------------------------------------------------------------------

def test_load_review_data_render_path_has_no_residual_tokens_and_shows_synthetic_data(tmp_path):
    review_p = _write_review_csv(tmp_path, [ROW_ACCOUNT_MAPPING, ROW_JUDGMENT])
    gnucash_p = tmp_path / "book.gnucash"
    gnucash_p.write_text("not a real book", encoding="utf-8")

    html = m._load_review_data(str(review_p), str(gnucash_p))

    assert re.findall(r"%%[A-Z_]*%%", html) == []
    assert "<table" in html
    assert "SYNCORP LTD" in html
    assert "CN1" in html
    assert "CN4" in html


def test_load_review_data_missing_both_inputs_is_safe():
    html = m._load_review_data("", "")
    assert "Select both" in html


def test_load_review_data_review_csv_not_found_is_safe(tmp_path):
    html = m._load_review_data(str(tmp_path / "nope.csv"), str(tmp_path / "book.gnucash"))
    assert "not found" in html.lower()


def test_load_review_data_empty_review_csv_reports_clearly(tmp_path):
    review_p = tmp_path / "Review.csv"
    review_p.write_text(_HEADER, encoding="utf-8")
    gnucash_p = tmp_path / "book.gnucash"
    gnucash_p.write_text("x", encoding="utf-8")
    html = m._load_review_data(str(review_p), str(gnucash_p))
    assert "nothing needs review" in html.lower()


# ---------------------------------------------------------------------------
# Hostile content / XSS — matches tests/test_itr_mapping_review.py's pattern.
# ---------------------------------------------------------------------------

def test_load_review_data_hostile_reason_not_live_markup(tmp_path):
    hostile = "<img src=x onerror=alert(1)>"
    row = f'CN9,Sale,{hostile},1000.00,"no security account match (fuzzy:0.10) for {hostile}"\n'
    review_p = _write_review_csv(tmp_path, [row])
    gnucash_p = tmp_path / "book.gnucash"
    gnucash_p.write_text("x", encoding="utf-8")

    html = m._load_review_data(str(review_p), str(gnucash_p))
    assert hostile not in html


# ---------------------------------------------------------------------------
# _row_presentation — locking + tags/badges per kind.
# ---------------------------------------------------------------------------

def test_row_presentation_account_mapping_row_is_unlocked():
    row = {"Reason": "no security account match (fuzzy:0.10) for 'X'"}
    m._row_presentation(row)
    assert row["kind"] == "account_mapping"
    assert row["_locked"] is False
    assert row["_tags"] == ["account_mapping"]


def test_row_presentation_data_value_row_is_locked():
    row = {"Reason": "no net amount"}
    m._row_presentation(row)
    assert row["kind"] == "data_value"
    assert row["_locked"] is True
    assert row["_tags"] == ["data_value"]


def test_row_presentation_judgment_row_is_locked():
    row = {"Reason": "insufficient FIFO lots for sale of X (need 1, have 0)"}
    m._row_presentation(row)
    assert row["kind"] == "judgment"
    assert row["_locked"] is True
    assert row["_tags"] == ["judgment"]


def test_row_presentation_note_carries_reason_text():
    row = {"Reason": "no net amount"}
    m._row_presentation(row)
    assert row["_note"] == "no net amount"


# ---------------------------------------------------------------------------
# _save_changes — locking enforced SERVER-SIDE, re-derived from Review.csv
# read fresh from disk (never trusted from the payload).
# ---------------------------------------------------------------------------

def _payload(review_path: str, changes: list[dict], gnucash_path: str = "") -> str:
    context = {"review_path": review_path}
    if gnucash_path:
        context["gnucash_path"] = gnucash_path
    return json.dumps({
        "context": context,
        "changes": changes,
        "all_rows": [],
    })


def test_save_changes_no_changes_is_a_noop():
    assert "No changes to save" in m._save_changes("")
    assert "No changes to save" in m._save_changes(_payload("", []))


def test_save_changes_malformed_json_reports_error():
    assert "Error parsing changes" in m._save_changes("{not valid json")


def test_save_changes_blank_review_path_never_touches_filesystem(tmp_path, monkeypatch):
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)
    payload = _payload("", [{"_idx": 0, "CN No": "CN1", "Account": "Assets:X"}])
    msg = m._save_changes(payload)
    assert "no review file" in msg.lower()
    assert not data_root.exists()


def test_save_changes_applies_account_mapping_row_and_writes_alias(tmp_path, monkeypatch):
    review_p = _write_review_csv(tmp_path, [ROW_ACCOUNT_MAPPING])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)
    monkeypatch.setattr(
        m, "_extract_stock_accounts", lambda gnucash_path: ["Assets:Investments:SYNCORP LTD"]
    )

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN1", "Account": "Assets:Investments:SYNCORP LTD"},
    ], gnucash_path=str(tmp_path / "book.gnucash"))
    msg = m._save_changes(payload)

    assert "Applied 1 alias mapping" in msg
    assert "nothing to back up" in msg.lower()  # cold start

    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["security_aliases"]["SYNCORP LTD"] == "Assets:Investments:SYNCORP LTD"


def test_save_changes_rejects_edit_to_data_value_row(tmp_path, monkeypatch):
    """The core locking guarantee: a data_value row's Reason (re-read fresh
    from disk) is not account_mapping, so an attempted edit is REJECTED —
    the config file is never written at all."""
    review_p = _write_review_csv(tmp_path, [ROW_DATA_VALUE_NET])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN2", "Account": "Assets:Investments:Hijacked"},
    ])
    msg = m._save_changes(payload)

    assert "No changes applied" in msg
    assert "REJECTED" in msg
    assert "locked" in msg.lower()
    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    assert not cfg_path.exists()


def test_save_changes_rejects_edit_to_judgment_row(tmp_path, monkeypatch):
    review_p = _write_review_csv(tmp_path, [ROW_JUDGMENT])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN4", "Account": "Assets:Investments:Hijacked"},
    ])
    msg = m._save_changes(payload)

    assert "No changes applied" in msg
    assert "REJECTED" in msg
    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    assert not cfg_path.exists()


def test_save_changes_rejects_edit_to_unknown_reason_row_too(tmp_path, monkeypatch):
    """A row whose Reason isn't one of the four known strings must also be
    rejected (locked-by-default), not silently accepted as account_mapping."""
    review_p = _write_review_csv(tmp_path, [ROW_UNKNOWN_REASON])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN5", "Account": "Assets:Investments:Hijacked"},
    ])
    msg = m._save_changes(payload)
    assert "REJECTED" in msg
    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    assert not cfg_path.exists()


def test_save_changes_ignores_forged_kind_field_and_reclassifies_from_disk(tmp_path, monkeypatch):
    """Even if a hand-crafted payload claims kind="account_mapping" for a
    row that is actually data_value on disk, the save handler must recompute
    the kind from Review.csv itself (matched by _idx + CN No), not trust any
    'kind' value the client supplied."""
    review_p = _write_review_csv(tmp_path, [ROW_DATA_VALUE_NET])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN2", "Account": "Assets:Investments:Hijacked",
         "kind": "account_mapping", "Reason": "no security account match (forged)"},
    ])
    msg = m._save_changes(payload)
    assert "REJECTED" in msg
    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    assert not cfg_path.exists()


def test_save_changes_stale_idx_cn_no_mismatch_is_rejected(tmp_path, monkeypatch):
    """If the file changed shape since Load (order shifted), the CN No
    cross-check at the claimed _idx must catch the mismatch and reject
    rather than silently write an alias for the wrong security."""
    review_p = _write_review_csv(tmp_path, [ROW_ACCOUNT_MAPPING])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN-DOES-NOT-MATCH", "Account": "Assets:Investments:X"},
    ])
    msg = m._save_changes(payload)
    assert "REJECTED" in msg
    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    assert not cfg_path.exists()


def test_save_changes_out_of_range_idx_is_rejected(tmp_path, monkeypatch):
    review_p = _write_review_csv(tmp_path, [ROW_ACCOUNT_MAPPING])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)

    payload = _payload(str(review_p), [
        {"_idx": 99, "CN No": "CN1", "Account": "Assets:Investments:X"},
    ])
    msg = m._save_changes(payload)
    assert "REJECTED" in msg
    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    assert not cfg_path.exists()


def test_save_changes_blank_account_selection_is_skipped_not_applied(tmp_path, monkeypatch):
    review_p = _write_review_csv(tmp_path, [ROW_ACCOUNT_MAPPING])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN1", "Account": ""},
    ])
    msg = m._save_changes(payload)
    assert "No changes applied" in msg
    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    assert not cfg_path.exists()


# ---------------------------------------------------------------------------
# Account validation — the picker at _extract_stock_accounts is built from
# real GnuCash accounts, so the UI cannot produce a bad value, but a
# hand-crafted payload could. This must be rejected server-side the same way
# a locked row is: reported, not silently written into security_aliases.
# ---------------------------------------------------------------------------

def test_save_changes_rejects_forged_account_not_in_picker_list(tmp_path, monkeypatch):
    review_p = _write_review_csv(tmp_path, [ROW_ACCOUNT_MAPPING])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)
    monkeypatch.setattr(
        m, "_extract_stock_accounts", lambda gnucash_path: ["Assets:Investments:SYNCORP LTD"]
    )

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN1", "Account": "Assets:Investments:NOT-A-REAL-ACCOUNT"},
    ], gnucash_path=str(tmp_path / "book.gnucash"))
    msg = m._save_changes(payload)

    assert "No changes applied" in msg
    assert "REJECTED" in msg
    assert "not a known STOCK/MUTUAL account" in msg
    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    assert not cfg_path.exists()


def test_save_changes_valid_account_applies_forged_sibling_rejected(tmp_path, monkeypatch):
    """One valid row applies, one forged row in the same payload is rejected
    and reported — the rejection must not silently swallow the valid write,
    and the valid write must not silently swallow the rejection."""
    review_p = _write_review_csv(tmp_path, [
        ROW_ACCOUNT_MAPPING,
        'CN6,Purchase,SYNOTHER LTD,750.00,"no security account match (fuzzy:0.20) for \'SYNOTHER LTD\'"\n',
    ])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)
    monkeypatch.setattr(
        m, "_extract_stock_accounts", lambda gnucash_path: ["Assets:Investments:SYNCORP LTD"]
    )

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN1", "Account": "Assets:Investments:SYNCORP LTD"},
        {"_idx": 1, "CN No": "CN6", "Account": "Assets:Investments:FORGED"},
    ], gnucash_path=str(tmp_path / "book.gnucash"))
    msg = m._save_changes(payload)

    assert "Applied 1 alias mapping" in msg
    assert "REJECTED" in msg
    assert "not a known STOCK/MUTUAL account" in msg

    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["security_aliases"] == {"SYNCORP LTD": "Assets:Investments:SYNCORP LTD"}


# ---------------------------------------------------------------------------
# Backup-before-write + no-op-on-zero-changes.
# ---------------------------------------------------------------------------

def test_save_changes_backs_up_existing_config_before_rewrite(tmp_path, monkeypatch):
    review_p = _write_review_csv(tmp_path, [ROW_ACCOUNT_MAPPING])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)

    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump({
        "currency": "INR", "security_aliases": {"OLD SECURITY": "Assets:Old"},
    }), encoding="utf-8")
    monkeypatch.setattr(
        m, "_extract_stock_accounts", lambda gnucash_path: ["Assets:Investments:SYNCORP LTD"]
    )

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN1", "Account": "Assets:Investments:SYNCORP LTD"},
    ], gnucash_path=str(tmp_path / "book.gnucash"))
    msg = m._save_changes(payload)

    assert "Backup written" in msg
    backups = list((data_root / "settings").glob("krc_gnucash_config.yaml.bak-*"))
    assert len(backups) == 1
    backed_up = yaml.safe_load(backups[0].read_text(encoding="utf-8"))
    assert backed_up["security_aliases"] == {"OLD SECURITY": "Assets:Old"}

    # New alias merged in, old preserved, other keys untouched (no silent
    # field loss in the config round-trip).
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["currency"] == "INR"
    assert saved["security_aliases"]["OLD SECURITY"] == "Assets:Old"
    assert saved["security_aliases"]["SYNCORP LTD"] == "Assets:Investments:SYNCORP LTD"


def test_save_changes_zero_valid_changes_never_touches_filesystem(tmp_path, monkeypatch):
    """No-op discipline: when every change is rejected/skipped, the config
    file must never be opened for a backup — not written, not backed up."""
    review_p = _write_review_csv(tmp_path, [ROW_JUDGMENT])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)

    cfg_dir = data_root / "settings"
    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN4", "Account": "Assets:Investments:X"},
    ])
    msg = m._save_changes(payload)
    assert "No changes applied" in msg
    assert not cfg_dir.exists()


# ---------------------------------------------------------------------------
# Round-trip — no silent field loss translating engine payload -> alias file.
# ---------------------------------------------------------------------------

def test_save_changes_multiple_account_mapping_rows_all_survive_round_trip(tmp_path, monkeypatch):
    review_p = _write_review_csv(tmp_path, [
        ROW_ACCOUNT_MAPPING,
        'CN6,Purchase,SYNOTHER LTD,750.00,"no security account match (fuzzy:0.20) for \'SYNOTHER LTD\'"\n',
    ])
    data_root = tmp_path / "Data"
    monkeypatch.setattr(m._config_mod, "data_root_dir", lambda: data_root)
    monkeypatch.setattr(
        m, "_extract_stock_accounts",
        lambda gnucash_path: [
            "Assets:Investments:SYNCORP LTD", "Assets:Investments:SYNOTHER LTD",
        ],
    )

    payload = _payload(str(review_p), [
        {"_idx": 0, "CN No": "CN1", "Account": "Assets:Investments:SYNCORP LTD"},
        {"_idx": 1, "CN No": "CN6", "Account": "Assets:Investments:SYNOTHER LTD"},
    ], gnucash_path=str(tmp_path / "book.gnucash"))
    msg = m._save_changes(payload)
    assert "Applied 2 alias mapping" in msg

    cfg_path = data_root / "settings" / "krc_gnucash_config.yaml"
    saved = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert saved["security_aliases"]["SYNCORP LTD"] == "Assets:Investments:SYNCORP LTD"
    assert saved["security_aliases"]["SYNOTHER LTD"] == "Assets:Investments:SYNOTHER LTD"


# ---------------------------------------------------------------------------
# _scan_review_csvs — output-folder discovery.
# ---------------------------------------------------------------------------

def test_scan_review_csvs_finds_krc_gnucash_output_folders(tmp_path, monkeypatch):
    out_dir = tmp_path / "Data" / "outputs"
    run_dir = out_dir / "20260101-120000-mybook-KRC-GnuCash"
    run_dir.mkdir(parents=True)
    (run_dir / "Review.csv").write_text(_HEADER + ROW_ACCOUNT_MAPPING, encoding="utf-8")
    monkeypatch.setattr(m._config_mod, "output_dir", lambda: out_dir)

    found = m._scan_review_csvs()
    assert len(found) == 1
    assert found[0][0].endswith("Review.csv")
    assert Path(found[0][1]).is_file()


def test_scan_review_csvs_no_output_dir_is_safe(monkeypatch):
    def _boom():
        raise RuntimeError("no config")
    monkeypatch.setattr(m._config_mod, "output_dir", _boom)
    assert m._scan_review_csvs() == []
