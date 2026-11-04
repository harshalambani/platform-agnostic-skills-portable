"""
tests/skill_itr_workbook/test_itr02_partner_tds_194t.py -- ITR-02: s.194T
partner TDS. From AY2026-27 section 194T is its own recognised category: its
own TaxesPaid line, tied to 26AS 194T, credited against tax, and never mixed
into the interest / dividend / salary lines. AY2025-26 rules are unchanged.

Synthetic data only (the SYN-IND fixture plus in-memory 26AS rows).
"""
from __future__ import annotations

import dataclasses
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
RULES_DIR = ROOT / "bundling" / "canonical" / "itr" / "rules"

for p in (str(SCRIPTS), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import as26 as as26_engine  # noqa: E402
import configs  # noqa: E402
import fixture_gen  # noqa: E402
import mapping as mapping_engine  # noqa: E402
import parse_eguile as pe  # noqa: E402
import rules as rules_engine  # noqa: E402
import schedules as sch  # noqa: E402
import tags as tag_vocab  # noqa: E402

TDS_INT_GUID = "3b305b86a4a5d6c626c6234b129ea174"      # Expense/TDS on Interest
NEW, OLD = "2025-26", "2024-25"   # income-year keys: AY2026-27, AY2025-26


@pytest.fixture(scope="module")
def world():
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    loaded = configs.load_mapping(FIXTURES / "syn_ind.mapping.yaml")
    result = mapping_engine.resolve_tree(tree, loaded)
    return tree, result.resolved, sch._node_by_guid(tree)


def _retag(resolved, guid, tag):
    out = dict(resolved)
    for k, leaf in resolved.items():
        if leaf.guid == guid:
            out[k] = dataclasses.replace(leaf, tag=tag)
    return out


def _txn(section, tds, tan="TAN1"):
    return as26_engine.As26Transaction(tan, "Example LLP", section, date(2025, 9, 1), tds * 10, tds, tds)


def _rules(year):
    return rules_engine.load_rules(RULES_DIR, year)


def _book_total(world):
    _t, resolved, nodes = world
    return abs(sch._sum_tag(resolved, nodes, "TAXPAID_TDS_INTEREST"))


# --- the vocabulary ---------------------------------------------------------------

def test_new_tags_exist_as_taxes_paid_tags():
    for t in ("TAXPAID_TDS_PARTNER", "TAXPAID_TDS_OTHER"):
        assert t in tag_vocab.TAGS and tag_vocab.TAGS[t].target == "TaxesPaid"


def test_existing_salary_tag_still_works(world):
    _t, resolved, nodes = world
    tp = sch.build_taxes_paid(resolved, nodes, _rules(NEW))
    assert tp.tds_salary == pytest.approx(abs(sch._sum_tag(resolved, nodes, "TAXPAID_TDS_SALARY")))


# --- 194T on AY2026-27 ------------------------------------------------------------

def test_194t_is_recognised_on_ay2026_27(world):
    _t, resolved, nodes = world
    data = as26_engine.As26Data(transactions=[_txn("194T", 7000.0)])
    tp = sch.build_taxes_paid(resolved, nodes, _rules(NEW), data)
    assert tp.unclassified_sections == []
    assert tp.as26_tds_partner == pytest.approx(7000.0)


def test_194t_never_enters_interest_or_dividend_tie_out(world):
    _t, resolved, nodes = world
    data = as26_engine.As26Data(transactions=[_txn("194T", 7000.0)])
    tp = sch.build_taxes_paid(resolved, nodes, _rules(NEW), data)
    assert tp.as26_tds_interest == 0.0 and tp.as26_tds_dividend == 0.0
    assert all(c["category"] != "TDS on interest" or c["as26"] == 0.0 for c in tp.tie_out_conflicts)


def test_book_partner_tds_ties_to_26as_and_is_credited_once(world):
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_PARTNER")
    amt = abs(sch._sum_tag(r2, nodes, "TAXPAID_TDS_PARTNER"))
    assert amt > 0
    data = as26_engine.As26Data(transactions=[_txn("194T", amt)])
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW), data)
    assert tp.tds_partner == pytest.approx(amt)
    assert tp.as26_tds_partner == pytest.approx(amt)
    assert not [c for c in tp.tie_out_conflicts if "194T" in c["category"]]
    # on its own line, never as salary or interest TDS
    assert tp.tds_interest == 0.0 and tp.tds_salary == pytest.approx(
        abs(sch._sum_tag(resolved, nodes, "TAXPAID_TDS_SALARY")))
    # counted once in the total (book line, not book + 26AS)
    base = sch.build_taxes_paid(resolved, nodes, _rules(NEW))
    assert tp.total == pytest.approx(base.total - base.tds_interest + amt)


