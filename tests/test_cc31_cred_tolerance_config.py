"""tests/test_cc31_cred_tolerance_config.py -- CC-31: the CRED tolerance is a per-entity setting
(cc_cred_max_overage in entities.yaml), editable on the Entities screen. Synthetic data only."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_cc_spend_booking import settings_io                  # noqa: E402
import configs                                                       # noqa: E402
from tests.test_cc_spend_booking import APR, MAY, book_pay, go, row, stmt, tie   # noqa: E402
from tests.test_ui35_entities_all_fields import _call, _profiles, _seed   # noqa: E402
from ui.tabs import itr_entities as ui_mod                             # noqa: E402


def run(tmp_path, monkeypatch, own=None, gap=30.0):
    """A statement of 1,000.00, paid by a CRED line of 1,000.00 against a bank payment `gap` rupees less."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    s1 = stmt(APR, [row("SWIGGY ORDER", 1000.0, (4, 5))])
    s2 = stmt(MAY, [row("CRED PAYMENT RECEIVED", 1000.0, (5, 12), kind="payment", direction="Cr")])
    extra = {"cc_cred_max_overage": own} if own is not None else None
    return go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=1000.0), tie(s2, printed=0.0)],
              [book_pay("p1", 1000.0 - gap, date(2025, 5, 12), desc="CRED CLUB")], entity_extra=extra)


def test_a_missing_key_gives_fifty_and_the_report_says_so(tmp_path, monkeypatch):
    reply, rows, _c, _o = run(tmp_path, monkeypatch)
    assert "CRED tolerance: 50 rupees (default)" in reply
    assert len(rows) >= 1                                              # the 30-rupee gap matched at 50


def test_the_report_says_when_the_value_comes_from_entities_yaml(tmp_path, monkeypatch):
    reply, _r, _c, _o = run(tmp_path, monkeypatch, own=40)
    assert "CRED tolerance: 40 rupees (from entities.yaml)" in reply


def test_fifteen_leaves_a_thirty_rupee_gap_unmatched_and_fifty_matches_it(tmp_path, monkeypatch):
    reply15, rows15, _c, _o = run(tmp_path / "a", monkeypatch, own=15)
    assert rows15 == [] and "RED FLAG" in reply15                          # NEGATIVE: not matched at 15
    reply50, rows50, _c, _o = run(tmp_path / "b", monkeypatch, own=50)
    assert rows50 and "RED FLAG" not in reply50


@pytest.mark.parametrize("bad", ["abc", -1, 501, "nan", True])
def test_a_bad_value_stops_the_run_loudly_naming_the_key(tmp_path, monkeypatch, bad):     # NEGATIVE
    reply, rows, _c, _o = run(tmp_path, monkeypatch, own=bad)
    assert reply.startswith("ERROR") and "cc_cred_max_overage" in reply
    assert rows == []


def test_the_limits_zero_and_five_hundred_are_accepted(tmp_path, monkeypatch):
    for ok in (0, 500):
        reply, _r, _c, _o = run(tmp_path / str(ok), monkeypatch, own=ok)
        assert not reply.startswith("ERROR") and f"CRED tolerance: {ok} rupees" in reply


def test_one_entitys_value_is_never_used_for_another():                                   # NEGATIVE
    a = configs.EntityProfile(key="A", name="A", pan="AAAAA0000A", status="Individual", cc_cred_max_overage=20.0)
    b = configs.EntityProfile(key="B", name="B", pan="BBBBB1111B", status="Individual")
    assert settings_io.resolve_cred_max_overage(a) == (20.0, "from entities.yaml")
    assert settings_io.resolve_cred_max_overage(b) == (50.0, "default")


# ---- the Entities tab ----

def test_the_field_shows_fifty_when_the_key_is_absent():
    e = configs.EntityProfile(key="A", name="A", pan="AAAAA0000A", status="Individual")
    assert ui_mod._entity_to_new_form("A", {"A": e})[7] == "50"


def test_a_stored_value_is_shown_on_the_form():
    e = configs.EntityProfile(key="A", name="A", pan="AAAAA0000A", status="Individual", cc_cred_max_overage=35.0)
    assert ui_mod._entity_to_new_form("A", {"A": e})[7] == "35"


def test_an_edit_is_saved_reloaded_and_kept_by_the_next_save(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, cc_cred_max_overage="25").startswith("**Saved**")
    assert _profiles(p)["SYN-P"].cc_cred_max_overage == 25.0
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-P"]["cc_cred_max_overage"] == 25     # the key is written
    assert _call("SYN-P", root).startswith("**Saved**")                    # an older caller (None) keeps it
    assert _profiles(p)["SYN-P"].cc_cred_max_overage == 25.0


def test_other_entities_keys_are_untouched_by_the_save(tmp_path):                           # NEGATIVE
    root, p = _seed(tmp_path)
    before = _profiles(p)["SYN-O"]
    _call("SYN-P", root, cc_cred_max_overage="25")
    after = _profiles(p)["SYN-O"]
    assert after.cc_cred_max_overage is None and after == before


@pytest.mark.parametrize("bad", ["abc", "-1", "501", "nan", "inf"])
def test_a_bad_value_is_refused_on_save_with_a_clear_message(tmp_path, bad):                # NEGATIVE
    root, p = _seed(tmp_path)
    _call("SYN-P", root, cc_cred_max_overage="25")
    msg = _call("SYN-P", root, cc_cred_max_overage=bad)
    assert "Not saved" in msg and "CRED tolerance" in msg and "0 to 500" in msg
    assert _profiles(p)["SYN-P"].cc_cred_max_overage == 25.0               # the stored value is unchanged


def test_blank_clears_the_override_and_is_never_read_as_zero(tmp_path):                      # NEGATIVE
    root, p = _seed(tmp_path)
    _call("SYN-P", root, cc_cred_max_overage="25")
    assert _call("SYN-P", root, cc_cred_max_overage="").startswith("**Saved**")
    prof = _profiles(p)["SYN-P"]
    assert prof.cc_cred_max_overage is None
    assert settings_io.resolve_cred_max_overage(prof) == (50.0, "default")


def test_the_save_round_trip_keeps_the_key_in_the_yaml_file(tmp_path):
    root, p = _seed(tmp_path)
    _call("SYN-P", root, cc_cred_max_overage="0")                          # zero is a real value, not "absent"
    assert yaml.safe_load(p.read_text(encoding="utf-8"))["SYN-P"]["cc_cred_max_overage"] == 0
    assert _profiles(p)["SYN-P"].cc_cred_max_overage == 0.0
