"""
ui/_review_engine.py — the shared interactive "needs review" table engine.

Every review screen in this app is the same interaction: show rows a skill
could not fully resolve, let the user sort/filter/multi-select them, pick a
replacement value from a searchable list, apply it to the selection, and save.
That skeleton was originally copy-pasted between ui/tabs/gnucash_review.py and
ui/tabs/itr_mapping_review.py (identical _js_json, identical %%TOKEN%% HTML
template, identical eval-bootstrap, identical payload bridge, ~600 lines of
duplicated JS). This module is that skeleton, factored out once.

Design rule that makes the JS reusable: **presentation is computed in Python,
not in JavaScript.** A loader decides what a row looks like and hands the
engine plain data:

    row["_tags"]     list[str]  — drives the "Show:" status filter
    row["_rowclass"] str        — extra CSS classes on the <tr>
    row["_badges"]   dict       — {col_key: {"text","cls","title"}} chips
    row["_locked"]   bool       — row cannot be assigned (picker skips it)
    row["_note"]     str        — tooltip for the whole row

That keeps per-skill logic (contra highlighting, RAG confidence, "this review
reason isn't fixable from here") in Python where it is unit-testable, instead
of in an eval'd <script> blob where it is not.

Two Gradio workarounds are load-bearing and deliberately preserved from the
original screens:

  1. gr.HTML strips real <script> tags, so the init code is parked in a
     <script type="text/plain"> and run via an <img onerror="eval(...)">.
  2. gr.State has no DOM element for a `js=` parameter to write into, so the
     save payload rides on a CSS-hidden gr.Textbox with a known elem_id, and
     the click handler pulls it out of a window global.

Gradio-free by design (json + html + dataclasses only) so it can be unit
tested without spinning up the UI.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

SortType = Literal["text", "number", "order"]


# ---------------------------------------------------------------------------
# Public row-metadata keys (documented above; centralised so tests can assert
# against the names rather than hard-coding strings in several places).
# ---------------------------------------------------------------------------

META_TAGS = "_tags"
META_ROWCLASS = "_rowclass"
META_BADGES = "_badges"
META_LOCKED = "_locked"
META_NOTE = "_note"
META_BAND = "_band"   # optional: one match-type band class, kept apart from _rowclass

_META_KEYS = (META_TAGS, META_ROWCLASS, META_BADGES, META_LOCKED, META_NOTE, META_BAND)


@dataclass(frozen=True)
class Column:
    """One table column.

    `key` indexes the row dict. `sort` of "number" parses floats before
    comparing; "order" ranks values by their position in `order` (used for
    confidence columns, where "suspense" must sort before "high" rather than
    alphabetically).
    """
    key: str
    label: str
    sortable: bool = True
    sort: SortType = "text"
    order: tuple[str, ...] = ()
    # UI-06. When set, the cell is editable (double-click). The user's text is
    # stored under THIS row key, never over `key`: the value in `key` stays the
    # original, so sorting, filtering, "apply to matching" and anything keyed on
    # the original text keep working. What the save handler does with the edit
    # is the consumer's business.
    edit_key: str = ""


@dataclass(frozen=True)
class PickerItem:
    """One entry in the searchable assign-dropdown.

    `primary` renders bold, `secondary` muted beside it. Both are matched by
    the search box. `value` is what actually gets written into the target
    column.
    """
    value: str
    primary: str
    secondary: str = ""


@dataclass
class ReviewSpec:
    """Everything a review screen needs to render and save.

    app_id namespaces every DOM id and CSS rule, so two engines on the same
    Gradio page cannot collide.
    """
    app_id: str
    columns: list[Column]
    target_col: str
    payload_var: str
    picker_label: str = "Assign:"
    picker_placeholder: str = "Type to search…"
    picker_items: list[PickerItem] = field(default_factory=list)
    status_options: list[tuple[str, str]] = field(default_factory=list)
    status_label: str = "Show:"
    default_sort: str = ""
    apply_matching_on: str = ""
    apply_matching_label: str = "Apply to matching"
    also_set: dict[str, str] = field(default_factory=dict)
    # Distinct also_set values for the "apply to matching" action (e.g. a
    # different provenance/MatchReason string than the ordinary apply path).
    # None (the default) falls back to `also_set` for both actions, which is
    # exactly today's behaviour for screens that don't need the distinction.
    also_set_matching: dict[str, str] | None = None
    extra_panel_html: str = ""
    context: dict[str, Any] = field(default_factory=dict)
    # When True, renders a "Remove selected" toolbar button that marks the
    # current selection as deleted (client-side `_deleted=true`, struck
    # through/dimmed) rather than reassigning TARGET. Default False keeps
    # every existing screen (gnucash_review, tds_journal_review, ...)
    # byte-for-byte unchanged. See itr_mapping_review.py for the first
    # consumer (row-level "delete this mapping entry").
    allow_delete: bool = False
    # IMP-11. When True, renders "Don't import selected" / "Import selected"
    # toolbar buttons. Rows flagged `_excluded` (by the loader) start struck
    # through and are NOT in the payload's `all_rows`; they travel in
    # `excluded` instead, so the default outcome for them is "not imported" and
    # the user opts one back in. Default False renders nothing extra.
    allow_exclude: bool = False
    # UI-04. When set (e.g. "Confidence"), assigning a row makes its
    # target-column badge and its leading status tag follow the row's new value
    # of this column: the badge on TARGET (SUSPENSE / DORMANT?) described the OLD
    # target and is dropped, and _tags[0] becomes the lower-cased new value so
    # the "Filter:" dropdown agrees with the row. Default "" keeps every other
    # screen unchanged.
    status_col: str = ""
    # UI-05. {lower-cased status value: CSS class}. With status_col set, an
    # assign swaps the row's class from this table (e.g. "accent-orange" ->
    # "accent-blue") so the coloured band follows the row's new match type.
    # A value with no entry gets no class. Empty (default) = untouched.
    status_classes: dict[str, str] = field(default_factory=dict)

    @property
    def payload_box_id(self) -> str:
        return f"{self.app_id}-payload-box"


# ---------------------------------------------------------------------------
# Safe embedding
# ---------------------------------------------------------------------------

def js_json(value: Any) -> str:
    """json.dumps that is safe to embed inside an inline <script> element.

    Plain json.dumps does not escape "<", so a value containing "</script>"
    can break out of the surrounding script tag and inject markup. Escaping
    &, < and > as \\u00xx keeps the payload valid JSON while making it inert
    as markup.
    """
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def prepare_rows(rows: list[dict]) -> list[dict]:
    """Normalise loader output so the JS can assume every meta key exists.

    Missing meta keys are filled with empty defaults rather than left absent,
    so the client never has to null-check them.
    """
    out: list[dict] = []
    for r in rows:
        row = dict(r)
        row.setdefault(META_TAGS, [])
        row.setdefault(META_ROWCLASS, "")
        row.setdefault(META_BADGES, {})
        row.setdefault(META_LOCKED, False)
        row.setdefault(META_NOTE, "")
        out.append(row)
    return out


def payload_box_css(elem_id: str) -> str:
    """CSS that hides the payload Textbox while leaving it in the DOM.

    It must stay visible=True for Gradio's `js=` parameter to reach it, so it
    is moved offscreen rather than display:none'd.
    """
    return (
        f"<style>#{elem_id}, #{elem_id} * {{ position: absolute !important; "
        f"left: -9999px !important; height: 0 !important; overflow: hidden "
        f"!important; opacity: 0 !important; pointer-events: none !important; }}"
        f"</style>"
    )


# ---------------------------------------------------------------------------
# CSS — one copy, namespaced by app_id at build time.
# ---------------------------------------------------------------------------

# UI-09: ONE source of truth for the match-type accent colours. They drive the
# 3px first-cell stripe, the whole-row tint and the legend swatches, so the
# three can never drift apart.
ACCENT_COLOURS = {
    "red": "#f87171",
    "amber": "#fbbf24",
    "green": "#4ade80",
    "blue": "#60a5fa",
    "orange": "#fb923c",
}
ACCENT_TINT_ALPHA = 0.18


def _rgb(hex_colour: str) -> str:
    h = hex_colour.lstrip("#")
    return f"{int(h[0:2], 16)},{int(h[2:4], 16)},{int(h[4:6], 16)}"


def _accent_css() -> str:
    """Row accents (set via _rowclass / _band): the first-cell stripe stays; every
    cell also gets an overlay of the accent at ACCENT_TINT_ALPHA. It is a
    background-IMAGE so the row's own background (zebra) shows through."""
    out = ["/* Row accents: stripe + whole-row tint (UI-09). */"]
    for name, hx in ACCENT_COLOURS.items():
        rgba = f"rgba({_rgb(hx)},{ACCENT_TINT_ALPHA})"
        out.append(f"#%%APP%%-app tbody tr.accent-{name} td:first-child {{ border-left: 3px solid {hx}; }}")
        out.append(f"#%%APP%%-app tbody tr.accent-{name} td {{ background-image: linear-gradient({rgba}, {rgba}); }}")
    out.append("#%%APP%%-app .legend { display: flex; flex-wrap: wrap; gap: 12px; font-size: 11px; color: #bbb; margin: 4px 0 8px; }")
    out.append("#%%APP%%-app .legend .sw { display: inline-block; width: 10px; height: 10px; margin-right: 4px; vertical-align: middle; }")
    for name, hx in ACCENT_COLOURS.items():
        out.append(f"#%%APP%%-app .legend .sw.{name} {{ background: {hx}; }}")
    return "\n".join(out)


