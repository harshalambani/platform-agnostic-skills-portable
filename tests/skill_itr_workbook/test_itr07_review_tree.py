"""
tests/skill_itr_workbook/test_itr07_review_tree.py -- ITR-07: the mapping review
as a book-ordered tree with a suggestion / coverage side panel.

Synthetic data only. The book is faked by patching _load_book_accounts.
"""
from __future__ import annotations

import hashlib
import json
import sys
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
import run_sidecar  # noqa: E402
import tag_suggest  # noqa: E402
import tags  # noqa: E402
from agents.gnucash_accounts import GncAccount  # noqa: E402
from ui import _review_engine as eng  # noqa: E402
from ui.tabs import itr_mapping_review as ui_mod  # noqa: E402

needs_node = pytest.mark.skipif(not js.have_node(), reason="node not installed")

PRE = """
globalThis.__alerts = [];
globalThis.alert = function (m) { __alerts.push(String(m)); };
globalThis.prompt = function (t, d) { return 'because'; };
"""

# Hash of the engine's output with tree=None for two generic specs, taken
# before the tree mode existed. It pins that the other three engine tabs
# (gnucash_review, krc_gnucash_review, tds_journal_review) are unchanged.
GOLDEN = "e8378bc070681084afb835fea143d422076cf6127006b03d1e381c027806ebf5"


def _acct(i, path, typ, parent=None, flags=()):
    return GncAccount(id=i, name=path.rsplit(":", 1)[-1], type=typ, parent_id=parent,
                      path=path, special_flags=frozenset(flags))


def _book():
    return [
        _acct("A", "Assets", "ASSET"),
        _acct("AB", "Assets:Bank", "BANK", "A"),
        _acct("sav", "Assets:Bank:Savings", "BANK", "AB"),
        _acct("cur", "Assets:Bank:Current", "BANK", "AB"),
        _acct("ins", "Assets:Car Insurance", "ASSET", "A"),
        _acct("carp", "Assets:Car", "ASSET", "A"),
        _acct("car", "Assets:Car:Fuel", "ASSET", "carp"),
        _acct("I", "Income", "INCOME"),
        _acct("ID", "Income:Dividends", "INCOME", "I"),
        _acct("acme", "Income:Dividends:Acme", "INCOME", "ID"),
        _acct("beta", "Income:Dividends:Beta", "INCOME", "ID"),
        _acct("hid", "Assets:Old Hidden", "ASSET", "A", ("hidden",)),
        _acct("ph", "Assets:Placeholder Leaf", "ASSET", "A", ("placeholder",)),
    ]


def _setup(tmp_path, with_book=True):
    root = tmp_path / "Data"
    (root / "itr" / "mappings").mkdir(parents=True)
    (root / "outputs").mkdir()
    (root / "itr" / "entities.yaml").write_text(yaml.safe_dump(
        {"ONE": {"name": "One", "pan": "AAAAA0000A", "status": "Individual", "default_regime": "new"}}),
        encoding="utf-8")
    (root / "itr" / "mappings" / "ONE.mapping.yaml").write_text(yaml.safe_dump([
        {"guid": "sav", "path": "Assets:Bank:Savings", "tag": "AL_CASH_BANK", "note": "n"},
        {"guid": "beta", "path": "Income:Dividends:Beta", "tag": "HOLD", "note": "ask CA"},
        {"guid": "gone", "path": "Assets:Gone", "tag": "AL_CASH_BANK", "note": "n"},
    ]), encoding="utf-8")
    # ITR-14: the run's own account list decides what is listed.
    run_sidecar.write_run(root, "ONE", unmapped=run_sidecar.leaves_of(_book()))
    return root, ((Path(__file__), _book()) if with_book else (None, None))


def _html(tmp_path, with_book=True):
    root, book = _setup(tmp_path, with_book)
    with patch("ui._config.data_root_dir", return_value=root), \
            patch.object(ui_mod, "_load_book_accounts", return_value=book):
        return ui_mod._load_review_data("ONE")


def _rows(tmp_path, with_book=True):
    root, book = _setup(tmp_path, with_book)
    with patch("ui._config.data_root_dir", return_value=root), \
            patch.object(ui_mod, "_load_book_accounts", return_value=book):
        rows, info = ui_mod._collect_review("ONE")
    return {r["guid"]: r for r in rows}, info, rows


