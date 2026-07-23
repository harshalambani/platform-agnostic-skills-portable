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

_META_KEYS = (META_TAGS, META_ROWCLASS, META_BADGES, META_LOCKED, META_NOTE)


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

#%%APP%%-app table { width: 100%; border-collapse: collapse; table-layout: auto; }
#%%APP%%-app thead th {
  position: sticky; top: 0; z-index: 2;
  background: #1e1e1e; color: #ccc; border-bottom: 2px solid #444;
  padding: 6px 8px; text-align: left; font-size: 12px;
  cursor: pointer; user-select: none; white-space: nowrap;
}
#%%APP%%-app thead th:hover { background: #2a2a2a; }
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
#%%APP%%-app thead .filter-row input::placeholder { color: #555; }

#%%APP%%-app tbody tr {
  border-bottom: 1px solid #262626; cursor: pointer; transition: background 0.1s;
}
#%%APP%%-app tbody tr:nth-child(even) { background: #111; }
#%%APP%%-app tbody tr:hover { background: #1a2744; }
#%%APP%%-app tbody tr.selected { background: #1e3a5f; }
#%%APP%%-app tbody tr.selected:hover { background: #254a73; }
#%%APP%%-app tbody tr.locked { opacity: 0.75; cursor: not-allowed; }
#%%APP%%-app tbody td {
  padding: 5px 8px; font-size: 12px; max-width: 420px;
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #ddd;
}