def _tint_off_css() -> str:
    """Declared AFTER the accent rules (same specificity, later wins): a selected
    row keeps its solid reverse colours, and a contra row's tone is its one
    background (the stripe still marks the match type). A plain HOVER does NOT
    drop the tint: it keeps its match-type hue plus a non-fill cue (see _CSS)."""
    return "\n".join([
        "/* UI-09: the tint never competes with selected / a contra tone. */",
        "#%%APP%%-app tbody tr.selected td,",
        "#%%APP%%-app tbody tr.tone-amber td,",
        "#%%APP%%-app tbody tr.tone-green td { background-image: none; }",
    ])


_CSS = r"""
<style>
#%%APP%%-app {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 13px; color: #e0e0e0;
}
#%%APP%%-app .toolbar {
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  padding: 8px 0; border-bottom: 1px solid #333; margin-bottom: 8px;
}
#%%APP%%-app .toolbar label { font-weight: 600; font-size: 12px; color: #ccc; }
#%%APP%%-app .toolbar button {
  padding: 4px 12px; border-radius: 4px; font-size: 12px; cursor: pointer;
  border: 1px solid #444; background: #2a2a2a; color: #e0e0e0;
}
#%%APP%%-app .toolbar button.primary {
  background: #2563eb; color: #fff; border-color: transparent;
}
#%%APP%%-app .toolbar select {
  padding: 5px 8px; border: 1px solid #555; border-radius: 4px;
  font-size: 12px; background: #1a1a1a; color: #f0f0f0;
}
#%%APP%%-app .toolbar select:hover { border-color: #777; }
#%%APP%%-app .toolbar select:focus { border-color: #2563eb; outline: none; }
#%%APP%%-app .toolbar .spacer { flex: 1; }
#%%APP%%-app .stats { font-size: 11px; color: #999; padding: 4px 0; }

#%%APP%%-app table { width: 100%; border-collapse: separate; border-spacing: 0; table-layout: auto; }
/* UI-08b: SEPARATE (not collapsed) borders: a scrolled row's border and 3px accent
   stripe are painted inside its own cell, so they never bleed through the sticky header. */
/* UI-08: the title row and the filter row stick together as ONE block (sticky on
   thead, opaque, above the body rows) so the filters never scroll away. */
#%%APP%%-app thead {
  position: sticky; top: 0; z-index: 3; background: #1a1a1a;
  box-shadow: 0 2px 0 #444;
}
#%%APP%%-app thead th {
  background: #1e1e1e; color: #ccc; border-bottom: 2px solid #444;
  padding: 6px 8px; text-align: left; font-size: 12px;
  cursor: pointer; user-select: none; white-space: nowrap;
}
#%%APP%%-app thead th:hover { background: #2a2a2a; }
/* UI-11: drag the right edge of a header to resize that column. The grab zone is
   6px wide; a header that no longer fits its text clips it with an ellipsis. */
#%%APP%%-app thead th { position: relative; overflow: hidden; text-overflow: ellipsis; }
#%%APP%%-app thead th .col-resizer {
  position: absolute; top: 0; right: 0; width: 6px; height: 100%;
  cursor: col-resize; z-index: 2; user-select: none; touch-action: none;
}
#%%APP%%-app thead th .col-resizer:hover,
#%%APP%%-app thead th .col-resizer.dragging { background: rgba(96,165,250,0.55); }
#%%APP%%-app table.resized { table-layout: fixed; width: auto; max-width: none; }
#%%APP%%-app table.resized tbody td { max-width: none; }
#%%APP%%-app .stats .reset-widths { margin-left: 8px; color: #60a5fa; cursor: pointer; text-decoration: underline; }
#%%APP%%-app thead th .sort-arrow { margin-left: 4px; font-size: 10px; }
#%%APP%%-app thead .filter-row td {
  padding: 3px 4px; background: #1a1a1a; border-bottom: 1px solid #444;
}
#%%APP%%-app thead .filter-row input {
  width: 100%; box-sizing: border-box; padding: 3px 6px;
  border: 1px solid #333; border-radius: 3px; font-size: 11px;
  background: #111; color: #ccc;
}
#%%APP%%-app thead .filter-row input:focus { border-color: #2563eb; outline: none; }
/* UI-10: a filter box with text is marked (lighter blue border + tinted fill, so it
   differs from the focus state, which is the darker blue border alone), carries an X
   to empty it, and its column header gets a small mark. */
#%%APP%%-app thead .filter-row td { position: relative; }
#%%APP%%-app thead .filter-row input.has-text {
  border-color: #60a5fa; background: #14233d; color: #e0e0e0; padding-right: 20px;
}
#%%APP%%-app thead .filter-row input.has-text:focus { border-color: #2563eb; }
#%%APP%%-app thead .filter-row .f-clear {
  position: absolute; right: 7px; top: 50%; transform: translateY(-50%);
  width: 14px; height: 14px; padding: 0; line-height: 12px; font-size: 13px;
  border: none; border-radius: 50%; background: transparent; color: #999; cursor: pointer;
}
#%%APP%%-app thead .filter-row .f-clear:hover { background: #333; color: #fff; }
#%%APP%%-app thead th .filter-mark { margin-left: 4px; font-size: 11px; color: #60a5fa; }
#%%APP%%-app .stats .clear-all { margin-left: 8px; color: #60a5fa; cursor: pointer; text-decoration: underline; }
#%%APP%%-app thead .filter-row input::placeholder { color: #555; }

#%%APP%%-app tbody tr {
  cursor: pointer; transition: background 0.1s;
}
#%%APP%%-app tbody tr:nth-child(even) { background: #111; }
#%%APP%%-app tbody tr:hover { background: #1a2744; }
#%%APP%%-app tbody tr.selected { background: #1e3a5f; }
#%%APP%%-app tbody tr.selected:hover { background: #254a73; }
#%%APP%%-app tbody tr.locked { opacity: 0.75; cursor: not-allowed; }
#%%APP%%-app tbody tr.row-deleted td { text-decoration: line-through; opacity: 0.55; }
/*%%EXCLUDE_CSS%%*/
#%%APP%%-app tbody td {
  padding: 5px 8px; font-size: 12px; max-width: 420px; border-bottom: 1px solid #262626;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #ddd;
}

/*%%ACCENT_CSS%%*/
#%%APP%%-app tbody tr.tone-amber  { background: #3a2a1d; }
#%%APP%%-app tbody tr.tone-amber:hover { background: #4a3626; }
#%%APP%%-app tbody tr.tone-green  { background: #16281d; }
#%%APP%%-app tbody tr.tone-green:hover { background: #1e3a2a; }
#%%APP%%-app tbody tr.tone-amber.selected,
#%%APP%%-app tbody tr.tone-green.selected { background: #1e3a5f; }
/*%%TINT_OFF_CSS%%*/
/* UI-09: hover keeps the row's accent tint (so it never reads as another match
   type) and adds a NON-fill cue: a light inset line above and below every cell. */
#%%APP%%-app tbody tr:hover td { box-shadow: inset 0 1px 0 #9db4d9, inset 0 -1px 0 #9db4d9; }
/* UI-09: a selected row is a REVERSE highlight -- light cells, dark text, a light
   top/bottom edge -- so it reads at a glance over every tint (including the blue
   override rows) and does not rely on hue. Declared after the tint, equal or
   higher specificity, so it always wins. */
#%%APP%%-app tbody tr.selected td {
  background-color: #dbe7ff; background-image: none; color: #0b1220;
  box-shadow: inset 0 2px 0 #ffffff, inset 0 -2px 0 #ffffff;
}
#%%APP%%-app tbody tr.selected:hover td { background-color: #c3d6ff; }
#%%APP%%-app tbody tr.selected td:first-child { box-shadow: inset 0 2px 0 #ffffff, inset 0 -2px 0 #ffffff, inset 5px 0 0 #1e3a8a; }

/* Badge + confidence colours — bright on dark, WCAG AA at 12px. */
#%%APP%%-app .badge {
  display: inline-block; font-size: 10px; padding: 1px 5px;
  border-radius: 3px; margin-right: 4px; font-weight: 600;
}
#%%APP%%-app .badge.red    { background: #7f1d1d; color: #fecaca; }
#%%APP%%-app .badge.amber  { background: #7c2d12; color: #fdba74; }
#%%APP%%-app .badge.green  { background: #14532d; color: #86efac; }
#%%APP%%-app .badge.blue   { background: #1e3a8a; color: #bfdbfe; }
#%%APP%%-app .badge.grey   { background: #374151; color: #d1d5db; }
#%%APP%%-app .badge.violet { background: #4c1d95; color: #ddd6fe; }
#%%APP%%-app .t-red    { color: #f87171; font-weight: 600; }
#%%APP%%-app .t-amber  { color: #fbbf24; font-weight: 600; }
#%%APP%%-app .t-green  { color: #4ade80; }
#%%APP%%-app .t-blue   { color: #60a5fa; }
#%%APP%%-app .t-purple { color: #a78bfa; font-weight: 600; }
#%%APP%%-app .t-cyan   { color: #22d3ee; }
#%%APP%%-app .t-orange { color: #fb923c; }
#%%APP%%-app .changed-marker { color: #a78bfa; font-weight: bold; margin-left: 4px; }

/* Searchable assign picker. */
#%%APP%%-app .picker { position: relative; display: inline-block; }
#%%APP%%-app .picker-search {
  width: 320px; padding: 5px 8px; border: 1px solid #444;
  border-radius: 4px; font-size: 12px; background: #1a1a1a; color: #e0e0e0;
}
#%%APP%%-app .picker-dropdown {
  position: absolute; top: 100%; left: 0; z-index: 100;
  width: 460px; max-height: 280px; overflow-y: auto;
  border: 1px solid #444; background: #1a1a1a; border-radius: 4px;
  box-shadow: 0 4px 16px rgba(0,0,0,0.5); display: none;
}
#%%APP%%-app .picker-dropdown.open { display: block; }
#%%APP%%-app .picker-dropdown .picker-item {
  padding: 5px 10px; cursor: pointer; font-size: 12px; color: #e0e0e0;
}
#%%APP%%-app .picker-dropdown .picker-item:hover { background: #1e3a5f; }
#%%APP%%-app .picker-dropdown .picker-item .p-primary { font-weight: 600; color: #fff; }
#%%APP%%-app .picker-dropdown .picker-item .p-secondary { color: #888; font-size: 11px; }
#%%APP%%-app .scroll-wrapper {
  max-height: 65vh; overflow-y: auto; overflow-x: auto; border: 1px solid #333; border-radius: 4px;
}
</style>
"""
_CSS = _CSS.replace("/*%%ACCENT_CSS%%*/", _accent_css()).replace(
    "/*%%TINT_OFF_CSS%%*/", _tint_off_css())