# --- vocabulary tables --------------------------------------------------------------

def test_every_tag_has_a_label_and_exactly_one_group():
    seen = [t for _g, ts in tag_suggest.GROUPS for t in ts if t != tags.HOLD]
    assert sorted(seen) == sorted(tags.TAGS)
    assert len(seen) == len(set(seen))
    for t in tags.TAGS:
        assert tag_suggest.TAG_LABELS.get(t)
    for t in tag_suggest.TAG_LABELS:
        assert t in tags.TAGS or t == tags.HOLD


def test_balance_sheet_section_comes_first():
    assert tag_suggest.GROUPS[0][1][0].startswith("AL_")


def test_book_order_is_by_segment_not_by_text():
    assert tag_suggest.book_key("A:Car Insurance") > tag_suggest.book_key("A:Car:Fuel")


def test_hold_is_never_suggested():
    for path in ("Assets:Hold", "Income:Hold on"):
        for kind in (tags.KIND_ASSET, tags.KIND_INCOME_EXPENSE):
            assert all(s["tag"] != "HOLD" for s in tag_suggest.suggest(path, kind, {}))


def test_suggestions_are_at_most_three_with_reasons():
    s = tag_suggest.suggest("Assets:Bank:Savings", tags.KIND_ASSET, {"AL_CASH_BANK": 2})
    assert 1 <= len(s) <= 3
    assert all(x["why"] for x in s)


def test_suggestions_only_offer_tags_valid_for_the_side():
    allowed = set(tags.allowed_tags_for_kind(tags.KIND_ASSET))
    for s in tag_suggest.suggest("Assets:Dividends receivable", tags.KIND_ASSET, {}):
        assert s["tag"] in allowed


# --- rows ------------------------------------------------------------------------

def test_stale_hidden_and_order(tmp_path):
    by, info, rows = _rows(tmp_path)
    assert by["gone"]["_stale"] is True
    assert by["sav"]["_stale"] is False
    assert "hid" not in by and "ph" not in by          # never listed
    assert info["hidden"] >= 2 and info["has_book"] is True
    assert [r["_ord"] for r in rows] == list(range(len(rows)))
    paths = [r["path"] for r in rows if not r["_stale"]]
    assert paths.index("Assets:Car:Fuel") < paths.index("Assets:Car Insurance")


def test_no_book_means_nothing_is_stale(tmp_path):
    by, info, _ = _rows(tmp_path, with_book=False)
    assert info["has_book"] is False
    assert not any(r["_stale"] for r in by.values())


def test_stale_row_gets_grey_badge_and_no_tags():
    row = {"unmapped": False, "needs_review": False, "tag": "AL_CASH_BANK", "suggested": None,
           "note": "", "_stale": True}
    ui_mod._row_presentation(row, {})
    assert row["_tags"] == ["stale"]
    assert row["_badges"]["tag"]["text"] == "NOT IN BOOK"


def test_hold_row_is_not_tagged_mapped():
    row = {"unmapped": True, "needs_review": False, "tag": None, "suggested": None,
           "note": "", "held": True, "hold_reason": "x"}
    ui_mod._row_presentation(row, {})
    assert "mapped" not in row["_tags"]


def test_status_options_drop_confirmed_and_add_tax_and_stale(tmp_path):
    html = _html(tmp_path)
    assert "Affects tax only" in html and "Not in book" in html
    assert "Confirmed only" not in html


# --- the other engine tabs are unchanged -------------------------------------------

def test_engine_without_tree_is_byte_identical_to_before():
    rows = [{"a": "x", "b": "y", "_tags": ["t"], "guid": "1"}, {"a": "p", "b": "q", "guid": "2"}]
    specs = [
        eng.ReviewSpec(app_id="g1", columns=[eng.Column("a", "A"), eng.Column("b", "B")],
                       target_col="b", payload_var="pv"),
        eng.ReviewSpec(app_id="g2", columns=[eng.Column("a", "A", edit_key="b"), eng.Column("b", "B")],
                       target_col="b", payload_var="pv", allow_delete=True, allow_exclude=True,
                       flag_toggle=("b", "s", "c", "v"), status_options=[("t", "T")],
                       apply_matching_on="a", kind_col="a", status_col="b"),
    ]
    out = "".join(eng.build_html(s, rows) for s in specs)
    assert hashlib.sha256(out.encode()).hexdigest() == GOLDEN
    assert "TREE" not in out and "-side-cov" not in out and "treeSide" not in out


