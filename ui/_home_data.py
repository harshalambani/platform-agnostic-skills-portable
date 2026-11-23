"""
ui/_home_data.py -- what the Home screen shows (UI-54, part 3).

Three read-only views built from stores that already exist:

  * NEEDS YOU     problem lines from the run history (ui/_runhistory.py) and coverage gaps from
                  the last coverage check (ui/_coverage_last.py), grouped by entity then
                  financial year;
  * RECENT RUNS   the last runs, newest first;
  * the AI line   the active LLM endpoint, from the shared health cache (ui/_health.py).

Rules this module keeps:
  * Reading scans nothing and writes nothing. Only run_check_now() writes, and it goes through
    ui/_coverage_last.check_now (read-only scan of the registered books; only
    coverage-last.json is written).
  * Nothing here may raise on bad data: a missing, empty or corrupt store renders as "nothing
    recorded"; anything unknown is shown as stored.
  * Every piece of stored text is escaped before it goes into HTML, so a "why" containing
    markdown or tags shows as text.

The model builders are pure (they take the loaded stores), so tests feed them synthetic data.
"""
from __future__ import annotations

import html
import re
import threading
from datetime import datetime

from . import _book_registry, _config, _coverage_last, _health, _runhistory

# A run's latest result raises a line on Home when it is one of these. ok, refused and
# cancelled raise nothing.
PROBLEM_RESULTS = ("stopped", "failed", "attention", "not_ready", "interrupted")

_RESULT_WORD = {
    "ok": "OK", "stopped": "stopped", "failed": "failed", "attention": "needs a look",
    "not_ready": "not ready", "interrupted": "cut off", "cancelled": "cancelled",
    "refused": "refused",
}

NO_ENTITY = "(no entity picked)"
MAX_GAP_LINES = 6            # per entity and year; the rest is summarised in one line

_YEAR_RX = re.compile(r"^\s*(?:(FY|AY)\s*[:\-]?\s*)?(\d{4})\s*[-/]\s*(\d{2}|\d{4})\s*$", re.I)


# ----------------------------------------------------------------------------- small helpers

def normalize_year(value) -> str:
    """A year as 'YYYY-YY' in FINANCIAL-year terms. 'FY2025-26' -> '2025-26'; an
    assessment year is one year later than its financial year, so 'AY 2026-27' -> '2025-26'.
    A bare '2025-26' is already a financial year (the ITR Workbook's own dropdown stores the
    income-year key). Anything unrecognised comes back trimmed, as stored."""
    text = str(value or "").strip()
    m = _YEAR_RX.match(text)
    if not m:
        return text
    kind, start = (m.group(1) or "").upper(), int(m.group(2))
    if kind == "AY":
        start -= 1
    return f"{start}-{(start + 1) % 100:02d}"


def _s(value) -> str:
    return str(value if value is not None else "")


def _fmt_ts(ts, with_time: bool) -> str:
    try:
        d = datetime.fromisoformat(_s(ts))
    except ValueError:
        return _s(ts)[:16]
    return d.strftime("%d %b %H:%M" if with_time else "%d %b").lstrip("0")


def _month_names(months) -> str:
    out = []
    for m in months:
        try:
            out.append(datetime.strptime(_s(m), "%Y-%m").strftime("%b"))
        except ValueError:
            out.append(_s(m))
    return ", ".join(out)


def _esc(value) -> str:
    return html.escape(_s(value), quote=True)


# ----------------------------------------------------------------------------- the model

