"""
mapping.py -- nearest-ancestor tag resolution engine (plan section 3.1).

Given the parsed eguile HTML tree (parse_eguile.ParsedBalanceSheet) and a
loaded entity mapping (configs.MappingLoadResult), resolve every LEAF node
to exactly one tag:
  - a mapping entry on a leaf's own GUID wins outright;
  - otherwise the nearest tagged ANCESTOR's tag applies to the whole
    subtree beneath it (a tag on a parent covers all untagged children);
  - a leaf with no tag anywhere up its ancestor chain is UNMAPPED -- fail
    loud (plan section 4.1): the run is BLOCKED-for-review and no value
    from that leaf may reach any downstream schedule.

Two more checks run alongside resolution:
  - section-aware tag validation (B4 carry-forward, plan section 3):
    RetainedEarnings (income/expense) leaves must resolve to an RE tag;
    Assets-section leaves accept only the AL asset buckets; Liability-section
    leaves accept only AL_LIABILITY; Equity-section leaves accept only
    EQUITY_CAPITAL; Trading-section leaves accept only TRADING (PERSONAL is
    allowed anywhere on the BS side -- tags.SECTION_ALLOWED_TAGS). A
    violation raises MappingValidationError -- this is a config bug (a tag
    from the wrong side would produce a nonsensical workbook cell), not a
    fail-loud "needs review" case, so it hard-blocks rather than warns.
  - double-derivation: every HTML "Total <X>" node's stated total must
    equal the sum of the LEAF totals beneath it (+/-0.01), independently
    of parse_eguile's own subtotal-vs-children identity check -- this is
    a second, independently-written traversal over the same invariant, so
    a bug in one doesn't silently pass because the other agrees with it.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import tags as tag_vocab
from configs import MappingLoadResult, MappingValidationError
from parse_eguile import AccountNode, ParsedBalanceSheet


def _is_re_section(section: str) -> bool:
    # parse_eguile emits "RetainedEarnings-Income" / "RetainedEarnings-Expense"
    # (see verify.py's cross_check, which uses the same startswith check).
    return section.startswith("RetainedEarnings")


def _check_section_tag(n: AccountNode, tag: str) -> None:
    """B4 carry-forward: hard-fail (not warn) when a resolved tag doesn't
    belong on this leaf's section (tags.SECTION_ALLOWED_TAGS)."""
    if _is_re_section(n.section):
        if tag not in tag_vocab.RE_TAGS:
            raise MappingValidationError(
                f"{n.path or n.name}: tag {tag!r} is not a valid RE tag for a "
                f"RetainedEarnings leaf"
            )
        return
    allowed = tag_vocab.SECTION_ALLOWED_TAGS.get(n.section)
    if allowed is not None and tag not in allowed:
        raise MappingValidationError(
            f"{n.path or n.name}: tag {tag!r} is not allowed on a {n.section!r} "
            f"leaf (allowed: {sorted(allowed)})"
        )


@dataclass
class ResolvedLeaf:
    guid: str
    path: str
    tag: str
    flags: list = field(default_factory=list)


@dataclass
class UnmappedLeaf:
    guid: str | None
    path: str
    total: float | None


@dataclass
class ResolutionResult:
    resolved: dict            # guid -> ResolvedLeaf
    unmapped: list            # list[UnmappedLeaf]
    warnings: list            # list[str]

    @property
    def blocked(self) -> bool:
        return len(self.unmapped) > 0


def _leaf_sum(node: AccountNode) -> float:
    if not node.children:
        return node.total or 0.0
    return sum(_leaf_sum(c) for c in node.children)


def check_double_derivation(tree: ParsedBalanceSheet) -> list:
    """Independently re-derive every branch's stated total from its leaves;
    return a list of human-readable mismatch strings (empty if all agree)."""
    mismatches: list[str] = []

    def _walk(nodes):
        for n in nodes:
            if n.children:
                leaf_sum = _leaf_sum(n)
                if n.total is not None and abs(n.total - leaf_sum) > 0.01:
                    mismatches.append(
                        f"{n.path or n.name}: stated total {n.total:.2f} != "
                        f"sum of leaves {leaf_sum:.2f}"
                    )
            _walk(n.children)

    for roots in tree.section_roots.values():
        _walk(roots)
    return mismatches


def resolve_tree(tree: ParsedBalanceSheet, mapping: MappingLoadResult) -> ResolutionResult:
    """Resolve every leaf in `tree` to a tag using `mapping`'s entries,
    nearest-ancestor wins. Returns the resolved leaves, the unmapped leaves
    (fail-loud list), and warnings (mapping-load warnings + double-derivation
    mismatches). Raises MappingValidationError if any resolved tag violates
    section-aware validation (_check_section_tag) -- a config bug, not a
    fail-loud "needs review" case."""
    resolved: dict[str, ResolvedLeaf] = {}
    unmapped: list[UnmappedLeaf] = []
    warnings: list[str] = list(mapping.warnings)

    def _walk(nodes, inherited_tag, inherited_flags):
        for n in nodes:
            entry = mapping.entries.get(n.guid) if n.guid else None
            tag = entry.tag if entry else inherited_tag
            flags = entry.flags if entry else inherited_flags

            if not n.children:
                if tag is None:
                    unmapped.append(UnmappedLeaf(guid=n.guid, path=n.path or n.name, total=n.total))
                else:
                    _check_section_tag(n, tag)
                    if n.guid:
                        resolved[n.guid] = ResolvedLeaf(guid=n.guid, path=n.path or n.name, tag=tag, flags=flags)
            else:
                _walk(n.children, tag, flags)

    for roots in tree.section_roots.values():
        _walk(roots, None, [])

    warnings.extend(check_double_derivation(tree))
    return ResolutionResult(resolved=resolved, unmapped=unmapped, warnings=warnings)


def proposed_mapping_snippet(result: ResolutionResult, suggestions: dict | None = None) -> str:
    """Build a <output>-proposed-mappings.yaml snippet (configs.py's list
    format) for every unmapped leaf, annotated with an LLM suggestion when
    one was found (suggest.py); ready to review and paste into the
    entity's mapping file. Never writes to the mapping file itself."""
    import datetime

    from configs import dump_mapping_entries

    suggestions = suggestions or {}
    today = datetime.date.today().isoformat()
    entries = []
    for leaf in result.unmapped:
        suggestion = suggestions.get(leaf.guid)
        entry = {
            "guid": leaf.guid,
            "path": leaf.path,
            "tag": suggestion.tag if suggestion else "REPLACE_ME",
            "note": suggestion.rationale if suggestion else "unmapped -- needs review",
        }
        if suggestion:
            entry["suggested_by_llm"] = today
        entries.append(entry)
    return dump_mapping_entries(entries)
