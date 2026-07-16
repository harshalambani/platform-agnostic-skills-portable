"""
ui/tabs/itr_mapping_review.py -- ITR Mapping review tab (2026-07-16, Part 2).

Gives the ITR account-tag mapping the same review UX as
ui/tabs/gnucash_review.py's "Review & Edit Account Mappings" tab: a
searchable-dropdown assignment picker, row multi-select, "Apply to
selected", and a Save -> YAML round trip with a backup kept before any
in-place rewrite. No YAML editing, no CLI -- everything here is driven
through apply_mapping_corrections.apply_corrections_map().

Data sources for the review table (per entity):
  - Data/itr/mappings/<entity>.mapping.yaml  -- already-resolved entries
    (anchored via ui._config.data_root_dir(), never a bare "Data/" prefix --
    see docs/history/2026-07-xx path-anchoring fix).
  - the most-recently-modified *-proposed-mappings.yaml under
    <data_root>/outputs -- unmapped leaves + any LLM suggestion from the
    latest ITR Workbook run. The proposed-mappings snippet isn't
    entity-tagged by filename (the ITR Workbook output stem is derived from
    the uploaded HTML, not the entity key), so "latest run's snippet" is
    read literally: the single most recent one in the outputs folder. A
    guid already present in the entity's own mapping file always wins over
    a stale snippet entry for the same guid (it's fully resolved, so it
    isn't shown as unmapped regardless of what an older snippet says).

Save discipline (mirrors gnucash_review._save_changes): write a timestamped
backup of the current mapping file BEFORE any in-place rewrite; a blank/
missing entity never touches the filesystem.
"""
from __future__ import annotations

import datetime
import json
import shutil
import sys
from pathlib import Path

import gradio as gr
import yaml

from .. import _config as _config_mod

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


def _latest_proposed_mappings_path() -> Path | None:
    """Most-recently-modified *-proposed-mappings.yaml under the outputs
    folder, or None if there isn't one yet (e.g. no ITR Workbook run at all)."""
    out_dir = _outputs_dir()
    if not out_dir.is_dir():
        return None
    candidates = sorted(
        out_dir.glob("*-proposed-mappings.yaml"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return candidates[0] if candidates else None


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

    Each row: {guid, path, tag, unmapped, suggested, note}. `tag` is the
    entity's currently-resolved tag (None when unmapped). `suggested` is the
    proposed-mappings snippet's suggestion for an unmapped leaf (None if the
    snippet has no suggestion, or has "REPLACE_ME"). Handles both cold start
    (mapping file absent, everything comes from the snippet) and correction
    (mapping file has entries; snippet supplies nothing new) cleanly.
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
            "suggested": None,
            "note": entry.note or "",
        })

    snippet_path = _latest_proposed_mappings_path()
    if snippet_path is not None:
        try:
            raw = yaml.safe_load(snippet_path.read_text(encoding="utf-8")) or []
        except Exception:
            raw = []
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
                rows.append({
                    "guid": guid,
                    "path": item.get("path", ""),
                    "tag": None,
                    "unmapped": True,
                    "suggested": suggested_tag,
                    "note": item.get("note", ""),
                })

    rows.sort(key=lambda r: (r["path"] or "", r["guid"]))
    return rows


def _tag_options() -> list[dict]:
    """(tag, description) pairs from tags.py's vocabulary, sorted by tag."""
    _configs, tag_vocab, _amc = _itr_modules()
    return [
        {"tag": tag, "desc": meta.treatment}
        for tag, meta in sorted(tag_vocab.TAGS.items())
    ]


# ---------------------------------------------------------------------------
# HTML/JS review table (mirrors gnucash_review.py's structure: searchable
# assign picker, row multi-select, Apply to selected).
# ---------------------------------------------------------------------------

def _js_json(value) -> str:
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


