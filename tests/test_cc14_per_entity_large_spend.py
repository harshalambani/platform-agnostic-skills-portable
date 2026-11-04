"""CC-14: the large-spend (possible asset) limit can be set per entity.
Synthetic data only."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_cc_spend_booking import settings_io                  # noqa: E402
import configs                                                       # noqa: E402
from tests.test_cc_spend_booking import (                              # noqa: E402
    APR, MAY, SHOP, book_pay, go, row, stmt, tie,
)
from tests.test_ui35_entities_all_fields import _call, _profiles, _seed   # noqa: E402
from ui.tabs import itr_entities as ui_mod                             # noqa: E402


def run(tmp_path, monkeypatch, amount, *, own=None, glob=None):
    rows_ = [row("PHONE STORE", amount, (4, 5))]
    s1 = stmt(APR, rows_)
    s2 = stmt(MAY, [row("PAYMENT RECEIVED", amount, (5, 12), kind="payment", direction="Cr")])
    extra = {"cc_large_spend_threshold": own} if own is not None else None
    return go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=amount), tie(s2, printed=0.0)],
              [book_pay("p1", amount, date(2025, 5, 12))], entity_extra=extra, threshold=glob)


def test_the_entity_value_applies_and_the_report_names_it(tmp_path, monkeypatch):
    reply, rows, _c, _o = run(tmp_path, monkeypatch, 6000.0, own=5000, glob=50000)
    assert "LARGE SPENDS (1)" in reply and "5,000.00" in reply
    assert "Large-spend limit: 5,000.00 (from this entity's own setting)" in reply


def test_an_entity_without_its_own_value_uses_the_global_one(tmp_path, monkeypatch):    # NEGATIVE
    reply, _r, _c, _o = run(tmp_path, monkeypatch, 6000.0, glob=5000)
    assert "LARGE SPENDS (1)" in reply and "from the global setting" in reply
    assert "own setting" not in reply


def test_with_neither_the_default_applies(tmp_path, monkeypatch):
    reply, _r, _c, _o = run(tmp_path, monkeypatch, 25000.0)
    assert "20,000.00 (from the default)" in reply and "LARGE SPENDS (1)" in reply


def test_one_entitys_value_never_applies_to_another(tmp_path):                         # NEGATIVE
    a = configs.EntityProfile(key="A", name="A", pan="AAAAA0000A", status="Individual",
                              cc_large_spend_threshold=1000.0)
    b = configs.EntityProfile(key="B", name="B", pan="BBBBB1111B", status="Individual")
    assert settings_io.resolve_large_spend_threshold(a, None)[0] == 1000.0
    assert settings_io.resolve_large_spend_threshold(b, None) == (20000.0, "the default")


def test_the_flag_never_moves_a_spend_to_an_asset_account(tmp_path, monkeypatch):      # NEGATIVE
    _r, rows, _c, _o = run(tmp_path, monkeypatch, 25000.0, own=1000)
    assert rows[0]["Account"] == SHOP and not rows[0]["Account"].startswith("Assets")


def test_a_spend_below_the_entity_value_is_not_flagged(tmp_path, monkeypatch):         # NEGATIVE
    reply, _r, _c, _o = run(tmp_path, monkeypatch, 6000.0, own=7000, glob=1000)
    assert "LARGE SPENDS" not in reply and "7,000.00 (from this entity's own setting)" in reply


# ---- the Entities tab ----

def test_a_valid_value_is_saved_and_survives_another_save(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, cc_large_spend_threshold="15,000").startswith("**Saved**")
    assert _profiles(p)["SYN-P"].cc_large_spend_threshold == 15000.0
    assert _call("SYN-P", root).startswith("**Saved**")                 # an older caller (None)
    assert _profiles(p)["SYN-P"].cc_large_spend_threshold == 15000.0
    assert _profiles(p)["SYN-O"].cc_large_spend_threshold is None       # NEGATIVE: not applied to another


def test_blank_clears_the_override_and_is_never_read_as_zero(tmp_path):
    root, p = _seed(tmp_path)
    _call("SYN-P", root, cc_large_spend_threshold="15000")
    assert _call("SYN-P", root, cc_large_spend_threshold="").startswith("**Saved**")
    got = _profiles(p)["SYN-P"].cc_large_spend_threshold
    assert got is None                                                  # NEGATIVE: not 0.0
    assert settings_io.resolve_large_spend_threshold(_profiles(p)["SYN-P"], None)[0] == 20000.0


def test_non_numeric_zero_and_negative_values_are_refused(tmp_path):                  # NEGATIVE
    root, p = _seed(tmp_path)
    _call("SYN-P", root, cc_large_spend_threshold="15000")
    for bad in ("abc", "0", "-5", "nan", "inf"):
        msg = _call("SYN-P", root, cc_large_spend_threshold=bad)
        assert "Not saved" in msg and "Large-spend limit" in msg, bad
        assert _profiles(p)["SYN-P"].cc_large_spend_threshold == 15000.0, bad


def test_the_form_value_round_trips():
    e = configs.EntityProfile(key="A", name="A", pan="AAAAA0000A", status="Individual",
                              cc_large_spend_threshold=12500.0)
    vals = ui_mod._entity_to_new_form("A", {"A": e})
    assert vals[6] == "12500"