# --- UI behaviour (node) ------------------------------------------------------------

HELP = """
function isHead(x) { return Number.isNaN(x.idx); }
function leaves() { return view(APP).filter(x => !isHead(x)); }
function idxOf(part) { return leaves().find(x => x.cells.some(c => c.includes(part))).idx; }
function headBox(part) {
  const h = $id(APP + '-tbody').children.find(t => t.classList._s.has('sech') && (t.children[0].children.some(c => (c.textContent || '') === part)));
  return h.children[0].children.find(c => c.type === 'checkbox');
}
"""
PV = json.dumps(ui_mod.PAYLOAD_VAR)


def _run(tmp_path, scenario):
    return js.run_js(_html(tmp_path), "itrmap", HELP + scenario, tmp_path, PRE)


@needs_node
def test_grouped_view_has_headings_and_flat_view_has_none(tmp_path):
    out = _run(tmp_path, """
        const g = view(APP).filter(isHead).length;
        $id(APP + '-v-flat').onclick();
        return {g, f: view(APP).filter(isHead).length, rows: leaves().length};
    """)
    assert out["g"] > 0 and out["f"] == 0 and out["rows"] >= 5


@needs_node
def test_heading_tick_selects_leaves_but_is_never_saved(tmp_path):
    out = _run(tmp_path, """
        headBox('Dividends').onclick({});
        const sel = leaves().filter(x => x.cls.includes('selected')).length;
        return {sel, p: payload(%s)};
    """ % PV)
    assert out["sel"] >= 1
    assert out["p"].get("changes", []) == []
    blob = json.dumps(out["p"])
    assert "_head" not in blob and "_leaves" not in blob


@needs_node
def test_heading_tick_never_selects_stale_rows(tmp_path):
    out = _run(tmp_path, """
        headBox('Assets').onclick({});
        return {stale: leaves().filter(x => x.cls.includes('stale')).map(x => x.cls.includes('selected')),
                sel: leaves().filter(x => x.cls.includes('selected')).length};
    """)
    assert out["sel"] >= 1
    assert out["stale"] and not any(out["stale"])


@needs_node
def test_coverage_line_and_not_filing_ready(tmp_path):
    out = _run(tmp_path, "return {cov: $id(APP + '-cov').textContent, ready: $id(APP + '-ready').textContent};")
    assert "not in book" in out["cov"] and "on hold" in out["cov"]
    assert "hidden or placeholder" in out["cov"]
    assert "NOT FILING-READY" in out["ready"]


@needs_node
def test_hold_is_not_counted_as_mapped_and_stale_excluded(tmp_path):
    out = _run(tmp_path, "return {cov: $id(APP + '-cov').textContent};")
    assert " 1 mapped" in out["cov"] and " 1 on hold" in out["cov"]


@needs_node
def test_likely_hint_is_display_only_never_counted_or_saved(tmp_path):
    out = _run(tmp_path, """
        return {cov: $id(APP + '-cov').textContent,
                hinted: leaves().filter(x => x.cells.some(c => c.includes('likely:'))).length,
                p: payload(%s)};
    """ % PV)
    assert out["hinted"] >= 1
    assert out["p"].get("changes", []) == []
    assert " 1 mapped" in out["cov"]


@needs_node
def test_stale_row_cannot_take_a_tag(tmp_path):
    out = _run(tmp_path, """
        select(APP, leaves().find(x => x.cls.includes('stale')).idx);
        const btn = $id(APP + '-side-apply');
        return {disabled: btn.disabled, p: payload(%s)};
    """ % PV)
    assert out["disabled"] is True
    assert out["p"].get("changes", []) == []


@needs_node
def test_side_panel_never_offers_a_cross_section_tag(tmp_path):
    out = _run(tmp_path, """
        select(APP, idxOf('Current'));
        const sug = $id(APP + '-side-sug')._h || '';
        const list = $id(APP + '-side-list')._h || '';
        const picks = (sug.match(/data-pick="[A-Z_]+"/g) || []).map(s => s.slice(11, -1));
        return {picks, list};
    """)
    allowed = set(tags.allowed_tags_for_kind(tags.KIND_ASSET))
    assert out["picks"] and len(out["picks"]) <= 3
    assert all(t in allowed for t in out["picks"])
    assert "OS_DIVIDEND" not in out["list"]


