"""
ui/_review_tree.py -- ITR-07 opt-in "tree + side panel" mode for the shared
review engine (ui/_review_engine.py).

A screen opts in with `ReviewSpec.tree = {...}` (see itr_mapping_review.py).
With `tree` unset, `tokens()` is never called and every hook token in the
engine template expands to the ORIGINAL text (or to nothing), so the other
review tabs render byte-for-byte as before (pinned by a golden-hash test).

What the mode adds (nothing here decides what a mapping means; it only shows,
filters and selects):
  left   book-order list, grouped by the account tree (headings never saved),
         a flat alternative, coverage line, legend, tax-kind stripe + pill,
         a dashed "likely" hint (display only, never counted, never saved);
  right  return-coverage panel with clickable counts, up to 3 rule-based
         suggestions with reasons, recently used tags, plain-word search, a
         CURRENT marker, and a named Apply button (double-click applies too).

Presentation facts (row tags, state) are recomputed in JS from the row's
current value after every edit, because a person's edits happen client-side.
"""
from __future__ import annotations

# ---------------------------------------------------------------------------
# CSS (tokens first; light and dark)
# ---------------------------------------------------------------------------

CSS = r"""
<style>
#%%APP%%-app .badge:empty { display: none; }
#%%APP%%-app {
  --inc: #34d399; --ded: #60a5fa; --paid: #c084fc; --rep: #9ca3af; --none: #6b7280;
  --inc-soft: rgba(52,211,153,.16); --ded-soft: rgba(96,165,250,.16); --paid-soft: rgba(192,132,252,.16);
  --panel: #161616; --line: #333; --muted: #9ca3af; --text: #e0e0e0; --warn: #fbbf24; --ok: #4ade80;
}
/* the engine's own screens are dark-only, so these tokens are too */
/* the side panel replaces the top picker */
#%%APP%%-app .picker, #%%APP%%-app #%%APP%%-apply-sel { display: none; }
#%%APP%%-app .tbar { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; padding: 4px 0 8px; }
#%%APP%%-app .tbar button {
  padding: 3px 10px; border-radius: 4px; font-size: 12px; cursor: pointer;
  border: 1px solid var(--line); background: transparent; color: var(--text);
}
#%%APP%%-app .tbar button.on { background: #2563eb; color: #fff; border-color: transparent; }
#%%APP%%-app .cov { font-size: 12px; color: var(--text); }
#%%APP%%-app .ready { font-size: 12px; font-weight: 700; }
#%%APP%%-app .ready.no { color: var(--warn); }
#%%APP%%-app .ready.yes { color: var(--ok); }
#%%APP%%-app .tlegend { display: flex; flex-wrap: wrap; gap: 10px; font-size: 11px; color: var(--muted); padding: 0 0 6px; }
#%%APP%%-app .tlegend .sw { display: inline-block; width: 4px; height: 12px; margin-right: 4px; vertical-align: middle; }
#%%APP%%-app .tlegend .sw.inc { background: var(--inc); }
#%%APP%%-app .tlegend .sw.ded { background: var(--ded); }
#%%APP%%-app .tlegend .sw.paid { background: var(--paid); }
#%%APP%%-app .tlegend .sw.rep { background: var(--rep); }
#%%APP%%-app .tmain { display: flex; gap: 12px; align-items: flex-start; }
#%%APP%%-app .tleft { flex: 1 1 auto; min-width: 0; }
#%%APP%%-app .tside {
  flex: 0 0 340px; max-height: 75vh; overflow-y: auto; border: 1px solid var(--line);
  border-radius: 4px; background: var(--panel); padding: 8px; font-size: 12px; color: var(--text);
}
#%%APP%%-app .tside .sbox { margin-bottom: 10px; }
#%%APP%%-app .tside .sh { font-weight: 700; font-size: 11px; text-transform: uppercase; color: var(--muted); margin-bottom: 4px; }
#%%APP%%-app .tside input[type=text] {
  width: 100%; box-sizing: border-box; padding: 5px 8px; border: 1px solid var(--line);
  border-radius: 4px; background: transparent; color: var(--text); font-size: 12px;
}
#%%APP%%-app .tside .apply { width: 100%; padding: 7px; font-size: 12px; font-weight: 700; border-radius: 4px;
  border: none; background: #2563eb; color: #fff; cursor: pointer; }
#%%APP%%-app .tside .apply[disabled] { background: var(--line); color: var(--muted); cursor: not-allowed; }
#%%APP%%-app .scard, #%%APP%%-app .tagopt {
  border: 1px solid var(--line); border-radius: 4px; padding: 5px 7px; margin-bottom: 4px; cursor: pointer;
}
#%%APP%%-app .scard:hover, #%%APP%%-app .tagopt:hover { border-color: #60a5fa; }
#%%APP%%-app .scard.chosen, #%%APP%%-app .tagopt.chosen { border-color: #2563eb; box-shadow: inset 0 0 0 1px #2563eb; }
#%%APP%%-app .scard .why, #%%APP%%-app .tagopt .note { color: var(--muted); font-size: 11px; }
#%%APP%%-app .chip { display: inline-block; border: 1px solid var(--line); border-radius: 10px; padding: 1px 8px;
  margin: 0 4px 4px 0; cursor: pointer; font-size: 11px; }
#%%APP%%-app .chip:hover { border-color: #60a5fa; }
#%%APP%%-app .covrow { display: flex; justify-content: space-between; padding: 2px 4px; border-radius: 3px; cursor: pointer; }
#%%APP%%-app .covrow:hover { background: rgba(96,165,250,.14); }
#%%APP%%-app .covrow.active { background: rgba(37,99,235,.28); }
#%%APP%%-app .covrow .n { font-weight: 700; }
#%%APP%%-app .cur { display: inline-block; font-size: 10px; font-weight: 700; padding: 0 5px; border-radius: 3px;
  background: #2563eb; color: #fff; margin-left: 4px; }
#%%APP%%-app .tp { display: inline-block; font-size: 10px; font-weight: 600; padding: 0 6px; border-radius: 8px;
  margin-left: 6px; border: 1px solid transparent; white-space: nowrap; }
#%%APP%%-app .tp.k-inc { background: var(--inc-soft); color: var(--inc); border-color: var(--inc); }
#%%APP%%-app .tp.k-ded { background: var(--ded-soft); color: var(--ded); border-color: var(--ded); }
#%%APP%%-app .tp.k-paid { background: var(--paid-soft); color: var(--paid); border-color: var(--paid); }
#%%APP%%-app .tp.k-rep { color: var(--rep); border-color: var(--rep); }
#%%APP%%-app .tp.k-none { color: var(--none); border-color: var(--none); }
#%%APP%%-app .tp.lk { background: transparent; border-style: dashed; font-style: italic; }
#%%APP%%-app .plabel { color: var(--muted); font-size: 11px; margin-left: 6px; }
#%%APP%%-app tbody tr.kr-inc td:first-child { box-shadow: inset 4px 0 0 var(--inc); }
#%%APP%%-app tbody tr.kr-ded td:first-child { box-shadow: inset 4px 0 0 var(--ded); }
#%%APP%%-app tbody tr.kr-paid td:first-child { box-shadow: inset 4px 0 0 var(--paid); }
#%%APP%%-app tbody tr.kr-rep td:first-child { box-shadow: inset 4px 0 0 var(--rep); }
#%%APP%%-app tbody tr.lr-inc td:first-child { box-shadow: inset 4px 0 0 var(--inc-soft); }
#%%APP%%-app tbody tr.lr-ded td:first-child { box-shadow: inset 4px 0 0 var(--ded-soft); }
#%%APP%%-app tbody tr.lr-paid td:first-child { box-shadow: inset 4px 0 0 var(--paid-soft); }
#%%APP%%-app tbody tr.stale td { color: var(--muted); font-style: italic; }
#%%APP%%-app tbody tr.sech { cursor: default; background: var(--panel); }
#%%APP%%-app tbody tr.sech td { font-weight: 700; color: var(--text); background: var(--panel); max-width: none; }
#%%APP%%-app tbody tr.sech .caret { cursor: pointer; display: inline-block; width: 14px; }
#%%APP%%-app tbody tr.sech input { margin-right: 6px; vertical-align: middle; }
#%%APP%%-app tbody tr.sech .cnt { color: var(--muted); font-weight: 400; margin-left: 6px; }
</style>
"""