_REVIEW_HTML = r"""
<style>
#itrmap-app {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 13px;
  color: #e0e0e0;
}
#itrmap-app .toolbar {
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  padding: 8px 0; border-bottom: 1px solid #333;
  margin-bottom: 8px;
}
#itrmap-app .toolbar label { font-weight: 600; font-size: 12px; color: #ccc; }
#itrmap-app .toolbar button {
  padding: 4px 12px; border-radius: 4px; font-size: 12px; cursor: pointer;
  border: 1px solid #444; background: #2a2a2a; color: #e0e0e0;
}
#itrmap-app .toolbar button.primary {
  background: #2563eb; color: #fff; border-color: transparent;
}
#itrmap-app .toolbar .spacer { flex: 1; }
#itrmap-app .stats { font-size: 11px; color: #999; padding: 4px 0; }

#itrmap-app table { width: 100%; border-collapse: collapse; table-layout: auto; }
#itrmap-app thead th {
  position: sticky; top: 0; z-index: 2;
  background: #1e1e1e; color: #ccc;
  border-bottom: 2px solid #444;
  padding: 6px 8px; text-align: left; font-size: 12px; white-space: nowrap;
}
#itrmap-app tbody tr {
  border-bottom: 1px solid #262626; cursor: pointer; transition: background 0.1s;
}
#itrmap-app tbody tr:nth-child(even) { background: #111; }
#itrmap-app tbody tr:hover { background: #1a2744; }
#itrmap-app tbody tr.selected { background: #1e3a5f; }
#itrmap-app tbody tr.selected:hover { background: #254a73; }
#itrmap-app tbody tr.unmapped-row td:first-child { border-left: 3px solid #f87171; }
#itrmap-app tbody td { padding: 5px 8px; font-size: 12px; max-width: 420px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #ddd; }

#itrmap-app .badge-unmapped { color: #f87171; font-weight: 600; }
#itrmap-app .badge-mapped { color: #4ade80; }
#itrmap-app .badge-suggested { color: #60a5fa; }
#itrmap-app .changed-marker { color: #a78bfa; font-weight: bold; margin-left: 4px; }

/* Tag search dropdown -- mirrors gnucash_review.py's .acct-picker */
#itrmap-app .tag-picker { position: relative; display: inline-block; }
#itrmap-app .tag-search {
  width: 320px; padding: 5px 8px; border: 1px solid #444;
  border-radius: 4px; font-size: 12px; background: #1a1a1a; color: #e0e0e0;
}
#itrmap-app .tag-dropdown {
  position: absolute; top: 100%; left: 0; z-index: 100;
  width: 460px; max-height: 280px; overflow-y: auto;
  border: 1px solid #444; background: #1a1a1a;
  border-radius: 4px; box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  display: none;
}
#itrmap-app .tag-dropdown.open { display: block; }
#itrmap-app .tag-dropdown .tag-item { padding: 5px 10px; cursor: pointer; font-size: 12px; color: #e0e0e0; }
#itrmap-app .tag-dropdown .tag-item:hover { background: #1e3a5f; }
#itrmap-app .tag-dropdown .tag-item .tag-name { font-weight: 600; color: #fff; }
#itrmap-app .tag-dropdown .tag-item .tag-desc { color: #888; font-size: 11px; display: block; }
#itrmap-app .scroll-wrapper { max-height: 65vh; overflow-y: auto; border: 1px solid #333; border-radius: 4px; }
</style>
<style>
#itrmap-payload-box, #itrmap-payload-box * { position: absolute !important; left: -9999px !important; height: 0 !important; overflow: hidden !important; opacity: 0 !important; pointer-events: none !important; }
</style>

<div id="itrmap-app">
  <div class="toolbar">
    <label>Show:</label>
    <select id="itrmap-filter">
      <option value="">All</option>
      <option value="unmapped" selected>Unmapped only</option>
      <option value="mapped">Mapped only</option>
    </select>
    <span class="spacer"></span>
    <span class="stats" id="itrmap-stats"></span>
  </div>

  <div class="toolbar">
    <label>Assign tag:</label>
    <div class="tag-picker">
      <input type="text" class="tag-search" id="itrmap-tag-search" placeholder="Type to search tags&#8230;" autocomplete="off">
      <div class="tag-dropdown" id="itrmap-tag-dropdown"></div>
    </div>
    <button id="itrmap-apply-sel" class="primary" title="Apply to selected rows">Apply to selected</button>
    <span class="spacer"></span>
  </div>

  <div class="scroll-wrapper">
    <table>
      <thead>
        <tr>
          <th>Account path</th>
          <th>Current tag</th>
          <th>Suggested</th>
          <th>New tag</th>
        </tr>
      </thead>
      <tbody id="itrmap-tbody"></tbody>
    </table>
  </div>
</div>

<script type="text/plain" id="itrmap-init-code">
(function() {
  const DATA = %%DATA_JSON%%;
  const TAGS = %%TAGS_JSON%%;
  const ENTITY = %%ENTITY_JSON%%;

  function esc(s) {
    if (!s) return '';
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;')
                     .replace(/>/g,'&gt;').replace(/"/g,'&quot;');
  }

  let rows = DATA.map((r, i) => ({...r, _idx: i, _assigned: null}));
  let selected = new Set();
  let lastClickIdx = null;
  let filter = 'unmapped';

  const filterDD = document.getElementById('itrmap-filter');
  filterDD.onchange = (e) => { filter = e.target.value; renderTable(); };

  // -- Tag search --
  const tagSearch = document.getElementById('itrmap-tag-search');
  const tagDD = document.getElementById('itrmap-tag-dropdown');
  let tagFiltered = TAGS.slice(0, 50);
  let selectedTag = '';

  function renderTagDropdown(filterText) {
    const q = (filterText || '').toLowerCase();
    tagFiltered = q
      ? TAGS.filter(t => t.tag.toLowerCase().includes(q) || t.desc.toLowerCase().includes(q)).slice(0, 50)
      : TAGS.slice(0, 50);
    tagDD.innerHTML = '';
    tagFiltered.forEach(t => {
      const div = document.createElement('div');
      div.className = 'tag-item';
      div.innerHTML = '<span class="tag-name">' + esc(t.tag) + '</span><span class="tag-desc">' + esc(t.desc) + '</span>';
      div.onclick = () => { selectedTag = t.tag; tagSearch.value = t.tag; tagDD.classList.remove('open'); };
      tagDD.appendChild(div);
    });
  }
  tagSearch.onfocus = () => { renderTagDropdown(tagSearch.value); tagDD.classList.add('open'); };
  tagSearch.oninput = () => { renderTagDropdown(tagSearch.value); tagDD.classList.add('open'); selectedTag = ''; };
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.tag-picker')) tagDD.classList.remove('open');
  });

  document.getElementById('itrmap-apply-sel').onclick = () => {
    if (!selectedTag) { alert('Select a tag first'); return; }
    if (selected.size === 0) { alert('Select rows first (click/shift-click)'); return; }
    rows.forEach(r => {
      if (selected.has(r._idx)) { r._assigned = selectedTag; }
    });
    syncPayload();
    renderTable();
  };

  function syncPayload() {
    const changes = rows.filter(r => r._assigned && r._assigned !== r.tag).map(r => ({
      guid: r.guid, path: r.path, tag: r._assigned,
    }));
    window._itrMapSavePayload = JSON.stringify({ entity: ENTITY, changes: changes });
  }
  syncPayload();

  function renderTable() {
    const tbody = document.getElementById('itrmap-tbody');
    let filtered = rows;
    if (filter === 'unmapped') filtered = rows.filter(r => r.unmapped);
    else if (filter === 'mapped') filtered = rows.filter(r => !r.unmapped);

    tbody.innerHTML = '';
    filtered.forEach(r => {
      const tr = document.createElement('tr');
      if (selected.has(r._idx)) tr.classList.add('selected');
      if (r.unmapped) tr.classList.add('unmapped-row');
      tr.dataset.idx = r._idx;

      const tdPath = document.createElement('td');
      tdPath.textContent = r.path || '(no path)';
      tdPath.title = r.path || '';
      tr.appendChild(tdPath);

      const tdCurrent = document.createElement('td');
      if (r.unmapped) {
        tdCurrent.innerHTML = '<span class="badge-unmapped">UNMAPPED</span>';
      } else {
        tdCurrent.textContent = r.tag || '';
      }
      tr.appendChild(tdCurrent);

      const tdSuggested = document.createElement('td');
      tdSuggested.innerHTML = r.suggested ? '<span class="badge-suggested">' + esc(r.suggested) + '</span>' : '';
      tr.appendChild(tdSuggested);

      const tdAssigned = document.createElement('td');
      if (r._assigned) {
        tdAssigned.innerHTML = '<span class="badge-mapped">' + esc(r._assigned) + '</span><span class="changed-marker">*</span>';
      } else {
        tdAssigned.textContent = '';
      }
      tr.appendChild(tdAssigned);

      tr.onclick = (e) => handleRowClick(r._idx, e);
      tbody.appendChild(tr);
    });

    const changed = rows.filter(r => r._assigned && r._assigned !== r.tag).length;
    const unmappedCount = rows.filter(r => r.unmapped).length;
    document.getElementById('itrmap-stats').textContent =
      filtered.length + '/' + rows.length + ' rows' +
      (selected.size ? ' | ' + selected.size + ' selected' : '') +
      (changed ? ' | ' + changed + ' changed' : '') +
      (unmappedCount ? ' | ' + unmappedCount + ' unmapped' : '');
  }

  function handleRowClick(idx, e) {
    if (e.shiftKey && lastClickIdx !== null) {
      const allIdxs = [...document.querySelectorAll('#itrmap-tbody tr')].map(tr => parseInt(tr.dataset.idx));
      const startPos = allIdxs.indexOf(lastClickIdx);
      const endPos = allIdxs.indexOf(idx);
      if (startPos >= 0 && endPos >= 0) {
        const from = Math.min(startPos, endPos);
        const to = Math.max(startPos, endPos);
        for (let i = from; i <= to; i++) selected.add(allIdxs[i]);
      }
    } else if (e.ctrlKey || e.metaKey) {
      if (selected.has(idx)) selected.delete(idx); else selected.add(idx);
    } else {
      selected.clear();
      selected.add(idx);
    }
    lastClickIdx = idx;
    renderTable();
  }

  renderTable();
})();
</script>
<img src="data:," onerror="eval(document.getElementById('itrmap-init-code').textContent)" style="display:none">
"""


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

    html = _REVIEW_HTML
    html = html.replace("%%DATA_JSON%%", _js_json(rows))
    html = html.replace("%%TAGS_JSON%%", _js_json(_tag_options()))
    html = html.replace("%%ENTITY_JSON%%", _js_json(entity_key))
    return html


