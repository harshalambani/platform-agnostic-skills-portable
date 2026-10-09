"""CC-01: shared period picker, range edges, date-only cards by month, and the
agent never claiming success over an issue. Synthetic data only."""
from datetime import date

import pytest

from agents import period_picker as PP
from agents.skill_cc_sort import agent as cc_agent
from agents.skill_cc_sort import completeness as C

D = date
FS, FE = C.fy_bounds("2025-26")


def P(s, e):
    return C.StatementInfo("period", s, e)


def entries(infos):
    return [(f"s{i}.pdf", inf) for i, inf in enumerate(infos)]


def monthly(skip=()):
    import calendar
    out = []
    for k in range(12):
        y, m = (2025, 4 + k) if k < 9 else (2026, k - 8)
        if (y, m) not in skip:
            out.append(P(D(y, m, 1), D(y, m, calendar.monthrange(y, m)[1])))
    return out


# ---- the picker -------------------------------------------------------------

def test_default_option_is_the_last_completed_fy_and_comes_first():
    opts = PP.period_options(D(2026, 10, 9))
    assert opts[0] == "FY 2025-26"
    assert PP.resolve_period("", today=D(2026, 10, 9))[:2] == (D(2025, 4, 1), D(2026, 3, 31))
    assert PP.resolve_period(opts[0])[:2] == (D(2025, 4, 1), D(2026, 3, 31))
    # the list moves on by itself in April
    assert PP.period_options(D(2027, 4, 2))[0] == "FY 2026-27"
    assert PP.period_options(D(2027, 3, 31))[0] == "FY 2025-26"


def test_options_cover_last_current_and_prior_fy_with_quarters_and_custom():
    opts = PP.period_options(D(2026, 10, 9))
    for fy in ("2025-26", "2026-27", "2024-25"):
        assert f"FY {fy}" in opts
        for q in ("Q1", "Q2", "Q3", "Q4"):
            assert f"FY {fy} {q}" in opts
    assert opts[-1] == PP.CUSTOM_OPTION and len(opts) == 16


@pytest.mark.parametrize("opt,start,end", [
    ("FY 2025-26 Q1", D(2025, 4, 1), D(2025, 6, 30)),
    ("FY 2025-26 Q2", D(2025, 7, 1), D(2025, 9, 30)),
    ("FY 2025-26 Q3", D(2025, 10, 1), D(2025, 12, 31)),
    ("FY 2025-26 Q4", D(2026, 1, 1), D(2026, 3, 31)),
    ("FY 2024-25 Q4", D(2025, 1, 1), D(2025, 3, 31)),
    ("FY 2026-27 Q1", D(2026, 4, 1), D(2026, 6, 30)),
])
def test_each_quarter_option_maps_to_the_right_dates(opt, start, end):
    assert PP.resolve_period(opt)[:2] == (start, end)


def test_every_offered_option_resolves():
    for o in PP.period_options(D(2026, 10, 9)):
        if o != PP.CUSTOM_OPTION:
            PP.resolve_period(o)


def test_custom_range_and_single_month_as_custom():
    assert PP.resolve_period(PP.CUSTOM_OPTION, "2025-07-15", "2025-09-20")[:2] == (D(2025, 7, 15), D(2025, 9, 20))
    assert PP.resolve_period(PP.CUSTOM_OPTION, "2025-11-01", "2025-11-30")[:2] == (D(2025, 11, 1), D(2025, 11, 30))


@pytest.mark.parametrize("s,e", [("", ""), ("2025-07-15", ""), ("", "2025-09-20")])
def test_custom_with_a_missing_date_is_an_error(s, e):
    with pytest.raises(ValueError) as ex:
        PP.resolve_period(PP.CUSTOM_OPTION, s, e)
    assert "missing" in str(ex.value)


def test_custom_back_to_front_is_an_error_never_swapped():
    with pytest.raises(ValueError) as ex:
        PP.resolve_period(PP.CUSTOM_OPTION, "2025-09-20", "2025-07-15")
    assert "back to front" in str(ex.value)