# ---------------------------------------------------------------------------
# Markup + client code. %%TOKEN%% slots are filled by build_html().
# ---------------------------------------------------------------------------

_BODY = r"""
<div id="%%APP%%-app">
  <div class="toolbar">
    %%STATUS_FILTER_HTML%%
    <span class="spacer"></span>
    <span class="stats" id="%%APP%%-stats"></span>
    <a href="#" class="stats reset-widths" id="%%APP%%-reset-widths"
       title="Forget the column widths you dragged on this screen">Reset widths</a>
    <a href="#" class="stats clear-all" id="%%APP%%-clear-all" style="display:none"></a>
  </div>

  <div class="toolbar">
    <label>%%PICKER_LABEL%%</label>
    <div class="picker">
      <input type="text" class="picker-search" id="%%APP%%-picker-search"
             placeholder="%%PICKER_PLACEHOLDER%%" autocomplete="off">
      <div class="picker-dropdown" id="%%APP%%-picker-dropdown"></div>
    </div>
    <button id="%%APP%%-apply-sel" class="primary" title="Apply to selected rows">Apply to selected</button>
    %%APPLY_MATCH_HTML%%
    %%DELETE_BTN_HTML%%%%EXCLUDE_BTN_HTML%%
    <span class="spacer"></span>
  </div>

  %%EXTRA_PANEL%%

  <div class="scroll-wrapper">
    <table id="%%APP%%-table">
      <colgroup id="%%APP%%-cols"></colgroup>
      <thead id="%%APP%%-thead"><tr></tr></thead>
      <tbody id="%%APP%%-tbody"></tbody>
    </table>
  </div>
</div>

<script type="text/plain" id="%%APP%%-init-code">
(function() {
  const APP        = %%APP_JSON%%;
  const DATA       = %%DATA_JSON%%;
  const COLS       = %%COLS_JSON%%;
  const ITEMS      = %%ITEMS_JSON%%;
  const TARGET     = %%TARGET_JSON%%;
  const ALSO_SET   = %%ALSO_SET_JSON%%;
  const ALSO_SET_MATCHING = %%ALSO_SET_MATCHING_JSON%%;
  const MATCH_ON   = %%MATCH_ON_JSON%%;
  const CONTEXT    = %%CONTEXT_JSON%%;
  const PAYLOAD_VAR = %%PAYLOAD_VAR_JSON%%;
  const STATUS_COL = %%STATUS_COL_JSON%%;
  const STATUS_CLASSES = %%STATUS_CLASSES_JSON%%;

  const $ = (suffix) => document.getElementById(APP + '-' + suffix);

  // Escape before every innerHTML insertion — row values originate from
  // parsed PDF/bank/LLM content and must never be trusted as markup.
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  let rows = DATA.map((r, i) => ({
    ...r, _idx: i, _changed: false, _orig: r[TARGET] || '', _deleted: false
  }));
  let selected = new Set();
  let lastClickIdx = null;
  let statusFilter = '';
  let colFilters = {};
  let focusAfter = null;   // UI-10: filter box to focus after the next render (X / Clear all)
  let shownCount = 0;      // rows in the current view (for the stats line)
  let editing = false;     // UI-06: an inline edit box is open
  let sortCol = %%DEFAULT_SORT_JSON%%;
  let sortAsc = true;

  function cellText(r, key) { return (r[key] === null || r[key] === undefined) ? '' : String(r[key]); }

  // ── Status filter ──
  const statusDD = $('status');
  if (statusDD) statusDD.onchange = (e) => { statusFilter = e.target.value; renderTable(); };

  // ── Searchable picker ──
  const pickSearch = $('picker-search');
  const pickDD = $('picker-dropdown');
  let chosen = '';

  function renderPicker(q) {
    const ql = (q || '').toLowerCase();
    const list = ql
      ? ITEMS.filter(it => (it.primary + ' ' + it.secondary + ' ' + it.value).toLowerCase().includes(ql))
      : ITEMS;
    pickDD.innerHTML = '';
    list.slice(0, 50).forEach(it => {
      const div = document.createElement('div');
      div.className = 'picker-item';
      div.innerHTML = '<span class="p-primary">' + esc(it.primary) + '</span> ' +
                      '<span class="p-secondary">' + esc(it.secondary) + '</span>';
      div.onclick = () => {
        chosen = it.value; pickSearch.value = it.value; pickDD.classList.remove('open');
      };
      pickDD.appendChild(div);
    });
    if (!list.length) pickDD.innerHTML = '<div class="picker-item">No matches.</div>';
  }
  pickSearch.onfocus = () => { renderPicker(pickSearch.value); pickDD.classList.add('open'); };
  pickSearch.oninput = () => { chosen = ''; renderPicker(pickSearch.value); pickDD.classList.add('open'); };
  document.addEventListener('click', (e) => {
    if (!e.target.closest('#' + APP + '-app .picker')) pickDD.classList.remove('open');
  });

  function assign(predicate, alsoSet) {
    if (!chosen) { alert('Pick a value first.'); return 0; }
    let n = 0;
    rows.forEach(r => {
      if (r._locked) return;
      if (!predicate(r)) return;
      r[TARGET] = chosen;
      for (const k in alsoSet) r[k] = alsoSet[k];
      if (STATUS_COL) {
        // UI-04: the badge described the old target -- drop it; keep the tags in step.
        if (r._badges && r._badges[TARGET]) delete r._badges[TARGET];
        const st = cellText(r, STATUS_COL).toLowerCase();
        if (st && Array.isArray(r._tags) && r._tags.length) r._tags[0] = st;
        if (Object.keys(STATUS_CLASSES).length) r._band = STATUS_CLASSES[st] || '';
      }
      r._changed = true;
      n++;
    });
    if (n > 0) markDirty();
    syncPayload();
    renderTable();
    return n;
  }

  $('apply-sel').onclick = () => {
    if (selected.size === 0) { alert('Select rows first (click / shift-click / ctrl-click).'); return; }
    const n = assign(r => selected.has(r._idx), ALSO_SET);
    if (n === 0 && chosen) alert('Selected rows cannot be reassigned from here.');
  };

  const applyMatchBtn = $('apply-match');
  if (applyMatchBtn && MATCH_ON) {
    applyMatchBtn.onclick = () => {
      if (selected.size === 0) { alert('Select a row first to define the pattern.'); return; }
      const keys = new Set();
      rows.forEach(r => { if (selected.has(r._idx)) keys.add(cellText(r, MATCH_ON)); });
      const n = assign(r => keys.has(cellText(r, MATCH_ON)), ALSO_SET_MATCHING);
      alert('Applied to ' + n + ' matching row' + (n === 1 ? '' : 's') + '.');
    };
  }

  // ── Delete selected (opt-in via spec.allow_delete) ──
  const removeBtn = $('remove-sel');
  if (removeBtn) {
    removeBtn.onclick = () => {
      if (selected.size === 0) { alert('Select rows first (click / shift-click / ctrl-click).'); return; }
      if (!confirm('Remove ' + selected.size + ' selected row' + (selected.size === 1 ? '' : 's') + '? ' +
                    'This marks them for deletion and cannot be undone here.')) return;
      rows.forEach(r => {
        if (r._locked || !selected.has(r._idx)) return;
        r._deleted = true;
        r._changed = true;
      });
      markDirty();
      syncPayload();
      renderTable();
    };
  }

%%EXCLUDE_JS%%  // ── JS → Python bridge. gr.State has no DOM node, so the save handler
  //    reads this global via its js= parameter at click time.
  // UI-12: unsaved-work flag, published on window[PAYLOAD_VAR + 'Dirty'] so the
  // page's Load / Reset buttons can ask before throwing the edits away. Set only
  // by a real user edit (assign / Remove / inline description edit / exclude
  // toggle) -- never by syncPayload() itself, which also runs once at init.
  // Cleared by the host after a successful save.
  const DIRTY_VAR = PAYLOAD_VAR + 'Dirty';
  window[DIRTY_VAR] = false;
  function markDirty() { window[DIRTY_VAR] = true; }
  if (typeof window.addEventListener === 'function' && !window[DIRTY_VAR + 'Guard']) {
    // Best effort: a browser tab shows its native "leave site?" prompt. An
    // embedded webview may suppress it, in which case the Load / Reset
    // confirmation is the protection.
    window[DIRTY_VAR + 'Guard'] = true;
    window.addEventListener('beforeunload', (e) => {
      if (!window[DIRTY_VAR]) return;
      e.preventDefault();
      e.returnValue = '';
      return '';
    });
  }

  function syncPayload() {
    const changes = rows.filter(r => r._changed).map(r => {
      const out = { _idx: r._idx, _orig: r._orig, _deleted: !!r._deleted };
      for (const c of COLS) out[c.key] = r[c.key];
      for (const k of ['guid', 'Sr', 'Transaction ID', 'CN No']) {
        if (r[k] !== undefined) out[k] = r[k];
      }
      return out;
    });
    window[PAYLOAD_VAR] = JSON.stringify({
      context: CONTEXT,
      changes: changes,
      all_rows: rows%%EXCL_FILTER%%.map(r => {
        const out = {};
        for (const k in r) if (k.charAt(0) !== '_') out[k] = r[k];
        return out;
      }),%%EXCL_FIELDS%%
    });
  }
  syncPayload();

  // UI-10: empty the given column filter boxes and re-render. Only colFilters
  // changes: the "Filter:" dropdown, the selection and pending changes are left alone.
  // Focus lands in the (first) emptied box, never the grid or the page top.
  function clearFilters(cols) {
    cols.forEach(k => { colFilters[k] = ''; });
    focusAfter = cols[0];
    renderTable();
  }
  const clearAll = $('clear-all');
  if (clearAll) clearAll.onclick = (e) => {
    if (e && e.preventDefault) e.preventDefault();
    const cols = Object.keys(colFilters).filter(k => colFilters[k]);
    if (cols.length) clearFilters(cols);
  };

  // The rows-counter line. Also called after an in-place selection change, so a
  // plain click never has to rebuild the table (UI-06).
  function updateStats() {
    const changed = rows.filter(r => r._changed && !r._deleted).length;
    const deleted = rows.filter(r => r._deleted).length;
    const editCols = COLS.filter(c => c.edit_key);
    const edited = editCols.length
      ? rows.filter(r => editCols.some(c => {
          const t = cellText(r, c.edit_key).trim();
          return t && t !== cellText(r, c.key).trim();
        })).length
      : 0;
    $('stats').textContent =
      shownCount + '/' + rows.length + ' rows' +
      (selected.size ? ' | ' + selected.size + ' selected' : '') +
      (changed ? ' | ' + changed + ' changed' : '') +
      (edited ? ' | ' + edited + ' edited' : '') +
      (deleted ? ' | ' + deleted + ' deleted' : '');
  }

  // ── UI-11: resizable columns ──
  // Widths live in colW {column key: px}, are remembered per screen (APP) in
  // localStorage (try/catch: when storage is unavailable they still hold for the
  // session) and are applied through a <colgroup>, so a re-render, sort or filter
  // never loses them. Once any width is set the table is fixed-layout and as wide
  // as its columns; the wrapper scrolls sideways instead of squeezing the others.
  const MIN_COL_W = 40;
  const W_KEY = 'pask.colw.' + APP;
  let colW = {};
  let suppressClick = false;
  let drag = null;
  function loadWidths() {
    try {
      const raw = window.localStorage && window.localStorage.getItem(W_KEY);
      const o = raw ? JSON.parse(raw) : null;
      if (o && typeof o === 'object') {
        COLS.forEach(c => {
          const v = Number(o[c.key]);
          if (isFinite(v) && v >= MIN_COL_W) colW[c.key] = Math.round(v);
        });
      }
    } catch (err) { /* storage unavailable: session-only */ }
  }
  function saveWidths() {
    try {
      if (!window.localStorage) return;
      if (Object.keys(colW).length) window.localStorage.setItem(W_KEY, JSON.stringify(colW));
      else window.localStorage.removeItem(W_KEY);
    } catch (err) { /* storage unavailable: session-only */ }
  }
  function applyWidths() {
    const cols = $('cols'), tbl = $('table');
    if (!cols || !tbl) return;
    if (!Object.keys(colW).length) {
      cols.innerHTML = '';
      if (tbl.classList) tbl.classList.remove('resized');
      tbl.style.width = '';
      return;
    }
    let total = 0;
    cols.innerHTML = COLS.map(c => {
      const w = colW[c.key] || 160; total += w;
      return '<col style="width:' + w + 'px">';
    }).join('');
    if (tbl.classList) tbl.classList.add('resized');
    tbl.style.width = total + 'px';
  }
  function thWidth(th) {
    if (!th) return 0;
    if (th.getBoundingClientRect) { const w = th.getBoundingClientRect().width; if (w) return Math.round(w); }
    return th.offsetWidth || 0;
  }
  // freeze every column at its current width (first resize): the others must not move
  function snapshotWidths() {
    if (Object.keys(colW).length) return;
    const ths = $('thead').querySelectorAll('th');
    COLS.forEach((c, i) => { colW[c.key] = Math.max(MIN_COL_W, thWidth(ths[i]) || 160); });
  }
  // the width a column needs to show its longest cell (and its header) unclipped
  function fitWidth(key) {
    const i = COLS.findIndex(c => c.key === key);
    const app = document.getElementById(APP + '-app');
    const m = document.createElement('span');
    m.style.cssText = 'position:absolute;visibility:hidden;white-space:nowrap;left:-9999px;top:0;font-size:12px;';
    app.appendChild(m);
    let best = 0;
    const measure = (html) => { m.innerHTML = html; best = Math.max(best, m.offsetWidth || 0); };
    $('tbody').querySelectorAll('tr').forEach(tr => {
      const td = tr.children[i];
      if (td) measure(td.innerHTML);
    });
    const th = $('thead').querySelectorAll('th')[i];
    if (th) measure(th.textContent);
    app.removeChild(m);
    return Math.max(MIN_COL_W, Math.min(1200, Math.ceil(best) + 20));
  }
  function onDragMove(e) {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    if (!drag.moved && Math.abs(dx) < 1) return;
    if (!drag.moved) { drag.moved = true; snapshotWidths(); drag.w0 = colW[drag.key] || drag.w0; }
    colW[drag.key] = Math.max(MIN_COL_W, Math.round(drag.w0 + dx));
    applyWidths();
  }
  function onDragEnd() {
    document.removeEventListener('mousemove', onDragMove);
    document.removeEventListener('mouseup', onDragEnd);
    if (!drag) return;
    if (drag.handle && drag.handle.classList) drag.handle.classList.remove('dragging');
    if (drag.moved) {
      saveWidths();
      suppressClick = true;                 // the click that ends a drag must not sort
      setTimeout(() => { suppressClick = false; }, 0);
    }
    drag = null;
  }
  const theadEl = $('thead');
  theadEl.addEventListener('mousedown', (e) => {
    const h = e && e.target;
    if (!h || !h.classList || !h.classList.contains('col-resizer')) return;
    // no default action: no text selection, and a filter box keeps its focus
    if (e.preventDefault) e.preventDefault();
    if (e.stopPropagation) e.stopPropagation();
    const key = h.dataset.col;
    drag = { key, x: e.clientX, moved: false, handle: h,
             w0: colW[key] || thWidth(h.parentNode) || 160 };
    if (h.classList.add) h.classList.add('dragging');
    document.addEventListener('mousemove', onDragMove);
    document.addEventListener('mouseup', onDragEnd);
  });
  theadEl.addEventListener('dblclick', (e) => {
    const h = e && e.target;
    if (!h || !h.classList || !h.classList.contains('col-resizer')) return;
    if (e.preventDefault) e.preventDefault();
    if (e.stopPropagation) e.stopPropagation();
    snapshotWidths();
    colW[h.dataset.col] = fitWidth(h.dataset.col);
    applyWidths();
    saveWidths();
  });
  const resetBtn = $('reset-widths');
  if (resetBtn) resetBtn.onclick = (e) => {
    if (e && e.preventDefault) e.preventDefault();
    colW = {};
    saveWidths();
    applyWidths();
  };
  loadWidths();
  applyWidths();

  // ── Rendering ──
  function renderTable() {
    const thead = $('thead');
    const tbody = $('tbody');

    // UI-08: give focus back to a filter box ONLY if one had it when this render
    // began (i.e. the user is typing in it). No sticky marker: a row click, Apply
    // or a sort after typing never pulls the cursor back into a filter.
    const ae = document.activeElement;
    let refocus = null;
    if (ae && ae.tagName === 'INPUT' && thead.contains(ae) && ae.dataset && ae.dataset.col) {
      refocus = { col: ae.dataset.col, s: ae.selectionStart, e: ae.selectionEnd };
    }
    if (focusAfter) { refocus = { col: focusAfter, s: 0, e: 0 }; focusAfter = null; }

    thead.innerHTML = '<tr>' + COLS.map(c =>
      '<th data-col="' + esc(c.key) + '"' + (colFilters[c.key] ? ' class="filtered"' : '') + '>' +
      esc(c.label) +
      (colFilters[c.key] ? '<span class="filter-mark" title="Filtered">⌕</span>' : '') +
      (c.key === sortCol ? '<span class="sort-arrow">' + (sortAsc ? '▲' : '▼') + '</span>' : '') +
      '<span class="col-resizer" data-col="' + esc(c.key) + '" title="Drag to resize, double-click to fit"></span>' +
      '</th>').join('') + '</tr>';
    thead.querySelectorAll('th').forEach(th => {
      th.title = th.textContent;
      th.onclick = (e) => {
        // UI-11: a click that ends a column drag (or lands on the handle) is not a sort
        if (suppressClick || (e && e.target && e.target.classList &&
                              e.target.classList.contains('col-resizer'))) return;
        const col = th.dataset.col;
        const spec = COLS.find(c => c.key === col);
        if (!spec || !spec.sortable) return;
        if (col === sortCol) sortAsc = !sortAsc; else { sortCol = col; sortAsc = true; }
        renderTable();
      };
    });

    const fRow = document.createElement('tr');
    fRow.className = 'filter-row';
    COLS.forEach(c => {
      const td = document.createElement('td');
      const inp = document.createElement('input');
      inp.type = 'text';
      inp.placeholder = '⌕';
      inp.dataset.col = c.key;
      inp.value = colFilters[c.key] || '';
      inp.oninput = () => { colFilters[c.key] = inp.value; renderTable(); };
      inp.onclick = (e) => e.stopPropagation();
      // UI-10: Esc empties THIS box only and goes no further (no other handler sees it).
      inp.onkeydown = (e) => {
        if (e.key !== 'Escape') return;
        if (e.stopPropagation) e.stopPropagation();
        if (!inp.value) return;
        if (e.preventDefault) e.preventDefault();
        clearFilters([c.key]);
      };
      if (inp.value) inp.className = 'has-text';
      td.appendChild(inp);
      if (inp.value) {
        const x = document.createElement('button');
        x.type = 'button';
        x.className = 'f-clear';
        x.title = 'Clear this filter';
        x.textContent = '×';
        x.dataset.clear = c.key;
        x.onclick = (e) => { if (e && e.stopPropagation) e.stopPropagation(); clearFilters([c.key]); };
        td.appendChild(x);
      }
      fRow.appendChild(td);
    });
    thead.appendChild(fRow);

    // Status filter matches against the loader-computed _tags list, so a row
    // can belong to several buckets at once (e.g. "suspense" AND "contra").
    let filtered = statusFilter
      ? rows.filter(r => (r._tags || []).indexOf(statusFilter) !== -1)
      : rows;

    for (const [col, q] of Object.entries(colFilters)) {
      if (!q) continue;
      const ql = q.toLowerCase();
      filtered = filtered.filter(r => cellText(r, col).toLowerCase().includes(ql));
    }

    const spec = COLS.find(c => c.key === sortCol);
    filtered = [...filtered].sort((a, b) => {
      let va = cellText(a, sortCol), vb = cellText(b, sortCol);
      if (spec && spec.sort === 'number') {
        va = parseFloat(va) || 0; vb = parseFloat(vb) || 0;
      } else if (spec && spec.sort === 'order') {
        const ord = spec.order || [];
        const ia = ord.indexOf(va.toLowerCase()), ib = ord.indexOf(vb.toLowerCase());
        va = ia === -1 ? 999 : ia; vb = ib === -1 ? 999 : ib;
      } else { va = va.toLowerCase(); vb = vb.toLowerCase(); }
      if (va < vb) return sortAsc ? -1 : 1;
      if (va > vb) return sortAsc ? 1 : -1;
      return 0;
    });

    tbody.innerHTML = '';
    filtered.forEach(r => {
      const tr = document.createElement('tr');
      if (r._rowclass) r._rowclass.split(/\s+/).forEach(c => c && tr.classList.add(c));
      if (r._band) tr.classList.add(r._band);
      if (selected.has(r._idx)) tr.classList.add('selected');
      if (r._locked) tr.classList.add('locked');
      if (r._deleted) tr.classList.add('row-deleted');
%%EXCL_ROW_JS%%      tr.dataset.idx = r._idx;
      if (r._deleted) tr.title = 'Marked for deletion';
      else if (r._note) tr.title = r._note;

      COLS.forEach(c => {
        const td = document.createElement('td');
        const orig = cellText(r, c.key);
        const editedTxt = c.edit_key ? cellText(r, c.edit_key).trim() : '';
        const isEdited = !!editedTxt && editedTxt !== orig.trim();
        const val = isEdited ? editedTxt : orig;
        const badge = (r._badges || {})[c.key];
        let html = '';
        if (badge) {
          html += '<span class="badge ' + esc(badge.cls || 'grey') + '"' +
                  (badge.title ? ' title="' + esc(badge.title) + '"' : '') + '>' +
                  esc(badge.text) + '</span>';
        }
        html += esc(val);
        if (c.key === TARGET && r._changed && !r._deleted) {
          html += '<span class="changed-marker">*</span>';
          td.title = 'Changed from: ' + r._orig;
        } else {
          td.title = badge && badge.title ? badge.title : val;
        }
        if (isEdited) {
          html += '<span class="changed-marker">&#9998;</span>';
          td.title = 'Edited. Original: ' + orig;
        }
        td.innerHTML = html;
        tr.appendChild(td);
      });

      tr.onclick = (e) => handleRowClick(r._idx, e);
      tbody.appendChild(tr);
    });

    shownCount = filtered.length;
    updateStats();
    if (clearAll) {
      const nf = Object.keys(colFilters).filter(k => colFilters[k]).length;
      clearAll.style.display = nf ? '' : 'none';
      clearAll.textContent = nf ? nf + (nf === 1 ? ' filter' : ' filters') + ' · Clear all' : '';
    }

    if (refocus) {
      const inp = thead.querySelector('.filter-row input[data-col="' + refocus.col + '"]');
      if (inp) {
        inp.focus();
        try { inp.setSelectionRange(refocus.s, refocus.e); }
        catch (err) { inp.selectionStart = inp.selectionEnd = inp.value.length; }
      }
    }
  }

  // ── Inline edit (opt-in via Column.edit_key). The text goes into
  //    r[c.edit_key]; r[c.key] (the original) is never touched. A blank or
  //    unchanged entry clears the edit. The value is set via the .value
  //    property, never innerHTML.
  function beginEdit(r, c, td, current, orig) {
    editing = true;
    const inp = document.createElement('input');
    inp.type = 'text';
    inp.value = current;
    inp.style.width = '100%';
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      editing = false;
      if (commit) {
        const v = String(inp.value || '').replace(/\s+/g, ' ').trim();
        const before = r[c.edit_key] || '';
        r[c.edit_key] = (v && v !== orig.trim()) ? v : '';
        if ((r[c.edit_key] || '') !== before) markDirty();
        syncPayload();
      }
      renderTable();
    };
    inp.onclick = (e) => { if (e && e.stopPropagation) e.stopPropagation(); };
    inp.onkeydown = (e) => {
      if (e.key === 'Enter') { if (e.preventDefault) e.preventDefault(); finish(true); }
      else if (e.key === 'Escape') { finish(false); }
    };
    inp.onblur = () => finish(true);
    td.innerHTML = '';
    td.appendChild(inp);
    inp.focus();
  }

  // UI-06: ONE delegated dblclick listener on the tbody, which is never rebuilt,
  // so a real double-click (whose second click must land on the SAME node the
  // first one did) reaches it. Row and column come from data / position, not
  // from closures over td nodes.
  $('tbody').addEventListener('dblclick', (e) => {
    if (editing) return;
    const t = e && e.target;
    if (!t || !t.closest) return;
    if (t.tagName === 'INPUT') return;
    const td = t.closest('td');
    const tr = td && td.parentNode;
    if (!td || !tr || tr.dataset.idx === undefined) return;
    const c = COLS[Array.prototype.indexOf.call(tr.children, td)];
    const r = rows.find(x => String(x._idx) === String(tr.dataset.idx));
    if (!c || !c.edit_key || !r || r._locked || r._deleted) return;
    const orig = cellText(r, c.key);
    const editedTxt = cellText(r, c.edit_key).trim();
    const val = (editedTxt && editedTxt !== orig.trim()) ? editedTxt : orig;
    beginEdit(r, c, td, val, orig);
  });

  // A click changes the selection IN PLACE (toggle the 'selected' class on the
  // existing rows, refresh the counter). It must not rebuild tbody: that
  // replaces every td and Chromium then drops the dblclick of a double-click.
  function handleRowClick(idx, e) {
    if (e.shiftKey && lastClickIdx !== null) {
      const all = Array.prototype.map.call($('tbody').children, tr => parseInt(tr.dataset.idx));
      const a = all.indexOf(lastClickIdx), b = all.indexOf(idx);
      if (a >= 0 && b >= 0) for (let i = Math.min(a,b); i <= Math.max(a,b); i++) selected.add(all[i]);
    } else if (e.ctrlKey || e.metaKey) {
      if (selected.has(idx)) selected.delete(idx); else selected.add(idx);
    } else {
      selected.clear(); selected.add(idx);
    }
    lastClickIdx = idx;
    Array.prototype.forEach.call($('tbody').children, tr => {
      if (selected.has(parseInt(tr.dataset.idx))) tr.classList.add('selected');
      else tr.classList.remove('selected');
    });
    updateStats();
  }

  renderTable();
})();
</script>
<img src="data:," onerror="eval(document.getElementById('%%APP%%-init-code').textContent)" style="display:none">
"""


