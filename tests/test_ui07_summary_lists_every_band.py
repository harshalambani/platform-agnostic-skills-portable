"""
UI-07 -- the run summary's confidence breakdown lists every band, including
Suspense and the MAP-19 'AI pass stopped' rows, so the listed counts add up to
the rows written.
"""
from __future__ import annotations

import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402


def _listed_counts(summary: str):
    block = summary.split("**Confidence breakdown:**", 1)[1]
    out = {}
    for line in block.splitlines():
        mt = re.match(r"^- ([^:`]+): (\d+) \(\d+%\)$", line.strip())
        if mt:
            out[mt.group(1)] = int(mt.group(2))
    return out


def test_helper_lists_every_band_and_sums_to_total():
    counts = {"high": 3, "medium": 2, "override": 1, "suspense": 4, "none": 0, "weird": 1}
    lines = m._confidence_breakdown_lines(counts, 11, stopped=0)
    text = "\n".join(lines)
    for label in ("High", "Medium", "Low", "Weak", "Smart", "History", "LLM",
                  "Override", "Suspense", "No match"):
        assert f"- {label}:" in text
    assert "- weird: 1" in text
    assert sum(int(re.search(r": (\d+) ", l).group(1)) for l in lines) == 11


def test_stopped_rows_are_their_own_line_and_still_sum():
    counts = {"high": 1, "suspense": 6}
    lines = m._confidence_breakdown_lines(counts, 7, stopped=4)
    got = {re.match(r"- (.+?): (\d+)", l).group(1): int(re.match(r"- (.+?): (\d+)", l).group(2)) for l in lines}
    assert got["Suspense"] == 2
    assert got[f"Suspense, {m._LLM_STOPPED_MARKER}"] == 4
    assert sum(got.values()) == 7


def test_stopped_count_never_exceeds_suspense_total():
    """NEGATIVE: a bad stopped figure cannot make a band negative or break the sum."""
    lines = m._confidence_breakdown_lines({"suspense": 2}, 2, stopped=9)
    nums = [int(re.search(r": (-?\d+) ", l).group(1)) for l in lines]
    assert min(nums) >= 0 and sum(nums) == 2


def test_run_summary_counts_sum_to_rows_written_including_suspense(tmp_path, monkeypatch):
    txns = [fx.txn_xml("CAFE ALPHA ORDER", "2025-06-10", [(fx.HDFC1, -10000), ("groc", 10000)])]
    book = fx.write_book(tmp_path / "b.gnucash", fx.standard_accounts(), txns)
    monkeypatch.chdir(tmp_path)
    rows = [("2025-08-01", "CAFE ALPHA ORDER", "", "100.00")] + \
           [("2025-08-02", f"ZZ UNKNOWN THING {i}", "", "50.00") for i in range(4)]
    csv_in = fx.canonical_csv(tmp_path / "in.csv", rows)
    out = tmp_path / "out.csv"
    summary = m.run(book, csv_in, str(out), config_path=None, bank_name="HDFC",
                    gnucash_bank_account=fx.P_HDFC1)
    with open(out, newline="", encoding="utf-8") as f:
        written = list(csv.DictReader(f))
    listed = _listed_counts(summary)
    assert listed["Suspense"] >= 1                       # Suspense is shown, not hidden
    assert sum(listed.values()) == len(written) == 5
    assert listed["Suspense"] == sum(1 for r in written if r["Confidence"] == "suspense")
