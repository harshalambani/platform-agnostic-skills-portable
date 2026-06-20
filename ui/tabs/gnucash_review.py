"""
ui/tabs/gnucash_review.py — Review & Edit Account Mappings tab.

Interactive table for reviewing mapped CSVs before GnuCash import.
Supports:
  - Sortable columns (click header)
  - Shift-click / Ctrl-click multi-select
  - Searchable account picker dropdown
  - Batch "Apply to selected" and "Apply to all matching"
  - Save corrections → per-GnuCash override YAML + re-export CSV
"""
from __future__ import annotations

import csv
import gzip
import json
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import gradio as gr

from ui import _config as _config_mod


# ---------------------------------------------------------------------------
# Output-folder CSV scanner
# ---------------------------------------------------------------------------

def _scan_import_ready_csvs() -> list[str]:
    """Find *GnuCash_import_ready.csv files in the output dir, newest first."""
    try:
        out_dir = _config_mod.output_dir()
    except Exception:
        return []
    csvs = sorted(
        out_dir.glob("*GnuCash_import_ready*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [str(p) for p in csvs[:20]]


# ---------------------------------------------------------------------------
# GnuCash account tree extraction (lightweight — no full parse)
# ---------------------------------------------------------------------------

# XML namespaces used by GnuCash
_NS = {
    "gnc":   "{http://www.gnucash.org/XML/gnc}",
    "act":   "{http://www.gnucash.org/XML/act}",
}


def _extract_account_tree(gnucash_file: str) -> list[str]:
    """Extract all account full-paths from a .gnucash file."""
    try:
        with gzip.open(gnucash_file, "rt", encoding="utf-8") as f:
            tree = ET.parse(f)
    except Exception:
        return []

    root = tree.getroot()
    acc_map: dict[str, dict] = {}
    for acc in root.findall(f'.//{_NS["gnc"]}account'):
        aid = acc.findtext(f'{_NS["act"]}id', "")
        aname = acc.findtext(f'{_NS["act"]}name', "")
        parent_el = acc.find(f'{_NS["act"]}parent')
        parent_id = parent_el.text if parent_el is not None else None
        acc_map[aid] = {"name": aname, "parent_id": parent_id}

    def _full_path(aid: str) -> str:
        parts: list[str] = []
        visited: set[str] = set()
        while aid and aid in acc_map and aid not in visited:
            visited.add(aid)
            parts.append(acc_map[aid]["name"])
            aid = acc_map[aid]["parent_id"]
        return ":".join(reversed(parts))

    paths = sorted(
        {_full_path(aid) for aid in acc_map if acc_map[aid]["name"]},
    )
    # Strip "Root Account:" prefix
    cleaned = []
    for p in paths:
        if p.startswith("Root Account:"):
            p = p[len("Root Account:"):]
        if p and ":" in p:  # skip the root itself and single-level
            cleaned.append(p)
    return cleaned


# ---------------------------------------------------------------------------
# HTML/JS template for the interactive review table
# ---------------------------------------------------------------------------

_REVIEW_HTML = r"""
<style>
#review-app {
  font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
  font-size: 13px;
  color: #e0e0e0;
}
#review-app .toolbar {
  display: flex; gap: 8px; align-items: center; flex-wrap: wrap;
  padding: 8px 0; border-bottom: 1px solid #333;
  margin-bottom: 8px;
}
#review-app .toolbar label { font-weight: 600; font-size: 12px; color: #ccc; }
#review-app .toolbar select, #review-app .toolbar input[type=text] {
  padding: 4px 8px; border: 1px solid #444;
  border-radius: 4px; font-size: 12px;
  background: #1a1a1a; color: #e0e0e0;
}
#review-app .toolbar input[type=text] { width: 220px; }
#review-app .toolbar button {
  padding: 4px 12px; border-radius: 4px; font-size: 12px; cursor: pointer;
  border: 1px solid #444; background: #2a2a2a; color: #e0e0e0;
}
#review-app .toolbar button.primary {
  background: #2563eb; color: #fff; border-color: transparent;
}
#review-app .toolbar .spacer { flex: 1; }
#review-app .stats { font-size: 11px; color: #999; padding: 4px 0; }

#review-app table {
  width: 100%; border-collapse: collapse; table-layout: auto;
}
#review-app thead th {
  position: sticky; top: 0; z-index: 2;
  background: #1e1e1e; color: #ccc;
  border-bottom: 2px solid #444;
  padding: 6px 8px; text-align: left; font-size: 12px; cursor: pointer;
  user-select: none; white-space: nowrap;
}
#review-app thead th:hover { background: #2a2a2a; }
#review-app thead th .sort-arrow { margin-left: 4px; font-size: 10px; }
#review-app thead .filter-row td {
  padding: 3px 4px; background: #1a1a1a; border-bottom: 1px solid #444;
}
#review-app thead .filter-row input {
  width: 100%; box-sizing: border-box;
  padding: 3px 6px; border: 1px solid #333; border-radius: 3px;
  font-size: 11px; background: #111; color: #ccc;
}
#review-app thead .filter-row input:focus {
  border-color: #2563eb; outline: none;
}
#review-app thead .filter-row input::placeholder { color: #555; }

#review-app tbody tr {
  border-bottom: 1px solid #262626;
  cursor: pointer; transition: background 0.1s;
}
#review-app tbody tr:nth-child(even) { background: #111; }
#review-app tbody tr:hover { background: #1a2744; }
#review-app tbody tr.selected { background: #1e3a5f; }
#review-app tbody tr.selected:hover { background: #254a73; }
#review-app tbody td { padding: 5px 8px; font-size: 12px; max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: #ddd; }

/* Confidence badges — bright on dark */
#review-app .conf-high     { color: #4ade80; font-weight: 600; }
#review-app .conf-medium   { color: #facc15; }
#review-app .conf-smart    { color: #60a5fa; }
#review-app .conf-override { color: #a78bfa; font-weight: 600; }
#review-app .conf-llm      { color: #22d3ee; }
#review-app .conf-low      { color: #fb923c; }
#review-app .conf-suspense { color: #f87171; font-weight: 600; }
#review-app .conf-none     { color: #f87171; }

#review-app .contra-badge {
  display: inline-block; font-size: 10px; padding: 1px 5px;
  border-radius: 3px; margin-left: 4px;
  background: #7c2d12; color: #fdba74; font-weight: 600;
}
#review-app .contra-badge.high { background: #991b1b; color: #fca5a5; }

#review-app .changed-marker { color: #a78bfa; font-weight: bold; margin-left: 4px; }

/* Account search dropdown */
#review-app .acct-picker {
  position: relative; display: inline-block;
}
#review-app .acct-search {
  width: 320px; padding: 5px 8px; border: 1px solid #444;
  border-radius: 4px; font-size: 12px;
  background: #1a1a1a; color: #e0e0e0;
}
#review-app .acct-dropdown {
  position: absolute; top: 100%; left: 0; z-index: 100;
  width: 420px; max-height: 280px; overflow-y: auto;
  border: 1px solid #444; background: #1a1a1a;
  border-radius: 4px; box-shadow: 0 4px 16px rgba(0,0,0,0.5);
  display: none;
}
#review-app .acct-dropdown.open { display: block; }
#review-app .acct-dropdown .acct-item {
  padding: 5px 10px; cursor: pointer; font-size: 12px;
  color: #e0e0e0;
}
#review-app .acct-dropdown .acct-item:hover { background: #1e3a5f; }
#review-app .acct-dropdown .acct-item .acct-leaf { font-weight: 600; color: #fff; }
#review-app .acct-dropdown .acct-item .acct-path { color: #888; font-size: 11px; }
#review-app .scroll-wrapper { max-height: 65vh; overflow-y: auto; border: 1px solid #333; border-radius: 4px; }
</style>
<style>
/* Hide the payload transfer textbox — must be visible=True for Gradio js param to work */
#rv-payload-box, #rv-payload-box * { position: absolute !important; left: -9999px !important; height: 0 !important; overflow: hidden !important; opacity: 0 !important; pointer-events: none !important; }
</style>

<div id="review-app">
  <div class="toolbar">
    <label>Sort:</label>
    <select id="rv-sort-col"></select>
    <button id="rv-sort-dir" title="Toggle sort direction">↑ Asc</button>
    <label style="margin-left:12px">Filter:</label>
    <select id="rv-filter-conf">
      <option value="">All</option>
      <option value="suspense">Suspense</option>
      <option value="low">Low</option>
      <option value="none">Unmatched</option>
      <option value="smart">Smart</option>
      <option value="override">Override</option>
      <option value="medium">Medium</option>
      <option value="high">High</option>
      <option value="contra">Contra</option>
    </select>
    <span class="spacer"></span>
    <span class="stats" id="rv-stats"></span>
  </div>

  <div class="toolbar">
    <label>Assign account:</label>
    <div class="acct-picker">
      <input type="text" class="acct-search" id="rv-acct-search" placeholder="Type to search accounts…" autocomplete="off">
      <div class="acct-dropdown" id="rv-acct-dropdown"></div>
    </div>
    <button id="rv-apply-sel" class="primary" title="Apply to selected rows">Apply to selected</button>
    <button id="rv-apply-match" title="Apply to all rows with same description pattern">Apply to matching</button>
    <span class="spacer"></span>
  </div>

  <div class="scroll-wrapper">
    <table>
      <thead id="rv-thead"><tr></tr></thead>
      <tbody id="rv-tbody"></tbody>
    </table>
  </div>
</div>

<script type="text/plain" id="rv-init-code">
(function() {
  const DATA = %%DATA_JSON%%;
  const ACCOUNTS = %%ACCOUNTS_JSON%%;
  const GNUCASH_FILE = %%GNUCASH_FILE_JSON%%;
  const CSV_PATH = %%CSV_PATH_JSON%%;

  // Column config: key, label, sortable
  const COLS = [
    {key: 'Date', label: 'Date', sort: true},
    {key: 'Description', label: 'Description', sort: true},
    {key: 'Account', label: 'Account', sort: true},
    {key: 'Transfer Account', label: 'Transfer Acct', sort: false},
    {key: 'Deposit - Amount Negated', label: 'Deposit', sort: true},
    {key: 'Withdrawal - Amount', label: 'Withdrawal', sort: true},
    {key: 'Balance', label: 'Balance', sort: false},
    {key: 'Confidence', label: 'Conf', sort: true},
    {key: 'MatchReason', label: 'Reason', sort: true},
  ];
  // Fallback column keys for older CSVs
  const DEPOSIT_KEY = DATA.length > 0 && ('Deposit - Amount Negated' in DATA[0]) ? 'Deposit - Amount Negated' : 'Deposit';
  const WITHDRAWAL_KEY = DATA.length > 0 && ('Withdrawal - Amount' in DATA[0]) ? 'Withdrawal - Amount' : 'Withdrawal';
  if (DEPOSIT_KEY === 'Deposit') {
    COLS[4] = {key: 'Deposit', label: 'Deposit', sort: true};
    COLS[5] = {key: 'Withdrawal', label: 'Withdrawal', sort: true};
  }

  let rows = DATA.map((r, i) => ({...r, _idx: i, _changed: false, _origAccount: r.Account || ''}));
  let selected = new Set();
  let lastClickIdx = null;
  let sortCol = 'Confidence';
  let sortAsc = true;
  let filterConf = '';
  let colFilters = {};  // {colKey: 'search text'}
  let activeFilterCol = null;

  // Confidence sort order (worst first for review)
  const CONF_ORDER = {suspense:0, none:1, low:2, smart:3, medium:4, llm:5, override:6, high:7};

  // ── Sort dropdown ──
  const sortDD = document.getElementById('rv-sort-col');
  COLS.filter(c => c.sort).forEach(c => {
    const o = document.createElement('option');
    o.value = c.key; o.textContent = c.label;
    if (c.key === 'Confidence') o.selected = true;
    sortDD.appendChild(o);
  });
  sortDD.onchange = () => { sortCol = sortDD.value; renderTable(); };
  const sortDirBtn = document.getElementById('rv-sort-dir');
  sortDirBtn.onclick = () => { sortAsc = !sortAsc; sortDirBtn.textContent = sortAsc ? '↑ Asc' : '↓ Desc'; renderTable(); };

  // ── Filter ──
  document.getElementById('rv-filter-conf').onchange = (e) => { filterConf = e.target.value; renderTable(); };

  // ── Account search ──
  const acctSearch = document.getElementById('rv-acct-search');
  const acctDD = document.getElementById('rv-acct-dropdown');
  let acctFiltered = ACCOUNTS.slice(0, 50);
  let selectedAccount = '';

  function renderAcctDropdown(filter) {
    const q = (filter || '').toLowerCase();
    acctFiltered = q
      ? ACCOUNTS.filter(a => a.toLowerCase().includes(q)).slice(0, 50)
      : ACCOUNTS.slice(0, 50);
    acctDD.innerHTML = '';
    acctFiltered.forEach(a => {
      const div = document.createElement('div');
      div.className = 'acct-item';
      const parts = a.split(':');
      const leaf = parts[parts.length - 1];
      const path = parts.slice(0, -1).join(':');
      div.innerHTML = '<span class="acct-leaf">' + leaf + '</span> <span class="acct-path">' + path + '</span>';
      div.onclick = () => { selectedAccount = a; acctSearch.value = a; acctDD.classList.remove('open'); };
      acctDD.appendChild(div);
    });
  }

  acctSearch.onfocus = () => { renderAcctDropdown(acctSearch.value); acctDD.classList.add('open'); };
  acctSearch.oninput = () => { renderAcctDropdown(acctSearch.value); acctDD.classList.add('open'); };
  document.addEventListener('click', (e) => {
    if (!e.target.closest('.acct-picker')) acctDD.classList.remove('open');
  });

  // ── Apply to selected ──
  document.getElementById('rv-apply-sel').onclick = () => {
    if (!selectedAccount) { alert('Select an account first'); return; }
    if (selected.size === 0) { alert('Select rows first (click/shift-click)'); return; }
    rows.forEach(r => {
      if (selected.has(r._idx)) {
        r.Account = selectedAccount;
        r.Confidence = 'override';
        r.MatchReason = 'User override (review)';
        r._changed = true;
      }
    });
    renderTable();
  };

  // ── Apply to matching ──
  document.getElementById('rv-apply-match').onclick = () => {
    if (!selectedAccount) { alert('Select an account first'); return; }
    if (selected.size === 0) { alert('Select a row first to define the pattern'); return; }
    // Get descriptions of selected rows, build patterns
    const selDescs = new Set();
    rows.forEach(r => { if (selected.has(r._idx)) selDescs.add(r.Description); });
    let matchCount = 0;
    rows.forEach(r => {
      if (selDescs.has(r.Description)) {
        r.Account = selectedAccount;
        r.Confidence = 'override';
        r.MatchReason = 'User override (batch match)';
        r._changed = true;
        matchCount++;
      }
    });
    alert('Applied to ' + matchCount + ' matching rows');
    renderTable();
  };

  // ── Auto-sync save payload to global (Gradio reads via js parameter) ──
  function syncPayload() {
    const changes = rows.filter(r => r._changed).map(r => ({
      idx: r._idx,
      description: r.Description,
      account: r.Account,
      confidence: r.Confidence,
      matchReason: r.MatchReason,
    }));
    window._rvSavePayload = JSON.stringify({
      gnucash_file: GNUCASH_FILE,
      csv_path: CSV_PATH,
      changes: changes,
      all_rows: rows.map(r => {
        const out = {};
        for (const c of COLS) out[c.key] = r[c.key] || '';
        out['Transaction ID'] = r['Transaction ID'] || '';
        out['Currency'] = r['Currency'] || 'INR';
        return out;
      }),
    });
  }
  // Sync after every change
  const _origApplySel = document.getElementById('rv-apply-sel').onclick;
  const _origApplyMatch = document.getElementById('rv-apply-match').onclick;
  document.getElementById('rv-apply-sel').onclick = function(e) { _origApplySel.call(this, e); syncPayload(); };
  document.getElementById('rv-apply-match').onclick = function(e) { _origApplyMatch.call(this, e); syncPayload(); };
  syncPayload();

  // ── Table rendering ──
  function renderTable() {
    const thead = document.getElementById('rv-thead');
    const tbody = document.getElementById('rv-tbody');

    // Header
    thead.innerHTML = '<tr>' + COLS.map(c =>
      '<th data-col="' + c.key + '">' + c.label +
      (c.key === sortCol ? '<span class="sort-arrow">' + (sortAsc ? '▲' : '▼') + '</span>' : '') +
      '</th>'
    ).join('') + '</tr>';
    thead.querySelectorAll('th').forEach(th => {
      th.onclick = () => {
        const col = th.dataset.col;
        if (col === sortCol) { sortAsc = !sortAsc; sortDirBtn.textContent = sortAsc ? '↑ Asc' : '↓ Desc'; }
        else { sortCol = col; sortDD.value = col; sortAsc = true; sortDirBtn.textContent = '↑ Asc'; }
        renderTable();
      };
    });

    // Column filter row
    const existingFilter = thead.querySelector('.filter-row');
    if (!existingFilter) {
      const fRow = document.createElement('tr');
      fRow.className = 'filter-row';
      COLS.forEach(c => {
        const td = document.createElement('td');
        const inp = document.createElement('input');
        inp.type = 'text';
        inp.placeholder = '\u2315';
        inp.dataset.col = c.key;
        inp.value = colFilters[c.key] || '';
        inp.oninput = () => { colFilters[c.key] = inp.value; activeFilterCol = c.key; renderTable(); };
        inp.onclick = (e) => e.stopPropagation();
        td.appendChild(inp);
        fRow.appendChild(td);
      });
      thead.appendChild(fRow);
    } else {
      existingFilter.querySelectorAll('input').forEach(inp => {
        inp.value = colFilters[inp.dataset.col] || '';
      });
    }

    // Filter
    let filtered;
    if (filterConf === 'contra') {
      filtered = rows.filter(r => !!r._contra);
    } else {
      filtered = filterConf ? rows.filter(r => (r.Confidence||'').toLowerCase() === filterConf) : rows;
    }
    // Column text filters
    for (const [col, q] of Object.entries(colFilters)) {
      if (!q) continue;
      const ql = q.toLowerCase();
      filtered = filtered.filter(r => ((r[col]||'') + '').toLowerCase().includes(ql));
    }

    // Sort
    filtered = [...filtered].sort((a, b) => {
      let va = a[sortCol] || '', vb = b[sortCol] || '';
      if (sortCol === 'Confidence') {
        va = CONF_ORDER[va.toLowerCase()] ?? 99;
        vb = CONF_ORDER[vb.toLowerCase()] ?? 99;
      } else if (sortCol === DEPOSIT_KEY || sortCol === WITHDRAWAL_KEY || sortCol === 'Balance') {
        va = parseFloat(va) || 0; vb = parseFloat(vb) || 0;
      }
      if (va < vb) return sortAsc ? -1 : 1;
      if (va > vb) return sortAsc ? 1 : -1;
      return 0;
    });

    // Render rows
    tbody.innerHTML = '';
    filtered.forEach(r => {
      const tr = document.createElement('tr');
      if (selected.has(r._idx)) tr.classList.add('selected');
      tr.dataset.idx = r._idx;

      COLS.forEach(c => {
        const td = document.createElement('td');
        let val = r[c.key] || '';
        if (c.key === 'Confidence') {
          const cls = 'conf-' + (val||'none').toLowerCase();
          td.innerHTML = '<span class="' + cls + '">' + val + '</span>';
          if (r._changed) td.innerHTML += '<span class="changed-marker">*</span>';
        } else if (c.key === 'Account' && r._changed) {
          td.innerHTML = val + '<span class="changed-marker"> *</span>';
          td.title = 'Changed from: ' + r._origAccount;
        } else if (c.key === 'MatchReason' && r._contra) {
          td.innerHTML = val + '<span class="contra-badge ' + (r._contra_conf || '') +
            '" title="' + r._contra + '">CONTRA</span>';
          td.title = r._contra;
        } else {
          td.textContent = val;
          td.title = val;
        }
        tr.appendChild(td);
      });

      tr.onclick = (e) => handleRowClick(r._idx, e);
      tbody.appendChild(tr);
    });

    // Stats
    const changed = rows.filter(r => r._changed).length;
    const stats = document.getElementById('rv-stats');
    stats.textContent = filtered.length + '/' + rows.length + ' rows' +
      (selected.size ? ' | ' + selected.size + ' selected' : '') +
      (changed ? ' | ' + changed + ' changed' : '');

    // Re-focus the filter input that was being typed in
    if (activeFilterCol) {
      const inp = thead.querySelector('.filter-row input[data-col="' + activeFilterCol + '"]');
      if (inp) { inp.focus(); inp.selectionStart = inp.selectionEnd = inp.value.length; }
    }
  }

  function handleRowClick(idx, e) {
    if (e.shiftKey && lastClickIdx !== null) {
      // Range select
      const allIdxs = [...document.querySelectorAll('#rv-tbody tr')].map(tr => parseInt(tr.dataset.idx));
      const startPos = allIdxs.indexOf(lastClickIdx);
      const endPos = allIdxs.indexOf(idx);
      if (startPos >= 0 && endPos >= 0) {
        const from = Math.min(startPos, endPos);
        const to = Math.max(startPos, endPos);
        for (let i = from; i <= to; i++) selected.add(allIdxs[i]);
      }
    } else if (e.ctrlKey || e.metaKey) {
      // Toggle single
      if (selected.has(idx)) selected.delete(idx); else selected.add(idx);
    } else {
      // Single select
      selected.clear();
      selected.add(idx);
    }
    lastClickIdx = idx;
    renderTable();
  }

  // Initial render
  renderTable();
})();
</script>
<img src="data:," onerror="eval(document.getElementById('rv-init-code').textContent)" style="display:none">
"""


# ---------------------------------------------------------------------------
# Python backend: load CSV + account tree → HTML, save handler
# ---------------------------------------------------------------------------

def _load_review_data(csv_path: str, gnucash_path: str) -> str:
    """Load mapped CSV + GnuCash account tree, return interactive HTML."""
    if not csv_path or not gnucash_path:
        return "<p>Select both a mapped CSV and a GnuCash file, then click Load.</p>"

    csv_p = Path(csv_path)
    gc_p = Path(gnucash_path)

    if not csv_p.is_file():
        return f"<p>CSV not found: {csv_p.name}</p>"
    if not gc_p.is_file():
        return f"<p>GnuCash file not found: {gc_p.name}</p>"

    # Read CSV
    with open(csv_p, "r", encoding="utf-8", errors="replace") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        return "<p>CSV is empty — no rows to review.</p>"

    # Extract account tree
    accounts = _extract_account_tree(str(gc_p))
    if not accounts:
        accounts = sorted({r.get("Account", "") for r in rows if r.get("Account")})

    # Load contra sidecar if it exists
    contra_path = csv_p.with_suffix('.contra.json')
    contra_flags = {}
    if contra_path.is_file():
        try:
            with open(contra_path, "r", encoding="utf-8") as cf:
                raw = json.load(cf)
                # Keys are stringified row indices → convert for lookup
                contra_flags = {str(k): v for k, v in raw.items()}
        except Exception:
            pass

    # Merge contra info into rows
    for i, row in enumerate(rows):
        ci = contra_flags.get(str(i))
        if ci:
            row['_contra'] = ci.get('reason', 'Possible contra')
            row['_contra_conf'] = ci.get('confidence', 'medium')
        else:
            row['_contra'] = ''
            row['_contra_conf'] = ''

    # Build HTML with data embedded
    html = _REVIEW_HTML
    html = html.replace("%%DATA_JSON%%", json.dumps(rows))
    html = html.replace("%%ACCOUNTS_JSON%%", json.dumps(accounts))
    html = html.replace("%%GNUCASH_FILE_JSON%%", json.dumps(str(gc_p)))
    html = html.replace("%%CSV_PATH_JSON%%", json.dumps(str(csv_p)))
    return html


def _generalize_pattern(desc: str) -> str:
    """Turn a bank description into a broader regex that catches variants.

    Strips trailing reference numbers (5+ digits), dates (DD-MM-YYYY,
    DD/MM/YYYY), and trailing whitespace/punctuation so that future
    transactions with different ref numbers still match.
    """
    s = desc.strip()
    # Strip trailing reference numbers (e.g. -5150102, /8089934)
    s = re.sub(r'[\s/\-]*\d{5,}\s*$', '', s)
    # Strip trailing dates (DD-MM-YYYY or DD/MM/YYYY)
    s = re.sub(r'[\s/\-]*\d{2}[\-/]\d{2}[\-/]\d{4}\s*$', '', s)
    # Strip trailing punctuation and whitespace
    s = s.rstrip(' -/')
    if len(s) < 6:
        # Too short after stripping — fall back to exact match
        return re.escape(desc.strip())
    # Escape for regex, then allow flexible trailing content
    return re.escape(s) + r'.*'


def _save_changes(changes_json: str) -> tuple[str, str | None]:
    """Process save from the review UI — write overrides + re-export CSV.

    Returns (status_markdown, csv_path_or_None).
    """
    if not changes_json or not changes_json.strip():
        return "No changes to save.", gr.update(visible=False)

    try:
        payload = json.loads(changes_json)
    except json.JSONDecodeError as e:
        return f"Error parsing changes: {e}", gr.update(visible=False)

    gnucash_file = payload.get("gnucash_file", "")
    csv_path = payload.get("csv_path", "")
    changes = payload.get("changes", [])
    all_rows = payload.get("all_rows", [])

    if not changes:
        return "No changes to save.", gr.update(visible=False)

    # ── Save overrides YAML ──
    # Ensure agents path is importable
    agents_root = Path(__file__).resolve().parent.parent.parent / "src" / "agents"
    if str(agents_root) not in sys.path:
        sys.path.insert(0, str(agents_root))

    try:
        from skill_gnucash_account_mapper.persistent_rules import (
            load_overrides,
            rules_path,
            save_overrides_batch,
        )

        _cfg_path = str(_config_mod.PORTABLE_CONFIG_PATH)
        existing = load_overrides(gnucash_file, config_path=_cfg_path)
        existing_patterns: set[str] = set()
        for o in existing:
            for p in o.get("patterns", []):
                existing_patterns.add(p)

        new_overrides = []
        for ch in changes:
            desc = ch.get("description", "")
            account = ch.get("account", "")
            if not desc or not account:
                continue
            # Never save overrides that map to Suspense — those are unresolved rows
            if "Suspense" in account:
                continue
            # Generalize pattern — strip trailing refs/dates for broader matching
            pattern = _generalize_pattern(desc)
            if pattern not in existing_patterns:
                new_overrides.append({"pattern": pattern, "account": account})
                existing_patterns.add(pattern)

        if new_overrides:
            all_overrides = existing + new_overrides
            save_overrides_batch(gnucash_file, all_overrides, config_path=_cfg_path)
            _rp = rules_path(gnucash_file, config_path=_cfg_path)
            override_msg = f"Saved {len(new_overrides)} new override(s) ({len(all_overrides)} total) → {_rp}"
        else:
            override_msg = "No new overrides needed (all patterns already saved)"

    except Exception as e:
        override_msg = f"Warning: could not save overrides — {e}"

    # ── Re-export CSV ──
    download_path: str | None = None
    if all_rows and csv_path:
        try:
            csv_p = Path(csv_path)
            # Preserve original CSV column order by reading existing header
            try:
                with open(csv_p, "r", encoding="utf-8", errors="replace") as rf:
                    original_headers = csv.DictReader(rf).fieldnames or []
                headers = list(original_headers)
                # Add any new keys from payload that aren't in original
                payload_keys = set(all_rows[0].keys())
                for k in payload_keys - set(headers):
                    headers.append(k)
            except Exception:
                headers = list(all_rows[0].keys())
            with open(csv_p, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(all_rows)
            export_msg = f"CSV re-exported: {csv_p.name} ({len(all_rows)} rows)"
            # Copy to download staging dir so Gradio's file server can serve it
            import shutil
            try:
                staging = _config_mod.download_staging_dir()
                staging.mkdir(parents=True, exist_ok=True)
                staged = staging / csv_p.name
                shutil.copy2(csv_p, staged)
                download_path = str(staged)
            except Exception:
                download_path = str(csv_p)
        except Exception as e:
            export_msg = f"Warning: could not re-export CSV — {e}"
    else:
        export_msg = "CSV not re-exported (no row data)"

    msg = f"✅ **Saved**\n\n{override_msg}\n\n{export_msg}"
    if download_path:
        return msg, gr.update(value=download_path, visible=True)
    return msg, gr.update(visible=False)


# ---------------------------------------------------------------------------
# Gradio tab renderer
# ---------------------------------------------------------------------------

def render() -> None:
    """Render the Review Mappings tab. Must be called inside gr.Tab()."""

    gr.Markdown("## Review & Edit Account Mappings\n\nSelect a mapped CSV and GnuCash book, then click Load.")

    initial_csvs = _scan_import_ready_csvs()

    with gr.Row():
        csv_dropdown = gr.Dropdown(
            label="Mapped CSV",
            choices=initial_csvs,
            value=initial_csvs[0] if initial_csvs else None,
            allow_custom_value=True,
            scale=4,
        )
        refresh_btn = gr.Button("↻", scale=0, min_width=40)
        gnucash_file = gr.File(
            label="GnuCash book (.gnucash)",
            file_types=[".gnucash"],
            type="filepath",
        )

    load_btn = gr.Button("Load for Review", variant="primary")

    refresh_btn.click(
        fn=lambda: gr.update(choices=_scan_import_ready_csvs()),
        inputs=[],
        outputs=[csv_dropdown],
    )

    review_html = gr.HTML(value="<p><em>Load a CSV to begin reviewing.</em></p>")

    save_btn = gr.Button("Save & Export", variant="primary")
    save_result = gr.Markdown("")
    download_file = gr.DownloadButton(
        label="Download corrected CSV", visible=False, variant="primary",
    )

    # Real textbox (visible=True so it's in the DOM) hidden via CSS.
    # gr.State has no frontend element, so the js parameter can't inject into it.
    _payload_box = gr.Textbox(
        value="", show_label=False, container=False, lines=1,
        elem_id="rv-payload-box",
    )

    load_btn.click(
        fn=_load_review_data,
        inputs=[csv_dropdown, gnucash_file],
        outputs=review_html,
    )
    save_btn.click(
        fn=_save_changes,
        inputs=[_payload_box],
        outputs=[save_result, download_file],
        js="(x) => window._rvSavePayload || ''",
    )
