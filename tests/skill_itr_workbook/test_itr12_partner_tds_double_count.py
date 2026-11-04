"""
ITR-12 (RED FLAG class: double counting): s.194T partner TDS tagged as salary or
other TDS is the same money as the 26AS 194T line that already enters the credit,
so the 234B/234C credit counts it twice. That is flagged loudly; a correct tagging
raises nothing. Synthetic data only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_itr02_partner_tds_194t as base  # noqa: E402
from test_itr02_partner_tds_194t import (  # noqa: E402,F401
    NEW, TDS_INT_GUID, _retag, _rules, _txn, as26_engine, sch, world,
)


def _flags(tp):
    return [w for w in tp.double_count_warnings if w.startswith("RED FLAG")]


def _salary_26as(nodes, resolved):
    s = abs(sch._sum_tag(resolved, nodes, "TAXPAID_TDS_SALARY"))
    return s


def test_partner_tds_tagged_as_salary_is_flagged_not_silently_credited_twice(world):
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_SALARY")
    amt = abs(sch._sum_tag(_retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_PARTNER"), nodes, "TAXPAID_TDS_PARTNER"))
    real_sal = _salary_26as(nodes, resolved)
    txns = [_txn("194T", amt)]
    if real_sal:
        txns.append(_txn("192", real_sal, tan="TAN2"))
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW), as26_engine.As26Data(transactions=txns))
    assert len(_flags(tp)) == 1 and "194T" in _flags(tp)[0]
    # and the 234 credit carries it too
    import interest_234
    credit = interest_234.resolve_tds_credit(1.0, 1.0, True)
    assert credit is not None


def test_partner_tds_tagged_as_other_is_flagged(world):
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_OTHER")
    amt = abs(sch._sum_tag(r2, nodes, "TAXPAID_TDS_OTHER"))
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW), as26_engine.As26Data(transactions=[_txn("194T", amt)]))
    assert len(_flags(tp)) == 1 and "other-section TDS" in _flags(tp)[0]


def test_a_correctly_tagged_run_raises_no_red_flag(world):                    # NEGATIVE
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_PARTNER")
    amt = abs(sch._sum_tag(r2, nodes, "TAXPAID_TDS_PARTNER"))
    real_sal = _salary_26as(nodes, r2)
    txns = [_txn("194T", amt)] + ([_txn("192", real_sal, tan="TAN2")] if real_sal else [])
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW), as26_engine.As26Data(transactions=txns))
    assert not _flags(tp)


def test_real_salary_tds_backed_by_26as_s192_is_not_flagged(world):           # NEGATIVE
    _t, resolved, nodes = world
    sal = _salary_26as(nodes, resolved)
    if not sal:
        pytest.skip("fixture has no salary TDS")
    tp = sch.build_taxes_paid(resolved, nodes, _rules(NEW),
                              as26_engine.As26Data(transactions=[_txn("192", sal, tan="TAN2")]))
    assert tp.double_count_warnings == []


def test_salary_tds_with_no_26as_s192_behind_it_warns(world):
    _t, resolved, nodes = world
    sal = _salary_26as(nodes, resolved)
    if not sal:
        pytest.skip("fixture has no salary TDS")
    tp = sch.build_taxes_paid(resolved, nodes, _rules(NEW),
                              as26_engine.As26Data(transactions=[_txn("194A", 100.0)]))
    assert any("no s.192 salary TDS" in w for w in tp.double_count_warnings)


def test_no_26as_means_no_double_count_claim(world):                          # NEGATIVE
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_SALARY")
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW), None)
    assert tp.double_count_warnings == []


def test_the_red_flag_reaches_the_234_credit_and_the_sheet(world):
    _t, resolved, nodes = world
    r2 = _retag(resolved, TDS_INT_GUID, "TAXPAID_TDS_OTHER")
    amt = abs(sch._sum_tag(r2, nodes, "TAXPAID_TDS_OTHER"))
    tp = sch.build_taxes_paid(r2, nodes, _rules(NEW), as26_engine.As26Data(transactions=[_txn("194T", amt)]))
    import inspect
    import write_workbook
    assert "double_count_warnings" in inspect.getsource(write_workbook.write_taxes_paid_sheet)
    assert "double_count_warnings" in inspect.getsource(sch)
    assert _flags(tp)
