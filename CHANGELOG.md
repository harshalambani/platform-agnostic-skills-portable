# Changelog

All notable changes to this project are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- **Help system, single-source-of-truth.** Every skill's user help now lives in
  a `help:` block in its `skill.yaml` (overview, when-to-use, per-input
  tooltips/formats/gotchas, steps, per-output-file interpretation, tips,
  troubleshooting). One generator, `scripts/gen_docs.py`, renders it to per-skill
  guides in `docs/user-guide/`, a bundled standalone `docs/USER-GUIDE.html`, and
  the developer `docs/dev/skills-reference.md`.
- **In-app help.** A collapsible "How to use — formats & output" panel on every
  skill tab, a central **Help** tab, and two-tier tooltips (native `info=` helper
  text on inputs; `title=` hover on each output file). All read the `help:` block
  live via `agents.registry` (new `SkillHelp` model) — see `ui/_help.py`.
- **Docs.** `docs/dev/help-block-schema.md` and `docs/dev/editing-help.md`;
  `USER-GUIDE.html` bundled into the frozen package via `paskills.spec`.
- **CI.** `tests/test_help_coverage.py` fails if any UI skill lacks help or if
  the generated docs are stale (`gen_docs.py --check`).

### Fixed
- **HDFC — Value Dt now used on every input path.** HDFC statements carry
  both a posting Date and a Value Dt; the canonical CSV's "Date" column
  (which flows unchanged through balance checks, dedup, and account mapping)
  now emits the Value Dt on the PDF text path (`skill_hdfc`) and the PDF OCR
  path, matching the XLS/XLSX path which already preferred it. Falls back to
  the posting date only when Value Dt is blank. Note: opening-balance
  reconciliation and duplicate detection key on this field, so rows where
  posting and value dates differ (e.g. cheque clearing) may now be bucketed
  by a different date than before.
- **ICICI — docstring corrected.** The module docstring wrongly claimed
  ICICI used Transaction Date; the code already preferred Value Date
  (falling back to Transaction Date only when blank). No behavior change —
  documentation and a regression test now match the existing code.
- **Intercompany skills moved out of GnuCash > Banks.** "Intercompany Reco"
  and "Intercompany Matrix" are not bank-statement tools and were rendering
  alongside statement-import skills under the Banks sub-tab. Both now use a
  dedicated `category: "intercompany"` and render under a new
  GnuCash > Intercompany sub-tab (Reco first, Matrix second). Banks now shows
  only statement import + Review Mappings.

## [1.0.1] — 2026-06-25

### Fixed
- **README skills table** — was stale at 9 skills from the 1.0.0 release;
  now lists all 16 user-facing skills (KRChoksey ledger/import/reconcile,
  HDFC, GnuCash Import pipeline, 26AS Journal, ICICI added since), grouped
  to match the UI, with accurate mode and LLM-requirement columns.

### Verified
- Frozen-build smoke test against current `main` (sha `9158ed5`): rebuilt
  `pa_skills.exe` from source, launched it, confirmed HTTP 200 on the
  Gradio root and all 16 skill tabs present in the served UI tree. No
  regressions found.

## [1.0.0] — 2026-06-04

### Added
- **MSG / Email Parser skill** — direct-mode skill that parses `.msg`
  (Outlook) and `.eml` files into structured JSON (sender, date, subject,
  body, attachment list). Uses `extract-msg` for `.msg` and stdlib `email`
  for `.eml`. No LLM required. (B7 resolved)
- **Auto-update checker** — Home tab checks the GitHub releases API on
  startup and shows a banner when a newer version is available. Background
  thread, cached for the process lifetime. (D2 resolved)
- **Frozen-build CI smoke test** — new step in `release.yml`: launches
  `pa_skills.exe`, waits for port file, GETs the root URL, verifies
  HTTP 200, then kills the process. Catches PyInstaller bundling
  regressions. (E4 resolved)
- **qpdf vendored** — added to `binaries.toml`, `refresh_binaries.py`,
  `build.py` (step 6b), and `_native.py` resolver. CC Sort no longer
  requires qpdf to be installed by the user. (B2 resolved)
- Unit tests for `_config.py` (~15 tests), `_runner.py` (~12 tests),
  `_health.py` (~13 tests), MSG parser (~15 tests), update checker
  (~12 tests). Total test count ~175+. (E1 substantially resolved)
- `README.md` overhauled — architecture overview, dev + user setup,
  skill authoring guide, CI badge. (F1 resolved)
- `BUILDING.md` — consolidated build guide replacing 4 date-stamped
  notes files. (F3 resolved)

