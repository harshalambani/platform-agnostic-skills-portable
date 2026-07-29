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
        stem = p.name[: -len(suffix)] if p.name.endswith(suffix) else p.stem
        matched = _snippet_entity_key(stem, match_map)
        if matched is not None and matched == entity_key:
            return p
    return None


def _entity_book_path(entity_key: str) -> Path | None:
    """Optional per-entity `book:` path from entities.yaml (Layer B -- see
    the module docstring). Deliberately read straight out of the raw YAML
    dict here rather than added to configs.EntityProfile, so this stays
    purely additive plumbing with zero blast radius on the entities CRUD
    tab's dataclass/dump/validate surface. Returns None whenever entities.
    yaml is missing/malformed, `entity_key` isn't defined in it, or the
    entity has no `book:` key -- every case falls back to Layer A only,
    never an error."""
    path = _config_mod.data_root_dir() / "itr" / "entities.yaml"
    if not path.is_file():
        return None
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    fields = raw.get(entity_key)
    if not isinstance(fields, dict):
        return None
    book = fields.get("book")
    if not book or not str(book).strip():
        return None
    return Path(str(book))


def _apply_book_validation(entity_key: str, snippet_rows: list[dict]) -> list[dict]:
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
    book_path = _entity_book_path(entity_key)
    if book_path is None or not book_path.is_file():
        return snippet_rows
    try:
        from agents.gnucash_accounts import load_accounts  # noqa: PLC0415
        accounts = {a.id: a for a in load_accounts(book_path)}
    except Exception:
        return snippet_rows
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
    if not entity_key or not entity_key.strip():
        return []
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
        rows.append({
            "guid": guid,
            "path": entry.path,
            "tag": entry.tag,
            "unmapped": False,
            # RAG confidence: an entry the LLM suggested that no human has
            # approved yet (suggested_by_llm still set) is "needs_review"
            # (amber); once a human has touched/approved it (cleared to
            # None -- see apply_corrections_map) it's "confirmed" (green).
            "needs_review": bool(entry.suggested_by_llm),
            "suggested": None,
            "note": entry.note or "",
        })

    snippet_path = _latest_proposed_mappings_path(entity_key)
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
                if suggested_tag == "REPLACE_ME":
                    suggested_tag = None
                snippet_rows.append({
                    "guid": guid,
                    "path": item.get("path", ""),
                    "tag": None,
                    "unmapped": True,
                    "needs_review": False,
                    "suggested": suggested_tag,
                    "note": item.get("note", ""),
                })
        rows.extend(_apply_book_validation(entity_key, snippet_rows))

    rows.sort(key=lambda r: (r["path"] or "", r["guid"]))
    return rows


def _tag_options() -> list[dict]:
    """(tag, sheet, target, description) rows from tags.py's vocabulary,
    sorted by tag. `sheet`/`target` (e.g. "RE"/"OtherSources") give the
    glossary a bit more orientation than the one-line treatment note alone."""
    _configs, tag_vocab, _amc = _itr_modules()
    return [
        {"tag": tag, "sheet": meta.sheet, "target": meta.target, "desc": meta.treatment}
        for tag, meta in sorted(tag_vocab.TAGS.items())
    ]


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
    if unmapped:
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
        ],
        target_col=TARGET_COL,
        payload_var=PAYLOAD_VAR,
        picker_label="Assign tag:",
        picker_placeholder="Type to search tags…",
        picker_items=picker_items,
        status_options=[
            ("unmapped", "Unmapped only"),
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
    )


def _load_review_data(entity_key: str) -> str:
    """Load `entity_key`'s mapping + latest proposed-mappings snippet into
    the interactive HTML table."""
    if not entity_key or not entity_key.strip():
        return "<p>Select an entity, then click Load.</p>"

    rows = _load_review_rows(entity_key)
    if not rows:
        return (
            f"<p>No accounts found for <strong>{entity_key}</strong>. "
            "Run the ITR Workbook once for this entity to generate a "
            "proposed-mappings snippet, or check "
            "<code>Data/itr/mappings/&lt;entity&gt;.mapping.yaml</code>.</p>"
        )

    tag_opts = _tag_options()
    tag_desc = {t["tag"]: t["desc"] for t in tag_opts}
    for row in rows:
        _row_presentation(row, tag_desc)

    picker_items = [PickerItem(value=t["tag"], primary=t["tag"], secondary=t["desc"]) for t in tag_opts]
    glossary_html = _build_glossary_html(tag_opts)
    spec = _spec(picker_items, entity_key, glossary_html)
    return payload_box_css(spec.payload_box_id) + build_html(spec, rows)


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
    if mapping_file.is_file():
        try:
            existing_guids = set(_configs.load_mapping(mapping_file).entries)
        except Exception:
            existing_guids = set()

    backup_msg = "No existing mapping file -- nothing to back up (cold start)."
    if mapping_file.is_file():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = mapping_file.with_name(f"{mapping_file.name}.bak-{stamp}")
        shutil.copy2(mapping_file, backup_path)
        backup_msg = f"Backup written: {backup_path}"

    corrections: dict[str, str] = {}
    paths: dict[str, str] = {}
    deletions: set[str] = set()
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
        if not tag or not str(tag).strip():
            skipped_blank += 1
            continue
        corrections[guid] = str(tag).strip()
        if ch.get("path"):
            paths[guid] = ch["path"]

    persisted_deletions = deletions & existing_guids
    view_only_deletions = deletions - existing_guids

    if not corrections and not persisted_deletions:
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
        deletions=persisted_deletions,
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
        lines.append(f"{len(invalid)} correction(s) NOT applied (unknown tag):")
        for path, guid, tag in invalid:
            lines.append(f"  - {path} (guid {guid}): {tag!r} is not a valid tag")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Gradio tab renderer.
# ---------------------------------------------------------------------------

def render(container_tab=None) -> None:
    """Render the ITR Mapping review tab. Must be called inside gr.Tab()."""
    gr.Markdown(
        "## ITR Mapping\n\n"
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
