"""H35-11: the partner skill reads entity settings from `entities_path`, never
from the AI-model settings file handed in as `config_path`. Everything here is
synthetic (made-up entity keys, names, PANs, accounts and amounts)."""
import csv
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.registry import discover  # noqa: E402
from agents.skill_partner_comp_recon import agent as partner_agent  # noqa: E402
from tests.test_skill_partner_comp_recon import (  # noqa: E402
    _bank_deposit_txn, _gc_document_xml, _gc_tree_with_clearing,
    _write_gnucash_book,
)
from ui.tabs import _generic  # noqa: E402

ACCTS = {
    "bank": "Assets:Bank:Current Account",
    "tds_expense": "Expenses:Tax:TDS",
    "remuneration_income": "Income:PGBP:Remuneration",
    "share_of_profit_income": "Income:PGBP:Share of Profit",
}
FY = "2025-26"
NOT_CONFIGURED = "not configured"
CFG_FIELD_ERR = "missing required field"


def _partner_skill():
    return next(s for s in discover(refresh=True)
                if Path(s.manifest_path).parent.name == "skill_partner_comp_recon")


def _entities_file(path, accounts=ACCTS):
    body = {"SYN-LLP": {"name": "Synthetic Partner One", "pan": "AAAAA0000A",
                        "status": "Individual"}}
    if accounts is not None:
        body["SYN-LLP"]["partner_comp_accounts"] = dict(accounts)
    body["SYN-PLAIN"] = {"name": "Synthetic Plain", "pan": "BBBBB1111B",
                         "status": "Individual"}
    path.write_text(yaml.safe_dump(body), encoding="utf-8")
    return path


def _model_settings(path):
    path.write_text(yaml.safe_dump({"provider": "synthetic", "model": "x"}), encoding="utf-8")
    return path


@pytest.fixture
def stubbed_docs(tmp_path, monkeypatch):
    d = tmp_path / "advices"
    d.mkdir()
    (d / "a.pdf").write_bytes(b"%PDF-1.4 fake")
    adv = tmp_path / "adv.pdf"
    adv.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(partner_agent._advisory_parser, "parse",
                        lambda p, pw: {"financial_year": FY})
    monkeypatch.setattr(partner_agent._payout_advice_parser, "parse",
                        lambda p, pw: {"month": "2025-04", "source_name": "a.pdf",
                                       "total_paid": 480000.0, "remuneration": 200000.0,
                                       "share_of_profit_gross": 300000.0,
                                       "additional_share_of_profit": 0.0, "tds": -20000.0})
    return d, adv


def _run(tmp_path, docs, entity="SYN-LLP", **kw):
    d, adv = docs
    out = tmp_path / "o.xlsx"
    res = partner_agent.run(entity=entity, advices_dir=str(d), advisory_path=str(adv),
                            output_path=str(out), financial_year=FY, **kw)
    return res, out


def _workbook_text(path):
    import openpyxl
    if not path.exists():
        return ""
    wb = openpyxl.load_workbook(path)
    return " ".join(str(c.value) for ws in wb for r in ws.iter_rows() for c in r
                    if c.value is not None)


def _book(tmp_path, with_deposit=True):
    accounts, guids = _gc_tree_with_clearing()
    txns = []
    if with_deposit:
        txns.append(_bank_deposit_txn("dep-1", "2025-04-30", 480000.0,
                                      guids["Current Account"], guids["Other Clearing"]))
    return _write_gnucash_book(tmp_path / "book.gnucash", _gc_document_xml(accounts, txns))


# (a) app path -------------------------------------------------------------

def test_app_substitution_hands_the_entities_file_to_the_skill(tmp_path, monkeypatch):
    data = tmp_path / "Data"
    (data / "itr").mkdir(parents=True)
    ents = _entities_file(data / "itr" / "entities.yaml")
    cfg = _model_settings(tmp_path / "config.yaml")
    monkeypatch.setattr(_generic._config, "data_root_dir", lambda: data)
    kw = _generic.build_run_kwargs(_partner_skill(), {}, tmp_path / "o.xlsx",
                                   cfg, "", str(tmp_path))
    assert Path(kw["entities_path"]) == ents
    assert kw["config_path"] == str(cfg)
    profile, note = partner_agent._resolve_entity_config("SYN-LLP", kw["entities_path"])
    assert note is None and profile.pan == "AAAAA0000A"


