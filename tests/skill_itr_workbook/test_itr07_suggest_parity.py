"""
tests/skill_itr_workbook/test_itr07_suggest_parity.py -- ITR-07: the page's JS
suggester and tag_suggest.py must rank the same way.

The word scores come from Python, but the combination (sibling bonus, minimum
score, top-fraction cut, word-hit filter, tie order, HOLD) is implemented twice:
once in tag_suggest.suggest() and once in the page's suggestFor(). This test
feeds one synthetic book to both and compares tags AND order.

Synthetic data only. Node is required: without node the test SKIPS loudly
locally and FAILS when the CI environment variable is set.
"""
from __future__ import annotations

import json
import os
import sys
import warnings
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
SCRIPTS = SRC / "agents" / "skill_itr_workbook" / "scripts"
for p in (str(ROOT), str(SRC), str(SCRIPTS), str(SRC / "agents" / "skill_itr_workbook"),
          str(Path(__file__).resolve().parent), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

import _review_js as js  # noqa: E402
import tag_suggest  # noqa: E402
import tags  # noqa: E402
from agents.gnucash_accounts import GncAccount  # noqa: E402
from ui.tabs import itr_mapping_review as ui_mod  # noqa: E402
import run_sidecar  # noqa: E402


def _need_node():
    if js.have_node():
        return
    if os.environ.get("CI"):
        pytest.fail("node is not installed but CI is set: the JS-vs-Python suggestion "
                    "parity test must run in CI, so this is a failure, not a skip")
    msg = ("SKIPPED: node is not installed, so the JS-vs-Python suggestion parity "
           "test did NOT run on this machine")
    warnings.warn(msg)
    print("\n*** " + msg + " ***", file=sys.stderr)
    pytest.skip(msg)


# (path, type, saved tag or None). A saved tag makes the row a mapped sibling.
CASES = [
    # word hit on the leaf beats the generic word ("interest")
    ("Income:Interest:Savings Interest", "INCOME", None),
    # sibling-only: two siblings already carry OS_DIVIDEND, the leaf has no word hit
    ("Income:Misc:Alpha", "INCOME", "OS_DIVIDEND"),
    ("Income:Misc:Beta", "INCOME", "OS_DIVIDEND"),
    ("Income:Misc:Zeta", "INCOME", None),
    # a sibling on HOLD is never suggested
    ("Income:Parked:One", "INCOME", "HOLD"),
    ("Income:Parked:Two", "INCOME", None),
    # sibling bonus plus a word hit on the same tag
    ("Income:Div2:Acme", "INCOME", "OS_DIVIDEND"),
    ("Income:Div2:Beta Dividend", "INCOME", None),
    # sibling carries a different tag from the word hit: hits-only filter drops the sibling
    ("Income:Mixed:Anchor", "INCOME", "OS_INTEREST_BANK"),
    ("Income:Mixed:Dividend Received", "INCOME", None),
    # nothing matches
    ("Income:Nothing:Qwerty", "INCOME", None),
    # only generic words: below the minimum score
    ("Income:Generic:Tax Income", "INCOME", None),
    # health + school: several word-hit tags at different scores
    ("Expenses:Family:Medical Insurance", "EXPENSE", None),
    ("Expenses:Family:School Fee", "EXPENSE", None),
    ("Expenses:Family:Health", "EXPENSE", None),
    ("Expenses:Family:Donation Charity", "EXPENSE", None),
    # asset side: kind restriction applies to both
    ("Assets:Bank:Savings Bank Fixed Deposit", "BANK", None),
    ("Assets:Bank:Current", "BANK", "AL_CASH_BANK"),
    ("Assets:Bank:Old Cash", "BANK", None),
    ("Assets:Home:Flat", "ASSET", None),
    ("Assets:Home:Land Plot", "ASSET", None),
    ("Assets:Securities:Share Demat Mutual Fund", "ASSET", None),
    ("Liabilities:Cards:Credit Card", "LIABILITY", None),
    ("Liabilities:Loans:Home Loan", "LIABILITY", None),
]


def _book():
    out, ids = [], {}
    for i, (path, typ, _t) in enumerate(CASES):
        segs = path.split(":")
        for d in range(1, len(segs) + 1):
            sub = ":".join(segs[:d])
            if sub in ids:
                continue
            ids[sub] = "id%d" % len(ids)
            parent = ids[":".join(segs[:d - 1])] if d > 1 else None
            t = typ if d == len(segs) else {"Income": "INCOME", "Expenses": "EXPENSE",
                                            "Assets": "ASSET", "Liabilities": "LIABILITY"}[segs[0]]
            out.append(GncAccount(id=ids[sub], name=segs[d - 1], type=t, parent_id=parent,
                                  path=sub, special_flags=frozenset()))
    return out, ids


def _html_and_rows(tmp_path):
    book, ids = _book()
    root = tmp_path / "Data"
    (root / "itr" / "mappings").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / "itr" / "entities.yaml").write_text(yaml.safe_dump(
        {"ONE": {"name": "One", "pan": "AAAAA0000A", "status": "Individual",
                 "default_regime": "new"}}), encoding="utf-8")
    saved = [{"guid": ids[p], "path": p, "tag": t, "note": "n"} for p, _ty, t in CASES if t]
    (root / "itr" / "mappings" / "ONE.mapping.yaml").write_text(yaml.safe_dump(saved), encoding="utf-8")
    run_sidecar.write_run(root, "ONE", unmapped=run_sidecar.leaves_of(book))
    with patch("ui._config.data_root_dir", return_value=root), \
            patch.object(ui_mod, "_load_book_accounts", return_value=(Path(__file__), book)):
        html = ui_mod._load_review_data("ONE")
        rows, _info = ui_mod._collect_review("ONE")
    return html, rows


