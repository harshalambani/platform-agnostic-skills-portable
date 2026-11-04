"""
ITR-15: the partner s.194T double-count RED FLAG is shown at the TOP of the run
result (like the ITR-07 HELD line). It never stops the run, never changes the
status, and never touches the credit figure. Synthetic data only.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_itr01_precheck_warn as base  # noqa: E402
from test_itr01_precheck_warn import FIXTURES, _agent_run, agent  # noqa: E402,F401
sch = agent.sch

FLAG = ("RED FLAG: the book's salary TDS (Rs 1.00) equals the 26AS s.194T partner TDS (Rs 1.00). "
        "Retag the account as TAXPAID_TDS_PARTNER and re-run before filing.")
MAP = str(FIXTURES / "syn_ind.mapping.yaml")


def _inject(monkeypatch, warnings, seen=None):
    real = getattr(sch.build_all_schedules, "_real", sch.build_all_schedules)

    def wrapped(*a, **k):
        model = real(*a, **k)
        if warnings is not None:
            model.taxes_paid.double_count_warnings = list(warnings)
        if seen is not None:
            tp = model.taxes_paid
            seen.append((tp.tds_salary, tp.tds_other, tp.tds_interest, model.interest_234.total,
                         tuple(sorted(model.interest_234.warnings))))
        return model

    wrapped._real = real
    monkeypatch.setattr(sch, "build_all_schedules", wrapped)



def test_red_flag_is_the_first_line_of_the_result(tmp_path, monkeypatch):
    _inject(monkeypatch, [FLAG])
    summary = _agent_run(tmp_path, MAP)
    assert summary.splitlines()[0] == FLAG
    assert summary.count(FLAG) == 1


def test_status_and_credit_are_unchanged_by_the_flag(tmp_path, monkeypatch):     # NEGATIVE
    clean_dir, flag_dir = tmp_path / "a", tmp_path / "b"
    clean_dir.mkdir(); flag_dir.mkdir()
    seen: list = []
    _inject(monkeypatch, None, seen)
    clean = _agent_run(clean_dir, MAP)
    _inject(monkeypatch, [FLAG], seen)
    flagged = _agent_run(flag_dir, MAP)
    assert len(seen) == 2 and seen[0][:4] == seen[1][:4]       # credit and 234 total identical
    assert "STATUS: OK" in clean and "STATUS: OK" in flagged
    assert "STATUS: REVIEW" not in flagged and "BLOCKED" not in flagged.splitlines()[0]
    # removing the lifted line gives the unflagged result byte for byte
    assert flagged.split("\n\n", 1)[1] == clean


def test_no_flag_line_when_there_is_no_double_count(tmp_path):                  # NEGATIVE
    summary = _agent_run(tmp_path, MAP)
    assert "RED FLAG" not in summary


def test_a_plain_warning_is_not_lifted_to_the_top(tmp_path, monkeypatch):       # NEGATIVE
    _inject(monkeypatch, ["WARNING: the book has salary TDS of Rs 1.00 but 26AS shows no s.192."])
    summary = _agent_run(tmp_path, MAP)
    assert "RED FLAG" not in summary.splitlines()[0]


def test_amounts_that_differ_raise_no_flag():                                    # NEGATIVE
    import test_itr02_partner_tds_194t as b2
    tree = b2.pe.parse_html(b2.fixture_gen.build_syn_ind_html())
    res = b2.mapping_engine.resolve_tree(tree, b2.configs.load_mapping(FIXTURES / "syn_ind.mapping.yaml"))
    nodes = b2.sch._node_by_guid(tree)
    r2 = b2._retag(res.resolved, b2.TDS_INT_GUID, "TAXPAID_TDS_OTHER")
    amt = abs(b2.sch._sum_tag(r2, nodes, "TAXPAID_TDS_OTHER"))
    tp = b2.sch.build_taxes_paid(r2, nodes, b2._rules(b2.NEW),
                                 b2.as26_engine.As26Data(transactions=[b2._txn("194T", amt + 5000)]))
    assert not [w for w in tp.double_count_warnings if w.startswith("RED FLAG")]