# ---------------------------------------------------------------------------
# Markup around the table and the side panel
# ---------------------------------------------------------------------------

OPEN_HTML = r"""  <div class="tbar">
    <button type="button" id="%%APP%%-v-group" class="on" title="Group accounts under their parent accounts">Grouped</button>
    <button type="button" id="%%APP%%-v-flat" title="One flat list, in book order">Flat list</button>
    <button type="button" id="%%APP%%-t-expand" title="Open every group">Expand all</button>
    <button type="button" id="%%APP%%-t-collapse" title="Close every group">Collapse all</button>
    <span class="cov" id="%%APP%%-cov"></span>
    <span class="ready" id="%%APP%%-ready"></span>
  </div>
  <div class="tlegend">
    <span><span class="sw inc"></span>adds income</span>
    <span><span class="sw ded"></span>deduction</span>
    <span><span class="sw paid"></span>tax paid</span>
    <span><span class="sw rep"></span>reported, no tax</span>
    <span>dashed pill = a likely tag (only a hint; not saved, not counted)</span>
  </div>
  <div class="tmain"><div class="tleft">
"""

CLOSE_HTML = r"""  </div>
  <aside class="tside" id="%%APP%%-side">
    <div class="sbox"><div class="sh">Return coverage</div><div id="%%APP%%-side-cov"></div></div>
    <div class="sbox" id="%%APP%%-side-head"></div>
    <div class="sbox"><div class="sh">Suggested</div><div id="%%APP%%-side-sug"></div></div>
    <div class="sbox"><div class="sh">Recently used</div><div id="%%APP%%-side-recent"></div></div>
    <div class="sbox"><div class="sh">Find a tag</div>
      <input type="text" id="%%APP%%-side-q" placeholder="Type plain words: medical, school, rent..." autocomplete="off">
      <div id="%%APP%%-side-list" style="margin-top:6px"></div></div>
    <button type="button" class="apply" id="%%APP%%-side-apply" disabled>Apply</button>
  </aside></div>
"""

