# PA Skills Portable — Gap Tracker

> Compiled 2026-05-27 from Phase 1/2a/2b/3 build notes, recon notes, memory
> files, and a full codebase audit. This document tracks every known outstanding
> item across the project so Phase 4 work can proceed with full visibility.
>
> **Current state:** v0.3.1 (tag `b156cdc`), 119 uncommitted vendor changes on
> `main`. Five skills in `src/agents/`, three wired into the UI.

---

## A. Phase 3 remnants (incomplete items carried forward)

### A1. Code-signing — BLOCKED

Executables `pa_skills.exe` and `PASkillsPortable.exe` are unsigned.
PortableApps.com recommends but doesn't require signing. Blocked on sourcing
a code-signing certificate. No code changes needed — just the certificate
and a signing step in `build.py` + CI.

**Status:** Parked until certificate is acquired.

### A2. 117 uncommitted vendor changes on main

The Phase 3 recon session (2026-05-25) found ~117 modified files in
`vendor/` — binary sizes jumped from LFS pointer stubs (131 bytes) to real
binaries (100s of KB). This is either an LFS smudge or a `refresh_binaries.py`
run that was never committed. These changes need to be committed or reverted
before any new work lands on `main`.

**Action needed:** Harshal to run `git add vendor/ && git commit` or
`git checkout -- vendor/` on Windows.

### A3. build.py discrepancies vs Phase 3 starter-prompt decisions

The recon notes (2026-05-25-PHASE-3-RECON-NOTES.md §3) flagged three
mismatches between the user's stated decisions and what `c1ff29f` shipped:

| Decision | What was requested | What shipped |
|---|---|---|
| Cache path | `build_pyinstaller/agents_src/` (flat) | `build_pyinstaller/.agents_cache/<sha256>[:12]/` |
| `--clean` flag | Should exist to nuke cache | Not present |
| Upstream unreachable, no cache | Fall back to `src/agents/` if present | Hard-fail |

**Action needed:** Decide whether to reconcile (refactor ~100 lines in
`build.py:428-535`) or accept the shipped behaviour as good enough.

---

## B. Skills library gaps

### B1. cc_sort and cc_transactions have no UI tabs

Both skills exist in `src/agents/` with full AGENT.md, agent.py, tools.py,
and scripts. But `ui/tabs/` has no `skill_cc_sort.py` or
`skill_cc_transactions.py`, and `ui/webui.py` doesn't import them.

**Note:** cc_sort depends on `qpdf` (external binary, not vendored) and
`extract-msg` (in requirements.txt). cc_transactions depends on cc_sort's
output folder structure as input.

### B2. cc_sort requires qpdf — not vendored

The cc_sort skill calls `check_qpdf_available()` and hard-stops if qpdf
isn't on PATH. Unlike Tesseract/Poppler, qpdf is not in `vendor/` or
`bundling/binaries.toml`. Either vendor it or document it as a user prereq.

### B3. Skills are not auto-discoverable

Tab registration in `webui.py` is hard-coded:

```python
from ui.tabs import skill_26as as tab_26as
from ui.tabs import skill_bob as tab_bob
from ui.tabs import skill_hsbc as tab_hsbc
```

Adding a new skill requires writing a hand-coded tab file AND editing
`webui.py`. No manifest/registry pattern exists.

### B4. No lightweight (non-agent) execution path

Every skill goes through the full LangGraph ReAct agent loop
(`create_react_agent` in `base_agent.py`). Simple prompt-in/text-out tasks
(summarisation, translation) don't need tool-calling overhead. A "direct"
mode (prompt → LLM → response) would make new skill types practical.

### B5. All existing skills are financial/document-specific

The five skills (26as, bob, cc_sort, cc_transactions, hsbc) are all Indian
financial document processors. The project goal is "skills that can work on
any LLM including local ones" — broader skill types (summarisation,
translation, data analysis) are needed to demonstrate that promise.

### B6. Upstream repo has no git history

`platform-agnostic-skills/` (the sibling repo) has no `.git` directory —
`sources.toml` points to it via `kind = "local"` with empty `git_url`/
`git_ref`. The build-time pull works for local dev, but CI uses
`--skip-pull` because there's no remote to clone from. Publishing the
upstream repo would unblock true CI agent pulls.

---

## C. UI / UX gaps

### C1. Home tab text is stale

Home tab still reads:

> "Three skills ship with this build; **Phase 1 wires the 26AS skill only**.
> BoB and HSBC become available in Phase 2."

Both BoB and HSBC have been wired since Phase 2a. The text also references
"three skills" but there are five in `src/agents/`.

### C2. No Settings tab