@needs_node
def test_named_apply_button_and_double_click_apply(tmp_path):
    out = _run(tmp_path, """
        select(APP, idxOf('Current'));
        const sug = $id(APP + '-side-sug')._h || '';
        const tag = sug.match(/data-pick="([A-Z_]+)"/)[1];
        $id(APP + '-side').onclick({ target: { dataset: { pick: tag }, parentNode: null } });
        const label = $id(APP + '-side-apply').textContent;
        $id(APP + '-side-apply').onclick();
        return {tag, label, p: payload(%s), recent: $id(APP + '-side-recent')._h || ''};
    """ % PV)
    assert out["label"].startswith("Apply ") and "1 account" in out["label"]
    assert len(out["p"]["changes"]) == 1
    assert tags.is_valid_tag(out["p"]["changes"][0]["tag"])
    assert out["tag"] in out["recent"]


@needs_node
def test_coverage_click_filters_and_clears(tmp_path):
    out = _run(tmp_path, """
        const n = () => leaves().length;
        const all = n();
        const el = $id(APP + '-side');
        el.onclick({ target: { dataset: { cov: 'unmapped', name: 'Unmapped' }, parentNode: null } });
        const un = n();
        el.onclick({ target: { dataset: { cov: 'clear', name: '' }, parentNode: null } });
        return {all, un, back: n()};
    """)
    assert 0 < out["un"] < out["all"] and out["back"] == out["all"]


@needs_node
def test_tax_filter_hides_non_tax_rows(tmp_path):
    out = _run(tmp_path, """
        const all = leaves().length;
        setStatus(APP, 'tax');
        return {all, tax: leaves().length};
    """)
    assert out["tax"] < out["all"]


# --- approved colours (mockup values) ---------------------------------------------

def test_tree_css_uses_the_approved_kind_colours_and_no_old_ones():
    from ui import _review_tree as rt
    css = rt.CSS.lower()
    for v in ("#f0a868", "#6fd6cf", "#b9a6f5",      # dark
              "#9a4d00", "#0b6e69", "#5b3fb0"):     # light
        assert v in css
    for old in ("#34d399", "#60a5fa", "#c084fc"):
        assert old not in css
    # the legend, the pills and the row markers all read the tokens
    assert "var(--inc)" in css and "var(--ded)" in css and "var(--paid)" in css
    html = _html_for_colours()
    assert "#34d399" not in html.lower() and "#f0a868" in html.lower()


def _html_for_colours():
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        return _html(Path(d))


# --- depth indent in the grouped view ----------------------------------------------

def _deep_book():
    segs = ["Assets", "L1", "L2", "L3", "L4", "L5", "L6", "L7", "L8", "Deep Leaf"]
    out, parent = [], None
    for i in range(len(segs)):
        path = ":".join(segs[:i + 1])
        out.append(_acct("d%d" % i, path, "ASSET", parent))
        parent = "d%d" % i
    return out + _book()


def _run_deep(tmp_path, scenario):
    root, _b = _setup(tmp_path)
    run_sidecar.write_run(root, "ONE", unmapped=run_sidecar.leaves_of(_deep_book()))
    with patch("ui._config.data_root_dir", return_value=root), \
            patch.object(ui_mod, "_load_book_accounts", return_value=(Path(__file__), _deep_book())):
        html = ui_mod._load_review_data("ONE")
    return js.run_js(html, "itrmap", HELP + scenario, tmp_path, PRE)


@needs_node
def test_grouped_rows_are_indented_by_depth_and_capped(tmp_path):
    out = _run_deep(tmp_path, """
        const lv = (x) => { const m = /data-depth="([0-9]+)"/.exec(x.cells[0]); return m ? Number(m[1]) : 0; };
        const g = leaves();
        const deep = g.find(x => x.cells[0].includes('Deep Leaf'));
        const shallow = g.find(x => x.cells[0].includes('Assets:Bank:Savings'));
        const heads = $id(APP + '-tbody').children.filter(t => t.classList._s.has('sech'))
                         .map(t => t.children[0].style.paddingLeft);
        return {deep: lv(deep), shallow: lv(shallow), heads};
    """)
    assert out["shallow"] == 2          # Assets > Bank > Savings
    assert out["deep"] == 6             # nine levels deep, capped
    assert all(h.endswith("em") for h in out["heads"])
    assert max(float(h[:-2]) for h in out["heads"]) <= 6.5 + 1e-9


