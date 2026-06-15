# Pending Manual Steps — 2026-06-08

> Complete these five steps before starting Phase 5 work.
> Order matters: commits first (1, 2, 5), then test (3), then housekeeping (4).

---

## 1. Run `commit_4c_skills.bat`

**What:** Double-click `commit_4c_skills.bat` in the project root. It
stages and commits the Phase 4C deliverables in 6 logical commits:

1. `feat(skill_summarize)` — Document Summarizer (direct mode)
2. `feat(skill_translate)` — Text Translator agent.py + AGENT.md + updated skill.yaml (select inputs)
3. `feat(skill_csv_analyzer)` — CSV Data Analyzer (agent mode, parameterised tools, safety hardened)
4. `test:` — 60-test suite + synthetic fixtures
5. `docs:` — Gap tracker update (B5 resolved, E1/E2 partial)
6. `docs:` — Phase 4C plan file

**Current state:** All files are untracked (`??`) or modified (`M` for
skill_translate/skill.yaml). The script uses `git add` on explicit paths
— it will not touch vendor/, CHANGELOG, or the Phase 4D streaming files.

**Expected outcome:** 6 new commits on the current branch. Run
`git log --oneline -8` afterwards to confirm they landed cleanly after
the existing `16e0ee6` (gap tracker / next-session prompt) commit.

**If it fails:** Most likely cause is a stale `.git/index.lock` file.
Delete it (`del .git\index.lock`) and re-run. If a commit is empty
(files were already committed in another session), the script prints a
warning but continues — that's fine.

---

## 2. Commit Phase 4D streaming changes

**What:** The agent progress streaming work (C4) is in the working tree
but not yet committed. Run these commands from the project root:

```powershell
cd C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable

git add src/agents/base_agent.py ui/_runner.py ui/tabs/_generic.py tests/test_phase4d_streaming.py
git commit -m "feat: agent progress streaming (Phase 4D, C4 resolved)"
```

**What changed in each file:**

- `src/agents/base_agent.py` — added `_StreamingAgentWrapper`,
  `set_progress_queue()`, `get_progress_queue()`. The wrapper intercepts
  `.invoke()` and uses `.stream(stream_mode="updates")` internally,
  pushing tool_call / tool_result / llm_response events to a queue.
  Transparent to all existing skills — no skill code changes needed.
- `ui/_runner.py` — added `run_with_streaming()` generator that polls
  the progress queue and yields formatted markdown events to Gradio.
  Original `run_with_progress()` is unchanged (used for direct-mode).
- `ui/tabs/_generic.py` — dispatches agent-mode skills to
  `run_with_streaming()`, direct-mode skills to `run_with_progress()`.
- `tests/test_phase4d_streaming.py` — 17 unit tests for the streaming
  wrapper and event formatting.

**Also consider committing** (separate commit, optional):

```powershell
git add CHANGELOG.md
git commit -m "docs: CHANGELOG entries for Phases 4C and 4D"
```

---

## 3. Run `test_4c_e2e.bat` (requires Ollama)

**What:** Double-click `test_4c_e2e.bat` in the project root. It runs
`src/test_4c_e2e_runner.py` which tests all 3 Phase 4C skills against
a live Ollama endpoint.

**Prerequisites:**
- Ollama running on `localhost:11434`
- At least one model pulled (gemma4, qwen3, or llama3.1 preferred)
- The script auto-detects config.yaml from `staging/Data/settings/`

**What it tests:**

| Skill | Mode | Checks |
|---|---|---|
| Summarizer | direct | Non-empty output, "Summary" heading, structured sections (key points / detailed / conclusions) |
| Translator | direct | Non-empty output, output file written, translation differs from English input |
| CSV Analyzer | agent | Non-empty output, mentions "North" region, cites correct revenue figure (3945) |

**Expected outcome:** Results printed to console and saved to
`tests/e2e_results.txt`. The script exits 0 if all pass, 1 if any fail.

**If a test fails:**

- **Summarizer missing sections:** The LLM may not follow the exact
  heading format. The test is lenient (checks for keywords like "key
  point", "detail", "conclusion" case-insensitively). If it still fails,
  check the output preview — if the summary is reasonable but formatted
  differently, the AGENT.md prompt may need tuning for your model.
- **Translator echoes English:** Some smaller models struggle with
  Hindi. Try with a different target language (Spanish, French) by
  editing the `test_text` and `target_lang` in the script.
- **CSV Analyzer wrong numbers:** The expected North revenue is 3945.0
  (sum of 1050 + 1575 + 1320). If the model cites different numbers,
  it may have made a tool-calling error. Check the output preview for
  the pandas expressions it used. The parameterised tools (post security
  hardening) require the model to use `aggregate_csv` and `filter_csv`
  instead of raw expressions — confirm the model is calling the right
  tool names.
- **Connection refused:** Ollama isn't running. Start it with
  `ollama serve` in a separate terminal.

---

## 4. Decide: delete `2026-05-25-PHASE-3-RECON-NOTES.md` or keep as history

**Background:** A sibling Phase 3 dispatch session tried to delete this
file, but the `allow_cowork_file_delete` dialog came back denied
(possibly accidental). The file is the original Phase 3 reconnaissance
notes — useful as history, harmless if kept, no downstream dependency.

**If keep:** No action needed.

**If delete:**

```powershell
cd C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable
rm 2026-05-25-PHASE-3-RECON-NOTES.md
git add -A 2026-05-25-PHASE-3-RECON-NOTES.md
git commit -m "chore: remove Phase 3 recon notes (superseded)"
```

---

## 5. Commit the stashed Phase 5 plan

**What:** `2026-06-03-PHASE-5-LAUNCHER-GEN-PLAN.md` was written but
never committed — the git `index.lock` was held by the 4C session at
the time.

**Option A — standalone commit now:**

```powershell
cd C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable
git add 2026-06-03-PHASE-5-LAUNCHER-GEN-PLAN.md
git commit -m "docs: Phase 5 Launcher Generator CI plan"
```

**Option B — fold into Phase 5:** If the new session modifies this plan
file, just include it in that session's first commit instead.

---

## After all steps

The working tree should be clean except for:
- `vendor/` — ~117 modified files (LFS smudge issue, gap tracker A2).
  Resolve separately with `git add vendor/` or `git checkout -- vendor/`.
- Scratch files: `commit_4c_skills.bat`, `test_4c_e2e.bat`,
  `src/test_4c_e2e_runner.py`, various dated prompt/plan `.md` files.
  These can be committed as docs or `.gitignore`'d at your discretion.

You're then ready for Phase 5 (Launcher Generator CI fix). Plan is at
`2026-06-03-PHASE-5-LAUNCHER-GEN-PLAN.md`.