def test_book_vs_26as_partner_mismatch_is_a_conflict(world):
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_PARTNER")
    amt = abs(sch._sum_tag(r2, nodes, "TAXPAID_TDS_PARTNER"))
    data = as26_engine.As26Data(transactions=[_txn("194T", amt + 500.0)])
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW), data)
    assert any("194T" in c["category"] for c in tp.tie_out_conflicts) and not tp.tie_out_ok


def test_partner_tds_is_credited_in_234(world):
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_PARTNER")
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW))
    assert tp.total >= tp.tds_partner > 0
    # no 26AS: the book figure is the credit
    assert tp.as26_available is False


# --- NEGATIVES --------------------------------------------------------------------

def test_ay2025_26_rules_unchanged_194t_still_unrecognised(world):
    _t, resolved, nodes = world
    assert "partner" not in _rules(OLD).common["tds_sections"]
    data = as26_engine.As26Data(transactions=[_txn("194T", 7000.0)])
    tp = sch.build_taxes_paid(resolved, nodes, _rules(OLD), data)
    assert [i["section"] for i in tp.unclassified_sections] == ["194T"]
    assert tp.as26_tds_partner == 0.0


def test_genuinely_unknown_section_still_warns_on_new_year(world):
    _t, resolved, nodes = world
    data = as26_engine.As26Data(transactions=[_txn("195", 2000.0)])
    tp = sch.build_taxes_paid(resolved, nodes, _rules(NEW), data)
    assert [i["section"] for i in tp.unclassified_sections] == ["195"]


def test_entity_without_partner_tds_is_unchanged(world):
    _t, resolved, nodes = world
    data = as26_engine.As26Data(transactions=[_txn("194A", 100.0)])
    tp = sch.build_taxes_paid(resolved, nodes, _rules(NEW), data)
    assert tp.tds_partner == 0.0 and tp.tds_other == 0.0
    assert not [c for c in tp.tie_out_conflicts if "194T" in c["category"]]
    assert tp.total == pytest.approx(
        tp.advance_tax + tp.self_assessment_tax + tp.tds_salary + tp.tds_interest
        + tp.tds_dividend + tp.tcs)


def test_194t_in_26as_without_book_entry_is_flagged_not_silently_credited(world):
    _t, resolved, nodes = world
    data = as26_engine.As26Data(transactions=[_txn("194T", 7000.0)])
    tp = sch.build_taxes_paid(resolved, nodes, _rules(NEW), data)
    assert tp.tds_partner == 0.0
    assert any("194T" in c["category"] for c in tp.tie_out_conflicts)


def test_partner_tds_never_counted_twice_in_credit(world):
    """The 26AS-basis credit uses the 26AS partner figure OR the book figure,
    never both."""
    import interest_234
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_PARTNER")
    amt = abs(sch._sum_tag(r2, nodes, "TAXPAID_TDS_PARTNER"))
    data = as26_engine.As26Data(transactions=[_txn("194T", amt)])
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW), data)
    book_tds = tp.tds_salary + tp.tds_interest + tp.tds_dividend + tp.tds_partner + tp.tds_other + tp.tcs
    as26_tds = (tp.tds_salary + tp.as26_tds_interest + tp.as26_tds_dividend
                + tp.as26_tds_partner + tp.tds_other + tp.tcs)
    assert as26_tds == pytest.approx(book_tds)
    assert interest_234 is not None


# --- TAXPAID_TDS_OTHER ------------------------------------------------------------

def test_other_tds_names_its_section_and_is_credited(world):
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_OTHER")
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW))
    assert tp.tds_other > 0 and tp.tds_interest == 0.0
    assert tp.total >= tp.tds_other
    # the fixture account name carries no section -> loud warning, still credited
    assert tp.tds_other_warnings and tp.tds_other_items[0]["section"] == "(section not named)"


def test_other_tds_never_lands_in_the_interest_line(world):
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_OTHER")
    data = as26_engine.As26Data(transactions=[_txn("194A", 100.0)])
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW), data)
    assert tp.tds_interest == 0.0
    assert tp.as26_tds_interest == pytest.approx(100.0)


def test_section_regex_reads_a_named_section():
    m = sch._SECTION_RE.search("Expenses:TDS 194J Fees")
    assert m and m.group(1).upper() == "194J"
    assert sch._SECTION_RE.search("Expenses:TDS Misc") is None