`config.yaml` editing requires opening the file manually. There's no UI for
switching active endpoints, adding new endpoints, or changing models. The
`_config.py` adapter has full read/write support — it just lacks a Gradio
front-end.

### C3. No dynamic skill listing on Home

Home tab's "Quick links" section is hand-written markdown. Once skills are
auto-discoverable (B3), Home should dynamically list all available skills
with their descriptions and status.

### C4. No progress/streaming during agent runs

`_runner.py` shows elapsed-time ticks while the agent runs, but there's no
streaming of intermediate agent thoughts, tool calls, or step-by-step logs
visible in the UI. The agent's stdout goes to the console (or devnull in
frozen mode).

---

## D. Infrastructure / build gaps

### D1. Launcher Generator can't be downloaded in CI

PortableApps.com CDN (`download.portableapps.com`) rejects TLS handshakes
from Azure-hosted GitHub runner IPs. Both `curl.exe` and PowerShell
`Invoke-WebRequest` with TLS 1.2 fail. The CI workflow gracefully falls
back to `--skip-launcher` and produces a zip without `PASkillsPortable.exe`.

**Options:** (a) self-host the `.paf.exe` in the repo via LFS, (b) find a
mirror, (c) accept the fallback permanently.

### D2. No auto-update / version-check mechanism

Users have no way to know a new version is available. PortableApps.com has
an update-checker protocol (`update.ini` + a URL endpoint), but no
`update.ini` has been created.

### D3. CI uses Python 3.10, pyproject.toml targets 3.13

`release.yml` sets up Python 3.10, but `pyproject.toml` declares
`requires-python = ">=3.13"`. This mismatch hasn't caused issues yet
(PyInstaller doesn't enforce the target's Python version at build time)
but could bite if a dependency drops 3.10 support or if type hints use
3.13-only syntax.

### D4. No `--clean` flag on build.py

The agents cache at `build_pyinstaller/.agents_cache/` can only be cleaned
manually. A `--clean` flag was requested but never implemented (see A3).

---

## E. Testing gaps

### E1. Tests are smoke-only

`tests/test_smoke.py` has 8 tests: import checks, buildinfo shape,
native resolver, and Gradio app construction. No tests exercise:

- Actual skill execution (even with mocked LLM responses)
- The `_config.py` adapter's legacy materialisation
- The `_runner.py` background-thread executor
- The `_health.py` endpoint checker
- Build pipeline steps

### E2. No end-to-end skill tests

No test sends a real (or mocked) PDF through a skill and verifies the
output Excel/CSV. This means regressions in the extraction scripts
(`scripts/*.py`) are only caught by manual smoke testing.

### E3. cc_sort / cc_transactions never tested in the portable build

These skills have never been wired into the UI or smoke-tested in the
frozen PyInstaller build. Unknown whether their subprocess calls,
`extract-msg` imports, or qpdf dependency work correctly in frozen mode.

---

## F. Documentation / repo hygiene

### F1. README.md is minimal

Current README is a brief description with no setup instructions, no
architecture overview, no contribution guide, and no screenshots.

### F2. CHANGELOG.md stopped at Phase 1

`CHANGELOG.md` was created during Phase 1 and hasn't been updated for
Phases 2a, 2b, or 3.

### F3. Build notes are session-specific, not consolidated

Four separate date-stamped notes files exist in the repo root:
`2026-05-20-BUILD-NOTES.md`, `2026-05-22-PHASE-2A-NOTES.md`,
`2026-05-24-PHASE-2B-NOTES.md`, `2026-05-25-PHASE-3-RECON-NOTES.md`.
These are useful as historical records but there's no single consolidated
"how to build" document for a new contributor.

---

## G. Phase 4 plan mapping

How the planned Phase 4 work addresses these gaps:

| Phase 4 step | Gaps addressed |
|---|---|
| **4A — Pluggable skill architecture** | B3, B4, C3 |
| **4B — Wire remaining skills + cleanup** | B1, B2, C1, E3 |
| **4C — New skill types** | B5 |
| **4D — UI improvements** (if scoped in) | C2, C4 |

| Gap | NOT addressed by Phase 4 ABCD |
|---|---|
| A1 (code-sign) | Blocked externally |
| A2 (uncommitted vendor) | Needs user action before Phase 4 starts |
| A3 (build.py cache discrepancies) | Low priority, can be folded into 4A |
| B6 (upstream repo publishing) | Separate decision |
| D1 (Launcher Gen CI) | Deferred |
| D2 (auto-update) | Deferred |
| D3 (Python version mismatch) | Quick fix, can land anytime |
| D4 (--clean flag) | Can fold into 4A |
| E1/E2 (testing) | Should add as Phase 4 deliverable |
| F1/F2/F3 (docs) | Lower priority, post-4C |
