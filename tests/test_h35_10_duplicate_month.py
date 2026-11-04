"""
tests/test_h35_10_duplicate_month.py -- H35-10: two payout documents for the
same month are REFUSED (never counted twice, never silently de-duplicated).
Everything is synthetic.
"""
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_partner_comp_recon import agent as agent_module  # noqa: E402
from agents.skill_partner_comp_recon.mapper import DuplicateMonthError, build_input_data  # noqa: E402


def cert(month, name=None):
    return {"month": month, "source_name": name or f"cert-{month}.pdf", "total_paid": 480000.0,
            "remuneration": 200000.0, "share_of_profit_gross": 300000.0,
            "additional_share_of_profit": 0.0, "tds": -20000.0}


def stmt(month_name, year, name=None):
    return {"doc_class": "B", "month": month_name, "year": year,
            "source_name": name or f"stmt-{month_name}.pdf", "total": 375168.0,
            "remuneration": 200000.0, "share_of_profit": 195168.0,
            "additional_share_of_profit": 0.0, "tds": -20000.0}


def _build(records):
    return build_input_data(financial_year="2025-26", advice_records=records)


def test_same_month_twice_is_refused_naming_both_files_and_the_month():
    with pytest.raises(DuplicateMonthError) as e:
        _build([cert("2025-04", "a.pdf"), cert("2025-04", "b.pdf")])
    msg = str(e.value)
    assert "a.pdf" in msg and "b.pdf" in msg and "2025-04" in msg


def test_certificate_and_statement_for_one_month_are_refused():
    with pytest.raises(DuplicateMonthError) as e:
        _build([cert("2025-05", "cert.pdf"), stmt("May", 2025, "stmt.pdf")])
    assert "cert.pdf" in str(e.value) and "stmt.pdf" in str(e.value)


def test_duplicate_never_reaches_the_totals():                            # NEGATIVE
    with pytest.raises(DuplicateMonthError):
        _build([cert("2025-04"), cert("2025-04", "dup.pdf")])
    # the refusal is raised before any `monthly` structure is returned


def test_one_file_per_month_is_unchanged():                               # NEGATIVE
    data = _build([cert("2025-05"), cert("2025-04")])
    assert [m["month"] for m in data["monthly"]] == ["2025-04", "2025-05"]
    assert len(data["monthly"]) == 2


def test_different_months_are_not_refused():                              # NEGATIVE
    data = _build([cert("2025-04"), cert("2025-05"), cert("2025-06")])
    assert [m["month"] for m in data["monthly"]] == ["2025-04", "2025-05", "2025-06"]


def test_document_run_refuses_and_writes_nothing(tmp_path, monkeypatch):
    d = tmp_path / "advices"
    d.mkdir()
    for n in ("a.pdf", "b.pdf"):
        (d / n).write_bytes(b"%PDF-1.4 fake")
    adv = tmp_path / "adv.pdf"
    adv.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(agent_module._advisory_parser, "parse", lambda p, pw: {"financial_year": "2025-26"})
    monkeypatch.setattr(agent_module._payout_advice_parser, "parse",
                        lambda p, pw: cert("2025-04", Path(p).name))
    monkeypatch.setattr(agent_module, "_resolve_entity_config", lambda e, c: (None, None))
    out_path = tmp_path / "o.xlsx"
    out = agent_module._run_from_documents(
        entity="X", advices_dir=str(d), doc_password=None, advisory_path=str(adv),
        llp_statement="", payment_schedule="", gnucash_path="", xlsx_26as="",
        output_path=str(out_path), config_path=None, model_override=None, journal_path="")
    assert out.startswith("ERROR") and "a.pdf" in out and "b.pdf" in out and "2025-04" in out
    assert not out_path.exists() and not list(tmp_path.glob("*.csv"))


def test_structured_input_with_a_repeated_month_is_refused(tmp_path):
    import json
    f = tmp_path / "in.json"
    f.write_text(json.dumps({"financial_year": "2025-26",
                             "monthly": [{"month": "2025-04"}, {"month": "2025-04"}]}))
    out_path = tmp_path / "o.xlsx"
    out = agent_module.run(input_path=str(f), output_path=str(out_path))
    assert out.startswith("ERROR") and "2025-04" in out
    assert not out_path.exists()
