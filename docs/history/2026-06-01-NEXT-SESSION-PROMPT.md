# PA Skills Portable — Next Session Opening Prompt

> Use this file to start a new Cowork session. Paste its contents or
> reference the file path.

---

## Context

Project: `C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable`

Phase 4C just landed (2026-06-01). The project now has 8 skills — 5
financial and 3 general-purpose — all running through a pluggable
architecture (registry + generic tabs + YAML manifests). 60 unit tests
in `tests/test_phase4c_skills.py`. Gap tracker is current.

## Read these files first

1. `2026-05-27-GAP-TRACKER.md` — master list of all open items with status
2. `2026-05-28-PHASE-4C-PLAN.md` — just-completed phase (for context on "What comes after 4C")

## Recommended open items (in priority order)

### 1. Phase 4D — Settings Tab + Progress Streaming (highest impact)

**Why:** Two biggest UX pain points right now. Users must hand-edit
`config.yaml` to switch LLM endpoints/models, and agent runs show no
intermediate progress (just elapsed-time ticks). Both are gap tracker
items C2 and C4.

**Scope:**
- Settings tab in Gradio: list endpoints, switch active, add/edit/delete,
  test connection, change default model. `_config.py` already has full
  read/write support — just needs a Gradio front-end.
- Agent progress streaming: surface LangGraph intermediate messages
  (tool calls, thoughts) in the result markdown area during runs.
  `_runner.py` already has the yield-from pattern — extend it to stream
  agent steps.

### 2. Add `type: "select"` input to generic tabs (quick win)

**Why:** The translator skill had to use a text field instead of a
dropdown for language selection because `_generic.py` doesn't support
`type: "select"` yet. Adding it is ~20 lines in `_generic.py` and
unlocks better UX for any future skill that needs predefined choices.

**Scope:** Add to `_generic.py` render function, update `SkillInput`
dataclass in `registry.py` to accept `options: [...]`, update translator
`skill.yaml` to use it.

### 3. Quick infrastructure wins (low effort, high hygiene)

- **D3 — CI Python version:** Change `release.yml` from 3.10 to 3.13
  to match `pyproject.toml`. One-line fix.
- **D4 — `--clean` flag on build.py:** Add a flag to nuke the agents
  cache directory. ~10 lines.
- **F2 — CHANGELOG catch-up:** Update CHANGELOG.md for Phases 2a, 2b,
  3, 4A, 4B, 4C.

### 4. End-to-end LLM testing on local machine

**Why:** The 60 sandbox tests cover everything except the actual LLM
round-trip. These need Ollama running locally.

**Manual test plan:**
- Summarizer: upload a PDF, confirm `.md` output has Key Points /
  Detailed Summary / Conclusions sections
- Translator: paste text, set source=English target=Hindi, confirm
  translated `.txt` output
- CSV Analyzer: upload `tests/fixtures/sales.csv`, ask "What is the
  total revenue by region?", confirm `.md` output cites correct numbers
  (North=3945, South=3790, East=3750, West=2227.5)

### 5. Deferred / lower priority

- A1 (code-sign) — still blocked on certificate
- A2 (117 uncommitted vendor files) — needs `git add vendor/` or
  `git checkout -- vendor/` on Windows
- B2 (qpdf not vendored) — runtime check in place, document as prereq
- B6 (upstream repo publishing) — separate decision
- D1 (Launcher Generator CI) — TLS failure on Azure runners, fallback works
- D2 (auto-update mechanism) — post-4D
- F1/F3 (README + consolidated build docs) — post-4D

---

## Key files for reference

| File | Purpose |
|---|---|
| `src/agents/registry.py` | Skill auto-discovery, SkillInfo dataclass |
| `src/agents/base_agent.py` | `build_agent()` + `run_direct()` |
| `ui/tabs/_generic.py` | Generic tab rendering + run handler |
| `ui/_runner.py` | Background-thread executor with progress ticks |
| `ui/_config.py` | Config loading, legacy materialisation |
| `ui/_health.py` | LLM endpoint health check |
| `ui/webui.py` | Main Gradio app construction |
| `tests/test_phase4c_skills.py` | 60 unit tests for Phase 4C skills |

## CLAUDE.md reminders

Per the project's CLAUDE.md: plan first, wait for approval, then execute
with checkpoints. Don't delete/overwrite/rename existing files without
showing the diff first. Use YYYY-MM-DD naming for new files.