_EXCLUDE_JS = """
  // ── IMP-11: Don't import / Import selected (opt-in via spec.allow_exclude) ──
  let exclDirty = false;
  function setExcluded(flag) {
    if (selected.size === 0) { alert('Select rows first (click / shift-click / ctrl-click).'); return; }
    rows.forEach(r => {
      if (r._locked || !selected.has(r._idx)) return;
      if (!!r._excluded !== flag) { r._excluded = flag; exclDirty = true; markDirty(); }
    });
    syncPayload();
    renderTable();
  }
  const exclBtn = $('exclude-sel');
  if (exclBtn) exclBtn.onclick = () => setExcluded(true);
  const inclBtn = $('include-sel');
  if (inclBtn) inclBtn.onclick = () => setExcluded(false);
"""


def build_html(spec: ReviewSpec, rows: list[dict]) -> str:
    """Render a complete, self-contained review widget for `rows`.

    Returns HTML suitable for a gr.HTML component. Every dynamic value goes
    through js_json() or html-escaping, so untrusted row content cannot break
    out of the template.
    """
    prepared = prepare_rows(rows)

    if spec.status_options:
        opts = "".join(
            f'<option value="{_attr(v)}">{_attr(lbl)}</option>'
            for v, lbl in [("", "All"), *spec.status_options]
        )
        status_html = (
            f"<label>{_attr(spec.status_label)}</label>"
            f'<select id="{spec.app_id}-status">{opts}</select>'
        )
    else:
        status_html = ""

    apply_match_html = (
        f'<button id="{spec.app_id}-apply-match" '
        f'title="Apply to every row sharing the selected row\'s '
        f'{_attr(spec.apply_matching_on)}">{_attr(spec.apply_matching_label)}</button>'
        if spec.apply_matching_on else ""
    )

    delete_btn_html = (
        f'<button id="{spec.app_id}-remove-sel" '
        f'title="Mark selected rows for deletion">Remove selected</button>'
        if spec.allow_delete else ""
    )

    if spec.allow_exclude:
        exclude_btn_html = (
            f'<button id="{spec.app_id}-exclude-sel" '
            f'title="Leave the selected rows out of the import">Don\'t import selected</button>'
            f'<button id="{spec.app_id}-include-sel" '
            f'title="Put the selected rows back into the import">Import selected</button>'
        )
        exclude_css = (
            f" #{spec.app_id}-app tbody tr.row-excluded td "
            f"{{ text-decoration: line-through; opacity: 0.6; }}"
        )
        exclude_js = _EXCLUDE_JS
        excl_filter = ".filter(r => !r._excluded)"
        excl_fields = (
            "\n      excluded: rows.filter(r => r._excluded).map(r => {"
            "\n        const out = {};"
            "\n        for (const k in r) if (k.charAt(0) !== '_') out[k] = r[k];"
            "\n        return out;"
            "\n      }),\n      excluded_dirty: exclDirty"
        )
        excl_row_js = (
            "      if (r._excluded) { tr.classList.add('row-excluded'); "
            "if (!r._deleted) tr.title = (r._note || 'Not imported') + ' (not imported; select and click Import selected to include)'; }\n"
        )
    else:
        exclude_btn_html = exclude_css = exclude_js = excl_filter = ""
        excl_fields = excl_row_js = ""

    default_sort = spec.default_sort or (spec.columns[0].key if spec.columns else "")

    html = _CSS + _BODY
    for token, value in (
        ("%%STATUS_FILTER_HTML%%", status_html),
        ("%%APPLY_MATCH_HTML%%", apply_match_html),
        ("%%DELETE_BTN_HTML%%", delete_btn_html),
        ("%%EXCLUDE_BTN_HTML%%", exclude_btn_html),
        ("/*%%EXCLUDE_CSS%%*/\n", exclude_css + "\n" if exclude_css else ""),
        ("%%EXCLUDE_JS%%", exclude_js),
        ("%%EXCL_FILTER%%", excl_filter),
        ("%%EXCL_FIELDS%%", excl_fields),
        ("%%EXCL_ROW_JS%%", excl_row_js),
        ("%%EXTRA_PANEL%%", spec.extra_panel_html),
        ("%%PICKER_LABEL%%", _attr(spec.picker_label)),
        ("%%PICKER_PLACEHOLDER%%", _attr(spec.picker_placeholder)),
        ("%%APP_JSON%%", js_json(spec.app_id)),
        ("%%DATA_JSON%%", js_json(prepared)),
        ("%%COLS_JSON%%", js_json([_col_dict(c) for c in spec.columns])),
        ("%%ITEMS_JSON%%", js_json([_item_dict(i) for i in spec.picker_items])),
        ("%%TARGET_JSON%%", js_json(spec.target_col)),
        ("%%ALSO_SET_JSON%%", js_json(spec.also_set)),
        (
            "%%ALSO_SET_MATCHING_JSON%%",
            js_json(spec.also_set_matching if spec.also_set_matching is not None else spec.also_set),
        ),
        ("%%MATCH_ON_JSON%%", js_json(spec.apply_matching_on)),
        ("%%CONTEXT_JSON%%", js_json(spec.context)),
        ("%%PAYLOAD_VAR_JSON%%", js_json(spec.payload_var)),
        ("%%STATUS_COL_JSON%%", js_json(spec.status_col)),
        ("%%STATUS_CLASSES_JSON%%", js_json(spec.status_classes)),
        ("%%DEFAULT_SORT_JSON%%", js_json(default_sort)),
    ):
        html = html.replace(token, value)
    # App id last: it appears inside tokens' own text (e.g. the payload box id).
    return html.replace("%%APP%%", spec.app_id)


