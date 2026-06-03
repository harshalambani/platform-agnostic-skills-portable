# Changelog

All notable changes to this project are recorded here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
- 17 unit tests for streaming infrastructure in
  `tests/test_phase4d_streaming.py`.

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
