"""
tests/test_cc15_ui39_cc_sort.py -- CC-15 (end-of-year tail wording) and UI-39 (CC Sort and
CC Transactions panel text must not run together in Markdown).

Everything is synthetic.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (str(SRC), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_cc_sort import completeness as C  # noqa: E402

D = date
FS, FE = C.fy_bounds("2025-26")
OLD_WORDING = "card opened/closed, or statement missing?"
NEW_WORDING = "these days fall in the next statement"


def P(s, e):
    return C.StatementInfo("period", s, e)


def _cov(periods):
    return C.check_card("Sample", [(f"s{i}.pdf", P(s, e)) for i, (s, e) in enumerate(periods)], FS, FE)


FULL_YEAR = [(D(2025, 4, 1), D(2025, 4, 30)), (D(2025, 5, 1), D(2025, 5, 31)),
             (D(2025, 6, 1), D(2025, 6, 30)), (D(2025, 7, 1), D(2025, 7, 31)),
             (D(2025, 8, 1), D(2025, 8, 31)), (D(2025, 9, 1), D(2025, 9, 30)),
             (D(2025, 10, 1), D(2025, 10, 31)), (D(2025, 11, 1), D(2025, 11, 30)),
             (D(2025, 12, 1), D(2025, 12, 31)), (D(2026, 1, 1), D(2026, 1, 31)),
             (D(2026, 2, 1), D(2026, 2, 28))]


# --- CC-15 ------------------------------------------------------------------

def test_only_a_short_tail_gets_the_next_statement_wording():
    cov = _cov(FULL_YEAR[:-1] + [(D(2026, 2, 1), D(2026, 3, 20))])
    assert len(cov.gaps) == 1
    g = cov.gaps[0]
    assert NEW_WORDING in g and "add it" in g
    assert "21 Mar 2026" in g or "21-Mar-2026" in g or "2026" in g
    assert OLD_WORDING not in g
    # does not claim the statement exists
    assert "exists" not in g and "is missing" not in g


def test_a_real_end_of_year_gap_still_shows_as_missing():
    cov = _cov(FULL_YEAR[:-2])           # last statement ends 31 Jan: a two-month hole
    assert len(cov.gaps) == 1
    assert OLD_WORDING in cov.gaps[0]
    assert NEW_WORDING not in cov.gaps[0]


def test_a_mid_year_gap_keeps_its_wording():
    cov = _cov(FULL_YEAR[:3] + FULL_YEAR[5:] + [(D(2026, 3, 1), D(2026, 3, 31))])
    assert cov.gaps
    assert all(NEW_WORDING not in g for g in cov.gaps)
    assert any("no statement covers" in g for g in cov.gaps)


def test_a_short_tail_with_another_gap_keeps_the_old_wording():
    periods = FULL_YEAR[:3] + FULL_YEAR[5:-1] + [(D(2026, 2, 1), D(2026, 3, 20))]
    cov = _cov(periods)
    assert len(cov.gaps) == 2
    assert all(NEW_WORDING not in g for g in cov.gaps)


def test_full_coverage_has_no_gap():
    cov = _cov(FULL_YEAR + [(D(2026, 3, 1), D(2026, 3, 31))])
    assert cov.gaps == []


# --- UI-39: CC Sort ---------------------------------------------------------

def _report(periods_by_card):
    rep = C.CompletenessReport("2025-26", [], [], [])
    for card, periods in periods_by_card.items():
        rep.cards.append(C.check_card(card, [(f"{card}{i}.pdf", P(s, e)) for i, (s, e) in enumerate(periods)],
                                      FS, FE))
    return rep


def _two_card_report():
    return _report({"Axis-A": FULL_YEAR[:-2], "HDFC-B": FULL_YEAR + [(D(2026, 3, 1), D(2026, 3, 31))]})


def _no_joined_lines(md: str):
    """In Markdown without line breaks, a line directly followed by a non-blank line is joined to it
    unless the next line is a list item."""
    lines = md.split("\n")
    for a, b in zip(lines, lines[1:]):
        if a.strip() and b.strip():
            assert b.startswith("- "), (a, b)


def test_panel_text_has_every_card_on_its_own_list_item():
    rep = _two_card_report()
    pt = rep.panel_text()
    assert pt.split("\n")[0] == rep.text().split("\n")[0]
    items = [ln for ln in pt.split("\n") if ln.startswith("- ")]
    assert len(items) == len(rep.text().split("\n")) - 1
    assert any("Axis-A" in ln for ln in items) and any("HDFC-B" in ln for ln in items)
    _no_joined_lines(pt)


def test_text_and_saved_report_are_unchanged_single_newline_format():
    rep = _two_card_report()
    t = rep.text()
    assert "\n\n" not in t and not any(ln.startswith("- ") for ln in t.split("\n"))
    assert all(ln.startswith("  ") for ln in t.split("\n")[1:3])
    assert rep.full_text().startswith(t)
    assert rep.panel_text() != t and rep.full_text() == rep.full_text()


def test_agent_message_is_markdown_safe_and_keeps_the_status_box():
    from agents.skill_cc_sort import agent as cc_agent
    from ui.tabs._generic import _colorize_status
    import inspect
    src = inspect.getsource(cc_agent)
    assert "report.panel_text()" in src and "report.text()" not in src.replace("report.panel_text()", "")
    msg = ("Sort completed successfully.\n\n- Input:  a\n- Output: b\n"
           "- Check the Decrypted_PDFs_Correct/ folder in the output location.\n\n"
           + _two_card_report().panel_text())
    html = _colorize_status(msg)
    assert 'class="rag-ok"' in html


# --- UI-39: CC Transactions blocks -----------------------------------------

SCRIPT = SRC / "agents" / "skill_cc_transactions" / "scripts" / "create_cc_transaction_list.py"


def _load():
    spec = importlib.util.spec_from_file_location("create_cc_transaction_list", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load()


def test_md_list_makes_each_line_its_own_item_and_keeps_the_heading():
    out = m.md_list("HEAD (2):\n  a | b\n  c | d\n  Total x: 1.00")
    assert out.split("\n")[0] == "HEAD (2):"
    assert out.split("\n")[1] == ""
    assert out.split("\n")[2:] == ["- a | b", "- c | d", "- Total x: 1.00"]


def test_md_list_leaves_a_single_line_alone():
    assert m.md_list("only a heading") == "only a heading"


def test_run_message_never_joins_lines(tmp_path, monkeypatch):
    lines = [
        "Statement Period : 1-Sep-2025 to 30-Sep-2025",
        "Opening Balance Purchase/Debits Finance Charges Payment/Credit Total Amount Due",
        "0.00 500.00 0.00 0.00 500.00",
        "5-Sep-2025 EXAMPLE SHOP 300.00 -",
        "6-Sep-2025 ANNUAL FEE 200.00 -",
    ]
    for card in ("SBM-One", "SBM-Two"):
        d = tmp_path / "in" / card
        d.mkdir(parents=True, exist_ok=True)
        (d / "s.pdf").write_bytes(b"%PDF-1.4 synthetic")
    monkeypatch.setattr(m, "read_lines", lambda p: lines)
    res = m.run_extraction(tmp_path / "in", tmp_path / "out.xlsx")
    msg = res.message
    assert msg.startswith("CARD FEES CHARGED")
    for block in msg.split("\n\n"):
        bl = block.split("\n")
        for a, b in zip(bl, bl[1:]):
            # a bare line followed by another bare line would be joined in the panel
            assert b.startswith("- ") or not (a.strip() and b.strip()), (a, b)
    assert not re.search(r"^  \S", msg, re.M)      # no two-space-indented run-together lines
    assert "- SBM-One: " in msg and "- SBM-Two: " in msg
