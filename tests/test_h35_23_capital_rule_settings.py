"""H35-23: the capital-rule rate and total months are per-FY entity settings, edited on the
Entities screen, stored in the same partner_comp_drivers keys. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import configs                                                          # noqa: E402
from agents.skill_partner_comp_recon import writer                       # noqa: E402
from agents.skill_partner_comp_recon.agent import apply_capital_rule_defaults   # noqa: E402
from agents.skill_partner_comp_recon.engine import build_report          # noqa: E402
from tests.test_h35_18_19_20_partner_drivers import _cap_data, _lines, _s194t_data   # noqa: E402
from tests.test_ui35_entities_all_fields import _call, _profiles, _seed   # noqa: E402
from ui.tabs import itr_entities as ui_mod                               # noqa: E402


def _drivers(p, key="SYN-P"):
    return yaml.safe_load(p.read_text(encoding="utf-8"))[key]["extra_items"]["partner_comp_drivers"]


# ---- the run: value and source ----

def _run(fy="2025-26", **drv):
    data = _cap_data(drv)
    data["financial_year"] = fy
    apply_capital_rule_defaults(data, "2025-04-01")
    return data, build_report(data)


def test_a_missing_rate_shows_default_as_its_source():                          # NEGATIVE: never silent
    _data, rep = _run()
    assert rep.drivers["capital_rate"] == 0.40 and rep.drivers["capital_months_total"] == 48
    assert rep.driver_sources["capital_rate"].startswith("Default")
    assert rep.driver_sources["capital_months_total"].startswith("Default")


def test_an_entity_rate_for_the_fy_wins_and_is_named():
    _data, rep = _run(capital_rate=0.50, capital_months_total=60)
    assert rep.drivers["capital_rate"] == 0.50 and rep.drivers["capital_months_total"] == 60
    assert rep.driver_sources["capital_rate"] == "Entity setting for 2025-26"
    assert rep.driver_sources["capital_months_total"] == "Entity setting for 2025-26"
    # 50% x 1,000,000 x 12/60
    assert rep.capital_rule.required_cumulative_capital == pytest.approx(100000.0)


def test_a_rate_set_in_one_fy_never_changes_another_fys_figure():                # NEGATIVE
    _d1, set_year = _run("2025-26", capital_rate=0.50)
    _d2, other_year = _run("2026-27")
    assert set_year.drivers["capital_rate"] == 0.50
    assert other_year.drivers["capital_rate"] == 0.40
    assert other_year.driver_sources["capital_rate"].startswith("Default")


def test_the_drivers_sheet_prints_the_source(tmp_path):
    import openpyxl
    _d, rep = _run(capital_rate=0.50)
    out = tmp_path / "d.xlsx"
    writer.write_report_workbook(rep, str(out))
    ws = openpyxl.load_workbook(str(out))["Drivers"]
    cells = {r[0].value: r[3].value for r in ws.iter_rows(min_row=2)}
    assert cells["Capital contribution rate"] == "Entity setting for 2025-26"
    assert cells["Capital months (total)"].startswith("Default")


def test_no_journal_amount_changes_when_the_rate_changes():                        # NEGATIVE
    a = _s194t_data(rem_rate=0.10)
    b = _s194t_data(rem_rate=0.10)
    for d, rate in ((a, 0.40), (b, 0.50)):
        d["drivers"]["target_compensation"] = 1_000_000.0
        d["drivers"]["capital_rate"] = rate
        apply_capital_rule_defaults(d, "2031-04-01")
    assert _lines(a) == _lines(b)


# ---- the Entities screen ----

def test_the_form_shows_a_stored_rate_as_percent_and_months_as_a_number(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, capital_rate_by_fy_text="2025-26 = 45", capital_months_by_fy_text="2025-26 = 36"
                 ).startswith("**Saved**")
    e = _profiles(p)["SYN-P"]
    vals = ui_mod._entity_to_new_form("SYN-P", {"SYN-P": e})
    assert vals[-2] == "2025-26 = 45" and vals[-1] == "2025-26 = 36"
    assert _drivers(p)["2025-26"]["capital_rate"] == pytest.approx(0.45)        # stored as a fraction
    assert _drivers(p)["2025-26"]["capital_months_total"] == 36
    assert "capital_rate" not in ui_mod._format_drivers(e.extra_items["partner_comp_drivers"])


def test_a_blank_form_shows_blank_not_the_default():
    e = configs.EntityProfile(key="A", name="A", pan="AAAAA0000A", status="Individual")
    vals = ui_mod._entity_to_new_form("A", {"A": e})
    assert vals[-2] == "" and vals[-1] == ""


def test_saving_an_unrelated_field_keeps_a_hand_set_capital_rate(tmp_path):         # NEGATIVE
    root, p = _seed(tmp_path)
    d = yaml.safe_load(p.read_text(encoding="utf-8"))
    d["SYN-P"]["extra_items"]["partner_comp_drivers"]["2025-26"].update(capital_rate=0.55, capital_months_total=30)
    p.write_text(yaml.safe_dump(d, sort_keys=True), encoding="utf-8")
    assert _call("SYN-P", root).startswith("**Saved**")                           # older caller (None)
    assert _drivers(p)["2025-26"]["capital_rate"] == 0.55
    assert _drivers(p)["2025-26"]["capital_months_total"] == 30
    # the form as it opens: the YAML box without the capital keys, the two boxes as shown
    e = _profiles(p)["SYN-P"]
    vals = ui_mod._entity_to_new_form("SYN-P", {"SYN-P": e})
    assert _call("SYN-P", root, partner_comp_drivers_text=vals[2], capital_rate_by_fy_text=vals[-2],
                 capital_months_by_fy_text=vals[-1]).startswith("**Saved**")
    assert _drivers(p)["2025-26"]["capital_rate"] == pytest.approx(0.55)
    assert _drivers(p)["2025-26"]["capital_months_total"] == 30
    assert _drivers(p)["2025-26"]["firms_tax_rate"] == 0.25                        # the other drivers too


def test_saving_the_yaml_box_alone_does_not_wipe_the_capital_keys(tmp_path):        # NEGATIVE
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, capital_rate_by_fy_text="2025-26 = 45").startswith("**Saved**")
    assert _call("SYN-P", root, partner_comp_drivers_text="2025-26:\n  firms_tax_rate: 0.3\n"
                 ).startswith("**Saved**")
    blk = _drivers(p)["2025-26"]
    assert blk["capital_rate"] == pytest.approx(0.45) and blk["firms_tax_rate"] == 0.3


def test_clearing_the_box_removes_only_the_capital_rate(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, capital_rate_by_fy_text="2025-26 = 45").startswith("**Saved**")
    assert _call("SYN-P", root, capital_rate_by_fy_text="").startswith("**Saved**")
    blk = _drivers(p)["2025-26"]
    assert "capital_rate" not in blk and blk["firms_tax_rate"] == 0.25


@pytest.mark.parametrize("bad", ["2025-26 = 0", "2025-26 = 100", "2025-26 = -5", "2025-26 = abc",
                                 "2025-26 = 150", "2025 = 40", "40", "2025-26 = 40\n2025-26 = 50"])
def test_a_bad_rate_is_refused_and_nothing_is_saved(tmp_path, bad):                # NEGATIVE
    root, p = _seed(tmp_path)
    before = p.read_text(encoding="utf-8")
    reply = _call("SYN-P", root, capital_rate_by_fy_text=bad)
    assert not reply.startswith("**Saved**")
    assert p.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("bad", ["2025-26 = 0", "2025-26 = -1", "2025-26 = 48.5", "2025-26 = x", "2025-26 = "])
def test_bad_months_are_refused_and_nothing_is_saved(tmp_path, bad):                # NEGATIVE
    root, p = _seed(tmp_path)
    before = p.read_text(encoding="utf-8")
    reply = _call("SYN-P", root, capital_months_by_fy_text=bad)
    assert not reply.startswith("**Saved**")
    assert p.read_text(encoding="utf-8") == before


def test_a_percent_sign_is_accepted(tmp_path):
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, capital_rate_by_fy_text="2025-26 = 42.5%").startswith("**Saved**")
    assert _drivers(p)["2025-26"]["capital_rate"] == pytest.approx(0.425)


def test_the_yaml_box_refuses_the_two_keys_so_there_is_one_place(tmp_path):         # NEGATIVE
    root, p = _seed(tmp_path)
    reply = _call("SYN-P", root, partner_comp_drivers_text="2025-26:\n  capital_rate: 0.5\n")
    assert not reply.startswith("**Saved**") and "own field" in reply
    assert "capital_rate" not in _drivers(p)["2025-26"]


def test_another_fy_is_not_touched_by_setting_one(tmp_path):                       # NEGATIVE
    root, p = _seed(tmp_path)
    assert _call("SYN-P", root, capital_rate_by_fy_text="2025-26 = 45").startswith("**Saved**")
    assert _call("SYN-P", root, capital_rate_by_fy_text="2025-26 = 45\n2026-27 = 30").startswith("**Saved**")
    d = _drivers(p)
    assert d["2025-26"]["capital_rate"] == pytest.approx(0.45)
    assert d["2026-27"]["capital_rate"] == pytest.approx(0.30)
    assert "capital_months_total" not in d["2025-26"]
