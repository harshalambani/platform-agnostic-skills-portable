"""
ui/tabs/itr_mapping_review.py -- ITR Mapping review tab.

Built on the shared ui._review_engine skeleton (searchable assign picker, row
multi-select, sort/filter, save payload bridge) -- see ui/tabs/gnucash_review.py
(the closest analogue: also a picker that reassigns one column in place) and
ui/tabs/tds_journal_review.py for the sibling consumers this mirrors. This
module used to carry its own copy of the ~600-line HTML/JS skeleton (identical
_js_json, %%TOKEN%% template, eval-bootstrap, payload bridge) -- that is gone;
everything below is either row-loading, row-presentation, or save logic, all
plain Python and unit-testable.

Tag vocabulary help: every tag code shown in the "Current tag" / "Suggested"
columns carries a title tooltip with its one-line meaning from tags.py's
TagMeta.treatment (via the engine's per-column _badges title), and the
glossary of the full searchable vocabulary lives in the spec's
extra_panel_html as a collapsible reference table -- the raw tag codes (e.g.
OS_INTEREST_BANK) are otherwise meaningless to anyone who hasn't memorized
tags.py. Searching the vocabulary itself is done via the "Assign tag" picker
(type to search primary/secondary text), which already indexes the same list.

Data sources for the review table (per entity):
  - Data/itr/mappings/<entity>.mapping.yaml -- already-resolved entries
    (anchored via ui._config.data_root_dir(), never a bare "Data/" prefix --
    see docs/history/2026-07-xx path-anchoring fix).
  - the newest *-proposed-mappings.yaml under <data_root>/outputs that is
    ATTRIBUTABLE TO THIS ENTITY -- unmapped leaves + any LLM suggestion from
    that entity's latest ITR Workbook run. This used to be "the single most
    recent snippet in the whole outputs folder, no entity filter", which
    leaked one entity's accounts into every other entity's review the
    moment two entities' runs landed in the same outputs folder (reported
    symptom: an account from one family member's book appearing under a
    different member's review -- impossible, since GnuCash account GUIDs
    are only unique WITHIN one book, not across the family's cloned-from-
    template books). Fixed in two independent layers (2026-07-28):

    Layer A (filename attribution) -- _latest_proposed_mappings_path()
    scans outputs newest-first and returns the first snippet whose filename
    stem is attributable to `entity_key` via _snippet_entity_key(), driven
    by an explicit, optional per-entity `workbook_match:` field in
    entities.yaml (real-world validation showed workbook output stems are
    user-controlled CamelCase `<First><Last>[HUF]<year>`, e.g.
    "AliceDoe25626" / "BobDoeHUF2526" -- neither the entity key
    itself nor a boundary-delimited-substring match against it is reliable
    against that shape, so attribution is config-driven instead).
    `_workbook_match_map()` reads {entity_key: workbook_match} for every
    entity with a non-empty `workbook_match:` string; `_snippet_entity_key()`
    then returns the entity key whose token is a case-insensitive SUBSTRING
    of the stem (the token is deliberately year-agnostic -- just the name
    portion, e.g. "AliceSmith" -- so it matches every year's snippet for
    that entity), with the LONGEST matching token winning the individual-
    vs-HUF collision (e.g. token "AliceSmithHUF" beats "AliceSmith" for
    stem "AliceSmith2526-...-HUF..." style overlaps). A genuine tie between
    two equally-long matching tokens attributes to NEITHER (returns None
    rather than guessing). An entity with no `workbook_match:` configured
    can never match anything -- it simply gets no snippet, never a foreign
    one. No snippet attributable to the entity -> no snippet rows loaded
    (mapping file only), never an old/foreign one shown as a fallback.

    Layer B (book GUID validation, optional) -- when the entity has a
    `book:` path configured in entities.yaml and it's readable,
    _apply_book_validation() cross-checks every surviving snippet row's
    GUID against that entity's OWN parsed book: a GUID absent from the book
    is dropped outright (belt-and-suspenders against a cross-entity leak
    even if two entities' snippet filenames happened to collide under
    Layer A), and a GUID that IS present has its displayed `path`
    overwritten with the book's own current account path for that GUID --
    so a GUID that's shared/cloned across the family's template books (the
    GUIDs are not globally unique) always shows this entity's local account
    name, never a foreign one. No `book:` configured, or the file is
    missing/unreadable -- Layer B is skipped silently; Layer A's result
    stands as-is. `book:` is a manually-added, purely optional entities.yaml
    field (see configs.py); nothing ships with a real path pre-filled.

    A guid already present in the entity's own mapping file always wins
    over a snippet entry for the same guid (it's fully resolved, so it
    isn't shown as unmapped regardless of what the snippet says).

Save discipline (mirrors gnucash_review._save_changes): write a timestamped
backup of the current mapping file BEFORE any in-place rewrite; a blank/
missing entity never touches the filesystem.

Payload-shape note: the engine's syncPayload() builds each change as
{_idx, _orig, <declared Column keys>, guid?}. Declaring "path" as a real
Column (rather than stuffing it into context) means it rides along on every
change automatically -- `paths[guid] = ch["path"]` at save time needs
nothing extra. `guid` passes through the engine's own guid/Sr/'Transaction
ID'/'CN No' allowlist. `entity_key` isn't a per-row value, so it goes in
spec.context instead of a second Gradio input -- this also fixes a latent
bug in the old wiring, where Save read whatever the entity dropdown
currently showed rather than what was actually Loaded.
"""
from __future__ import annotations

import datetime
import html as _html
import shutil
import sys
from pathlib import Path