def build_needs_you(history: list, coverage: dict, registered: dict, display_names: dict) -> list[dict]:
    """The NEEDS YOU model: [{"entity": str, "years": [{"year": str, "lines": [line]}]}].

    A line is {"kind": "problem"|"gap"|"clear"|"unchecked", "text": str, "date": str}.
      history        _runhistory.tail() records, oldest first
      coverage       _coverage_last.load(): {"entity|year": entry}
      registered     {entity key: [FY, ...]} of registered books
      display_names  {internal skill name: display name}
    """
    history = history if isinstance(history, list) else []
    coverage = coverage if isinstance(coverage, dict) else {}
    registered = registered if isinstance(registered, dict) else {}
    display_names = display_names if isinstance(display_names, dict) else {}

    # One canonical spelling per entity: the coverage key, else the registry key, else as stored.
    alias: dict[str, str] = {}
    for key in registered:
        alias[_s(key).strip().casefold()] = _s(key).strip()
    for entry in coverage.values():
        if isinstance(entry, dict):
            ent = _s(entry.get("entity")).strip()
            alias[ent.casefold()] = ent
    for entry in coverage.values():
        if isinstance(entry, dict) and _s(entry.get("label")).strip():
            alias.setdefault(_s(entry.get("label")).strip().casefold(), _s(entry.get("entity")).strip())

    def canon(name) -> str:
        text = _s(name).strip()
        return alias.get(text.casefold(), text)

    groups: dict[str, dict[str, list[dict]]] = {}      # entity -> year -> lines
    seen_years: dict[str, set] = {}                    # entity -> every year we know of

    def touch(entity: str, year: str) -> list[dict]:
        seen_years.setdefault(entity, set()).add(year)
        return groups.setdefault(entity, {}).setdefault(year, [])

    # -- problem lines: the latest run per (skill, entity, year) decides --
    latest: dict[tuple, dict] = {}
    for rec in history:
        if not isinstance(rec, dict):
            continue
        entity, year = canon(rec.get("entity")), normalize_year(rec.get("year"))
        seen_years.setdefault(entity, set()).add(year)
        latest[(_s(rec.get("skill")), entity, year)] = rec
    for (skill, entity, year), rec in latest.items():
        result = _s(rec.get("result"))
        if result not in PROBLEM_RESULTS:
            continue
        name = _s(display_names.get(skill) or skill)
        why = _s(rec.get("why")).strip()
        text = f"{name} {_RESULT_WORD.get(result, result)}" + (f": {why}" if why else "")
        touch(entity, year).append({"kind": "problem", "text": text, "date": _fmt_ts(rec.get("ts"), False),
                                    "_ts": _s(rec.get("ts"))})

    # -- coverage lines: months with no statement, from the last check --
    checked: dict[tuple, dict] = {}
    for entry in coverage.values():
        if not isinstance(entry, dict):
            continue
        entity, year = canon(entry.get("entity")), normalize_year(entry.get("year"))
        checked[(entity, year)] = entry
        lines = touch(entity, year)
        accounts = entry.get("accounts") if isinstance(entry.get("accounts"), dict) else {}
        gap_lines = []
        for path, acct in sorted(accounts.items()):
            if not isinstance(acct, dict):
                continue
            months = sorted(set(acct.get("gap_months") or []) | set(acct.get("trailing_months") or []))
            if not months:
                continue
            name = _s(path).split(":")[-1] or _s(path)
            n = len(months)
            gap_lines.append(f"{n} month{'s' if n != 1 else ''} with no {name} statement ({_month_names(months)})")
        for text in gap_lines[:MAX_GAP_LINES]:
            lines.append({"kind": "gap", "text": text, "date": _fmt_ts(entry.get("checked"), False), "_ts": ""})
        if len(gap_lines) > MAX_GAP_LINES:
            lines.append({"kind": "gap", "text": f"and {len(gap_lines) - MAX_GAP_LINES} more accounts with gaps",
                          "date": "", "_ts": ""})

    # -- a registered book that has never been checked --
    for entity_key, years in registered.items():
        entity = canon(entity_key)
        for fy in years or []:
            year = normalize_year(fy)
            seen_years.setdefault(entity, set()).add(year)
            if (entity, year) not in checked:
                touch(entity, year).append({"kind": "unchecked", "text": "coverage not checked yet",
                                            "date": "", "_ts": ""})

    # -- all clear: only an entity's LATEST year, and only when nothing else is listed for it --
    for entity, years in seen_years.items():
        if not years:
            continue
        latest_year = max(years, key=lambda y: (bool(_YEAR_RX.match(y)), y))
        entry = checked.get((entity, latest_year))
        if entry is not None and not groups.get(entity, {}).get(latest_year):
            touch(entity, latest_year).append({"kind": "clear", "text": "all clear",
                                               "date": f"coverage checked {_fmt_ts(entry.get('checked'), False)}",
                                               "_ts": ""})

    out = []
    for entity in sorted(groups, key=lambda e: (e == "", e.casefold())):
        years = [{"year": y, "lines": lines} for y, lines in sorted(groups[entity].items(), reverse=True) if lines]
        if years:
            out.append({"entity": entity or NO_ENTITY, "years": years})
    return out