def _col_dict(c: Column) -> dict:
    return {
        "key": c.key, "label": c.label, "sortable": c.sortable,
        "sort": c.sort, "order": [o.lower() for o in c.order],
        "edit_key": c.edit_key,
    }


def _item_dict(i: PickerItem) -> dict:
    return {"value": i.value, "primary": i.primary, "secondary": i.secondary}


def _attr(s: str) -> str:
    """Escape a value being placed into an HTML attribute or text node."""
    import html as _html
    return _html.escape(str(s or ""), quote=True)


def parse_payload(raw: str) -> dict:
    """Parse the JSON the client left in its payload global.

    Returns {"context": {...}, "changes": [...], "all_rows": [...]} with all
    three keys always present, so save handlers can index without guarding.
    Raises ValueError on malformed input.
    """
    if not raw or not raw.strip():
        return {"context": {}, "changes": [], "all_rows": []}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"could not parse review payload: {e}") from e
    if not isinstance(data, dict):
        raise ValueError("review payload was not a JSON object")
    out = {
        "context": data.get("context") or {},
        "changes": data.get("changes") or [],
        "all_rows": data.get("all_rows") or [],
    }
    if "excluded" in data:  # IMP-11: only screens with allow_exclude send it
        out["excluded"] = data.get("excluded") or []
        out["excluded_dirty"] = bool(data.get("excluded_dirty"))
    return out