@needs_node
def test_flat_view_rows_carry_no_depth_indent(tmp_path):
    out = _run_deep(tmp_path, """
        $id(APP + '-v-flat').onclick();
        return {cells: leaves().map(x => x.cells[0]), heads: view(APP).filter(isHead).length};
    """)
    assert out["cells"] and out["heads"] == 0
    assert not any('class="ind"' in c or "data-depth" in c for c in out["cells"])


def test_other_review_tabs_get_no_indent_code():
    from ui import _review_tree as rt
    out = "".join(eng.build_html(eng.ReviewSpec(app_id="g1", columns=[eng.Column("a", "A")],
                                               target_col="a", payload_var="pv"),
                                [{"a": "x", "guid": "1"}]) for _ in range(1))
    assert "data-depth" not in out and "indentLevel" not in out and ".ind " not in out
    assert rt.NO_TOKENS["%%TREE_CELL%%"] == ""


# --- UI-37: Apply stays in view -----------------------------------------------------

def test_ui37_apply_sits_in_a_sticky_bar_and_panel_is_sticky(tmp_path):
    html = _html(tmp_path)
    assert 'class="applybar"' in html and "position: sticky; bottom: -8px" in html
    bar = html.index('class="applybar"')
    assert bar < html.index("itrmap-side-apply") < html.index("</div>", bar)
    assert "position: sticky; top: 8px" in html


def _css_rule(html, selector):
    import re
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", html)
    assert m, selector
    return m.group(1)


def test_ui37_applybar_offset_cancels_panel_padding_so_nothing_shows_below_it(tmp_path):
    import re
    html = _html(tmp_path)
    panel = _css_rule(html, "-app .tside")
    bar = _css_rule(html, "-app .tside .applybar")
    pad = int(re.search(r"padding:\s*(\d+)px", panel).group(1))
    off = re.search(r"bottom:\s*(-?\d+)px", bar)
    assert off, "the bar must have a bottom offset"
    assert int(off.group(1)) == -pad                 # flush on the panel edge
    assert "bottom: 0" not in bar                    # the regression: 0 inside a padded panel
    assert "position: sticky" in bar                 # UI-37 original fix still in place
    assert re.search(r"margin:\s*8px -8px -8px", bar)   # bar still spans the panel width


@needs_node
def test_ui37_apply_disabled_with_nothing_selected_and_label_names_tag(tmp_path):
    out = _run(tmp_path, """
        const none = $id(APP + '-side-apply').disabled;
        select(APP, idxOf('Current'));
        const sug = $id(APP + '-side-sug')._h || '';
        const tag = sug.match(/data-pick="([A-Z_]+)"/)[1];
        $id(APP + '-side').onclick({ target: { dataset: { pick: tag }, parentNode: null } });
        const btn = $id(APP + '-side-apply');
        const label = btn.textContent;
        btn.onclick();
        return {none, tag, label, p: payload(%s)};
    """ % PV)
    assert out["none"] is True
    assert tag_suggest.TAG_LABELS[out["tag"]] in out["label"]
    assert [c["tag"] for c in out["p"]["changes"]] == [out["tag"]]


@needs_node
def test_ui37_apply_never_applies_a_tag_other_than_the_one_shown(tmp_path):
    out = _run(tmp_path, """
        select(APP, idxOf('Current'));
        const picks = ($id(APP + '-side-sug')._h.match(/data-pick="([A-Z_]+)"/g) || []).map(s => s.slice(11, -1));
        $id(APP + '-side').onclick({ target: { dataset: { pick: picks[0] }, parentNode: null } });
        const shown = $id(APP + '-side-apply').textContent;
        $id(APP + '-side-apply').onclick();
        return {picks, shown, p: payload(%s)};
    """ % PV)
    applied = {c["tag"] for c in out["p"]["changes"]}
    assert applied == {out["picks"][0]}
    assert tag_suggest.TAG_LABELS[out["picks"][0]] in out["shown"]
    for other in out["picks"][1:]:
        if other != out["picks"][0] and tag_suggest.TAG_LABELS[other] not in tag_suggest.TAG_LABELS[out["picks"][0]]:
            assert tag_suggest.TAG_LABELS[other] not in out["shown"]