# ---------------------------------------------------------------------------
# Save handler.
# ---------------------------------------------------------------------------

def _save_changes(entity_key: str, changes_json: str) -> str:
    """Process a Save from the review UI: writes a backup of the entity's
    current mapping file (if it exists) THEN rewrites it in place via
    apply_corrections_map(). A blank entity, or no changes, never touches
    the filesystem. Returns a status markdown string."""
    if not entity_key or not entity_key.strip():
        return "Error: select an entity first -- nothing was saved."

    if not changes_json or not changes_json.strip():
        return "No changes to save."

    try:
        payload = json.loads(changes_json)
    except json.JSONDecodeError as e:
        return f"Error parsing changes: {e}"

    changes = payload.get("changes", [])
    if not changes:
        return "No changes to save."

    _configs, _tag_vocab, amc = _itr_modules()

    mapping_file = _mapping_path(entity_key)
    mapping_file.parent.mkdir(parents=True, exist_ok=True)

    backup_msg = "No existing mapping file -- nothing to back up (cold start)."
    if mapping_file.is_file():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = mapping_file.with_name(f"{mapping_file.name}.bak-{stamp}")
        shutil.copy2(mapping_file, backup_path)
        backup_msg = f"Backup written: {backup_path}"

    corrections: dict[str, str] = {}
    paths: dict[str, str] = {}
    skipped_blank = 0
    for ch in changes:
        guid = ch.get("guid")
        tag = ch.get("tag")
        if not guid or not tag or not str(tag).strip():
            skipped_blank += 1
            continue
        corrections[guid] = str(tag).strip()
        if ch.get("path"):
            paths[guid] = ch["path"]

    if not corrections:
        return (
            "No valid changes to save (all selections were blank). "
            f"{backup_msg}"
        )

    applied, invalid = amc.apply_corrections_map(
        str(mapping_file), corrections, str(mapping_file), paths=paths,
    )

    lines = [
        "**Saved**",
        "",
        f"{backup_msg}",
        f"Applied {applied} correction(s) -> {mapping_file}",
    ]
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

    _payload_box = gr.Textbox(
        value="", show_label=False, container=False, lines=1,
        elem_id="itrmap-payload-box",
    )

    load_btn.click(
        fn=_load_review_data,
        inputs=[entity_dropdown],
        outputs=review_html,
    )
    save_btn.click(
        fn=_save_changes,
        inputs=[entity_dropdown, _payload_box],
        outputs=[save_result],
        js="(entity, x) => [entity, window._itrMapSavePayload || '']",
    )
