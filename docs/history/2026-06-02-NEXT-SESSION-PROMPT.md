# PA Skills Portable — Next Session Opening Prompt

> Use this file to start a new Cowork session. Paste its contents or
> reference the file path.

---

## Context

Project: `C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable`

Session 2026-06-02 (second) delivered:
- **Agent progress streaming (C4)** — agent-mode skills now show live
  tool calls, tool results, and LLM reasoning steps in the result area
  instead of just elapsed-time ticks
- `_StreamingAgentWrapper` in `base_agent.py` — intercepts `.invoke()`
  → `.stream(stream_mode="updates")`, pushes events to a `queue.Queue`
- `run_with_streaming()` in `_runner.py` — polls the queue from the main
  thread, renders events as markdown
- 17 unit tests in `tests/test_phase4d_streaming.py`
- Zero changes to individual skill files
- Gap tracker updated: C4 resolved, Phase 4D → Done

The project now has 8 skills, Settings tab, 77 unit tests (60 + 17),
streaming agent progress, and a fully pluggable architecture.
Gap tracker is current at `2026-05-27-GAP-TRACKER.md`.

## Read these files first

1. `2026-05-27-GAP-TRACKER.md` — master list of all open items with status
2. `src/agents/base_agent.py` — streaming wrapper lives here
3. `ui/_runner.py` — both runners (elapsed-tick + streaming)

## Recommended open items (in priority order)

### 1. End-to-end LLM testing on local machine (highest priority)

**Why:** The 77 sandbox tests cover everything except the actual LLM
round-trip. The new streaming code especially needs real-world validation.

**Manual test plan:**
- **CSV Analyzer** (agent-mode, best streaming test): upload
  `tests/fixtures/sales.csv`, ask "What is the total revenue by region?",
  confirm live tool call/result steps appear in the result area, and
  `.md` output cites correct numbers (North=3945, South=3790, East=3750,
  West=2227.5)
- **Summarizer** (direct-mode): upload a PDF, confirm `.md` output has
  Key Points / Detailed Summary / Conclusions sections
- **Translator** (direct-mode): paste text, select source=English
  target=Hindi, confirm translated `.txt` output
- **Settings tab**: switch endpoints, test connection, add/delete endpoint

### 2. Streaming edge cases to verify

- What happens if the LLM errors mid-stream (e.g. Ollama OOM)?
- Does the final return value from `_StreamingAgentWrapper.invoke()`
  match what the skill expects? (`result["messages"][-1].content`)
  — the `get_state()` path vs the fallback path both need testing.
- Multi-step agent runs (3+ tool calls) — do step numbers render correctly?

### 3. New gaps (lower priority)

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
- F1/F3 (README + consolidated build docs) — lower priority

---

## Key files for reference

| File | Purpose |
|---|---|
| `src/agents/base_agent.py` | `build_agent()`, `run_direct()`, `_StreamingAgentWrapper`, progress queue |
| `src/agents/registry.py` | Skill auto-discovery, SkillInfo + SkillInput |
| `ui/tabs/_generic.py` | Generic tab rendering + run handler (streaming for agent, ticks for direct) |
| `ui/tabs/settings.py` | Settings tab — endpoint management UI |
| `ui/_runner.py` | `run_with_progress()` (elapsed ticks) + `run_with_streaming()` (live events) |
| `ui/_config.py` | Config loading, legacy materialisation, read/write support |
| `ui/_health.py` | LLM endpoint health check |
| `ui/webui.py` | Main Gradio app construction |
| `tests/test_phase4c_skills.py` | 60 unit tests for Phase 4C skills |
| `tests/test_phase4d_streaming.py` | 17 unit tests for streaming infrastructure |

## CLAUDE.md reminders

Per the project's CLAUDE.md: plan first, wait for approval, then execute
with checkpoints. Don't delete/overwrite/rename existing files without
showing the diff first. Use YYYY-MM-DD naming for new files.