def build_recent(history: list, display_names: dict, n: int = 20) -> list[dict]:
    """The last n runs, newest first: [{"when", "entity", "skill", "result", "why"}]."""
    history = history if isinstance(history, list) else []
    display_names = display_names if isinstance(display_names, dict) else {}
    rows = []
    for rec in reversed([r for r in history if isinstance(r, dict)][-max(0, n):]):
        skill = _s(rec.get("skill"))
        rows.append({
            "when": _fmt_ts(rec.get("ts"), True),
            "entity": _s(rec.get("entity")) or "-",
            "skill": _s(display_names.get(skill) or skill),
            "result": _s(rec.get("result")),
        })
    return rows


# ----------------------------------------------------------------------------- rendering

_KIND_CLASS = {"problem": "rag-error", "gap": "rag-warn", "unchecked": "", "clear": "rag-ok"}
_KIND_MARK = {"problem": "!", "gap": "!", "unchecked": "?", "clear": "&#10003;"}


def render_needs_you(model: list[dict], note: str = "") -> str:
    """HTML for the NEEDS YOU panel. All stored text is escaped."""
    if not model:
        body = ("<div style='opacity:.75'>Nothing needs you. Runs and coverage checks you do will be "
                "listed here.</div>")
    else:
        parts = []
        for ent in model:
            parts.append(f"<div style='margin:.5rem 0 .1rem'><b>{_esc(ent['entity'])}</b></div>")
            for yr in ent["years"]:
                for i, line in enumerate(yr["lines"]):
                    cls = _KIND_CLASS.get(line["kind"], "")
                    year = _esc(yr["year"]) if i == 0 else ""
                    date = f"<span style='opacity:.7;white-space:nowrap'> {_esc(line['date'])}</span>" if line["date"] else ""
                    parts.append(
                        "<div style='display:flex;gap:.75rem;margin-left:1rem'>"
                        f"<span style='min-width:4.5rem;opacity:.8'>{year}</span>"
                        f"<span class='{cls}'>{_KIND_MARK.get(line['kind'], '')} {_esc(line['text'])}</span>{date}</div>")
        body = "".join(parts)
    if note:
        body += f"<div style='margin-top:.4rem;opacity:.8'>{_esc(note)}</div>"
    return f"<div class='pask-home-needs'>{body}</div>"


def render_recent(rows: list[dict]) -> str:
    if not rows:
        return "<div class='pask-home-recent' style='opacity:.75'>No runs recorded yet.</div>"
    out = []
    for r in rows:
        cls = {"ok": "rag-ok", "stopped": "rag-error", "failed": "rag-error", "attention": "rag-warn",
               "not_ready": "rag-warn", "interrupted": "rag-warn"}.get(r["result"], "")
        word = _RESULT_WORD.get(r["result"], r["result"])
        out.append(
            "<div style='display:flex;gap:.75rem'>"
            f"<span style='min-width:7.5rem;white-space:nowrap;opacity:.8'>{_esc(r['when'])}</span>"
            f"<span style='min-width:9rem'>{_esc(r['entity'])}</span>"
            f"<span style='flex:1'>{_esc(r['skill'])}</span>"
            f"<span class='{cls}'>{_esc(word)}</span></div>")
    return f"<div class='pask-home-recent'>{''.join(out)}</div>"