@pytest.mark.parametrize("bad", ["FY 2025-27", "FY 2025-26 Q5", "garbage"])
def test_bad_period_is_rejected(bad):
    with pytest.raises(ValueError):
        PP.resolve_period(bad)


def test_impossible_custom_date_is_rejected():
    with pytest.raises(ValueError):
        PP.resolve_period(PP.CUSTOM_OPTION, "2025-02-30", "2025-03-01")


def test_custom_dates_are_ignored_unless_custom_is_picked():
    assert PP.resolve_period("FY 2025-26 Q1", "2020-01-01", "2020-01-02")[:2] == (D(2025, 4, 1), D(2025, 6, 30))


def test_skill_form_uses_the_picker_and_the_old_free_text_input_is_gone():
    from agents.registry import discover
    s = next(x for x in discover() if x.name == "CC Sort")
    names = [i.name for i in s.inputs]
    assert "financial_year" not in names
    per = next(i for i in s.inputs if i.name == "period")
    assert per.type == "select" and per.options_from == "report_periods"
    assert "custom_start" in names and "custom_end" in names
    from ui.tabs import _generic
    first = _generic._resolve_options_from("report_periods")[0][1]
    assert first == f"FY {PP.default_financial_year()}"


# ---- range edges, quarter, month --------------------------------------------

def test_quarter_and_single_month_full_coverage_no_gap():
    q = PP.resolve_period("FY 2025-26 Q2")
    assert C.check_card("S", entries(monthly()), q[0], q[1]).gaps == []
    a, b, _ = PP.resolve_period(PP.CUSTOM_OPTION, "2025-11-01", "2025-11-30")
    cov = C.check_card("S", entries(monthly()), a, b)
    assert cov.gaps == [] and cov.covered == (a, b)


def test_month_with_no_statement_is_flagged():
    a, b, _ = PP.resolve_period(PP.CUSTOM_OPTION, "2025-11-01", "2025-11-30")
    cov = C.check_card("S", entries(monthly(skip={(2025, 11)})), a, b)
    assert len(cov.gaps) == 1 and "Nov 2025" in cov.gaps[0]


def test_statement_straddling_range_start_is_not_an_edge_gap():
    a, b, _ = PP.resolve_period("FY 2025-26 Q2")
    infos = [P(D(2025, 5, 1), D(2025, 8, 31)), P(D(2025, 9, 1), D(2025, 9, 30))]
    cov = C.check_card("S", entries(infos), a, b)
    assert cov.gaps == [] and cov.covered == (a, b)


def test_straddle_at_range_end_is_not_an_edge_gap():
    a, b, _ = PP.resolve_period("FY 2025-26 Q2")
    assert C.check_card("S", entries([P(D(2025, 7, 1), D(2025, 10, 31))]), a, b).gaps == []


# ---- date-only cards, month by month ----------------------------------------

def _dates(*pairs):
    return [C.StatementInfo("date", None, D(y, m, d)) for y, m, d in pairs]


def test_date_only_moving_day_within_month_is_not_a_gap():
    pairs = [(2025, 4, 10), (2025, 5, 15), (2025, 6, 3), (2025, 7, 28), (2025, 8, 10), (2025, 9, 15),
             (2025, 10, 10), (2025, 11, 15), (2025, 12, 10), (2026, 1, 15), (2026, 2, 10), (2026, 3, 15)]
    assert C.check_card("Synth-Bank", entries(_dates(*pairs)), FS, FE).gaps == []


def test_date_only_missing_month_is_a_gap_with_no_edge_wording():
    pairs = [(2025, m, 10) for m in (4, 5, 7, 8, 9, 10, 11, 12)] + [(2026, m, 10) for m in (1, 2, 3)]
    cov = C.check_card("Synth-Bank", entries(_dates(*pairs)), FS, FE)
    assert len(cov.gaps) == 1 and "no statement dated in Jun 2025" in cov.gaps[0]
    assert "opened/closed" not in cov.gaps[0]