import gradio as gr
import yaml

from .. import _config as _config_mod
from .._review_engine import (
    Column,
    PickerItem,
    ReviewSpec,
    build_html,
    parse_payload,
    payload_box_css,
)

APP_ID = "itrmap"
TARGET_COL = "tag"
PAYLOAD_VAR = "_itrMapSavePayload"

# ---------------------------------------------------------------------------
# Import the ITR Workbook skill's flat (non-package) scripts/ modules.
#
# scripts/*.py (configs.py, tags.py, apply_mapping_corrections.py, ...) are
# NOT a Python package (no __init__.py) -- agent.py itself imports them the
# same way: resolve the scripts/ directory via the agents.skill_itr_workbook
# package (frozen-build-safe, mirrors gnucash_review.py's
# `from agents.skill_gnucash_account_mapper...` import) and insert it onto
# sys.path once, then import the bare module names.
# ---------------------------------------------------------------------------

_SCRIPTS_ON_PATH = False


def _ensure_itr_scripts_importable() -> None:
    global _SCRIPTS_ON_PATH
    if _SCRIPTS_ON_PATH:
        return
    import agents.skill_itr_workbook as _itr_pkg  # noqa: PLC0415

    scripts_dir = Path(_itr_pkg.__file__).resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    _SCRIPTS_ON_PATH = True


def _itr_modules():
    """Return (configs, tags, apply_mapping_corrections) modules, importable
    in both source and frozen builds."""
    _ensure_itr_scripts_importable()
    import apply_mapping_corrections as amc  # noqa: PLC0415
    import configs  # noqa: PLC0415
    import tags as tag_vocab  # noqa: PLC0415
    return configs, tag_vocab, amc


# ---------------------------------------------------------------------------
# Path helpers (anchored via data_root_dir() -- works in source + frozen).
# ---------------------------------------------------------------------------

def _mapping_path(entity_key: str) -> Path:
    return _config_mod.data_root_dir() / "itr" / "mappings" / f"{entity_key}.mapping.yaml"


def _outputs_dir() -> Path:
    return _config_mod.data_root_dir() / "outputs"


def _workbook_match_map() -> dict[str, str]:
    """{entity_key: workbook_match} for every entity in Data/itr/entities.yaml
    that has a non-empty string `workbook_match:` field (Layer A's config-
    driven attribution token -- see the module docstring). A lightweight,
    tolerant read straight off the raw YAML dict (mirrors _entity_book_path's
    style): a missing/malformed file, or an entity with no/blank
    `workbook_match`, just yields no entry for that entity -- never an
    error."""
    path = _config_mod.data_root_dir() / "itr" / "entities.yaml"
    if not path.is_file():
        return {}
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, str] = {}
    for key, fields in raw.items():
        if not isinstance(key, str) or not isinstance(fields, dict):
            continue
        token = fields.get("workbook_match")
        if token and str(token).strip():
            out[key] = str(token).strip()
    return out


def _snippet_entity_key(stem: str, match_map: dict[str, str]) -> str | None:
    """Attribute a proposed-mappings filename stem to exactly one entity
    key, via `match_map` ({entity_key: workbook_match} from
    _workbook_match_map()), or None if no token is attributable / the
    match is ambiguous.

    A token matches iff it occurs in `stem` as a case-insensitive
    SUBSTRING -- no boundary discipline, because real workbook stems are
    user-controlled CamelCase with no separators (e.g. "AliceDoe25626",
    "BobDoeHUF2526") where a boundary check would never fire.
    When more than one token matches, the LONGEST one wins (e.g. an HUF's
    longer token beats the individual's shorter token when both are
    substrings of the HUF's own stem). If several tokens tied for that
    longest length all match, the attribution is genuinely ambiguous and
    None is returned rather than guessing.
    """
    candidates: list[str] = []
    for key, token in match_map.items():
        if not key or not token:
            continue
        if token.lower() in stem.lower():
            candidates.append(key)
    if not candidates:
        return None
    max_len = max(len(match_map[k]) for k in candidates)
    longest = [k for k in candidates if len(match_map[k]) == max_len]
    return longest[0] if len(longest) == 1 else None