# ----------------------------------------------------------------------------- the AI line

def ai_line(cfg: dict | None = None, check=None) -> str:
    """Plain text: 'AI: <active endpoint> OK (n of m up)', or the failing state. Reads the shared
    health cache; if an endpoint was never probed it is probed once (the same probe the model
    dropdown fill makes, run after the window shows)."""
    check = check or _health.check_cached
    cfg = cfg if cfg is not None else _config.load_portable_config()
    endpoints = (cfg or {}).get("endpoints") or {}
    if not endpoints:
        return "AI: no endpoint set up"
    active = _s((cfg or {}).get("active_endpoint"))
    up, state = 0, {}
    for name, ep in endpoints.items():
        try:
            res = check(ep)
            ok, status = bool(res.ok), _s(res.status)
        except Exception:  # noqa: BLE001 -- one bad endpoint never breaks the line
            ok, status = False, "unreachable"
        state[name] = (ok, status)
        up += 1 if ok else 0
    ok, status = state.get(active, (False, "unreachable"))
    word = "OK" if status == "ok" else ("slow" if status == "slow" else "DOWN")
    label = active or "(none active)"
    return f"AI: {label} {word} ({up} of {len(endpoints)} up)"


def ai_placeholder() -> str:
    """Same height as the real line; nothing probed."""
    return "AI: checking..."


def render_ai(text: str) -> str:
    return f"<span class='pask-home-ai'>{_esc(text)}</span>"


# ----------------------------------------------------------------------------- loading

def load_needs_you(display_names: dict) -> tuple[list[dict], str]:
    """(model, note). Reads the run history, the coverage store and the registry; never raises."""
    note = ""
    try:
        history = _runhistory.tail(_runhistory.MAX_RECORDS)
    except Exception:  # noqa: BLE001
        history, note = [], "Run history could not be read."
    try:
        coverage = _coverage_last.load()
    except Exception:  # noqa: BLE001
        coverage, note = {}, (note + " Last coverage check could not be read.").strip()
    try:
        registered = _book_registry.registered_entity_years()
    except Exception:  # noqa: BLE001
        registered = {}
    try:
        return build_needs_you(history, coverage, registered, display_names), note
    except Exception:  # noqa: BLE001
        return [], "The list could not be built."


def load_recent(display_names: dict, n: int = 20) -> list[dict]:
    try:
        return build_recent(_runhistory.tail(n), display_names, n)
    except Exception:  # noqa: BLE001
        return []


def load_home(display_names: dict) -> tuple[str, str, str]:
    """(ai html, needs-you html, recent html) for the Home screen."""
    try:
        ai = render_ai(ai_line())
    except Exception:  # noqa: BLE001
        ai = render_ai("AI: unknown")
    model, note = load_needs_you(display_names)
    return ai, render_needs_you(model, note), render_recent(load_recent(display_names))


# ----------------------------------------------------------------------------- Check coverage now

_CHECK_LOCK = threading.Lock()


def run_check_now(check=None, paths_fn=None) -> str:
    """The one global [Check coverage now]: scan every registered book (read-only) and save the
    result as the last check. A second call while one is running does nothing. Returns a short
    status line; never raises."""
    if not _CHECK_LOCK.acquire(blocking=False):
        return "A coverage check is already running."
    try:
        check = check or _coverage_last.check_now
        paths_fn = paths_fn or _book_registry.registered_book_paths
        paths = sorted(paths_fn())
        if not paths:
            return "No books are registered yet (Entities tab)."
        scan = check(paths, _book_registry._default_entities_path())
        return f"Checked {getattr(scan, 'books_scanned', len(paths))} book(s)."
    except Exception as exc:  # noqa: BLE001
        return f"The coverage check stopped: {type(exc).__name__}."
    finally:
        _CHECK_LOCK.release()
