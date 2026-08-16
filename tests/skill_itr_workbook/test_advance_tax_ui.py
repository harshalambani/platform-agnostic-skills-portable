"""
tests/skill_itr_workbook/test_advance_tax_ui.py -- Advance Tax Estimator tab
(ui/tabs/advance_tax.py), PR2 (UI) of the 2026-08 advance-tax-estimator work.

Pure-Python against the tab's handler functions -- no browser driven,
mirroring test_itr_entities_ui.py's patch("ui._config.data_root_dir")
pattern. Manual-input mode is the PRIMARY path under test: every gate below
exercises the form with no GnuCash book, no mapping and no entity registry
at all, per the brief ("the tab must be fully usable with no GnuCash book,
no mapping and no registry").

Covers:
  1. Line-format parsers (advance_tax_paid, capital_gains) -- valid, blank,
     malformed, round-trip.
  2. _form_to_input validation (blank entity/fy blocked, bad dates blocked).
  3. Save / load / delete(archive) CRUD against a synthetic Data/advance_tax/.
  4. Annualise leaves dated/undated fields alone appropriately (delegates to
     advance_tax.annualise, exercised end-to-end through the tab handler).
  5. Compute + write workbook end-to-end against the real canonical rules
     (bundling/canonical/itr/rules), fully manual (no book/mapping), and
     that unprojected-head / undated-capital-gain flags surface in the
     status message.

Fully offline; synthetic fixtures only. No access to any Data/ path, any
*.gnucash file outside tests/fixtures, or the live PortableApps store.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
AGENT_DIR = ROOT / "src" / "agents" / "skill_itr_workbook"
SRC = ROOT / "src"
RULES_DIR = ROOT / "bundling" / "canonical" / "itr" / "rules"

for p in (str(SCRIPTS), str(AGENT_DIR), str(SRC)):
    if p not in sys.path:
        sys.path.insert(0, p)
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ui.tabs import advance_tax as ui_mod  # noqa: E402

YEAR_KEY = "2024-25"


def _blank_values(**overrides) -> tuple:
    """A full _FORM_FIELDS-shaped tuple, valid-manual-entry by default."""
    d = dict(zip(ui_mod._FORM_FIELDS, ui_mod._blank_form()))
    d["entity_name"] = "Synthetic Individual"
    d["fy"] = YEAR_KEY
    d.update(overrides)
    return tuple(d[f] for f in ui_mod._FORM_FIELDS)


# ---------------------------------------------------------------------------
# Line-format parsers.
# ---------------------------------------------------------------------------

def test_parse_paid_lines_basic():
    items, errors = ui_mod._parse_paid_lines("2024-09-10 = 25000\n2024-12-12=30000\n\n# comment\n")
    assert errors == []
    assert [a for _, a in items] == [25000.0, 30000.0]


def test_parse_paid_lines_bad_date():
    items, errors = ui_mod._parse_paid_lines("not-a-date = 100")
    assert not items
    assert errors and "date" in errors[0]


def test_parse_paid_lines_bad_amount():
    items, errors = ui_mod._parse_paid_lines("2024-09-10 = not-a-number")
    assert not items
    assert errors and "number" in errors[0]


def test_format_paid_lines_round_trips():
    items, _ = ui_mod._parse_paid_lines("2024-09-10 = 25000")
    text = ui_mod._format_paid_lines(items)
    items2, errors = ui_mod._parse_paid_lines(text)
    assert errors == []
    assert items == items2


def test_parse_cg_lines_basic():
    items, errors = ui_mod._parse_cg_lines(
        "150000, LTCG_112A, 2024-11-02, Synthetic sale\n60000, STCG_111A\n"
    )
    assert errors == []
    assert len(items) == 2
    assert items[0].amount == 150000.0
    assert items[0].gain_type == "LTCG_112A"
    assert items[0].note == "Synthetic sale"
    assert items[1].sale_date is None
    assert items[1].undated is True


def test_parse_cg_lines_bad_gain_type():
    items, errors = ui_mod._parse_cg_lines("100, NOT_A_TYPE")
    assert not items
    assert errors and "gain_type" in errors[0]


def test_parse_cg_lines_bad_amount():
    items, errors = ui_mod._parse_cg_lines("not-a-number, OTHER")
    assert not items
    assert errors and "number" in errors[0]


def test_format_cg_lines_round_trips():
    items, _ = ui_mod._parse_cg_lines("150000, LTCG_112A, 2024-11-02, Note here")
    text = ui_mod._format_cg_lines(items)
    items2, errors = ui_mod._parse_cg_lines(text)
    assert errors == []
    assert len(items2) == 1
    assert items2[0].amount == items[0].amount
    assert items2[0].sale_date == items[0].sale_date


# ---------------------------------------------------------------------------
# _form_to_input validation -- manual mode, no book/mapping/registry.
# ---------------------------------------------------------------------------

def test_form_to_input_valid_manual_minimum():
    input_data, errors = ui_mod._form_to_input(*_blank_values())
    assert errors == []
    assert input_data.entity_name == "Synthetic Individual"
    assert input_data.fy == YEAR_KEY
    assert input_data.salary.actual_to_date == 0.0
    assert input_data.salary.unprojected is True


def test_form_to_input_blank_entity_blocked():
    input_data, errors = ui_mod._form_to_input(*_blank_values(entity_name=""))
    assert input_data is None
    assert any("Entity name" in e for e in errors)


def test_form_to_input_blank_fy_blocked():
    input_data, errors = ui_mod._form_to_input(*_blank_values(fy=""))
    assert input_data is None
    assert any("Financial year" in e for e in errors)


def test_form_to_input_bad_as_of_date_blocked():
    input_data, errors = ui_mod._form_to_input(*_blank_values(as_of="not-a-date"))
    assert input_data is None
    assert any("As of" in e for e in errors)


def test_form_to_input_propagates_paid_and_cg_parse_errors():
    input_data, errors = ui_mod._form_to_input(
        *_blank_values(advance_tax_paid_text="garbage-line", capital_gains_text="also-garbage")
    )
    assert input_data is None
    assert any("Advance tax paid" in e for e in errors)
    assert any("Capital gains" in e for e in errors)


def test_form_to_input_full_round_trip_via_input_to_form():
    values = _blank_values(
        salary_actual=500000.0, salary_remainder=250000.0,
        remuneration_actual=100000.0, remuneration_remainder=None,
        other_sources_other=5000.0, deductions_via=150000.0, tds_to_date=20000.0,
        advance_tax_paid_text="2024-09-10 = 25000",
        capital_gains_text="150000, LTCG_112A, 2024-11-02, Note",
    )
    input_data, errors = ui_mod._form_to_input(*values)
    assert errors == []
    form_again = ui_mod._input_to_form(input_data)
    input_data2, errors2 = ui_mod._form_to_input(*form_again)
    assert errors2 == []
    assert input_data2.salary.actual_to_date == input_data.salary.actual_to_date
    assert input_data2.salary.projected_remainder == input_data.salary.projected_remainder
    assert input_data2.remuneration.unprojected is True
    assert input_data2.capital_gains[0].amount == 150000.0


# ---------------------------------------------------------------------------
# Save / load / delete(archive) CRUD.
# ---------------------------------------------------------------------------

def test_save_then_load_round_trips(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        save_msg = ui_mod._handle_save("SYN-ESTIMATE", *_blank_values(salary_actual=42000.0))
        assert "Saved" in save_msg

        choices = dict(ui_mod._estimate_choices())
        assert "SYN-ESTIMATE" in choices

        loaded = ui_mod._handle_load("SYN-ESTIMATE")
    *form, msg = loaded
    assert "Loaded" in msg
    input_data, errors = ui_mod._form_to_input(*form)
    assert errors == []
    assert input_data.salary.actual_to_date == 42000.0

    saved_file = data_root / "advance_tax" / "SYN-ESTIMATE.json"
    assert saved_file.is_file()


def test_save_blocked_by_validation_errors(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._handle_save("SYN-BAD", *_blank_values(entity_name=""))
    assert "Error" in msg
    assert not (data_root / "advance_tax" / "SYN-BAD.json").exists()


def test_save_blank_name_blocked(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg = ui_mod._handle_save("", *_blank_values())
    assert "Error" in msg
    assert not data_root.exists()


def test_load_unknown_name_returns_blank_with_error():
    with patch("ui._config.data_root_dir", return_value=Path("Z:\\does-not-exist")):
        result = ui_mod._handle_load("NOT-A-REAL-ESTIMATE")
    *_form, msg = result
    assert "Error loading" in msg


def test_load_blank_name_returns_blank_form_no_error():
    result = ui_mod._handle_load("")
    *_form, msg = result
    assert "Error" not in msg


def test_delete_archives_not_permanently_deletes(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        ui_mod._handle_save("SYN-DEL", *_blank_values())
        assert (data_root / "advance_tax" / "SYN-DEL.json").is_file()

        msg, _update = ui_mod._handle_delete("SYN-DEL")

    assert "Archived" in msg
    assert not (data_root / "advance_tax" / "SYN-DEL.json").exists()
    archived = list((data_root / "advance_tax" / "_archive").glob("SYN-DEL.json.deleted-*"))
    assert len(archived) == 1


def test_delete_unknown_name_reported_not_found(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg, _update = ui_mod._handle_delete("NOT-A-REAL-ESTIMATE")
    assert "No saved estimate" in msg


def test_delete_blank_name_reported(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        msg, _update = ui_mod._handle_delete("")
    assert "Pick a saved estimate" in msg


def test_list_estimates_never_errors_on_missing_dir(tmp_path):
    data_root = tmp_path / "Data"
    with patch("ui._config.data_root_dir", return_value=data_root):
        choices = ui_mod._estimate_choices()
    assert choices == [("(new estimate)", "")]


# ---------------------------------------------------------------------------
# Annualise.
# ---------------------------------------------------------------------------

def test_annualise_fills_only_blank_remainders():
    values = _blank_values(
        as_of="2024-09-15",
        salary_actual=200000.0, salary_remainder=None,
        remuneration_actual=50000.0, remuneration_remainder=100000.0,  # already projected
    )
    result = ui_mod._handle_annualise(*values)
    *form, msg = result
    assert "Annualised" in msg
    input_data, errors = ui_mod._form_to_input(*form)
    assert errors == []
    assert input_data.salary.unprojected is False
    assert input_data.salary.projected_remainder > 0
    # Already-projected head is left exactly as entered.
    assert input_data.remuneration.projected_remainder == 100000.0


def test_annualise_blocked_on_validation_errors():
    result = ui_mod._handle_annualise(*_blank_values(entity_name=""))
    *_form, msg = result
    assert "Error" in msg


# ---------------------------------------------------------------------------
# Compute + write workbook -- manual mode, no book/mapping, real canonical
# rules (mirrors test_advance_tax.py's own rules fixture).
# ---------------------------------------------------------------------------

@pytest.fixture()
def isolated_data_root(tmp_path):
    data_root = tmp_path / "Data"
    outputs_dir = data_root / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    return data_root, outputs_dir


def test_compute_manual_mode_writes_workbook_and_reports_flags(isolated_data_root, monkeypatch):
    data_root, outputs_dir = isolated_data_root
    monkeypatch.setattr(ui_mod._config_mod, "data_root_dir", lambda: data_root)
    monkeypatch.setattr(ui_mod._config_mod, "output_dir", lambda: outputs_dir)
    monkeypatch.setattr(ui_mod._config_mod, "download_staging_dir", lambda: outputs_dir / "_staging")

    values = _blank_values(
        fy=YEAR_KEY,
        salary_actual=500000.0, salary_remainder=None,  # left unprojected on purpose
        capital_gains_text="150000, LTCG_112A",  # no sale_date on purpose
    )
    msg, download_update, path_update = ui_mod._handle_compute(*values)

    assert "Done" in msg
    assert "New regime tax liability" in msg
    assert "unprojected heads" in msg
    assert "undated capital-gains" in msg
    assert download_update["interactive"] is True
    # path_update feeds a gr.State, not a gr.Textbox -- _handle_compute
    # returns the raw path string directly (State.postprocess() passes its
    # value through untouched, it does not unwrap a gr.update(...) dict).
    out_path = Path(path_update)
    assert out_path.is_file()
    assert out_path.suffix == ".xlsx"

    served_path = Path(download_update["value"])
    assert served_path.is_file()
    assert served_path.parent == outputs_dir / "_staging"


def test_compute_blocked_on_validation_errors(isolated_data_root, monkeypatch):
    data_root, outputs_dir = isolated_data_root
    monkeypatch.setattr(ui_mod._config_mod, "data_root_dir", lambda: data_root)
    monkeypatch.setattr(ui_mod._config_mod, "output_dir", lambda: outputs_dir)

    msg, download_update, _path_update = ui_mod._handle_compute(*_blank_values(entity_name=""))
    assert "Error" in msg
    assert download_update["interactive"] is False


def test_compute_unknown_fy_reports_error_not_crash(isolated_data_root, monkeypatch):
    data_root, outputs_dir = isolated_data_root
    monkeypatch.setattr(ui_mod._config_mod, "data_root_dir", lambda: data_root)
    monkeypatch.setattr(ui_mod._config_mod, "output_dir", lambda: outputs_dir)

    msg, download_update, _path_update = ui_mod._handle_compute(*_blank_values(fy="1999-00"))
    assert "Error" in msg
    assert download_update["interactive"] is False
