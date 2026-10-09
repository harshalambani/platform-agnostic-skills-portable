"""CC-01: completeness check for sorted credit-card statements.
Synthetic text only: invented dates, no real statements."""
from datetime import date

import pytest

from agents.skill_cc_sort import completeness as C
from agents.skill_cc_sort import agent as cc_agent

D = date


# ---- patterns a-g -----------------------------------------------------------

@pytest.mark.parametrize("text,kind,start,end,name", [
    ("Statement Period Statement Generation Date\n16/04/2025 - 15/05/2025 15/05/2025 02/06/2025",
     "period", D(2025, 4, 16), D(2025, 5, 15), "Axis"),
    ("Billing Period 5 Jun, 2025 - 4 Jul, 2025", "period", D(2025, 6, 5), D(2025, 7, 4), "HDFC"),
    ("Billing Period 5 June, 2025 - 4 July, 2025", "period", D(2025, 6, 5), D(2025, 7, 4), "HDFC"),
    ("01 AUG 2025 To 31 AUG 2025", "period", D(2025, 8, 1), D(2025, 8, 31), "HSBC"),
    ("Statement period : September 12, 2025 to October 11, 2025",
     "period", D(2025, 9, 12), D(2025, 10, 11), "ICICI"),
    ("02/11/2025 To 01/12/2025", "period", D(2025, 11, 2), D(2025, 12, 1), "YES"),
    ("Statement Date:20/01/2026", "date", None, D(2026, 1, 20), "HDFC-old"),
    ("Statement Date : 7-Feb-2026", "date", None, D(2026, 2, 7), "SBM"),
    ("Statement Date : 1-July-2025", "date", None, D(2025, 7, 1), "SBM"),
])
def test_each_pattern(text, kind, start, end, name):
    i = C.classify_text(text)
    assert (i.kind, i.start, i.end, i.pattern) == (kind, start, end, name)


def test_no_pattern_is_not_a_statement():
    assert C.classify_text("Terms and conditions of the card. Year summary.") is None
    assert C.classify_text("") is None and C.classify_text(None) is None
    # an impossible date never classifies
    assert C.classify_text("31/02/2025 To 01/03/2025") is None


def test_default_fy_is_last_completed():
    assert C.default_financial_year(D(2026, 10, 9)) == "2025-26"
    assert C.default_financial_year(D(2026, 3, 31)) == "2024-25"
    assert C.default_financial_year(D(2026, 4, 1)) == "2025-26"
    with pytest.raises(ValueError):
        C.fy_bounds("2025-27")


# ---- coverage ---------------------------------------------------------------

FS, FE = C.fy_bounds("2025-26")
PERIOD = (FS, FE, "FY 2025-26")


def P(s, e):
    return C.StatementInfo("period", s, e)


def monthly_periods(skip=()):
    """Contiguous monthly 1st-to-end periods for FY25-26."""
    import calendar
    out = []
    for k in range(12):
        y, m = (2025, 4 + k) if k < 9 else (2026, k - 8)
        if (y, m) in skip:
            continue
        out.append(P(D(y, m, 1), D(y, m, calendar.monthrange(y, m)[1])))
    return out


def entries(infos):
    return [(f"s{i}.pdf", inf) for i, inf in enumerate(infos)]


def test_complete_year_has_no_gap():
    cov = C.check_card("Synth-Gold", entries(monthly_periods()), FS, FE)
    assert cov.gaps == [] and cov.covered == (FS, FE)


def test_gap_in_the_middle_is_flagged():
    cov = C.check_card("Synth-Gold", entries(monthly_periods(skip={(2025, 8)})), FS, FE)
    assert len(cov.gaps) == 1 and "1 Aug 2025 - 31 Aug 2025" in cov.gaps[0]