def test_model_settings_file_is_never_parsed_as_entities(tmp_path, stubbed_docs):   # NEGATIVE
    cfg = _model_settings(tmp_path / "config.yaml")
    ents = _entities_file(tmp_path / "entities.yaml")
    res, out = _run(tmp_path, stubbed_docs, config_path=str(cfg), entities_path=str(ents))
    assert "entity 'provider'" not in res
    assert CFG_FIELD_ERR not in res
    assert "could not be loaded" not in res


def test_config_path_alone_does_not_supply_entity_settings(tmp_path, stubbed_docs):   # NEGATIVE
    cfg = _entities_file(tmp_path / "looks_like_entities.yaml")
    res, out = _run(tmp_path, stubbed_docs, config_path=str(cfg))
    assert "entity settings could not be loaded" in res


# (b) failed load wording ----------------------------------------------------

def test_failed_load_names_the_failure_and_never_says_not_configured(tmp_path, stubbed_docs):   # NEGATIVE
    bad = tmp_path / "missing.yaml"
    jr = tmp_path / "j.csv"
    res, out = _run(tmp_path, stubbed_docs, entities_path=str(bad),
                    gnucash_path=_book(tmp_path), journal_path=str(jr))
    text = res + " " + _workbook_text(out)
    assert "entity settings could not be loaded from" in res
    assert str(bad) in res
    assert NOT_CONFIGURED not in text.lower()
    assert not jr.exists()


def test_failed_load_warning_is_at_the_top_of_the_reply(tmp_path, stubbed_docs):   # NEGATIVE
    res, out = _run(tmp_path, stubbed_docs, entities_path=str(tmp_path / "nope.yaml"))
    assert res.splitlines()[0].startswith("WARNING: entity settings could not be loaded"), res
    assert NOT_CONFIGURED not in (res + _workbook_text(out)).lower()


def test_failed_load_with_a_book_names_the_failure_in_the_tieout_rows(tmp_path, stubbed_docs):   # NEGATIVE
    res, out = _run(tmp_path, stubbed_docs, entities_path=str(tmp_path / "nope.yaml"),
                    gnucash_path=_book(tmp_path))
    text = _workbook_text(out)
    assert "entity settings could not be loaded" in text
    assert NOT_CONFIGURED not in text.lower()


def test_entity_key_absent_from_file_is_a_load_failure(tmp_path, stubbed_docs):
    ents = _entities_file(tmp_path / "entities.yaml")
    res, _ = _run(tmp_path, stubbed_docs, entity="SYN-OTHER", entities_path=str(ents))
    assert "'SYN-OTHER' is not in that file" in res


# (c) genuinely no accounts --------------------------------------------------

def test_entity_without_partner_accounts_keeps_the_plain_note(tmp_path, stubbed_docs):
    ents = _entities_file(tmp_path / "entities.yaml")
    res, out = _run(tmp_path, stubbed_docs, entity="SYN-PLAIN", entities_path=str(ents),
                    gnucash_path=_book(tmp_path))
    text = res + " " + _workbook_text(out)
    assert "WARNING: entity settings" not in res                       # NEGATIVE
    assert "could not be loaded" not in text                           # NEGATIVE
    assert "no partner_comp_accounts configured" in text


# (f) end to end, no double booking ---------------------------------------------

def test_already_imported_payout_is_not_booked_to_the_bank_again(tmp_path, stubbed_docs):   # NEGATIVE
    ents = _entities_file(tmp_path / "entities.yaml")
    jr = tmp_path / "j.csv"
    res, out = _run(tmp_path, stubbed_docs, entities_path=str(ents),
                    gnucash_path=_book(tmp_path), journal_path=str(jr))
    assert jr.exists(), res
    rows = list(csv.DictReader(jr.open(newline="", encoding="utf-8")))
    joined = [" ".join(r.values()) for r in rows]
    assert not any("Current Account" in j for j in joined), joined   # bank import booked it
    assert any("Other Clearing" in j for j in joined)


def test_without_bank_credit_in_book_no_bank_leg_is_invented(tmp_path, stubbed_docs):   # NEGATIVE
    ents = _entities_file(tmp_path / "entities.yaml")
    jr = tmp_path / "j.csv"
    res, _ = _run(tmp_path, stubbed_docs, entities_path=str(ents),
                  gnucash_path=_book(tmp_path, with_deposit=False), journal_path=str(jr))
    rows = list(csv.DictReader(jr.open(newline="", encoding="utf-8"))) if jr.exists() else []
    assert not any("Current Account" in " ".join(r.values()) for r in rows), res
