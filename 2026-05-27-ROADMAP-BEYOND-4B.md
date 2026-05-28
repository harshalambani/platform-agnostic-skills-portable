# PA Skills Portable — Roadmap beyond Phase 4B

> Written 2026-05-27. This document covers everything planned after Phase 4B,
> drawn from the gap tracker (`2026-05-27-GAP-TRACKER.md`) and the original
> Phase 4 discussion. Items are grouped into phases with priority and
> dependency notes.
>
> Referenced from: `2026-05-27-PHASE-4B-PLAN.md`

---

## Phase 4C — New skill types

**Goal:** Demonstrate the "any LLM, any task" promise by adding skills
beyond Indian financial documents. These use the pluggable architecture
from 4A — just drop a folder with `skill.yaml` + `agent.py` into
`src/agents/`.

**Addresses:** Gap B5 (all skills are financial/document-specific)

### Candidate skills (pick 2–3)

1. **Document Summarizer**
   - Input: any PDF or text file
   - Mode: `direct` (no tools needed — first real use of `run_direct()`)
   - Output: markdown summary
   - Why: simplest possible skill, great test of the `direct` execution path

2. **Text Translator**
   - Input: text + source/target language dropdowns
   - Mode: `direct`
   - Output: translated text
   - Why: shows local LLMs (Ollama) can do this without cloud APIs

3. **CSV/Data Analyzer**
   - Input: CSV file + natural-language question
   - Mode: `agent` (needs pandas tool-calling)
   - Output: text answer + optional chart
   - Why: demonstrates tool-calling with a non-PDF, non-financial use case

4. **Email/MSG Parser**
   - Input: .msg or .eml file
   - Mode: `direct` with a preprocessing script
   - Output: structured fields (sender, date, subject, body, attachments)
   - Why: leverages existing `extract-msg` dependency

### Effort estimate
- Each `direct`-mode skill: ~1 session (skill.yaml + agent.py + AGENT.md)
- Each `agent`-mode skill: ~2 sessions (+ tools.py + scripts/)

---

## Phase 4D — UI improvements

**Goal:** Make the Gradio interface more useful for daily operation.

### 4D-1. Settings tab (Gap C2)

Add a Gradio tab for editing `config.yaml` without opening the file manually.
The `_config.py` adapter already has `load_portable_config()` and
`write_portable_config()` — this is purely a UI build.

Features:
- View/switch active endpoint
- Add/edit/remove endpoints (provider, URL, model, temperature)
- Test endpoint connectivity (reuse `_health.check()`)
- Save changes (writes to `Data\settings\config.yaml`)

### 4D-2. Agent progress streaming (Gap C4)

Replace the elapsed-time ticks with real-time streaming of agent
thoughts and tool calls. Options:

- **a) LangGraph callback handler** — hook into the agent's event stream
  and yield intermediate messages to the Gradio output
- **b) stdout capture** — redirect agent stdout to a buffer and stream it
  to the UI (simpler but less structured)
- **c) Gradio Chatbot component** — replace the Markdown output with a
  Chatbot that shows agent turns as chat bubbles

### 4D-3. Skill output history

Add a tab or section that lists previous skill runs (timestamp, skill name,
input file, output file) with links to re-download outputs. Data source:
scan the `outputs/` directory.

---

## Phase 5 — Infrastructure hardening

### 5-1. Code-signing (Gap A1) — BLOCKED

Sign `pa_skills.exe` and `PASkillsPortable.exe`. Requires sourcing a
code-signing certificate. Options:

- Free: SignPath (open source projects), Let's Encrypt (experimental)
- Paid: Certum (~$30/yr), Sectigo, DigiCert
- Self-signed: works for personal use, triggers SmartScreen warning

### 5-2. Launcher Generator CI fix (Gap D1)

Options (pick one):

1. **Self-host via LFS** — commit the `.paf.exe` installer into the repo
   under `bundling/tools/` tracked by LFS. CI extracts it instead of
   downloading. ~5 MB addition to LFS.
2. **Accept fallback** — users get a zip without `PASkillsPortable.exe`,
   launch `pa_skills.exe` directly. Functionally identical.
3. **Mirror download** — host the installer on GitHub Releases of a
   separate utility repo.