- **Dependency management infrastructure:**
  - `requirements-lock.txt` — exact pinned versions for reproducible
    frozen builds. `build.py` prefers the lock file when present.
  - Dependabot config (`.github/dependabot.yml`) — weekly PRs for pip
    and GitHub Actions dependency updates.
  - Native binary update checker (`.github/workflows/check-native-binaries.yml`)
    — weekly scheduled job checks Tesseract, Poppler, qpdf releases via
    GitHub API and opens an issue when updates are available.
  - Compatibility check (`.github/workflows/compat-check.yml`) — weekly
    scheduled job installs latest-compatible deps from loose pins, runs
    full test suite, and attempts frozen build + smoke test.

### Changed
- **Upstream repo published** — `platform-agnostic-skills` pushed to
  GitHub. `sources.toml` switched from `kind = "local"` to `kind = "git"`
  with public URL. CI no longer uses `--skip-pull`. (B6 resolved)
- Historical plan/notes/prompt files moved to `docs/history/`.
- Skill count: 8 → 9 (MSG Parser added).
- `_native.py` now resolves Tesseract, Poppler, and qpdf.
- `.gitattributes` comment updated for qpdf.

## [0.4.1] — 2026-06-03

### Fixed
- **Self-hosted Launcher Generator (D1)** — bundled PortableApps.com
  Launcher Generator v2.2.4 under `bundling/launcher-gen/2.2.4/` so CI
  no longer needs to download it from portableapps.com (TLS handshake
  was failing on GitHub-hosted runners). Every release zip now ships
  with `PASkillsPortable.exe`.
- Simplified `release.yml` — removed download step, `SKIP_LAUNCHER`
  env var, and fallback zip logic.
- Updated `build.py` `LAUNCHER_GEN_HINTS` to check bundled copy first.
- Smoke test `test_registry_discovers_all_skills` updated for 8 skills.
- `test_webui_constructs` skipped gracefully when gradio is not installed.
- `test_history_tab.py` mocks gradio safely (try real import first).

## [0.4.0] — 2026-06-03

### Added
- `type: "select"` input for generic skill tabs — renders a `gr.Dropdown`
  with predefined choices from `options:` in `skill.yaml`. Allows custom
  values typed by the user.
- `--clean` flag on `bundling/build.py` — deletes
  `build_pyinstaller/.agents_cache/` and exits.
- **Agent progress streaming (C4)** — agent-mode skills now show live
  intermediate steps (tool calls, tool results, LLM reasoning) in the
  result area instead of just elapsed-time ticks. Implemented via
  `_StreamingAgentWrapper` in `base_agent.py` and `run_with_streaming()`
  in `_runner.py`. Zero changes to individual skill files.
- **Skill output history tab (C5)** — scan outputs directory, sortable
  table with download and delete actions.
- **Document Summarizer** — direct-mode skill for PDF/text summarization.
- **Text Translator** — direct-mode skill with select dropdowns.
- **CSV Data Analyzer** — agent-mode skill with pandas tools and safety guards.
- 60-test suite for Phase 4C skills with synthetic fixtures.
- 30-test suite for history tab.
- 17 unit tests for streaming infrastructure.
- End-to-end test runner (`test_4c_e2e.bat`) for Ollama-backed validation.

### Changed
- CI Python version bumped from 3.10 to 3.13 to match `pyproject.toml`
  (`requires-python = ">=3.13"`).
- Translator skill now uses `type: "select"` dropdowns for source/target
  language instead of free-text fields.

---

## Phase 4C — 2026-05-28

### Added
- **Document Summarizer** skill (`skill_summarize`, `mode: "direct"`) —
  upload a PDF or text file, get a structured markdown summary with
  Key Points / Detailed Summary / Conclusions sections.
- **Text Translator** skill (`skill_translate`, `mode: "direct"`) — paste
  text, specify source and target languages, get a translated `.txt` file.
  Works with any chat-capable LLM including local models via Ollama.
- **CSV Data Analyzer** skill (`skill_csv_analyzer`, `mode: "agent"`) —
  upload a CSV file and ask a natural-language question. Uses pandas-based
  tools (`describe_csv`, `query_csv`) with expression safety guards
  (allowlist + blocklist) to prevent code injection.
- 60 unit tests in `tests/test_phase4c_skills.py` covering registry
  discovery, YAML validation, file reading, truncation, input validation,
  safety guards, and CSV tool functions with synthetic fixtures.

---

## Phase 4B — 2026-05-27

### Added
- Multi-file upload input type (`type: "files"` in `skill.yaml`) — Gradio
  `file_count="multiple"`, staged into a temp directory for the skill.
- BoB skill updated to accept multiple PDF uploads via `type: "files"`.

### Changed
- HSBC skill switched from `type: "file"` to `type: "directory"` input
  to match its agent API (accepts a folder of PDFs).
- cc_sort and cc_transactions agent scripts: replaced `subprocess` calls
  with `runpy.run_path()` for frozen-mode compatibility.
- Fixed `check_extract_msg_available` — was broken in frozen mode due to
  `sys.executable -c` pattern; now uses direct import.

### Fixed
- Frozen-mode subprocess failures for cc_sort and cc_transactions skills.

