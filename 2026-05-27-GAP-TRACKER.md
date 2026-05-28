# PA Skills Portable — Gap Tracker

> Compiled 2026-05-27 from Phase 1/2a/2b/3 build notes, recon notes, memory
> files, and a full codebase audit. This document tracks every known outstanding
> item across the project so Phase 4 work can proceed with full visibility.
>
> **Current state:** Post Phase 4B. Five skills in `src/agents/`, all five
> wired into the UI via the pluggable architecture (registry + generic tabs).
> Multi-file upload support added. Frozen-mode subprocess fixes applied.

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

### B1. ~~cc_sort and cc_transactions have no UI tabs~~ — RESOLVED (Phase 4A+4B)

Both skills now render via the generic tab system (`ui/tabs/_generic.py`)
driven by `skill.yaml` manifests. cc_sort shows folder + password inputs
with qpdf dependency checking. cc_transactions shows folder input.
Frozen-mode subprocess calls replaced with `runpy.run_path()` in agent.py.
`check_extract_msg_available` fixed to use direct import (was broken in
frozen mode due to `sys.executable -c` pattern).

### B2. cc_sort requires qpdf — not vendored

The cc_sort skill calls `check_qpdf_available()` and hard-stops if qpdf
isn't on PATH. Unlike Tesseract/Poppler, qpdf is not in `vendor/` or
`bundling/binaries.toml`. Either vendor it or document it as a user prereq.

### B3. ~~Skills are not auto-discoverable~~ — RESOLVED (Phase 4A)

`agents/registry.py` scans `agents/*/skill.yaml` at startup and exposes
`SkillInfo` objects. `ui/webui.py` calls `registry.discover()` and builds
tabs dynamically via `ui/tabs/_generic.py`. Adding a new skill requires
only a `skill.yaml` manifest — no code changes to webui.py.

### B4. ~~No lightweight (non-agent) execution path~~ — RESOLVED (Phase 4A)

`base_agent.py` now has `run_direct()` for simple prompt → LLM → response
skills. Skills declare `mode: "direct"` in `skill.yaml` to use it.
The generic tab runner dispatches based on mode.

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

### C1. ~~Home tab text is stale~~ — RESOLVED (Phase 4A)

Home tab now dynamically lists all discovered skills from the registry
instead of hard-coded text.

### C2. No Settings tab

`config.yaml` editing requires opening the file manually. There's no UI for
switching active endpoints, adding new endpoints, or changing models. The
`_config.py` adapter has full read/write support — it just lacks a Gradio
front-end.

### C3. ~~No dynamic skill listing on Home~~ — RESOLVED (Phase 4A)

Home tab now uses `registry.discover()` to dynamically list all available
skills with their descriptions. No more hand-written quick-links.

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

### E3. cc_sort / cc_transactions never tested in the portable build — PARTIALLY RESOLVED (Phase 4B)

Both skills are now wired into the UI via generic tabs. Subprocess calls
in agent.py replaced with `runpy.run_path()` for frozen mode.
`check_extract_msg_available` fixed (was using broken `sys.executable -c`
pattern). **Still needed:** actual end-to-end frozen-build smoke test with
real PDFs. Source-mode UI rendering verified.

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

| Phase 4 step | Gaps addressed | Status |
|---|---|---|
| **4A — Pluggable skill architecture** | B3, B4, C1, C3 | **Done** |
| **4B — Wire remaining skills + cleanup** | B1, E3 (partial) | **Done** |
| **4C — New skill types** | B5 | Planned |
| **4D — UI improvements** (if scoped in) | C2, C4 | Planned |

**Also delivered in 4B (beyond original plan):**
- Multi-file upload input type (`type: "files"` in skill.yaml) — BoB now accepts multiple PDFs
- HSBC switched to directory input (matches agent API)
- Frozen-mode `runpy` bypass for cc_sort + cc_transactions agent scripts
- Fixed `check_extract_msg_available` broken in frozen mode

| Gap | NOT addressed by Phase 4 ABCD |
|---|---|
| A1 (code-sign) | Blocked externally |
| A2 (uncommitted vendor) | Needs user action |
| A3 (build.py cache discrepancies) | Low priority |
| B2 (qpdf not vendored) | User prereq — runtime check in place |
| B6 (upstream repo publishing) | Separate decision |
| D1 (Launcher Gen CI) | Deferred |
| D2 (auto-update) | Deferred |
| D3 (Python version mismatch) | Quick fix, can land anytime |
| D4 (--clean flag) | Low priority |
| E1/E2 (testing) | Should add as Phase 4 deliverable |
| F1/F2/F3 (docs) | Lower priority, post-4C |
