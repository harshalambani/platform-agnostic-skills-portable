# PA Skills Portable — Next Session Opening Prompt

> Use this file to start a new Cowork session. Paste its contents or
> reference the file path.

---

## Context

Project: `C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable`

Session 2026-06-02 delivered:
- `type: "select"` input for generic tabs (translator has language dropdowns)
- CI Python 3.13 (D3), `--clean` flag (D4), CHANGELOG catch-up (F2) — all resolved
- Gap tracker expanded with 4 new gaps (B7, B8, C5, E4)
- **Settings tab** (`ui/tabs/settings.py`) — endpoint management UI (C2 resolved)
- Memory consolidated (3 phase-status files → 1)

The project now has 8 skills, a Settings tab, 60 unit tests, and a fully
pluggable architecture. Gap tracker is current at `2026-05-27-GAP-TRACKER.md`.

## Read these files first

1. `2026-05-27-GAP-TRACKER.md` — master list of all open items with status
2. `ui/tabs/settings.py` — just-completed Settings tab (for context)

## Recommended open items (in priority order)

### 1. Phase 4D — Agent Progress Streaming (C4, highest remaining impact)

**Why:** Agent runs show no intermediate progress (just elapsed-time
ticks). Users can't tell what the agent is doing or whether it's stuck.

**Scope:**
- Surface LangGraph intermediate messages (tool calls, thoughts) in the
  result markdown area during runs.
- `_runner.py` already has the yield-from pattern — extend it to stream
  agent steps.
- Options: (a) LangGraph callback handler that yields intermediate events,
  (b) stdout capture from the agent process, (c) Gradio Chatbot component
  for agent turns as chat bubbles.

**Key files:**
- `ui/_runner.py` — background-thread executor with progress ticks
- `ui/tabs/_generic.py` — run handler that yields (markdown, download_update)
- `src/agents/base_agent.py` — `build_agent()` + `run_direct()`

### 2. End-to-end LLM testing on local machine

**Why:** The 60 sandbox tests cover everything except the actual LLM
round-trip. These need Ollama running locally.

**Manual test plan:**
- Summarizer: upload a PDF, confirm `.md` output has Key Points /
  Detailed Summary / Conclusions sections
- Translator: paste text, select source=English target=Hindi, confirm
  translated `.txt` output
- CSV Analyzer: upload `tests/fixtures/sales.csv`, ask "What is the
  total revenue by region?", confirm `.md` output cites correct numbers
  (North=3945, South=3790, East=3750, West=2227.5)
- **Settings tab**: switch endpoints, test connection, add/delete endpoint

### 3. New gaps from session scan (lower priority)

- **B7** — MSG/Email parser skill (candidate, not yet scoped)
- **B8** — Multi-bank cc_transactions coverage not verified
- **C5** — Skill output history tab (list previous runs with re-download)
- **E4** — Frozen-build CI smoke test (run pa_skills.exe, verify startup)

### 4. Deferred / blocked

- A1 (code-sign) — blocked on certificate
- A2 (117 uncommitted vendor files) — needs `git add vendor/` or
  `git checkout -- vendor/` on Windows
- B2 (qpdf not vendored) — runtime check in place
- B6 (upstream repo publishing) — separate decision
- D1 (Launcher Generator CI) — TLS failure on Azure runners, fallback works
- D2 (auto-update mechanism) — post-4D
- F1/F3 (README + consolidated build docs) — post-4D

---

## Key files for reference

| File | Purpose |
|---|---|
| `src/agents/registry.py` | Skill auto-discovery, SkillInfo + SkillInput (with `options` for select) |
| `src/agents/base_agent.py` | `build_agent()` + `run_direct()` |
| `ui/tabs/_generic.py` | Generic tab rendering (file, files, select, directory, text) + run handler |
| `ui/tabs/settings.py` | Settings tab — endpoint management UI |
| `ui/_runner.py` | Background-thread executor with progress ticks |
| `ui/_config.py` | Config loading, legacy materialisation, read/write support |
| `ui/_health.py` | LLM endpoint health check |
| `ui/webui.py` | Main Gradio app construction |
| `tests/test_phase4c_skills.py` | 60 unit tests for Phase 4C skills |

## CLAUDE.md reminders

Per the project's CLAUDE.md: plan first, wait for approval, then execute
with checkpoints. Don't delete/overwrite/rename existing files without
showing the diff first. Use YYYY-MM-DD naming for new files.