---

## Phase 4A — 2026-05-27

### Added
- **Pluggable skill architecture:** `agents/registry.py` auto-discovers
  `agents/*/skill.yaml` at startup and exposes `SkillInfo` dataclass
  objects. Adding a new skill requires only a `skill.yaml` manifest —
  no code changes to `webui.py`.
- **Generic tab rendering:** `ui/tabs/_generic.py` dynamically builds
  Gradio tabs from skill manifests. Supports `file`, `directory`, and
  `text` input types.
- **`run_direct()` execution path** in `base_agent.py` for simple
  prompt → LLM → response skills (no tools, no agent loop). Skills
  declare `mode: "direct"` in `skill.yaml` to use it.
- Home tab now dynamically lists all discovered skills from the registry
  instead of hard-coded text.

---

## Phase 3 — 2026-05-25 (v0.3.0 / v0.3.1)

### Added
- Real icon artwork (gear + sparkle, rounded container) at 16/32/75/128 px.
- Real `git clone --depth 1` agent pull from `sources.toml` with
  SHA-256-keyed cache at `build_pyinstaller/.agents_cache/`.
- GitHub Actions CI release pipeline (`.github/workflows/release.yml`)
  triggered on tag push. Graceful fallback when PortableApps.com Launcher
  Generator CDN is unreachable from Azure runners.
- Vendored Tesseract 5.4.0 and Poppler 24.07.0 binaries via Git LFS.

---

## Phase 2b — 2026-05-24 (v0.2.0)

### Added
- `step8_render_inis` — renders `appinfo.ini` and Launcher INI from
  `bundling/templates/`.
- `step9_copy_defaults` — copies `bundling/templates/DefaultData` into
  staging.
- `step10_launcher_gen` — invokes the PortableApps.com Launcher Generator
  to produce `PASkillsPortable.exe`.
- `step11_zip` — builds a deterministic `dist/PASkillsPortable_<ver>.zip`.
- CLI flags: `--launcher-gen <PATH>`, `--skip-launcher`.

### Changed
- `paskills.spec`: `console=True` → `console=False`. `pa_skills.exe` now
  runs windowless; the PortableApps launcher is the user-facing entry point.

---

## Phase 2a — 2026-05-22

### Added
- Tesseract + Poppler vendoring under `vendor/` via Git LFS.
- `bundling/refresh_binaries.py` — download + SHA-256 verify + extract
  native binaries into `vendor/`.
- `ui/_native.py` — resolves native binary paths, prepends to `PATH`,
  configures `pytesseract`. Idempotent.
- BoB tab (`ui/tabs/skill_bob.py`) — pdfplumber only, no native binaries.
- HSBC tab (`ui/tabs/skill_hsbc.py`) — calls `_native.ensure_native_path()`
  with clear UI error if Tesseract or Poppler are missing.
- `build.py` steps 5–6: copy `vendor/*` into `staging/App/PASkills/`.
- `build.py` step 7: reads `bundling/sources.toml` for agent pull source
  (local sibling folder or git clone).
- Three new smoke tests: BoB import, HSBC import, `_native` resolver.

### Changed
- Pinned `gradio>=6.0,<7.0` in `requirements.txt` and `pyproject.toml`.
- Fixed `datetime.utcnow()` → `datetime.now(timezone.utc)` in `build.py`.

---

## Phase 1 — 2026-05-20

### Added
- Phase 1 scaffold of the portable packaging project per the v0.2 spec
  (`2026-05-01-portable-apps-packaging-spec.docx`).
- Repo root: `LICENSE` (Apache 2.0), `NOTICE`, `README.md`, `pyproject.toml`,
  `requirements.txt`, `.gitignore`, `.gitattributes` (LFS rules per spec §10.5).
- Source tree skeleton: `src/agents/`, `ui/{tabs,_buildinfo.py}`,
  `bundling/{templates,icons}`, `tests/`, placeholder `vendor/`.
- `src/agents/` mirrored from the sibling `platform-agnostic-skills` project
  per the build-time-pull contract (locked decision §15.1).
- Minimal Gradio `ui/webui.py` with Home and 26AS tabs only, custom black +
  electric-blue theme (locked decision §15.4), bound to 127.0.0.1 with
  free-port pick.
- `bundling/build.py` covering steps 1–4 of spec §10.2 (read git tag,
  reset staging, create venv, run PyInstaller `--onedir`).
- `bundling/paskills.spec` with hidden imports per spec §10.3.

### Changed
- Renamed top-level `packaging/` folder to `bundling/` to avoid a Python
  import-path shadow on the PyPI `packaging` package.

### Notes
- Native binaries (Tesseract, Poppler) deferred to Phase 2.
- AppInfo, DefaultData, Launcher INI, and Launcher Generator deferred to Phase 2.
- Frozen `pa_skills.exe` must be smoke-tested manually; see `BUILD-NOTES.md`.