### 5-3. Auto-update / version-check (Gap D2)

PortableApps.com protocol:
- Create `Other\Source\update.ini` with `[PA Skills Portable]`,
  `PackageVersion`, `DownloadURL`, `Hash`
- Host `update.ini` at a stable URL (GitHub Pages or raw GitHub)
- The PA platform checks this URL periodically

Alternative: simple version-check on Home tab startup — fetch
`https://api.github.com/repos/harshalambani/platform-agnostic-skills-portable/releases/latest`
and compare with `_buildinfo.VERSION`.

### 5-4. CI Python version fix (Gap D3)

One-line fix: change `python-version: "3.10"` to `python-version: "3.13"`
in `.github/workflows/release.yml`. Can land anytime.

### 5-5. build.py --clean flag (Gap D4 / A3)

Add `--clean` argument that nukes `build_pyinstaller/.agents_cache/` and
`build_pyinstaller/venv/` before building. Low effort.

---

## Phase 6 — Testing & documentation

### 6-1. Unit tests for core modules (Gap E1)

Add tests for:
- `_config.py` — legacy materialisation, endpoint resolution
- `_runner.py` — background-thread executor (mock the work function)
- `_health.py` — endpoint health check (mock HTTP responses)
- `registry.py` — malformed manifest handling, missing fields

### 6-2. End-to-end skill tests (Gap E2)

Create test PDFs (small, synthetic) and run each skill's extraction
script against them, verifying output structure. No LLM needed — test
the deterministic scripts directly, not the agent loop.

### 6-3. Frozen-build integration test (Gap E3)

CI step that runs the frozen `pa_skills.exe --no-browser` and verifies
it starts, binds a port, and responds to a health check. Catches
PyInstaller bundling regressions.

### 6-4. README overhaul (Gap F1)

Add: architecture diagram, setup instructions, skill authoring guide,
screenshot of the UI, contributing guide.

### 6-5. CHANGELOG update (Gap F2)

Retroactively fill in Phases 2a, 2b, 3, 4A, 4B entries.

### 6-6. Consolidated build guide (Gap F3)

Single `BUILDING.md` that replaces the four date-stamped notes files
as the canonical "how to build from source" document.

---

## Phase 7 — Publish & distribute

### 7-1. Publish upstream repo (Gap B6)

Push `platform-agnostic-skills` to GitHub so `sources.toml` can use
`kind = "git"` and CI can pull agents without `--skip-pull`.

### 7-2. PortableApps.com submission

Once code-signed and tested, submit to the PortableApps.com directory
for listing. Requires: signed exe, proper `appinfo.ini`, screenshots,
description, update URL.

### 7-3. First public release (v1.0.0)

Tag criteria:
- All 5 original skills working end-to-end in frozen build
- At least 1 non-financial skill (from 4C)
- Settings tab (from 4D)
- Code-signed (if certificate obtained)
- README + CHANGELOG current
- CI producing clean release zips

---

## Priority order

| Priority | Phase | Effort | Blocked by |
|---|---|---|---|
| 1 | **4B** — wire remaining skills | 1 session | — |
| 2 | **4C** — 2–3 new skills | 1–2 sessions | 4B |
| 3 | **4D** — Settings tab + streaming | 1–2 sessions | 4A (done) |
| 4 | **5-4** — CI Python fix | 5 minutes | — |
| 5 | **5-5** — build.py --clean | 30 minutes | — |
| 6 | **6-1/6-2** — unit + e2e tests | 1 session | 4B |
| 7 | **6-4/6-5/6-6** — docs | 1 session | 4C |
| 8 | **5-2** — Launcher Gen CI fix | 30 minutes | decision |
| 9 | **5-3** — auto-update | 1 session | 7-1 |
| 10 | **5-1** — code-sign | varies | certificate |
| 11 | **7-1/7-2/7-3** — publish | 1 session | 5-1 |

---

## Quick wins (can land in any session)

- [ ] 5-4: CI Python 3.10 → 3.13 (one-line change)
- [ ] 5-5: build.py `--clean` flag (~20 lines)
- [ ] 6-5: CHANGELOG catch-up (text only)
- [ ] 5-2: Self-host Launcher Generator via LFS (if choosing that option)