def test_two_statements_in_one_month_are_listed_not_a_gap():
    pairs = [(2025, m, 10) for m in range(4, 13)] + [(2026, m, 10) for m in (1, 2, 3)] + [(2025, 6, 25)]
    cov = C.check_card("Synth-Bank", entries(_dates(*pairs)), FS, FE)
    assert cov.gaps == [] and len(cov.same_month) == 1 and "Jun 2025" in cov.same_month[0]


# ---- agent: never a silent success ------------------------------------------

def _period_text(s, e):
    return f"{s:%d/%m/%Y} To {e:%d/%m/%Y}"


FULL = {f"m{i}.pdf": _period_text(p.start, p.end) for i, p in enumerate(monthly())}


def _env(tmp_path, monkeypatch, files, texts, results="ok"):
    import json
    root = tmp_path / "out"
    for rel in files:
        f = root / "Decrypted_PDFs_Correct" / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"x")
    root.mkdir(parents=True, exist_ok=True)
    if results == "ok":
        (root / "sort_results.json").write_text(json.dumps({"failed_decryption": []}))
    elif results == "garbled":
        (root / "sort_results.json").write_text("{not json")
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _n: "qpdf")
    monkeypatch.setattr(cc_agent, "_run_script", lambda *_a, **_k: 0)
    monkeypatch.setattr(C, "read_page1_text", lambda p: texts.get(p.name))
    return root


GOLD = [f"Synth-Gold/{n}" for n in FULL]


def test_agent_complete_run_with_a_quarter_says_successfully(tmp_path, monkeypatch):
    root = _env(tmp_path, monkeypatch, GOLD, FULL)
    msg = cc_agent.run("in", str(root), "pw", "FY 2025-26 Q3")
    assert msg.startswith("Sort completed successfully.") and "FY 2025-26 Q3" in msg


@pytest.mark.parametrize("mode", ["missing", "garbled"])
def test_agent_missing_or_unreadable_results_file_is_an_issue(tmp_path, monkeypatch, mode):
    root = _env(tmp_path, monkeypatch, GOLD, FULL, results=mode)
    msg = cc_agent.run("in", str(root), "pw", "FY 2025-26")
    assert "completed successfully" not in msg
    assert "could not read the decrypt results" in msg and "completed with 1 issue" in msg


def test_agent_no_card_folders_is_an_issue(tmp_path, monkeypatch):
    root = _env(tmp_path, monkeypatch, [], {})
    msg = cc_agent.run("in", str(root), "pw", "FY 2025-26")
    assert "completed successfully" not in msg and "no card folders" in msg


def test_agent_only_unknown_folder_is_an_issue(tmp_path, monkeypatch):
    root = _env(tmp_path, monkeypatch, ["Unknown-Unknown/odd.pdf"], {})
    msg = cc_agent.run("in", str(root), "pw", "FY 2025-26")
    assert "completed successfully" not in msg and "no card folders" in msg and "odd.pdf" in msg


def test_agent_card_folder_with_no_statements_is_an_issue(tmp_path, monkeypatch):
    root = _env(tmp_path, monkeypatch, ["Synth-Gold/terms.pdf"], {})
    msg = cc_agent.run("in", str(root), "pw", "FY 2025-26")
    assert "completed successfully" not in msg and "no statements found" in msg


def test_agent_custom_range_errors_and_success(tmp_path, monkeypatch):
    root = _env(tmp_path, monkeypatch, GOLD, FULL)
    assert cc_agent.run("in", str(root), "pw", PP.CUSTOM_OPTION, "2025-07-15", "").startswith("ERROR")
    m = cc_agent.run("in", str(root), "pw", PP.CUSTOM_OPTION, "2025-09-20", "2025-07-15")
    assert m.startswith("ERROR") and "back to front" in m
    ok = cc_agent.run("in", str(root), "pw", PP.CUSTOM_OPTION, "2025-11-01", "2025-11-30")
    assert ok.startswith("Sort completed successfully.")