def test_gap_at_fy_start_and_end_flagged_never_silent():
    infos = [i for i in monthly_periods() if i.start >= D(2025, 6, 1) and i.end <= D(2026, 2, 28)]
    cov = C.check_card("Synth-Gold", entries(infos), FS, FE)
    assert len(cov.gaps) == 2
    assert "1 Apr 2025 - 31 May 2025" in cov.gaps[0] and "opened/closed" in cov.gaps[0]
    assert "1 Mar 2026 - 31 Mar 2026" in cov.gaps[1] and "opened/closed" in cov.gaps[1]


def test_card_with_nothing_in_fy_is_reported():
    cov = C.check_card("Synth-Old", entries([P(D(2023, 1, 1), D(2023, 1, 31))]), FS, FE)
    assert cov.covered is None and len(cov.gaps) == 1 and cov.outside_fy == 1


def test_axis_one_day_skip_is_not_a_gap():
    infos = [P(D(2025, 3, 17), D(2025, 4, 15)), P(D(2025, 4, 17), D(2025, 5, 15)),
             P(D(2025, 5, 17), D(2026, 3, 31))]
    cov = C.check_card("Synth-Axis", entries(infos), FS, FE)
    assert cov.gaps == []


def test_two_and_three_month_periods_are_not_gaps():
    infos = [P(D(2025, 4, 1), D(2025, 5, 31)), P(D(2025, 6, 1), D(2025, 8, 31)),
             P(D(2025, 9, 1), D(2026, 3, 31))]
    assert C.check_card("Synth-Quiet", entries(infos), FS, FE).gaps == []


def test_a_four_day_hole_is_a_gap():
    infos = [P(D(2025, 4, 1), D(2025, 9, 30)), P(D(2025, 10, 5), D(2026, 3, 31))]
    assert len(C.check_card("Synth", entries(infos), FS, FE).gaps) == 1


def test_duplicate_counted_once_and_does_not_hide_a_gap():
    infos = monthly_periods(skip={(2025, 8)})
    ents = entries(infos) + [("copy of s0.pdf", infos[0]), ("copy2 of s0.pdf", infos[0])]
    cov = C.check_card("Synth-Gold", ents, FS, FE)
    assert cov.statements == len(infos)
    assert len(cov.duplicates) == 2 and all("duplicates" in d for d in cov.duplicates)
    assert len(cov.gaps) == 1


def test_not_a_statement_never_covers_a_month():
    infos = monthly_periods(skip={(2025, 8)})
    ents = entries(infos) + [("terms.pdf", None), ("year-summary.pdf", None)]
    cov = C.check_card("Synth-Gold", ents, FS, FE)
    assert cov.not_statements == ["terms.pdf", "year-summary.pdf"]
    assert len(cov.gaps) == 1 and "Aug 2025" in cov.gaps[0]


def test_statement_date_only_card():
    dates = [D(2025, m, 7) for m in range(4, 13)] + [D(2026, m, 7) for m in (1, 2, 3)]
    ents = entries([C.StatementInfo("date", None, d) for d in dates])
    assert C.check_card("Synth-Bank", ents, FS, FE).gaps == []
    missing = [d for d in dates if d.month != 9 or d.year != 2025]
    cov = C.check_card("Synth-Bank", entries([C.StatementInfo("date", None, d) for d in missing]), FS, FE)
    assert len(cov.gaps) == 1 and "no statement dated in Sep 2025" in cov.gaps[0]


# ---- folder + agent ---------------------------------------------------------

def _tree(tmp_path, files):
    root = tmp_path / "out"
    for rel in files:
        p = root / "Decrypted_PDFs_Correct" / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x")
    return root


def _reader_from(texts):
    return lambda p: texts.get(p.name)


def _period_text(s, e):
    return f"{s:%d/%m/%Y} To {e:%d/%m/%Y}"


FULL = {f"m{i}.pdf": _period_text(P_.start, P_.end) for i, P_ in enumerate(monthly_periods())}