/* Row accents — set via _rowclass from the loader. */
#%%APP%%-app tbody tr.accent-red    td:first-child { border-left: 3px solid #f87171; }
#%%APP%%-app tbody tr.accent-amber  td:first-child { border-left: 3px solid #fbbf24; }
#%%APP%%-app tbody tr.accent-green  td:first-child { border-left: 3px solid #4ade80; }
#%%APP%%-app tbody tr.tone-amber  { background: #3a2a1d; }
#%%APP%%-app tbody tr.tone-amber:hover { background: #4a3626; }
#%%APP%%-app tbody tr.tone-green  { background: #16281d; }
#%%APP%%-app tbody tr.tone-green:hover { background: #1e3a2a; }
#%%APP%%-app tbody tr.tone-amber.selected,
#%%APP%%-app tbody tr.tone-green.selected { background: #1e3a5f; }

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
  max-height: 65vh; overflow-y: auto; border: 1px solid #333; border-radius: 4px;
}
</style>
"""


# ---------------------------------------------------------------------------
# Markup + client code. %%TOKEN%% slots are filled by build_html().
# ---------------------------------------------------------------------------

_BODY = r"""
<div id="%%APP%%-app">
  <div class="toolbar">
    %%STATUS_FILTER_HTML%%
    <span class="spacer"></span>
    <span class="stats" id="%%APP%%-stats"></span>
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
    <span class="spacer"></span>
  </div>

  %%EXTRA_PANEL%%

  <div class="scroll-wrapper">
    <table>
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

  const $ = (suffix) => document.getElementById(APP + '-' + suffix);

  // Escape before every innerHTML insertion — row values originate from
  // parsed PDF/bank/LLM content and must never be trusted as markup.
  function esc(s) {
    if (s === null || s === undefined) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                    .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  let rows = DATA.map((r, i) => ({
    ...r, _idx: i, _changed: false, _orig: r[TARGET] || ''
  }));
  let selected = new Set();
  let lastClickIdx = null;
  let statusFilter = '';
  let colFilters = {};
  let activeFilterCol = null;
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
      r._changed = true;
      n++;
    });
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

  // ── JS → Python bridge. gr.State has no DOM node, so the save handler
  //    reads this global via its js= parameter at click time.
  function syncPayload() {
    const changes = rows.filter(r => r._changed).map(r => {
      const out = { _idx: r._idx, _orig: r._orig };
      for (const c of COLS) out[c.key] = r[c.key];
      for (const k of ['guid', 'Sr', 'Transaction ID', 'CN No']) {
        if (r[k] !== undefined) out[k] = r[k];
      }
      return out;
    });
    window[PAYLOAD_VAR] = JSON.stringify({
      context: CONTEXT,
      changes: changes,
      all_rows: rows.map(r => {
        const out = {};
        for (const k in r) if (k.charAt(0) !== '_') out[k] = r[k];
        return out;
      }),
    });
  }
  syncPayload();

  // ── Rendering ──
  function renderTable() {
    const thead = $('thead');
    const tbody = $('tbody');

    thead.innerHTML = '<tr>' + COLS.map(c =>
      '<th data-col="' + esc(c.key) + '">' + esc(c.label) +
      (c.key === sortCol ? '<span class="sort-arrow">' + (sortAsc ? '▲' : '▼') + '</span>' : '') +
      '</th>').join('') + '</tr>';
    thead.querySelectorAll('th').forEach(th => {
      th.onclick = () => {
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
      inp.oninput = () => { colFilters[c.key] = inp.value; activeFilterCol = c.key; renderTable(); };
      inp.onclick = (e) => e.stopPropagation();
      td.appendChild(inp);
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
      if (selected.has(r._idx)) tr.classList.add('selected');
      if (r._locked) tr.classList.add('locked');
      tr.dataset.idx = r._idx;
      if (r._note) tr.title = r._note;

      COLS.forEach(c => {
        const td = document.createElement('td');
        const val = cellText(r, c.key);
        const badge = (r._badges || {})[c.key];
        let html = '';
        if (badge) {
          html += '<span class="badge ' + esc(badge.cls || 'grey') + '"' +
                  (badge.title ? ' title="' + esc(badge.title) + '"' : '') + '>' +
                  esc(badge.text) + '</span>';
        }
        html += esc(val);
        if (c.key === TARGET && r._changed) {
          html += '<span class="changed-marker">*</span>';
          td.title = 'Changed from: ' + r._orig;
        } else {
          td.title = badge && badge.title ? badge.title : val;
        }
        td.innerHTML = html;
        tr.appendChild(td);
      });

      tr.onclick = (e) => handleRowClick(r._idx, e);
      tbody.appendChild(tr);
    });

    const changed = rows.filter(r => r._changed).length;
    $('stats').textContent =
      filtered.length + '/' + rows.length + ' rows' +
      (selected.size ? ' | ' + selected.size + ' selected' : '') +
      (changed ? ' | ' + changed + ' changed' : '');

    if (activeFilterCol) {
      const inp = thead.querySelector('.filter-row input[data-col="' + activeFilterCol + '"]');
      if (inp) { inp.focus(); inp.selectionStart = inp.selectionEnd = inp.value.length; }
    }
  }

  function handleRowClick(idx, e) {
    if (e.shiftKey && lastClickIdx !== null) {
      const all = [...document.querySelectorAll('#' + APP + '-tbody tr')].map(tr => parseInt(tr.dataset.idx));
      const a = all.indexOf(lastClickIdx), b = all.indexOf(idx);
      if (a >= 0 && b >= 0) for (let i = Math.min(a,b); i <= Math.max(a,b); i++) selected.add(all[i]);
    } else if (e.ctrlKey || e.metaKey) {
      if (selected.has(idx)) selected.delete(idx); else selected.add(idx);
    } else {
      selected.clear(); selected.add(idx);
    }
    lastClickIdx = idx;
    renderTable();
  }

  renderTable();
})();
</script>
<img src="data:," onerror="eval(document.getElementById('%%APP%%-init-code').textContent)" style="display:none">
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

    default_sort = spec.default_sort or (spec.columns[0].key if spec.columns else "")

    html = _CSS + _BODY
    for token, value in (
        ("%%STATUS_FILTER_HTML%%", status_html),
        ("%%APPLY_MATCH_HTML%%", apply_match_html),
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
        ("%%DEFAULT_SORT_JSON%%", js_json(default_sort)),
    ):
        html = html.replace(token, value)
    # App id last: it appears inside tokens' own text (e.g. the payload box id).
    return html.replace("%%APP%%", spec.app_id)


def _col_dict(c: Column) -> dict:
    return {
        "key": c.key, "label": c.label, "sortable": c.sortable,
        "sort": c.sort, "order": [o.lower() for o in c.order],
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
    return {
        "context": data.get("context") or {},
        "changes": data.get("changes") or [],
        "all_rows": data.get("all_rows") or [],
    }
