# PA Skills Portable — Next Session Opening Prompt

> Written 2026-06-03. Phases 4A through 4D are complete.

---

## Current state

Project: `C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable`

**What's landed (committed):**
- Phase 4A — pluggable skill architecture (registry, generic tabs, run_direct)
- Phase 4B — all 5 financial skills wired, multi-file upload, frozen-mode fixes
- Phase 4D — Settings tab (`ui/tabs/settings.py`), select input type in registry + generic tabs
- Quick wins — CI Python 3.13, `--clean` flag, CHANGELOG catch-up

**What's in the working tree (uncommitted):**
- Phase 4C skills: `skill_summarize/`, `skill_translate/` (AGENT.md + agent.py), `skill_csv_analyzer/`
- Phase 4C tests: `tests/test_phase4c_skills.py`, `tests/fixtures/`
- Phase 4C plan: `2026-05-28-PHASE-4C-PLAN.md`
- Phase 4D streaming: `base_agent.py` (_StreamingAgentWrapper), `_runner.py` (run_with_streaming), `_generic.py` (mode-based dispatch), `tests/test_phase4d_streaming.py`
- Gap tracker updates, CHANGELOG additions
- Commit scripts: `commit_4c_skills.bat`, `test_4c_e2e.bat` + `src/test_4c_e2e_runner.py`

**Registry:** 8 skills — 5 financial + 3 general (summarize, translate, CSV Analyzer).

## Read these files first

1. `2026-05-27-GAP-TRACKER.md` — master list of open items
2. `2026-06-03-PHASE-5-LAUNCHER-GEN-PLAN.md` — stashed Phase 5 plan (CI Launcher Generator fix)

## Recommended task sequence

### 1. Commit + test the uncommitted work (immediate)

Two scripts are ready in the project root:

- `commit_4c_skills.bat` — 6 logical commits for Phase 4C (skills, tests, fixtures, gap tracker, plan). Double-click to run.
- `test_4c_e2e.bat` — Ollama-backed end-to-end tests for all 3 Phase 4C skills. Requires Ollama running. Double-click to run.

The Phase 4D streaming changes (`base_agent.py`, `_runner.py`, `_generic.py`, `test_phase4d_streaming.py`) also need a commit. Suggested message:
```
feat: agent progress streaming (Phase 4D, C4 resolved)
```

After both sets are committed, the working tree should be clean (minus vendor/ LFS changes from A2).

### 2. Phase 5 — Launcher Generator CI fix (next major)

Plan is fully drafted in `2026-06-03-PHASE-5-LAUNCHER-GEN-PLAN.md`.

**TL;DR:** Self-host the extracted PortableApps.com Launcher Generator in `bundling/launcher-gen/` so CI no longer depends on the portableapps.com CDN (which rejects TLS from GitHub runners). This makes every CI release produce a complete zip with `PASkillsPortable.exe`.

**Before executing:** Complete the inspection checklist in §2 of the plan (check latest CI run logs, measure extracted size, confirm GPL credit approach).

### 3. Open gap tracker items (pick and choose)

Remaining open items from the gap tracker, grouped by effort:

**Quick wins (< 1 hour each):**
- A2 — Commit or revert the ~117 modified vendor files (`git add vendor/` or `git checkout -- vendor/`)
- F1 — Expand README.md with setup instructions and architecture overview
- F3 — Consolidate build notes into a single contributor doc

**Medium (half-day each):**
- B7 — MSG/Email parser standalone skill (extract-msg dep already in requirements)
- C5 — Skill output history tab (scan `outputs/` directory)
- E4 — Frozen-build CI smoke test (launch exe, check port binding)

**Deferred / blocked:**
- A1 (code-sign) — blocked on certificate
- B2 (qpdf) — runtime check in place, document as prereq
- B6 (upstream repo publishing) — separate decision
- B8 (multi-bank cc_transactions) — needs real PDF test data
- D1 (Launcher Gen CI) — addressed by Phase 5
- D2 (auto-update) — post-Phase 5

## Key files for reference

| File | Purpose |
|---|---|
| `src/agents/registry.py` | Skill auto-discovery, SkillInput.options for select |
| `src/agents/base_agent.py` | build_agent + run_direct + _StreamingAgentWrapper |
| `ui/tabs/_generic.py` | Generic tab rendering, select dropdown, streaming dispatch |
| `ui/tabs/settings.py` | Settings tab — endpoint management |
| `ui/_runner.py` | run_with_progress (direct) + run_with_streaming (agent) |
| `ui/_config.py` | Config loading, legacy materialisation |
| `ui/webui.py` | Main Gradio app construction |
| `tests/test_phase4c_skills.py` | 60 unit tests for Phase 4C skills |
| `tests/test_phase4d_streaming.py` | 17 unit tests for streaming wrapper |
| `.github/workflows/release.yml` | CI workflow (Phase 5 target) |
| `bundling/build.py` | Build script (Phase 5 target) |

## CLAUDE.md reminders

Per the project's CLAUDE.md: plan first, wait for approval, then execute
with checkpoints. Don't delete/overwrite/rename existing files without
showing the diff first. Use YYYY-MM-DD naming for new files.