def test_unknown_folder_files_never_cover_and_are_issues(tmp_path):
    names = list(FULL)
    root = _tree(tmp_path, [f"Synth-Gold/{n}" for n in names if n != "m4.pdf"] + ["Unknown-Unknown/m4.pdf"])
    rep = C.check_folder(root / "Decrypted_PDFs_Correct", PERIOD, reader=_reader_from(FULL))
    assert rep.unknown_files == ["m4.pdf"]
    assert any("Aug 2025" in g for c in rep.cards for g in c.gaps)   # not covered by the Unknown file
    assert any("Unknown-Unknown" in i for i in rep.issues)


def test_clean_run_report(tmp_path):
    root = _tree(tmp_path, [f"Synth-Gold/{n}" for n in FULL])
    rep = C.check_folder(root / "Decrypted_PDFs_Correct", PERIOD, reader=_reader_from(FULL))
    assert rep.issues == []
    assert "Synth-Gold: covered 1 Apr 2025 - 31 Mar 2026; gaps: none" in rep.text()


def _run_agent(tmp_path, monkeypatch, files, texts, failed=()):
    import json
    root = _tree(tmp_path, files)
    (root / "sort_results.json").write_text(json.dumps({"failed_decryption": list(failed)}))
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _n: "qpdf")
    monkeypatch.setattr(cc_agent, "_run_script", lambda *_a, **_k: 0)
    monkeypatch.setattr(C, "read_page1_text", lambda p: texts.get(p.name))
    return cc_agent.run(str(tmp_path / "in"), str(root), "pw", "FY 2025-26")


def test_agent_complete_run_says_successfully(tmp_path, monkeypatch):
    msg = _run_agent(tmp_path, monkeypatch, [f"Synth-Gold/{n}" for n in FULL], FULL)
    assert msg.startswith("Sort completed successfully.")


def test_agent_decrypt_failure_never_says_successfully(tmp_path, monkeypatch):
    msg = _run_agent(tmp_path, monkeypatch, [f"Synth-Gold/{n}" for n in FULL], FULL,
                     failed=["locked.pdf"])
    assert "completed successfully" not in msg
    assert "completed with 1 issue" in msg and "locked.pdf" in msg


def test_agent_unknown_file_never_says_successfully(tmp_path, monkeypatch):
    msg = _run_agent(tmp_path, monkeypatch,
                     [f"Synth-Gold/{n}" for n in FULL] + ["Unknown-Unknown/odd.pdf"], FULL)
    assert "completed successfully" not in msg and "odd.pdf" in msg


def test_agent_gap_never_says_successfully_and_lists_it(tmp_path, monkeypatch):
    msg = _run_agent(tmp_path, monkeypatch,
                     [f"Synth-Gold/{n}" for n in FULL if n != "m4.pdf"], FULL)
    assert "completed successfully" not in msg
    assert "completed with 1 issue" in msg and "Aug 2025" in msg


def test_agent_bad_fy_is_an_error(tmp_path, monkeypatch):
    root = _tree(tmp_path, [])
    monkeypatch.setattr(cc_agent.shutil, "which", lambda _n: "qpdf")
    monkeypatch.setattr(cc_agent, "_run_script", lambda *_a, **_k: 0)
    assert cc_agent.run("in", str(root), "pw", "FY 2025-99").startswith("ERROR")


def test_page1_reader_on_an_unreadable_file_returns_none(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"not a pdf")
    assert C.read_page1_text(p) is None


def test_date_only_card_missing_the_last_months_is_flagged_at_fy_end():
    dates = [D(2025, m, 7) for m in range(4, 13)] + [D(2026, 1, 7)]
    cov = C.check_card("Synth-Bank", entries([C.StatementInfo("date", None, d) for d in dates]), FS, FE)
    assert len(cov.gaps) == 1 and "Feb 2026 - Mar 2026" in cov.gaps[0] and "opened/closed" in cov.gaps[0]