# ---------------------------------------------------------------------------
# Client code. Placed just before the engine's final renderTable() call, so it
# can use rows / selected / assign / renderTable / esc / $ / chosen / ITEMS.
# ---------------------------------------------------------------------------

JS = r"""  // ── ITR-07: tree + side panel (opt-in via spec.tree) ──
  const TREE = __TREE_JSON__;
  const HOLD_V = 'HOLD';
  const KIND_WORD = { inc: 'adds income', ded: 'deduction', paid: 'tax paid', rep: 'reported only', none: 'not in return' };
  const ITEM_BY = {};
  ITEMS.forEach(it => { ITEM_BY[it.value] = it; });
  const GROUP_OF = {};
  TREE.groups.forEach(g => g[1].forEach(t => { GROUP_OF[t] = g[0]; }));
  let treeGrouped = true;
  let collapsed = {};
  let covFilter = null;          // {type: 'group'|'unmapped'|'held', name}
  let sideQ = '';
  let recent = [];
  let sibMap = {};
  const RECENT_KEY = 'pask.recent.' + APP;
  try {
    const raw = window.localStorage && window.localStorage.getItem(RECENT_KEY);
    const a = raw ? JSON.parse(raw) : null;
    if (Array.isArray(a)) recent = a.filter(t => ITEM_BY[t]).slice(0, 6);
  } catch (err) { /* storage unavailable: session only */ }

  function kindOf(t) { const m = TREE.tags[t]; return m ? m.k : null; }
  function kc(t) { return kindOf(t) || 'none'; }
  function labelOf(t) { const m = TREE.tags[t]; return m ? m.label : t; }
  function effTag(r) { if (r._deleted) return ''; const t = r[TARGET]; return t ? String(t) : ''; }
  function isReal(t) { return !!t && t !== HOLD_V && !!TREE.tags[t]; }
  function allowedTag(t, r) { return allowedFor(ITEM_BY[t], r); }

  // The one place a row's presentation state is derived from its CURRENT value.
  // HOLD is held (never mapped); a deleted row is unmapped; a stale row is neither.
  function refreshTags(r) {
    if (r._nr === undefined) r._nr = (r._tags || []).indexOf('needs_review') >= 0;
    if (r._stale) { r._tags = ['stale']; r._rowclass = ''; return; }
    const t = effTag(r);
    let tg, rc = '';
    if (!t) { tg = ['unmapped']; rc = 'accent-red'; }
    else if (t === HOLD_V) { tg = r._changed ? ['held', 'unmapped', 'changed'] : ['held', 'unmapped']; rc = 'accent-orange'; }
    else if (r._changed) { tg = ['changed', 'mapped']; }
    else if (r._nr) { tg = ['needs_review', 'mapped']; rc = 'accent-amber'; }
    else { tg = ['mapped']; }
    if (isReal(t) && TREE.tags[t].tax) tg.push('tax');
    r._tags = tg;
    r._rowclass = rc;
  }

  function treeRefreshAll() {
    sibMap = {};
    rows.forEach(r => {
      refreshTags(r);
      const t = effTag(r);
      if (!r._stale && isReal(t)) { const m = sibMap[r._pk] || (sibMap[r._pk] = {}); m[t] = (m[t] || 0) + 1; }
    });
  }

  // ── Suggestions: static word scores from Python (r._sg) + the live sibling bonus ──
  function rawScores(r) {
    const out = {};
    const sg = r._sg || {};
    for (const t in sg) out[t] = { s: sg[t][0], h: sg[t][1].slice(), n: 0 };
    const pk = sibMap[r._pk] || {};
    const own = effTag(r);
    for (const t in pk) {
      const n = pk[t] - (own === t && !r._stale ? 1 : 0);
      if (n > 0 && allowedTag(t, r)) {
        const o = out[t] || (out[t] = { s: 0, h: [], n: 0 });
        o.s += TREE.sibling_bonus; o.n += n;
      }
    }
    return out;
  }
  function suggestFor(list) {
    const total = {};
    list.forEach(r => {
      const raw = rawScores(r);
      for (const t in raw) {
        const o = total[t] || (total[t] = { s: 0, h: [], n: 0 });
        o.s += raw[t].s; o.h = o.h.concat(raw[t].h); o.n += raw[t].n;
      }
    });
    let ranked = Object.keys(total).filter(t => total[t].s >= TREE.min_score);
    ranked.sort((a, b) => (total[b].s - total[a].s) || (TREE.order.indexOf(a) - TREE.order.indexOf(b)));
    if (!ranked.length) return [];
    const top = total[ranked[0]].s;
    if (total[ranked[0]].h.length) ranked = ranked.filter(t => total[t].h.length);
    const segs = list[0]._segs || [];
    const parent = segs.length > 1 ? segs[segs.length - 2] : '';
    return ranked.filter(t => total[t].s >= top * TREE.top_fraction).slice(0, TREE.max_suggestions).map(t => {
      const why = [];
      const uniq = total[t].h.filter((w, i, a) => a.indexOf(w) === i);
      if (uniq.length) why.push('matches "' + uniq.join('", "') + '"');
      if (total[t].n) why.push('used by ' + total[t].n + ' other account' + (total[t].n > 1 ? 's' : '') + ' under ' + parent);
      return { tag: t, score: total[t].s, why: why.slice(0, 2).join('; ') };
    });
  }
  function hintOf(r) {
    if (r._stale || r._deleted || effTag(r)) return null;
    const s = suggestFor([r]);
    return s.length ? s[0] : null;
  }

  // ── Left side: ordering, headings, rows ──
  function passCov(r) {
    if (!covFilter) return true;
    if (r._stale) return false;
    if (covFilter.type === 'unmapped') return (r._tags || []).indexOf('unmapped') >= 0;
    if (covFilter.type === 'held') return (r._tags || []).indexOf('held') >= 0;
    const t = effTag(r);
    return isReal(t) && GROUP_OF[t] === covFilter.name;
  }
  function anyFilter() {
    return !!(statusFilter || covFilter || Object.keys(colFilters).some(k => colFilters[k]));
  }
  function treeSequence(list) {
    list = list.filter(passCov);
    if (sortCol === 'path') list = list.slice().sort((a, b) => (a._ord - b._ord) * (sortAsc ? 1 : -1));
    if (!(treeGrouped && sortCol === 'path' && sortAsc)) return list;
    const ignore = anyFilter();
    const out = [];
    let stack = [];
    list.forEach(r => {
      const anc = (r._segs || []).slice(0, -1);
      let k = 0;
      while (k < stack.length && k < anc.length && stack[k]._name === anc[k]) k++;
      stack = stack.slice(0, k);
      for (let i = k; i < anc.length; i++) {
        const h = { _head: true, _key: anc.slice(0, i + 1).join(':'), _depth: i, _name: anc[i], _leaves: [] };
        stack.push(h);
        const shallowCollapsed = !ignore && stack.slice(0, i).some(x => collapsed[x._key]);
        if (!shallowCollapsed) out.push(h);
      }
      stack.forEach(h => h._leaves.push(r._idx));
      if (ignore || !stack.some(h => collapsed[h._key])) out.push(r);
    });
    return out;
  }
  function leavesOf(h) {
    return h._leaves.filter(i => { const r = rows[i]; return r && !r._stale; });
  }
  function treeHeadRow(h) {
    const tr = document.createElement('tr');
    tr.classList.add('sech');
    const td = document.createElement('td');
    td.colSpan = COLS.length;
    td.style.paddingLeft = (8 + h._depth * 16) + 'px';
    const live = leavesOf(h);
    const box = document.createElement('input');
    box.type = 'checkbox';
    box.checked = live.length > 0 && live.every(i => selected.has(i));
    box.title = 'Select every account listed under this heading (within the current filter)';
    box.onclick = (e) => {
      if (e && e.stopPropagation) e.stopPropagation();
      if (live.every(i => selected.has(i))) live.forEach(i => selected.delete(i));
      else live.forEach(i => selected.add(i));
      lastClickIdx = null;
      renderTable();
    };
    const caret = document.createElement('span');
    caret.className = 'caret';
    caret.textContent = collapsed[h._key] && !anyFilter() ? '▸' : '▾';
    const name = document.createElement('span');
    name.textContent = h._name;
    const cnt = document.createElement('span');
    cnt.className = 'cnt';
    cnt.textContent = '(' + h._leaves.length + ')';
    const toggle = (e) => {
      if (e && e.stopPropagation) e.stopPropagation();
      collapsed[h._key] = !collapsed[h._key];
      renderTable();
    };
    caret.onclick = toggle;
    name.onclick = toggle;
    td.appendChild(box); td.appendChild(caret); td.appendChild(name); td.appendChild(cnt);
    tr.appendChild(td);
    return tr;
  }
  function treeRowClass(r, tr) {
    if (r._stale) { tr.classList.add('stale'); return; }
    const t = effTag(r);
    if (isReal(t)) { tr.classList.add('kr-' + kc(t)); return; }
    const h = hintOf(r);
    if (h && kindOf(h.tag)) tr.classList.add('lr-' + kindOf(h.tag));
  }
  function treeCell(r, c, html) {
    if (c.key !== TARGET || r._stale || r._deleted) return html;
    const t = effTag(r);
    if (isReal(t)) {
      return html + '<span class="tp k-' + kc(t) + '" title="' + esc(KIND_WORD[kc(t)]) + '">' + esc(KIND_WORD[kc(t)]) +
             '</span><span class="plabel">' + esc(labelOf(t)) + '</span>';
    }
    const h = hintOf(r);
    if (h) {
      return html + '<span class="tp lk k-' + kc(h.tag) + '" title="Likely (a hint only; not saved, not counted): ' +
             esc(h.why) + '">likely: ' + esc(labelOf(h.tag)) + '</span>';
    }
    return html;
  }

  // ── Coverage line ──
  function treeCoverage() {
    let n = 0, mapped = 0, unm = 0, held = 0, stale = 0, changed = 0;
    rows.forEach(r => {
      if (r._stale) { stale++; return; }
      n++;
      if (r._changed) changed++;
      const t = effTag(r);
      if (!t) unm++; else if (t === HOLD_V) held++; else mapped++;
    });
    let s = n + ' accounts: ' + mapped + ' mapped, ' + unm + ' unmapped, ' + held + ' on hold';
    if (stale) s += ', ' + stale + ' not in book';
    if (TREE.hidden) s += '. ' + TREE.hidden + ' hidden or placeholder account(s) are not listed';
    if (!TREE.has_book) s += '. No book is registered, so the not-in-book check was not run';
    if (changed) s += '. Counts include unsaved changes';
    $('cov').textContent = s;
    const ok = n > 0 && unm + held === 0;
    const rd = $('ready');
    rd.className = 'ready ' + (ok ? 'yes' : 'no');
    rd.textContent = ok ? 'Every listed account has a tag'
                        : 'NOT FILING-READY: ' + (unm + held) + ' account(s) unmapped or on hold';
    return { n: n, mapped: mapped, unm: unm, held: held };
  }

  // ── Right side ──
  function selRows() { return rows.filter(r => selected.has(r._idx)); }
  function liveSel() { return selRows().filter(r => !r._stale); }
  function pill(t) { return '<span class="tp k-' + kc(t) + '">' + esc(KIND_WORD[kc(t)]) + '</span>'; }
  function currentTag(live) {
    if (!live.length) return '';
    const t0 = effTag(live[0]);
    return t0 && live.every(r => effTag(r) === t0) ? t0 : '';
  }
  function validForAll(t, live) { return !!ITEM_BY[t] && live.every(r => allowedTag(t, r)); }
  function card(t, why, cur, extra) {
    return '<div class="' + extra + (chosen === t ? ' chosen' : '') + '" data-pick="' + esc(t) + '">' +
           '<b>' + esc(labelOf(t)) + '</b>' + (t === cur ? '<span class="cur">CURRENT</span>' : '') +
           (isReal(t) ? pill(t) : '') +
           '<div class="' + (extra === 'scard' ? 'why' : 'note') + '">' + esc(t) + (why ? ' - ' + esc(why) : '') + '</div></div>';
  }
  function treeSide() {
    const live = liveSel(), all = selRows();
    const cur = currentTag(live);
    const head = $('side-head');
    if (!all.length) head.innerHTML = '<div class="sh">Selection</div>Click an account on the left (shift-click for a range, tick a heading for a group).';
    else if (!live.length) head.innerHTML = '<div class="sh">Selection</div>' + all.length +
      ' selected, none in the book any more. They cannot take a tag; use Remove selected.';
    else head.innerHTML = '<div class="sh">Selection</div><b>' + live.length + '</b> account' + (live.length > 1 ? 's' : '') +
      ' selected' + (all.length > live.length ? ' (' + (all.length - live.length) + ' not in book, skipped)' : '') +
      (live.length === 1 ? '<div class="note">' + esc(cellText(live[0], COLS[0].key)) + '</div>' : '');

    // suggestions
    const sug = live.length ? suggestFor(live).filter(s => validForAll(s.tag, live)) : [];
    $('side-sug').innerHTML = !live.length ? '<div class="note">Select accounts to see suggestions.</div>'
      : sug.length ? sug.map(s => card(s.tag, s.why, cur, 'scard')).join('')
      : '<div class="note">No confident suggestion. Search below in plain words.</div>';

    // recently used
    const rec = recent.filter(t => !live.length || validForAll(t, live));
    $('side-recent').innerHTML = rec.length
      ? rec.map(t => '<span class="chip" data-pick="' + esc(t) + '" title="' + esc(t) + '">' + esc(labelOf(t)) + '</span>').join('')
      : '<div class="note">Nothing yet.</div>';

    // plain-word search over the tags valid for ALL selected accounts
    const ql = sideQ.trim().toLowerCase();
    let base = ITEMS.filter(it => !live.length || validForAll(it.value, live));
    if (ql) base = base.filter(it => (labelOf(it.value) + ' ' + it.primary + ' ' + it.secondary + ' ' + it.value + ' ' + (it.keywords || '')).toLowerCase().includes(ql));
    $('side-list').innerHTML = base.slice(0, 40).map(it => card(it.value, it.secondary, cur, 'tagopt')).join('') ||
      '<div class="note">No matching tag for the selected accounts.</div>';

    // coverage (BS first, as in the return)
    const cnt = {};
    let unm = 0, held = 0;
    rows.forEach(r => {
      if (r._stale) return;
      const t = effTag(r);
      if (!t) unm++; else if (t === HOLD_V) held++; else if (isReal(t)) cnt[GROUP_OF[t]] = (cnt[GROUP_OF[t]] || 0) + 1;
    });
    const line = (type, name, n) => '<div class="covrow' + (covFilter && covFilter.type === type && covFilter.name === name ? ' active' : '') +
      '" data-cov="' + esc(type) + '" data-name="' + esc(name) + '"><span>' + esc(name) + '</span><span class="n">' + n + '</span></div>';
    $('side-cov').innerHTML =
      (covFilter ? '<div class="covrow" data-cov="clear" data-name="">Showing one section. Show everything</div>' : '') +
      TREE.groups.filter(g => g[0] !== TREE.hold_group).map(g => line('group', g[0], cnt[g[0]] || 0)).join('') +
      line('unmapped', 'Unmapped', unm) + line('held', 'On hold', held) +
      line('group', TREE.hold_group, cnt[TREE.hold_group] || 0);

    // named apply button
    const btn = $('side-apply');
    if (chosen && live.length && validForAll(chosen, live)) {
      btn.disabled = false;
      btn.textContent = 'Apply ' + labelOf(chosen) + ' to ' + live.length + ' account' + (live.length > 1 ? 's' : '');
    } else {
      btn.disabled = true;
      btn.textContent = live.length ? 'Pick a tag to apply' : 'Select accounts first';
    }
  }

  function addRecent(t) {
    recent = [t].concat(recent.filter(x => x !== t)).slice(0, 6);
    try { if (window.localStorage) window.localStorage.setItem(RECENT_KEY, JSON.stringify(recent)); } catch (err) { /* session only */ }
  }
  function treeApply(tag) {
    const all = selRows();
    if (!all.length) { alert('Select accounts on the left first.'); return 0; }
    const live = all.filter(r => !r._stale);
    if (!live.length) { alert('These accounts are not in the book any more, so they cannot take a tag. Use Remove selected.'); return 0; }
    if (tag) { chosen = tag; pickSearch.value = tag; syncPickClear(); }
    const n = assign(r => selected.has(r._idx) && !r._stale, ALSO_SET);
    if (n > 0) {
      addRecent(chosen);
      clearPicker();
      if (all.length > live.length) alert((all.length - live.length) + ' selected account(s) are not in the book and were skipped.');
      treeSide();
    }
    return n;
  }

  // delegated handlers: walk up from the target and read data-*
  function dataUp(t, key, stop) {
    for (let n = t; n && n !== stop; n = n.parentNode) {
      if (n.dataset && n.dataset[key] !== undefined) return n;
    }
    return null;
  }
  const sideEl = $('side');
  sideEl.onclick = (e) => {
    const t = e && e.target;
    const pk = dataUp(t, 'pick', sideEl);
    if (pk) { chosen = pk.dataset.pick; pickSearch.value = chosen; syncPickClear(); treeSide(); return; }
    const cv = dataUp(t, 'cov', sideEl);
    if (cv) {
      const type = cv.dataset.cov, name = cv.dataset.name;
      if (type === 'clear' || (covFilter && covFilter.type === type && covFilter.name === name)) covFilter = null;
      else covFilter = { type: type, name: name };
      renderTable();
    }
  };
  sideEl.ondblclick = (e) => {
    const pk = dataUp(e && e.target, 'pick', sideEl);
    if (pk) treeApply(pk.dataset.pick);
  };
  const sideQEl = $('side-q');
  sideQEl.oninput = () => { sideQ = sideQEl.value; treeSide(); };
  $('side-apply').onclick = () => { treeApply(''); };

  const vg = $('v-group'), vf = $('v-flat');
  function setView(g) {
    treeGrouped = g;
    if (vg && vg.classList) { if (g) vg.classList.add('on'); else vg.classList.remove('on'); }
    if (vf && vf.classList) { if (g) vf.classList.remove('on'); else vf.classList.add('on'); }
    renderTable();
  }
  vg.onclick = () => setView(true);
  vf.onclick = () => setView(false);
  $('t-expand').onclick = () => { collapsed = {}; renderTable(); };
  $('t-collapse').onclick = () => {
    rows.forEach(r => {
      const anc = (r._segs || []).slice(0, -1);
      for (let i = 0; i < anc.length; i++) collapsed[anc.slice(0, i + 1).join(':')] = true;
    });
    renderTable();
  };

  function treeAfterRender() { treeCoverage(); treeSide(); }
  treeRefreshAll();

"""