def _latest_proposed_mappings_path(
    entity_key: str, match_map: dict[str, str] | None = None,
) -> Path | None:
    """Newest *-proposed-mappings.yaml under the outputs folder that is
    ATTRIBUTABLE TO `entity_key` by filename (Layer A -- see the module
    docstring), or None if there isn't one yet (no ITR Workbook run for
    this entity at all, the entity has no `workbook_match:` configured, or
    the only snippets present belong to other entities). Snippet filenames
    look like "<stamp>-<WorkbookStem>-proposed-mappings.yaml"; the
    "<WorkbookStem>" portion is matched against `match_map`'s tokens via
    _snippet_entity_key().

    `match_map` lets callers pass a pre-loaded {entity_key: workbook_match}
    dict (tests use this to avoid re-reading entities.yaml); defaults to
    _workbook_match_map(). If `entity_key` has no token in `match_map`, it
    can never match any stem, so this always returns None for it -- safe
    by construction, never a foreign snippet.
    """
    out_dir = _outputs_dir()
    if not out_dir.is_dir() or not entity_key:
        return None
    if match_map is None:
        match_map = _workbook_match_map()

    suffix = "-proposed-mappings.yaml"
    candidates = sorted(
        out_dir.glob(f"*{suffix}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    for p in candidates:
        if _snippet_is_for(p, entity_key, match_map):
            return p
    return None


ENTITY_HEADER = "# entity:"


def _snippet_recorded_entity(path: Path) -> str | None:
    """ITR-04: the entity key the workbook recorded INSIDE the snippet (a
    leading "# entity: KEY" comment), or None for an older snippet."""
    try:
        with path.open(encoding="utf-8") as fh:
            for _ in range(5):
                line = fh.readline()
                if not line:
                    break
                if line.startswith(ENTITY_HEADER):
                    return line[len(ENTITY_HEADER):].strip() or None
    except OSError:
        return None
    return None


def _snippet_is_for(path: Path, entity_key: str, match_map: dict[str, str]) -> bool:
    """Is this snippet attributable to `entity_key`? The key recorded inside
    the snippet decides; a snippet that records a DIFFERENT entity is never
    shown, whatever its filename contains. Only a snippet with no recorded
    key falls back to the filename token (Layer A)."""
    recorded = _snippet_recorded_entity(path)
    if recorded is not None:
        return recorded == entity_key
    suffix = "-proposed-mappings.yaml"
    stem = path.name[: -len(suffix)] if path.name.endswith(suffix) else path.stem
    return _snippet_entity_key(stem, match_map) == entity_key


def _entity_book_path(entity_key: str) -> Path | None:
    """Optional per-entity book path for Layer B (see the module docstring).
    Delegates to ui._book_registry.resolve_book() (fy=None -> newest
    registered FY in `books`, else the legacy raw `book:` key, else None) --
    a strict superset of this function's pre-registry behaviour (which read
    only the legacy `book:` key), so legacy-only entities are unaffected and
    entities with `books` now gain Layer B. Tolerant/never-raises contract
    preserved: resolve_book() itself never raises."""
    from .. import _book_registry  # noqa: PLC0415
    return _book_registry.resolve_book(entity_key)


def _load_book_accounts(entity_key: str):
    """(book path or None, accounts list or None). Loaded once per review and
    shared by the validation, the leaf listing and the row-kind lookup."""
    book_path = _entity_book_path(entity_key)
    if book_path is None or not book_path.is_file():
        return book_path, None
    try:
        from agents.gnucash_accounts import load_accounts  # noqa: PLC0415
        return book_path, list(load_accounts(book_path))
    except Exception:
        return book_path, None


def _apply_book_validation(entity_key: str, snippet_rows: list[dict], accounts=None) -> list[dict]:
    """Layer B (optional, graceful -- see the module docstring): when
    `entity_key` has a configured + readable `book:` in entities.yaml,
    cross-check every snippet row's GUID against that entity's OWN parsed
    book. A GUID absent from the book is dropped outright (belt-and-
    suspenders against a cross-entity leak even if two entities' snippet
    filenames happened to collide under Layer A); a GUID that IS present
    has its `path` overwritten with the book's own current account path
    for that GUID (GnuCash account GUIDs are not globally unique across
    the family's cloned-from-template books, so a shared GUID must always
    display THIS entity's local account name, never a foreign one).

    No `book:` configured, the file is missing/unreadable, or it fails to
    parse -- Layer B is skipped silently and `snippet_rows` passes through
    unchanged (Layer A's result stands as-is)."""
    if accounts is None:
        _bp, accounts = _load_book_accounts(entity_key)
        if accounts is None:
            return snippet_rows
    accounts = {a.id: a for a in accounts}
    if not accounts:
        return snippet_rows

    validated: list[dict] = []
    for row in snippet_rows:
        acct = accounts.get(row["guid"])
        if acct is None:
            continue
        if acct.path:
            row["path"] = acct.path
        validated.append(row)
    return validated


# ---------------------------------------------------------------------------
# Entity dropdown -- same options_from: itr_entities source as the ITR
# Workbook tab (Data/itr/entities.yaml via data_root_dir()).
# ---------------------------------------------------------------------------

def _entity_choices() -> list[tuple[str, str]]:
    from . import _generic  # noqa: PLC0415 -- imported lazily; _generic is heavier
    return _generic._options_from_itr_entities()


# ---------------------------------------------------------------------------
# Row loading -- mapping file entries + proposed-mappings unmapped suggestions.
# ---------------------------------------------------------------------------

def _load_review_rows(entity_key: str) -> list[dict]:
    """Rows only -- see _collect_review()."""
    return _collect_review(entity_key)[0]


def _book_leaf_rows(entity_key: str, entries: dict, snippet_by_guid: dict,
                    book=None) -> tuple[list[dict], Path | None]:
    """ITR-04. When the entity has a registered book, list that book's own
    leaf accounts that nothing maps yet. Hidden and placeholder accounts
    (and anything under a hidden parent) are never offered. An account is
    "mapped" when it, or any ancestor, has an entry in the mapping file --
    mapped accounts are never listed as unmapped."""
    book_path, accounts = book if book is not None else _load_book_accounts(entity_key)
    if book_path is None or not book_path.is_file():
        return [], None
    if accounts is None:
        return [], book_path
    from agents.gnucash_accounts import postable_accounts  # noqa: PLC0415
    by_id = {a.id: a for a in accounts}
    parents = {a.parent_id for a in accounts if a.parent_id}
    rows: list[dict] = []
    for a in postable_accounts(accounts):
        if a.id in parents or not a.path:
            continue                     # not a leaf
        mapped, cur, seen = False, a, set()
        while cur is not None and cur.id not in seen:
            seen.add(cur.id)
            if cur.id in entries:
                mapped = True
                break
            cur = by_id.get(cur.parent_id)
        if mapped:
            continue
        snip = snippet_by_guid.get(a.id) or {}
        rows.append({
            "guid": a.id, "path": a.path, "tag": None, "unmapped": True,
            "needs_review": False, "suggested": snip.get("suggested"),
            "note": snip.get("note") or "from the registered book",
        })
    return rows, book_path


def _collect_review(entity_key: str) -> tuple[list[dict], dict]:
    """Build the review table rows for `entity_key`.

    Each row: {guid, path, tag, unmapped, needs_review, suggested, note}.
    `tag` is the entity's currently-resolved tag (None when unmapped).
    `needs_review` is the RAG confidence signal for a mapped row: True when
    the entry is still an unapproved LLM suggestion (suggested_by_llm set),
    False once a human has confirmed/set it (suggested_by_llm cleared to
    None). `suggested` is the proposed-mappings snippet's suggestion for an
    unmapped leaf (None if the snippet has no suggestion, or has
    "REPLACE_ME"). Handles both cold start (mapping file absent, everything
    comes from the snippet) and correction (mapping file has entries;
    snippet supplies nothing new) cleanly.
    """
    info: dict = {"entries": 0, "snippet": None, "snippet_rows": 0, "book": None, "book_rows": 0}
    if not entity_key or not entity_key.strip():
        return [], info
    configs, _tag_vocab, _amc = _itr_modules()

    entries: dict = {}
    mapping_file = _mapping_path(entity_key)
    if mapping_file.is_file():
        try:
            entries = dict(configs.load_mapping(mapping_file).entries)
        except Exception:
            entries = {}

    rows: list[dict] = []
    for guid, entry in entries.items():
        on_hold = entry.tag == _tag_vocab.HOLD
        rows.append({
            "guid": guid,
            "path": entry.path,
            "tag": entry.tag,
            # ITR-07: a HOLD entry is unmapped for every purpose; it is shown
            # as ON HOLD with its reason, never as a mapped/confirmed tag.
            "held": on_hold,
            "hold_reason": (entry.note or "held for review") if on_hold else "",
            "unmapped": False,
            # RAG confidence: an entry the LLM suggested that no human has
            # approved yet (suggested_by_llm still set) is "needs_review"
            # (amber); once a human has touched/approved it (cleared to
            # None -- see apply_corrections_map) it's "confirmed" (green).
            "needs_review": bool(entry.suggested_by_llm) and not on_hold,
            "suggested": None,
            "note": entry.note or "",
            # ITR-05: the foreign-held flag, shown and editable on asset tags.
            "al_foreign": "AL_FOREIGN" if "AL_FOREIGN" in (entry.flags or []) else "",
        })

    info["entries"] = len(entries)
    book = _load_book_accounts(entity_key)
    snippet_path = _latest_proposed_mappings_path(entity_key)
    info["snippet"] = snippet_path
    snippet_by_guid: dict = {}
    sections: dict = {}
    if snippet_path is not None:
        try:
            raw = yaml.safe_load(snippet_path.read_text(encoding="utf-8")) or []
        except Exception:
            raw = []
        snippet_rows: list[dict] = []
        if isinstance(raw, list):
            for item in raw:
                if not isinstance(item, dict):
                    continue
                guid = item.get("guid")
                if not guid or guid in entries:
                    # Already resolved in the entity's own mapping file --
                    # a stale/other-entity snippet entry never overrides it.
                    continue
                suggested_tag = item.get("tag")
                if suggested_tag in ("REPLACE_ME", _tag_vocab.HOLD):
                    suggested_tag = None
                if item.get("section"):
                    sections[guid] = str(item["section"])
                snippet_rows.append({
                    "guid": guid,
                    "path": item.get("path", ""),
                    "tag": None,
                    "unmapped": True,
                    "needs_review": False,
                    "suggested": suggested_tag,
                    "note": item.get("note", ""),
                })
        snippet_rows = _apply_book_validation(entity_key, snippet_rows, accounts=book[1])
        info["snippet_rows"] = len(snippet_rows)
        snippet_by_guid = {r["guid"]: r for r in snippet_rows}
        rows.extend(snippet_rows)

    book_rows, book_path = _book_leaf_rows(entity_key, entries, snippet_by_guid, book=book)
    info["book"] = book_path
    have = {r["guid"] for r in rows}
    added = [r for r in book_rows if r["guid"] not in have]
    info["book_rows"] = len(added)
    rows.extend(added)

    by_id = {a.id: a for a in (book[1] or [])}
    for r in rows:
        r["kind"] = _row_kind(r, sections.get(r["guid"]), by_id.get(r["guid"]), _tag_vocab)
        r.setdefault("held", False)
        r.setdefault("hold_reason", "")
        r.setdefault("al_foreign", "")

    rows.sort(key=lambda r: (r["path"] or "", r["guid"]))
    return rows, info


# ---------------------------------------------------------------------------
# ITR-07: which side of the return a row is on, and which tags it may take.
# ---------------------------------------------------------------------------

_TYPE_KIND = {
    "INCOME": "Income/expense", "EXPENSE": "Income/expense",
    "ASSET": "Asset", "BANK": "Asset", "CASH": "Asset", "STOCK": "Asset",
    "MUTUAL": "Asset", "RECEIVABLE": "Asset", "CURRENCY": "Asset",
    "LIABILITY": "Liability", "CREDIT": "Liability", "PAYABLE": "Liability",
    "EQUITY": "Equity", "TRADING": "Trading",
}
_ROOT_KIND = {
    "income": "Income/expense", "expenses": "Income/expense", "expense": "Income/expense",
    "revenue": "Income/expense", "revenues": "Income/expense",
    "assets": "Asset", "asset": "Asset",
    "liabilities": "Liability", "liability": "Liability",
    "equity": "Equity", "trading": "Trading",
}


def _row_kind(row: dict, section, acct, tag_vocab) -> str:
    """The row's kind (tags.ROW_KINDS). Most reliable source first: the
    workbook's own section for the account, the book's account type, the
    path's root name, and last the kind a mapped row's current tag implies.
    Nothing known -> "Unknown", which offers almost nothing (fail closed)."""
    kind = tag_vocab.kind_from_section(section)
    if kind is None and acct is not None:
        kind = _TYPE_KIND.get(str(getattr(acct, "type", "") or "").upper())
    if kind is None:
        path = str(row.get("path") or "")
        root = path.replace("/", ":").split(":", 1)[0].strip().lower()
        kind = _ROOT_KIND.get(root)
    if kind is None and row.get("tag") and row["tag"] != tag_vocab.HOLD:
        implied = [k for k in tag_vocab.ROW_KINDS
                   if k != tag_vocab.KIND_UNKNOWN and row["tag"] in tag_vocab.allowed_tags_for_kind(k)]
        if len(implied) == 1:
            kind = implied[0]
    return kind or tag_vocab.KIND_UNKNOWN


def _tag_options() -> list[dict]:
    """(tag, sheet, target, description) rows from tags.py's vocabulary,
    sorted by tag. `sheet`/`target` (e.g. "RE"/"OtherSources") give the
    glossary a bit more orientation than the one-line treatment note alone."""
    _configs, tag_vocab, _amc = _itr_modules()
    return [
        {"tag": tag, "sheet": meta.sheet, "target": meta.target, "desc": meta.treatment}
        for tag, meta in sorted(tag_vocab.TAGS.items())
    ]


HOLD_DESC = ("Not a tag. Keeps this account out of every schedule and keeps the workbook "
             "NOT filing-ready until it gets a real tag. You are asked for a reason.")


def _picker_items(tag_opts: list[dict]) -> list[PickerItem]:
    """HOLD first, then every tag. Each item knows the row kinds it may go on
    (tags.kinds_allowing) and carries its plain-word synonyms as search-only
    keywords. The kind cut happens BEFORE the search in the engine, so a
    synonym can never surface a tag a selected row's side forbids."""
    _configs, tag_vocab, _amc = _itr_modules()
    items = [PickerItem(
        value=tag_vocab.HOLD, primary=tag_vocab.HOLD, secondary=HOLD_DESC,
        kinds=tag_vocab.kinds_allowing(tag_vocab.HOLD),
        keywords=" ".join(tag_vocab.synonyms_for(tag_vocab.HOLD)),
    )]
    for t in tag_opts:
        items.append(PickerItem(
            value=t["tag"], primary=t["tag"], secondary=t["desc"],
            kinds=tag_vocab.kinds_allowing(t["tag"]),
            keywords=" ".join(tag_vocab.synonyms_for(t["tag"])),
        ))
    return items


# ---------------------------------------------------------------------------
# Row presentation -- computed server-side in Python, per the engine's core
# design rule (see ui/_review_engine.py's module docstring): a loader decides
# what a row looks like (_tags/_rowclass/_badges/_note) and hands the engine
# plain data, instead of re-deriving badge/accent logic in eval'd JS.
# ---------------------------------------------------------------------------

def _tag_title(tag: str | None, tag_desc: dict[str, str]) -> str:
    if not tag:
        return ""
    desc = tag_desc.get(tag, "")
    return f"{tag} -- {desc}" if desc else tag


def _row_presentation(row: dict, tag_desc: dict[str, str]) -> None:
    """Fill in the engine's _tags / _rowclass / _badges / _note keys in place.

    RAG confidence tiers, same three states the old hand-rolled JS rendered:
      - unmapped   -> red accent,   "UNMAPPED"     badge on the tag column.
      - needs_review (mapped but still an unapproved LLM suggestion) ->
        amber accent, "NEEDS REVIEW" badge.
      - confirmed  (mapped, human-approved) -> green accent, "CONFIRMED"
        badge -- every confirmed row gets one, matching the original's
        unconditional "(confirmed)" suffix.
    A leaf with a proposed-mappings suggestion additionally gets a blue
    "SUGGESTED" badge on the suggested column, both title-tooltipped with
    the tag's one-line meaning from tags.py.
    """
    unmapped = bool(row.get("unmapped"))
    needs_review = bool(row.get("needs_review"))
    tag = row.get("tag")
    suggested = row.get("suggested")

    badges: dict = {}
    if row.get("held"):
        # ITR-07: on hold on purpose. Never green, never "confirmed".
        tags = ["held", "unmapped"]
        rowclass = "accent-orange"
        badges[TARGET_COL] = {"text": "ON HOLD", "cls": "amber",
                              "title": "Held: feeds no schedule, and the workbook is not filing-ready "
                                       "until this gets a real tag. " + str(row.get("hold_reason") or "")}
    elif unmapped:
        tags = ["unmapped"]
        rowclass = "accent-red"
        badges[TARGET_COL] = {"text": "UNMAPPED", "cls": "red"}
    elif needs_review:
        tags = ["needs_review", "mapped"]
        rowclass = "accent-amber"
        badges[TARGET_COL] = {"text": "NEEDS REVIEW", "cls": "amber", "title": _tag_title(tag, tag_desc)}
    else:
        tags = ["confirmed", "mapped"]
        rowclass = "accent-green"
        badges[TARGET_COL] = {"text": "CONFIRMED", "cls": "green", "title": _tag_title(tag, tag_desc)}

    if suggested:
        badges["suggested"] = {"text": "SUGGESTED", "cls": "blue", "title": _tag_title(suggested, tag_desc)}

    # ITR-05: the foreign flag can be set only on a saved row whose tag is a
    # Schedule AL asset tag.
    try:
        _flagok = (not unmapped) and bool(tag) and bool(row.get("guid")) and tag in _itr_modules()[1].AL_ASSET_TAGS
    except Exception:
        _flagok = False
    row["_flagok"] = bool(_flagok)
    row["_tags"] = tags
    row["_rowclass"] = rowclass
    row["_badges"] = badges
    row["_note"] = row.get("note") or ""


def _build_glossary_html(tag_opts: list[dict]) -> str:
    """A collapsible tag-vocabulary reference table for spec.extra_panel_html.

    Not a second copy of the picker's search box -- the "Assign tag" picker
    above already searches this same vocabulary (primary=tag, secondary=
    desc); this panel is the persistent, browsable reference, toggled via
    the native <details>/<summary> element (no JS required).
    """
    rows_html = "".join(
        "<tr>"
        f'<td style="font-weight:600;color:#60a5fa;white-space:nowrap;padding:3px 8px;'
        f'font-size:11px;vertical-align:top;">{_html.escape(t["tag"])}</td>'
        f'<td style="color:#888;white-space:nowrap;padding:3px 8px;font-size:11px;'
        f'vertical-align:top;">{_html.escape(t["target"] or t["sheet"] or "")}</td>'
        f'<td style="padding:3px 8px;font-size:11px;color:#ccc;vertical-align:top;">'
        f'{_html.escape(t["desc"])}</td>'
        "</tr>"
        for t in tag_opts
    )
    return (
        '<details style="margin-bottom:8px;border:1px solid #333;border-radius:4px;'
        'background:#161616;padding:6px 10px;color:#e0e0e0;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
        f'<summary style="cursor:pointer;font-weight:600;font-size:12px;color:#ccc;">'
        f'Tag glossary ({len(tag_opts)} tags) -- search vocabulary via the "Assign tag" '
        'box above</summary>'
        '<div style="max-height:240px;overflow-y:auto;margin-top:6px;">'
        f'<table style="width:100%;border-collapse:collapse;"><tbody>{rows_html}</tbody></table>'
        '</div></details>'
    )


# ---------------------------------------------------------------------------
# Spec + load
# ---------------------------------------------------------------------------

def _spec(picker_items: list[PickerItem], entity_key: str, glossary_html: str) -> ReviewSpec:
    return ReviewSpec(
        app_id=APP_ID,
        columns=[
            Column("path", "Account path"),
            Column(TARGET_COL, "Current tag"),
            Column("suggested", "Suggested"),
            Column("kind", "Side"),
            Column("al_foreign", "Foreign (AL)"),
            Column("hold_reason", "Hold reason"),
        ],
        target_col=TARGET_COL,
        payload_var=PAYLOAD_VAR,
        picker_label="Assign tag:",
        picker_placeholder="Type to search tags…",
        picker_items=picker_items,
        status_options=[
            ("unmapped", "Unmapped only (includes on hold)"),
            ("held", "On hold only"),
            ("changed", "Changed, not saved"),
            ("needs_review", "Needs review (LLM-suggested)"),
            ("confirmed", "Confirmed only"),
            ("mapped", "Mapped only (confirmed + needs review)"),
        ],
        status_label="Show:",
        default_sort="path",
        # Deliberately NOT set: unlike gnucash's bank-description matching,
        # there's no shared "same pattern" key across mapping rows worth
        # batch-reassigning by.
        apply_matching_on="",
        extra_panel_html=glossary_html,
        context={"entity_key": entity_key},
        # Row-level "delete this mapping entry" -- see _save_changes()'s
        # docstring for the persistence semantics (a row that exists in the
        # entity's mapping file is actually removed from it; an unmapped/
        # snippet-only row is view-only, nothing to persist).
        allow_delete=True,
        # ITR-05: "Mark foreign" / "Clear foreign" for asset accounts held abroad.
        flag_toggle=("al_foreign", "Mark foreign", "Clear foreign", "AL_FOREIGN"),
        # ITR-04: an applied tag shows at once as changed-and-unsaved, never
        # as UNMAPPED and never as saved/CONFIRMED.
        # ITR-07: the picker offers, and Apply accepts, only tags valid for each
        # row's side; HOLD asks for a reason and stores it with the entry.
        kind_col="kind",
        value_prompts={"HOLD": {"text": "Why is this account on hold? (kept with the entry)",
                                "key": "hold_reason", "default": "held for review"}},
        assigned_badge={"text": "CHANGED, NOT SAVED", "cls": "violet",
                        "title": "Applied in this screen; not saved yet",
                        "rowclass": "accent-orange", "tag": "changed"},
    )


EXPLAINER_HTML = (
    '<div style="margin:0 0 8px;padding:6px 10px;border:1px solid #333;border-radius:4px;'
    'font-size:12px;color:#ccc;">'
    "<strong>What happens to an account with no tag, or on HOLD:</strong> it feeds no schedule. "
    "It is listed on the Unclassified sheet, the workbook still builds as a draft, and it is "
    "<strong>NOT filing-ready</strong> until every such account has a real tag. "
    "<strong>HOLD</strong> is not a tag: use it when you are not sure yet - it keeps the account "
    "visibly unresolved, with your reason, instead of guessing. "
    "The list offers only tags valid for each account's side (income/expense, asset, liability, "
    "equity, trading); plain words such as <em>medical</em>, <em>school</em> or <em>partner</em> "
    "find the right tag."
    "</div>"
)


def _empty_state_html(entity_key: str, info: dict) -> str:
    """ITR-04: say WHICH source was empty. Never tells the user to run the
    workbook when a snippet attributable to this entity already exists."""
    key = _html.escape(entity_key)
    parts = [
        "mapping file: "
        + (f"{info['entries']} entries, all accounts mapped or none to show" if info["entries"]
           else "no mapping entries saved yet"),
    ]
    if info["snippet"] is not None:
        parts.append(
            f"latest workbook run ({_html.escape(Path(info['snippet']).name)}): "
            "attributable to this entity, but it lists no unmapped accounts"
        )
    else:
        parts.append("workbook run: no proposed-mappings snippet is attributable to this entity")
    parts.append(
        "book: " + ("registered, but it has no unmapped leaf accounts" if info["book"] is not None
                    else "no book registered for this entity")
    )
    advice = ""
    if info["snippet"] is None and info["book"] is None and not info["entries"]:
        advice = (" Run the ITR Workbook once for this entity (it records the entity in its "
                  "proposed-mappings file), or register this entity's book.")
    items = "".join(f"<li>{p}</li>" for p in parts)
    return f"<p>No accounts to review for <strong>{key}</strong>.</p><ul>{items}</ul><p>{advice.strip()}</p>"


def _load_review_data(entity_key: str) -> str:
    """Load `entity_key`'s mapping + latest proposed-mappings snippet into
    the interactive HTML table."""
    if not entity_key or not entity_key.strip():
        return "<p>Select an entity, then click Load.</p>"

    rows, info = _collect_review(entity_key)
    if not rows:
        return _empty_state_html(entity_key, info)

    tag_opts = _tag_options()
    tag_desc = {t["tag"]: t["desc"] for t in tag_opts}
    for row in rows:
        _row_presentation(row, tag_desc)

    picker_items = _picker_items(tag_opts)
    glossary_html = _build_glossary_html(tag_opts)
    spec = _spec(picker_items, entity_key, glossary_html)
    return payload_box_css(spec.payload_box_id) + EXPLAINER_HTML + build_html(spec, rows)


# ---------------------------------------------------------------------------
# Save handler.
# ---------------------------------------------------------------------------

def _save_changes(changes_json: str) -> str:
    """Process a Save from the review UI: writes a backup of the entity's
    current mapping file (if it exists) THEN rewrites it in place via
    apply_corrections_map(). A blank entity, or no changes, never touches
    the filesystem. Returns a status markdown string.

    `changes_json` is the engine's syncPayload() shape: {context, changes,
    all_rows}. Each `changes` entry carries {_idx, _orig, guid, path, tag,
    _deleted} (guid/path/tag are the row's guid plus the two declared
    Columns whose values the entry needs at save time -- `path` for a
    previously-unmapped leaf that has no existing mapping-file entry to
    source it from, `tag` for the corrected value; `_deleted` is set by the
    engine's "Remove selected" toolbar button, spec.allow_delete=True).
    `entity_key` travels in `context` rather than a second Gradio input --
    see the module docstring for why.

    Delete semantics: a deleted row whose guid EXISTS in the entity's
    mapping file is actually removed from that file (the real need this
    covers: noise entries a user must be able to delete, not just hide).
    A deleted row whose guid is NOT in the mapping file (an unmapped/
    snippet-only suggestion) has nothing to persist -- the client already
    dropped it from the table, so it's counted separately and not written
    anywhere. A row can't be both deleted and corrected; `_deleted` always
    wins over a `tag` value on the same change entry.
    """
    if not changes_json or not changes_json.strip():
        return "No changes to save."

    try:
        payload = parse_payload(changes_json)
    except ValueError as e:
        return f"Error parsing changes: {e}"

    changes = payload["changes"]
    context = payload["context"]
    entity_key = str(context.get("entity_key") or "").strip()

    if not entity_key:
        return "Error: select an entity first -- nothing was saved."

    if not changes:
        return "No changes to save."

    _configs, _tag_vocab, amc = _itr_modules()

    mapping_file = _mapping_path(entity_key)
    mapping_file.parent.mkdir(parents=True, exist_ok=True)

    # Existing mapping-file guids BEFORE this save -- needed to tell a real
    # deletion (guid is persisted, must be removed from the file) from a
    # snippet-only "remove from view" delete (guid was never persisted, so
    # there's nothing to delete server-side).
    existing_guids: set[str] = set()
    existing_entries: dict = {}
    if mapping_file.is_file():
        try:
            existing_entries = dict(_configs.load_mapping(mapping_file).entries)
            existing_guids = set(existing_entries)
        except Exception:
            existing_guids = set()
            existing_entries = {}

    backup_msg = "No existing mapping file -- nothing to back up (cold start)."
    if mapping_file.is_file():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = mapping_file.with_name(f"{mapping_file.name}.bak-{stamp}")
        shutil.copy2(mapping_file, backup_path)
        backup_msg = f"Backup written: {backup_path}"

    corrections: dict[str, str] = {}
    paths: dict[str, str] = {}
    row_kinds: dict[str, str] = {}
    hold_reasons: dict[str, str] = {}
    deletions: set[str] = set()
    flag_changes: dict[str, dict] = {}
    skipped_blank = 0
    for ch in changes:
        guid = ch.get("guid")
        if not guid:
            skipped_blank += 1
            continue
        if ch.get("_deleted"):
            deletions.add(guid)
            continue
        tag = ch.get(TARGET_COL)
        # ITR-05: a changed foreign flag on a saved row. A flag-only change
        # never re-applies (and so never re-approves) the tag.
        prior = existing_entries.get(guid)
        if prior is not None and "al_foreign" in ch:
            want = bool(str(ch.get("al_foreign") or "").strip())
            if want != ("AL_FOREIGN" in (prior.flags or [])):
                flag_changes[guid] = {"AL_FOREIGN": want}
                if ch.get("path"):
                    paths[guid] = ch["path"]
                if str(tag or "").strip() == str(ch.get("_orig") or "").strip():
                    continue
        if not tag or not str(tag).strip():
            skipped_blank += 1
            continue
        corrections[guid] = str(tag).strip()
        if ch.get("path"):
            paths[guid] = ch["path"]
        # ITR-07: the row's side rides in the payload; a missing or unrecognised
        # side is treated as Unknown, which accepts almost nothing (fail closed).
        # A payload with no side at all (an older page) gets it from the path.
        if "kind" in ch:
            kind = str(ch.get("kind") or "").strip()
        else:
            kind = _row_kind({"path": ch.get("path") or ""}, None, None, _tag_vocab)
        row_kinds[guid] = kind if kind in _tag_vocab.ROW_KINDS else _tag_vocab.KIND_UNKNOWN
        if ch.get("hold_reason"):
            hold_reasons[guid] = str(ch["hold_reason"]).strip()

    persisted_deletions = deletions & existing_guids
    view_only_deletions = deletions - existing_guids

    if not corrections and not persisted_deletions and not flag_changes:
        if deletions:
            return (
                f"Removed {len(view_only_deletions)} unmapped row(s) from view "
                "(not present in the mapping file -- nothing to save). "
                f"{backup_msg}"
            )
        return (
            "No valid changes to save (all selections were blank). "
            f"{backup_msg}"
        )

    applied, invalid = amc.apply_corrections_map(
        str(mapping_file), corrections, str(mapping_file), paths=paths,
        deletions=persisted_deletions, hold_reasons=hold_reasons, row_kinds=row_kinds,
        flag_changes=flag_changes,
    )

    lines = [
        "**Saved**",
        "",
        f"{backup_msg}",
        f"Applied {applied} correction(s) -> {mapping_file}",
    ]
    if persisted_deletions:
        noun = "entry" if len(persisted_deletions) == 1 else "entries"
        lines.append(f"Deleted {len(persisted_deletions)} {noun} from the mapping file.")
    if view_only_deletions:
        lines.append(
            f"Removed {len(view_only_deletions)} unmapped row(s) from view "
            "(not persisted -- not present in the mapping file)."
        )
    if skipped_blank:
        lines.append(f"Skipped {skipped_blank} row(s) with a blank tag selection.")
    if invalid:
        lines.append(f"{len(invalid)} correction(s) NOT applied:")
        for path, guid, tag in invalid:
            if str(tag).startswith("flag:"):
                lines.append(f"  - {path} (guid {guid}): the foreign flag applies only to a mapped "
                             "Schedule AL asset account")
            elif _tag_vocab.is_valid_entry_tag(tag):
                side = row_kinds.get(guid, _tag_vocab.KIND_UNKNOWN)
                lines.append(
                    f"  - {path} (guid {guid}): {tag!r} is not valid for a {side} account "
                    "-- pick a tag listed for that side, or HOLD")
            else:
                lines.append(f"  - {path} (guid {guid}): {tag!r} is not a valid tag")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Gradio tab renderer.
# ---------------------------------------------------------------------------

def render(container_tab=None) -> None:
    """Render the Review Mapping tab (GnuCash > ITR > Review Mapping). Must be called inside gr.Tab()."""
    gr.Markdown(
        "## Review Mapping\n\n"
        "Review and correct account-to-tag mappings for an entity. Select an "
        "entity, click Load, assign tags to unmapped (or mis-tagged) "
        "accounts via the searchable dropdown, select rows, Apply to "
        "selected, then Save."
    )

    initial_entities = _entity_choices()
    with gr.Row():
        entity_dropdown = gr.Dropdown(
            label="Entity",
            choices=initial_entities,
            value=initial_entities[0][1] if initial_entities else None,
            allow_custom_value=True,
            interactive=True,
            scale=4,
        )
        refresh_btn = gr.Button("↻", scale=0, min_width=40)
        load_btn = gr.Button("Load for Review", variant="primary", scale=0)

    refresh_btn.click(
        fn=lambda: gr.update(choices=_entity_choices()),
        inputs=[], outputs=[entity_dropdown],
    )

    if container_tab is not None:
        def _rescan_entities():
            choices = _entity_choices()
            return gr.update(choices=choices, value=(choices[0][1] if choices else None))
        container_tab.select(fn=_rescan_entities, inputs=[], outputs=[entity_dropdown])

    review_html = gr.HTML(value="<p><em>Select an entity and click Load.</em></p>")

    with gr.Row():
        save_btn = gr.Button("Save", variant="primary")
    save_result = gr.Markdown("")

    # Real textbox (visible=True so it's in the DOM) hidden via CSS.
    # gr.State has no frontend element, so the js parameter can't inject into it.
    _payload_box = gr.Textbox(
        value="", show_label=False, container=False, lines=1,
        elem_id=f"{APP_ID}-payload-box",
    )

    load_btn.click(
        fn=_load_review_data,
        inputs=[entity_dropdown],
        outputs=review_html,
    )
    save_btn.click(
        fn=_save_changes,
        inputs=[_payload_box],
        outputs=[save_result],
        js=f"(x) => window.{PAYLOAD_VAR} || ''",
    )
