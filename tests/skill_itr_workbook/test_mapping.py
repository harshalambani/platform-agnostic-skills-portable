"""
tests/skill_itr_workbook/test_mapping.py -- Batch 3 tests: the tag
vocabulary, config schemas/loaders, the mapping resolution engine, the
optional LLM suggestion path, and the agent.py fail-loud + learning-loop
wiring (plan sections 3, 3.1, 4.1, 4.2). Fully offline; synthetic fixtures
only. Real-corpus tests are behind @pytest.mark.local_samples and skip
when Data/GNUCashReports/ is absent, so CI never touches real data, and
they never hand-tag a real account (the whole point is that unmapped > 0
there -- that's the learning loop working).
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
SCRIPTS = SRC / "agents" / "skill_itr_workbook" / "scripts"
AGENT_DIR = SRC / "agents" / "skill_itr_workbook"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_SAMPLES_DIR = ROOT / "Data" / "GNUCashReports"

for p in (str(SCRIPTS), str(AGENT_DIR), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import parse_eguile as pe  # noqa: E402
import fixture_gen  # noqa: E402
import tags as tag_vocab  # noqa: E402
import configs  # noqa: E402
import mapping  # noqa: E402
import suggest  # noqa: E402
import bootstrap_mappings as bm  # noqa: E402
import agent  # noqa: E402
import rules as rules_engine  # noqa: E402

SYN_IND_MAPPING = FIXTURES / "syn_ind.mapping.yaml"
SYN_IND_UNMAPPED_MAPPING = FIXTURES / "syn_ind_unmapped.mapping.yaml"
SYN_HUF_MAPPING = FIXTURES / "syn_huf.mapping.yaml"


@pytest.fixture(scope="module")
def syn_ind_tree():
    return pe.parse_html(fixture_gen.build_syn_ind_html())


@pytest.fixture(scope="module")
def syn_huf_tree():
    return pe.parse_html(fixture_gen.build_syn_huf_html())


# ---------------------------------------------------------------------------
# tags.py
# ---------------------------------------------------------------------------

def test_tags_re_and_bs_sets_are_disjoint_except_either():
    both = tag_vocab.RE_TAGS & tag_vocab.BS_TAGS
    for tag in both:
        assert tag_vocab.TAGS[tag].sheet == tag_vocab.EITHER


def test_tags_al_foreign_is_a_flag_not_a_tag():
    assert "AL_FOREIGN" not in tag_vocab.TAGS
    assert tag_vocab.is_valid_flag("AL_FOREIGN")


# ---------------------------------------------------------------------------
# configs.py loader validation
# ---------------------------------------------------------------------------

def test_load_mapping_rejects_unknown_tag(tmp_path):
    bad = tmp_path / "bad.mapping.yaml"
    bad.write_text("- guid: abc123\n  path: X/Y\n  tag: NOT_A_REAL_TAG\n", encoding="utf-8")
    with pytest.raises(configs.MappingValidationError):
        configs.load_mapping(bad)


def test_load_mapping_rejects_duplicate_guid(tmp_path):
    dup = tmp_path / "dup.mapping.yaml"
    dup.write_text(
        "- guid: abc123\n  path: X/Y\n  tag: AL_CASH_BANK\n"
        "- guid: abc123\n  path: X/Z\n  tag: AL_SECURITIES\n",
        encoding="utf-8",
    )
    with pytest.raises(configs.MappingValidationError):
        configs.load_mapping(dup)


def test_load_mapping_flags_path_drift_as_warning_not_failure(tmp_path):
    drifted = tmp_path / "drift.mapping.yaml"
    drifted.write_text("- guid: abc123\n  path: Old/Path\n  tag: AL_CASH_BANK\n", encoding="utf-8")
    result = configs.load_mapping(drifted, known_paths={"abc123": "New/Path"})
    assert "abc123" in result.entries  # still loads -- GUID match wins
    assert any("abc123" in w for w in result.warnings)


def test_load_entities_example_and_scrips_example():
    entities = configs.load_entities(ROOT / "Data" / "itr" / "entities.example.yaml")
    assert "SYN-IND" in entities and entities["SYN-IND"].status == "Individual"
    assert "SYN-HUF" in entities and entities["SYN-HUF"].status == "HUF"

    scrips = configs.load_scrips(ROOT / "Data" / "itr" / "scrips.example.yaml")
    assert scrips["SYNCORP.NS"].isin == "INE000A00000"


def test_entity_profile_doi_field_loads_and_resolves_no_age_class():
    # CF6: HUF profile carries doi (date of incorporation), never dob; age
    # class must resolve to 'general' regardless of doi's value.
    entities = configs.load_entities(ROOT / "Data" / "itr" / "entities.example.yaml")
    huf = entities["SYN-HUF"]
    assert huf.doi == "1975-06-15"
    assert huf.dob is None
    assert rules_engine.resolve_age_class(huf.status, huf.dob, date(2025, 3, 31)) == "general"


def test_load_entities_rejects_missing_required_field(tmp_path):
    bad = tmp_path / "entities.yaml"
    bad.write_text("SYN-X:\n  name: X\n  pan: AAAAA0000A\n", encoding="utf-8")  # missing status
    with pytest.raises(configs.ConfigValidationError):
        configs.load_entities(bad)


# ---------------------------------------------------------------------------
# mapping.py -- nearest-ancestor resolution
# ---------------------------------------------------------------------------

def test_nearest_ancestor_resolution_with_child_override(syn_ind_tree):
    known_paths = {n.guid: n.path for n in syn_ind_tree.all_nodes() if n.guid}
    loaded = configs.load_mapping(SYN_IND_MAPPING, known_paths=known_paths)
    result = mapping.resolve_tree(syn_ind_tree, loaded)
    assert not result.blocked

    # Business Expenses is tagged directly (own GUID) -- not inherited.
    business_expenses_guid = fixture_gen._guid("SYN-IND:Business Expenses")
    assert result.resolved[business_expenses_guid].tag == "BUS_EXPENSE"

    # Every mapped leaf's tag must be a real vocabulary tag.
    for leaf in result.resolved.values():
        assert tag_vocab.is_valid_tag(leaf.tag)


def test_nearest_ancestor_child_tag_overrides_parent_tag():
    """A tag on a parent branch applies to the whole subtree unless a
    child carries its own tag (plan section 3.1)."""
    parent = pe.AccountNode(guid="parent-guid", name="Parent", depth=0, section="Assets", path="Assets/Parent")
    child_inherits = pe.AccountNode(guid="child-a", name="A", depth=1, section="Assets",
                                     path="Assets/Parent/A", total=100.0)
    child_overrides = pe.AccountNode(guid="child-b", name="B", depth=1, section="Assets",
                                      path="Assets/Parent/B", total=200.0)
    parent.children = [child_inherits, child_overrides]
    parent.total = 300.0
    tree = pe.ParsedBalanceSheet(section_roots={"Assets": [parent]}, section_totals={}, imbalance=0.0)

    loaded = configs.MappingLoadResult(
        entries={
            "parent-guid": configs.MappingEntry(guid="parent-guid", path="Assets/Parent", tag="AL_CASH_BANK"),
            "child-b": configs.MappingEntry(guid="child-b", path="Assets/Parent/B", tag="AL_SECURITIES"),
        },
        warnings=[],
    )
    result = mapping.resolve_tree(tree, loaded)
    assert not result.blocked
    assert result.resolved["child-a"].tag == "AL_CASH_BANK"   # inherited
    assert result.resolved["child-b"].tag == "AL_SECURITIES"  # overridden


def test_resolution_survives_account_rename_matching_is_guid_based_not_path_based(tmp_path):
    """Root-cause gate: confirm matching is GUID-based, not name/path-based
    (2026-07-19 mapping-precedence prompt, item 1a candidate cause). A GnuCash
    account renamed after the mapping file was written must still resolve
    correctly by GUID alone -- the mapping's stale `path` field is cosmetic
    and must not prevent resolution. The end-to-end round trip (load with
    known_paths -> resolve) both surfaces a rename warning AND still applies
    the tag -- rename detection is advisory, never a resolution failure."""
    renamed_leaf = pe.AccountNode(guid="stable-guid-123", name="Renamed Account", depth=0,
                                   section="Assets", path="Assets/Renamed Account", total=42.0)
    tree = pe.ParsedBalanceSheet(section_roots={"Assets": [renamed_leaf]}, section_totals={}, imbalance=0.0)

    mapping_file = tmp_path / "renamed.mapping.yaml"
    mapping_file.write_text(
        "- guid: stable-guid-123\n  path: Assets/Old Account Name\n  tag: AL_CASH_BANK\n",
        encoding="utf-8",
    )
    known_paths = {n.guid: n.path for n in tree.all_nodes() if n.guid}
    loaded = configs.load_mapping(mapping_file, known_paths=known_paths)
    assert any("stable-guid-123" in w for w in loaded.warnings)  # rename detected

    result = mapping.resolve_tree(tree, loaded)
    assert not result.blocked
    assert result.resolved["stable-guid-123"].tag == "AL_CASH_BANK"  # but still resolves by GUID


# ---------------------------------------------------------------------------
# mapping.py -- section-aware tag validation (B4 carry-forward)
# ---------------------------------------------------------------------------

def test_equity_capital_and_trading_are_bs_side_but_excluded_from_al_rollup():
    assert "EQUITY_CAPITAL" in tag_vocab.BS_TAGS
    assert "TRADING" in tag_vocab.BS_TAGS
    assert "EQUITY_CAPITAL" not in tag_vocab.AL_TAGS
    assert "TRADING" not in tag_vocab.AL_TAGS


def test_section_aware_validation_accepts_equity_capital_on_equity_leaf():
    equity = pe.AccountNode(guid="cap", name="Capital Account", depth=0, section="Equity",
                             path="Equity/Capital Account", total=500.0)
    tree = pe.ParsedBalanceSheet(section_roots={"Equity": [equity]}, section_totals={}, imbalance=0.0)
    loaded = configs.MappingLoadResult(
        entries={"cap": configs.MappingEntry(guid="cap", path=equity.path, tag="EQUITY_CAPITAL")},
        warnings=[],
    )
    result = mapping.resolve_tree(tree, loaded)
    assert not result.blocked
    assert result.resolved["cap"].tag == "EQUITY_CAPITAL"


def test_section_aware_validation_rejects_al_liability_on_equity_leaf():
    """Carry-forward bug this batch fixes: an Equity leaf tagged AL_LIABILITY
    (the old starter-mapping mistake) must now hard-fail, not silently pass."""
    equity = pe.AccountNode(guid="cap", name="Capital Account", depth=0, section="Equity",
                             path="Equity/Capital Account", total=500.0)
    tree = pe.ParsedBalanceSheet(section_roots={"Equity": [equity]}, section_totals={}, imbalance=0.0)
    loaded = configs.MappingLoadResult(
        entries={"cap": configs.MappingEntry(guid="cap", path=equity.path, tag="AL_LIABILITY")},
        warnings=[],
    )
    with pytest.raises(configs.MappingValidationError):
        mapping.resolve_tree(tree, loaded)


def test_section_aware_validation_rejects_al_cash_bank_on_liability_leaf():
    loan = pe.AccountNode(guid="loan", name="Bank Loan", depth=0, section="Liability",
                           path="Liability/Bank Loan", total=500.0)
    tree = pe.ParsedBalanceSheet(section_roots={"Liability": [loan]}, section_totals={}, imbalance=0.0)
    loaded = configs.MappingLoadResult(
        entries={"loan": configs.MappingEntry(guid="loan", path=loan.path, tag="AL_CASH_BANK")},
        warnings=[],
    )
    with pytest.raises(configs.MappingValidationError):
        mapping.resolve_tree(tree, loaded)


def test_section_aware_validation_rejects_bs_tag_on_trading_leaf():
    trading = pe.AccountNode(guid="trd", name="Trading Account", depth=0, section="Trading",
                              path="Trading/Trading Account", total=500.0)
    tree = pe.ParsedBalanceSheet(section_roots={"Trading": [trading]}, section_totals={}, imbalance=0.0)
    loaded = configs.MappingLoadResult(
        entries={"trd": configs.MappingEntry(guid="trd", path=trading.path, tag="AL_CASH_BANK")},
        warnings=[],
    )
    with pytest.raises(configs.MappingValidationError):
        mapping.resolve_tree(tree, loaded)


def test_section_aware_validation_allows_personal_on_any_bs_section():
    for section in ("Assets", "Liability", "Equity", "Trading"):
        leaf = pe.AccountNode(guid=f"p-{section}", name="Drawings", depth=0, section=section,
                               path=f"{section}/Drawings", total=1.0)
        tree = pe.ParsedBalanceSheet(section_roots={section: [leaf]}, section_totals={}, imbalance=0.0)
        loaded = configs.MappingLoadResult(
            entries={f"p-{section}": configs.MappingEntry(guid=f"p-{section}", path=leaf.path, tag="PERSONAL")},
            warnings=[],
        )
        result = mapping.resolve_tree(tree, loaded)
        assert not result.blocked


# ---------------------------------------------------------------------------
# mapping.py -- unmapped => BLOCKED + snippet
# ---------------------------------------------------------------------------

def test_unmapped_leaf_blocks_and_snippet_lists_it(syn_ind_tree):
    known_paths = {n.guid: n.path for n in syn_ind_tree.all_nodes() if n.guid}
    loaded = configs.load_mapping(SYN_IND_UNMAPPED_MAPPING, known_paths=known_paths)
    result = mapping.resolve_tree(syn_ind_tree, loaded)
    assert result.blocked
    assert len(result.unmapped) == 1
    assert result.unmapped[0].path == "Assets/Misc Holding (unmapped)"

    snippet = mapping.proposed_mapping_snippet(result)
    assert "Assets/Misc Holding (unmapped)" in snippet
    assert "REPLACE_ME" in snippet


# ---------------------------------------------------------------------------
# mapping.py -- double-derivation
# ---------------------------------------------------------------------------

def test_double_derivation_catches_a_doctored_total():
    leaf_a = pe.AccountNode(guid="a", name="A", depth=1, section="Assets", path="Assets/Parent/A", total=100.0)
    leaf_b = pe.AccountNode(guid="b", name="B", depth=1, section="Assets", path="Assets/Parent/B", total=200.0)
    parent = pe.AccountNode(guid="p", name="Parent", depth=0, section="Assets", path="Assets/Parent",
                             total=999.0,  # doctored -- should be 300.0
                             children=[leaf_a, leaf_b])
    tree = pe.ParsedBalanceSheet(section_roots={"Assets": [parent]}, section_totals={}, imbalance=0.0)
    mismatches = mapping.check_double_derivation(tree)
    assert len(mismatches) == 1
    assert "Assets/Parent" in mismatches[0]


def test_double_derivation_clean_on_undoctored_syn_ind_tree(syn_ind_tree):
    assert mapping.check_double_derivation(syn_ind_tree) == []


# ---------------------------------------------------------------------------
# suggest.py -- graceful degradation
# ---------------------------------------------------------------------------

def test_suggest_tag_degrades_gracefully_with_no_endpoint():
    result = suggest.suggest_tag("Assets/Some/Leaf", 123.45, [], config_path="definitely-does-not-exist.yaml")
    assert result is None


def test_suggest_for_unmapped_returns_empty_dict_when_endpoint_unavailable(syn_ind_tree):
    known_paths = {n.guid: n.path for n in syn_ind_tree.all_nodes() if n.guid}
    loaded = configs.load_mapping(SYN_IND_UNMAPPED_MAPPING, known_paths=known_paths)
    result = mapping.resolve_tree(syn_ind_tree, loaded)
    suggestions = suggest.suggest_for_unmapped(
        result.unmapped, result.resolved, config_path="definitely-does-not-exist.yaml"
    )
    assert suggestions == {}
    # The run must still complete: proposed_mapping_snippet works with no suggestions.
    snippet = mapping.proposed_mapping_snippet(result, suggestions)
    assert "REPLACE_ME" in snippet


# ---------------------------------------------------------------------------
# RULE-1 enforceability (Data/itr/rules/user_rules.yaml, RULE-1)
# ---------------------------------------------------------------------------

def test_rule1_refund_principal_and_interest_map_to_distinct_tags():
    """RULE-1: interest on an IT refund is taxable; the principal is not.
    A fixture with both booked to separate accounts must resolve to two
    distinct tags so the workbook can show one taxable, one excluded."""
    principal = pe.AccountNode(guid="refund-principal", name="IT Refund Principal", depth=1,
                                section="RetainedEarnings-Income", path="Income/IT Refund Principal",
                                total=15000.0)
    interest = pe.AccountNode(guid="refund-interest", name="Interest on IT Refund", depth=1,
                               section="RetainedEarnings-Income", path="Income/Interest on IT Refund",
                               total=500.0)
    tree = pe.ParsedBalanceSheet(
        section_roots={"RetainedEarnings": [principal, interest]},
        section_totals={}, imbalance=0.0,
    )
    loaded = configs.MappingLoadResult(
        entries={
            "refund-principal": configs.MappingEntry(guid="refund-principal", path=principal.path,
                                                       tag="NONTAX_REFUND_PRINCIPAL"),
            "refund-interest": configs.MappingEntry(guid="refund-interest", path=interest.path,
                                                     tag="OS_REFUND_INTEREST"),
        },
        warnings=[],
    )
    result = mapping.resolve_tree(tree, loaded)
    assert not result.blocked
    assert result.resolved["refund-principal"].tag == "NONTAX_REFUND_PRINCIPAL"
    assert result.resolved["refund-interest"].tag == "OS_REFUND_INTEREST"
    assert result.resolved["refund-principal"].tag != result.resolved["refund-interest"].tag


# ---------------------------------------------------------------------------
# agent.py end-to-end (Definition of Done)
# ---------------------------------------------------------------------------

def test_agent_run_ok_status_when_fully_mapped(tmp_path):
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out_path = tmp_path / "out.xlsx"
    summary = agent.run(str(html_path), str(out_path), mapping_file=str(SYN_IND_MAPPING))
    assert "STATUS: OK" in summary
    assert not Path(str(out_path) + "-proposed-mappings.yaml").exists()


def test_agent_run_summary_surfaces_heuristic_income_warning(tmp_path):
    """Fail-loud gate (2026-07-19 mapping-precedence prompt, item 1b): the
    run's own text summary -- not only the Mapping Review sheet buried in
    the workbook -- must report the heuristic/approved provenance split and
    call out any heuristic-tagged INCOME account by name."""
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out_path = tmp_path / "out.xlsx"
    heuristic_mapping = FIXTURES / "syn_ind_heuristic_income.mapping.yaml"
    summary = agent.run(str(html_path), str(out_path), mapping_file=str(heuristic_mapping))
    assert "tag provenance" in summary
    assert "1 heuristic" in summary
    assert "WARNING" in summary and "INCOME account" in summary
    assert "Income/Bank Interest" in summary


def test_agent_run_built_with_review_status_with_extra_unmapped_account(tmp_path):
    """2026-07-16 Part 1: an unmapped account no longer blocks the run --
    it still builds, with STATUS: BUILT -- N REVIEW ITEM(S) and a
    proposed-mappings learning-loop snippet."""
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out_path = tmp_path / "out.xlsx"
    summary = agent.run(str(html_path), str(out_path), mapping_file=str(SYN_IND_UNMAPPED_MAPPING))
    assert "STATUS: BUILT -- 1 REVIEW ITEM(S)" in summary
    assert "Misc Holding (unmapped)" in summary
    snippet_path = Path(str(out_path) + "-proposed-mappings.yaml")
    assert snippet_path.exists()
    assert "REPLACE_ME" in snippet_path.read_text(encoding="utf-8")


def test_agent_run_built_cold_start_when_no_mapping_file_supplied(tmp_path):
    """Defect B(ii): no mapping_file and no entity selected is a true cold
    start -- every leaf is treated as unmapped, but 2026-07-16 Part 1 still
    builds a best-effort workbook (everything routed to UNCLASSIFIED) and
    writes the proposed-mappings snippet, rather than reporting STATUS: OK
    on an empty stub."""
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out_path = tmp_path / "out.xlsx"
    summary = agent.run(str(html_path), str(out_path))
    assert "STATUS: BUILT --" in summary
    assert "REVIEW ITEM(S)" in summary
    assert "cold start" in summary
    snippet_path = Path(str(out_path) + "-proposed-mappings.yaml")
    assert snippet_path.exists()
    assert "REPLACE_ME" in snippet_path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Real-corpus bootstrap -- never run in CI (skipped when the folder is absent).
# Never hand-tags a real account: unmapped > 0 for every entity is the
# expected, correct result (the learning loop working as designed).
# ---------------------------------------------------------------------------

_REAL_HTML = [
    "HarshalAmbani2425.html",
    "KhytaiAmbani2425.html",
    "KiranAmbani2425.html",
    "VaikunthAmbani2425.html",
    "VaikunthAmbaniHUF2425.html",
]


@pytest.mark.local_samples
def test_real_corpus_bootstrap_reports_unmapped_counts_for_all_5_entities():
    if not REAL_SAMPLES_DIR.is_dir():
        pytest.skip("Data/GNUCashReports/ not present -- real-file smoke test skipped")
    for html_name in _REAL_HTML:
        result, snippet = bm.bootstrap(str(REAL_SAMPLES_DIR / html_name), config_path="definitely-does-not-exist.yaml")
        assert len(result.unmapped) > 0, f"{html_name}: expected unmapped accounts (no mapping file exists yet)"
        assert "REPLACE_ME" in snippet