def tokens(tree: dict, js_json) -> dict[str, str]:
    """Hook-token values for a screen that opted in. `tree` keys:
    tags {tag: {label, k, tax}}, order [tag...], groups [[name, [tag...]]],
    hold_group, hidden, has_book, plus the scoring constants."""
    cfg = dict(tree)
    cfg.setdefault("hidden", 0)
    cfg.setdefault("has_book", False)
    return {
        "%%TREE_OPEN%%": OPEN_HTML,
        "%%TREE_CLOSE%%": CLOSE_HTML,
        "%%TREE_PRE%%": "treeRefreshAll(); ",
        "%%TREE_SEQ%%": "filtered = treeSequence(filtered); ",
        "%%TREE_HEAD%%": "if (r._head) { tbody.appendChild(treeHeadRow(r)); return; } ",
        "%%TREE_ROW%%": "treeRowClass(r, tr); ",
        "%%TREE_CELL%%": "html = treeCell(r, c, html); ",
        "%%TREE_COUNT%%": ".filter(x => !x._head)",
        "%%TREE_AFTER%%": " treeAfterRender();",
        "%%TREE_NAN%%": ".filter(n => n === n)",
        "%%TREE_SEL%%": " treeSide();",
        "%%TREE_JS%%": JS.replace("__TREE_JSON__", js_json(cfg)),
    }


NO_TOKENS = {
    "%%TREE_OPEN%%": "", "%%TREE_CLOSE%%": "", "%%TREE_PRE%%": "", "%%TREE_SEQ%%": "",
    "%%TREE_HEAD%%": "", "%%TREE_ROW%%": "", "%%TREE_CELL%%": "", "%%TREE_COUNT%%": "",
    "%%TREE_AFTER%%": "", "%%TREE_NAN%%": "", "%%TREE_SEL%%": "", "%%TREE_JS%%": "",
}