def _expose(html):
    """The page keeps suggestFor() inside its closure. Add one line, for the test
    only, that hands it (and the rows) to the harness. The product code is not
    touched; the suggester under test is the page's own function."""
    marker = "  treeRefreshAll();\n"
    assert html.count(marker) == 1
    return html.replace(marker, marker + "  globalThis.__probe = { suggestFor: suggestFor, rows: rows };\n")


def _js_suggestions(html, tmp_path):
    out = js.run_js(_expose(html), "itrmap", """
        const res = {};
        __probe.rows.forEach(r => {
          if (r._stale) return;
          res[r.path] = __probe.suggestFor([r]).map(s => [s.tag, Math.round(s.score * 1e6) / 1e6]);
        });
        return res;
    """, tmp_path, "")
    return out


def _py_suggestions(rows):
    by_parent = {}
    for r in rows:
        by_parent.setdefault(r["_pk"], []).append(r)
    out = {}
    for r in rows:
        if r["_stale"]:
            continue
        sib = [x["tag"] for x in by_parent[r["_pk"]]
               if x is not r and x.get("tag") and not x.get("_stale")]
        s = tag_suggest.suggest(r["path"], r["kind"], sib)
        out[r["path"]] = [[x["tag"], round(x["score"], 6)] for x in s]
    return out


def _mismatches(js_out, py_out):
    return {p: (js_out.get(p), py_out.get(p)) for p in set(js_out) | set(py_out)
            if js_out.get(p) != py_out.get(p)}


@pytest.fixture(scope="module")
def parity_data(tmp_path_factory):
    _need_node()
    tmp = tmp_path_factory.mktemp("parity")
    html, rows = _html_and_rows(tmp)
    return _js_suggestions(html, tmp), rows


def test_js_and_python_give_the_same_ranked_suggestions(parity_data):
    js_out, rows = parity_data
    py_out = _py_suggestions(rows)
    assert len(py_out) >= len(CASES)
    assert _mismatches(js_out, py_out) == {}


def test_the_cases_exercise_every_rule(parity_data):
    """Guard against the comparison going vacuous: each rule must actually bite."""
    js_out, rows = parity_data
    allp = {p: [t for t, _s in v] for p, v in js_out.items()}
    # word hit beats generic
    assert allp["Income:Interest:Savings Interest"][0] == "OS_INTEREST_SB"
    # sibling-only suggestion (no word hit at all)
    assert allp["Income:Misc:Zeta"] == ["OS_DIVIDEND"]
    # HOLD is never suggested, even with a sibling on HOLD
    assert all("HOLD" not in v for v in allp.values())
    assert allp["Income:Parked:Two"] == []
    # sibling + word hit stack on the same tag
    assert allp["Income:Div2:Beta Dividend"][0] == "OS_DIVIDEND"
    # hits-only filter: the bank-interest sibling does not appear next to a word hit
    assert "OS_INTEREST_BANK" not in allp["Income:Mixed:Dividend Received"]
    # no match, and below the minimum score, give nothing
    assert allp["Income:Nothing:Qwerty"] == []
    assert allp["Income:Generic:Tax Income"] == []
    # at most three
    assert max(len(v) for v in allp.values()) <= tag_suggest.MAX_SUGGESTIONS
    # the top-fraction cut dropped a candidate somewhere
    cut = False
    for r in rows:
        if r["_stale"]:
            continue
        ws = tag_suggest.word_scores(r["path"], r["kind"])
        keep = [t for t, _ in js_out[r["path"]]]
        if any(sc >= tag_suggest.MIN_SCORE and t not in keep for t, (sc, _h) in ws.items()):
            cut = True
    assert cut


def test_a_one_constant_mismatch_makes_the_comparison_fail(parity_data, monkeypatch):
    """Prove the comparison can fail: nudge one Python-side constant (in this
    test only) and the two suggesters must disagree somewhere."""
    js_out, rows = parity_data
    assert _mismatches(js_out, _py_suggestions(rows)) == {}
    for name, value in (("SIBLING_BONUS", 0.5), ("TOP_FRACTION", 0.95), ("MIN_SCORE", 3.5)):
        with monkeypatch.context() as m:
            m.setattr(tag_suggest, name, value)
            assert _mismatches(js_out, _py_suggestions(rows)), name + " change went unnoticed"


def test_missing_node_fails_under_ci_and_skips_loudly_otherwise(monkeypatch, capsys):
    monkeypatch.setattr(js, "have_node", lambda: False)
    monkeypatch.setenv("CI", "true")
    with pytest.raises(pytest.fail.Exception):
        _need_node()
    monkeypatch.delenv("CI")
    with pytest.warns(UserWarning, match="did NOT run"):
        with pytest.raises(pytest.skip.Exception):
            _need_node()
    assert "did NOT run" in capsys.readouterr().err
