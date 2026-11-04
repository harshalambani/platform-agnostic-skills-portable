"""
IMP-14 -- advisory badge on own-name payments OUT whose other side is not in
the book. Advisory only: it never re-maps a row, never touches the CSV, never
flags IN rows, and takes the holder's name from entity config.

Synthetic names only.
"""
from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src", ROOT / "ui"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402
from ui.tabs import gnucash_review as rev  # noqa: E402

HOLDER = "Asha Verma"
COLS = ["Date", "Description", "Amount Negated (Deposit)", "Amount (Withdrawal)",
        "Account", "Confidence", "MatchReason"]


def _csv(tmp_path, rows):
    p = tmp_path / "out.csv"
    with open(p, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for d, desc, dep, wd, acct in rows:
            w.writerow({"Date": d, "Description": desc,
                        "Amount Negated (Deposit)": dep, "Amount (Withdrawal)": wd,
                        "Account": acct, "Confidence": "low", "MatchReason": ""})
    return str(p)


def _adv(tmp_path, rows, contra=None, name=HOLDER):
    return pipe._own_name_out_advisories(_csv(tmp_path, rows), contra or {}, name)


def test_own_name_payment_out_is_flagged(tmp_path):
    adv = _adv(tmp_path, [("2025-06-10", "UPI-ASHA VERMA-abc@ybl", "", "5000", "Expenses:Drawings")])
    assert list(adv) == [0]
    assert "other side not found" in adv[0]["reason"]


def test_insurer_payout_in_is_not_flagged(tmp_path):
    # money IN, even with the holder's own name; the deposit column is negated.
    adv = _adv(tmp_path, [("2025-06-10", "NEFT CR-LIFE INSURER-ASHA VERMA", "-5000", "", "Income:Insurance")])
    assert adv == {}


def test_relative_with_same_surname_is_not_flagged(tmp_path):
    adv = _adv(tmp_path, [("2025-06-10", "UPI-RAVI VERMA-abc@ybl", "", "5000", "Expenses:Gifts"),
                          ("2025-06-11", "UPI-ASHA KUMAR-abc@ybl", "", "5000", "Expenses:Gifts")])
    assert adv == {}


def test_row_with_other_side_found_keeps_todays_behaviour(tmp_path):
    adv = _adv(tmp_path, [("2025-06-10", "UPI-ASHA VERMA-abc@ybl", "", "5000", "Assets:Bank")],
               contra={0: {"status": "possible"}})
    assert adv == {}


def test_csv_and_accounts_are_untouched(tmp_path):
    path = _csv(tmp_path, [("2025-06-10", "UPI-ASHA VERMA-abc@ybl", "", "5000", "Expenses:Drawings")])
    before = Path(path).read_bytes()
    pipe._own_name_out_advisories(path, {}, HOLDER)
    assert Path(path).read_bytes() == before


def test_no_entity_or_one_word_name_flags_nothing(tmp_path):
    rows = [("2025-06-10", "UPI-ASHA VERMA-abc@ybl", "", "5000", "Expenses:Drawings")]
    assert _adv(tmp_path, rows, name="") == {}
    assert _adv(tmp_path, rows, name="Asha") == {}


def test_holder_name_comes_from_config_not_code(tmp_path):
    rows = [("2025-06-10", "UPI-ROHAN MEHTA-abc@ybl", "", "5000", "Expenses:Drawings")]
    assert _adv(tmp_path, rows, name="Rohan Mehta") != {}
    assert _adv(tmp_path, rows, name="Asha Verma") == {}


def test_middle_name_and_huf_words_are_ignored(tmp_path):
    rows = [("2025-06-10", "UPI-ASHA VERMA-abc@ybl", "", "5000", "Expenses:Drawings")]
    assert _adv(tmp_path, rows, name="Asha K Verma") != {}
    assert _adv(tmp_path, rows, name="Asha Verma HUF") != {}


def test_sidecar_written_and_stale_one_cleared(tmp_path):
    out = _csv(tmp_path, [("2025-06-10", "x", "", "1", "A")])
    pipe._write_advisory_sidecar(out, {0: {"kind": "k", "reason": "r"}})
    sc = Path(out).with_suffix(".advisory.json")
    assert json.loads(sc.read_text())["0"]["reason"] == "r"
    pipe._write_advisory_sidecar(out, {})
    assert json.loads(sc.read_text()) == {}


def test_empty_advisory_writes_no_file_when_none_exists(tmp_path):
    out = _csv(tmp_path, [("2025-06-10", "x", "", "1", "A")])
    pipe._write_advisory_sidecar(out, {})
    assert not Path(out).with_suffix(".advisory.json").exists()


# ---- Review presentation ---------------------------------------------------

def test_review_shows_badge_and_keeps_account():
    row = {"Confidence": "low", "Account": "Expenses:Drawings", "MatchReason": ""}
    rev._row_presentation(row, None, {"reason": "Possible own transfer, other side not found"})
    assert row["_badges"]["Date"]["text"] == "OWN?"
    assert "ownxfer" in row["_tags"]
    assert row["Account"] == "Expenses:Drawings"


def test_review_contra_wins_over_advisory():
    row = {"Confidence": "low", "Account": "A", "MatchReason": ""}
    rev._row_presentation(row, {"status": "possible", "reason": "r"}, {"reason": "adv"})
    assert row["_badges"]["Date"]["text"] == "POSSIBLE"
    assert "ownxfer" not in row["_tags"]


def test_review_without_advisory_is_unchanged():
    row = {"Confidence": "low", "Account": "A", "MatchReason": ""}
    rev._row_presentation(row, None)
    assert "_badges" not in row and row["_tags"] == ["low"]


def test_missing_advisory_sidecar_loads_empty(tmp_path):
    assert rev._load_advisory_sidecar(tmp_path / "nope.csv") == {}
