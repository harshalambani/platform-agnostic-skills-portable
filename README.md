# PA Skills Portable

[![Release](https://github.com/harshalambani/platform-agnostic-skills-portable/actions/workflows/release.yml/badge.svg)](https://github.com/harshalambani/platform-agnostic-skills-portable/actions/workflows/release.yml)

LLM-powered document processing skills packaged as a single portable folder
that conforms to the
[PortableApps.com Format](https://portableapps.com/development/portableapps_format).
Extract it anywhere, double-click `PASkillsPortable.exe`, and a local Gradio
web UI opens in your browser.

The package bundles Python, all pip dependencies, Tesseract OCR (English),
Poppler, and qpdf. The Large Language Model is intentionally **not** bundled —
you run [Ollama](https://ollama.com) (or any OpenAI-compatible endpoint)
externally. This means you choose the model, control the hardware, and keep
your data local.

## Skills

16 skills are exposed in the UI (a further 6 internal pipeline steps run only
as part of GnuCash Import — see [src/agents](src/agents)).

| Skill | Mode | LLM | What it does |
|-------|------|-----|--------------|
| Bank of Baroda → CSV | direct | no | Parse BoB statement PDFs → clean transaction CSV |
| HDFC → GnuCash CSV | direct | no | Convert HDFC statements (scanned PDF or net-banking XLS/XLSX) → GnuCash-importable CSV |
| HSBC → Excel | agent | yes | OCR + parse HSBC statement PDFs → reconciled Excel workbook |
| ICICI → GnuCash CSV | agent | yes | Convert ICICI XLS statement downloads → GnuCash-importable CSV |
| CC Sort — Extract & Organize | direct | no | Sort credit-card statement PDFs by bank/issuer, extract from .msg |
| CC Transactions → Excel | direct | no | Extract transaction tables from sorted CC PDFs → consolidated Excel |
| GnuCash Import | agent | yes | End-to-end: bank statement + GnuCash book → mapped, import-ready CSV in one step |
| 26AS → Excel (Convert) | direct | no | Parse Indian tax Form 26AS PDF → structured Excel workbook |
| 26AS Journal | agent | yes | Build GnuCash TDS journal entries from a 26AS workbook + your .gnucash file |
| KRChoksey Ledger | direct | no | Simplify a KR Choksey broker ledger PDF into a tied-out "Simplified Ledger" workbook |
| KRChoksey GnuCash Import | direct | no | Convert KR Choksey Bills workbook → GnuCash multi-split CSVs (Purchase/SLBM/Sale) |
| KRChoksey Reconcile | direct | no | Reconcile KR Choksey contract notes against the Simplified Ledger |
| CSV Data Analyzer | agent | yes | Ask natural-language questions about any CSV file |
| Document Summarizer | direct | yes | Summarise any PDF or text file → Markdown |
| MSG / Email Parser | direct | no | Parse .msg or .eml → structured JSON (sender, date, body, attachments) |
| Text Translator | direct | yes | Translate text between languages via LLM |

**Agent-mode** skills use an LLM with tool-calling for multi-step reasoning.
**Direct-mode** skills send a single prompt (or do pure extraction). The
**LLM** column shows which skills currently require a model connection —
HSBC and ICICI are next in line to go fully offline (see CHANGELOG).

## Quick start — end user

1. Download the latest `PASkillsPortable_<version>.zip` from
   [Releases](https://github.com/harshalambani/platform-agnostic-skills-portable/releases).
2. Extract the zip to any folder (USB drive, Desktop, wherever).
3. Install and start [Ollama](https://ollama.com) (or configure an
   OpenAI-compatible endpoint in the Settings tab).
4. Pull a model: `ollama pull gemma3` (or any chat-capable model).
5. Double-click `PASkillsPortable.exe`. The Gradio UI opens at
   `http://127.0.0.1:<port>`.

No Python install required. No admin rights. No registry changes.

**Closing the app:** click the **Exit** button in the top-right of the UI (or
just close the browser window — the server shuts itself down a few seconds
after the tab closes). This ensures a clean exit so PortableApps.com does not
report "did not close correctly" or block a later upgrade.

### Windows SmartScreen / SmartApp Control

The download is not code-signed yet, so Windows 11 may warn or block it the
first time you run it:

- **SmartScreen** ("Windows protected your PC"): click **More info -> Run
  anyway**. You can also pre-clear the file in PowerShell before running:
  ```powershell
  cd "C:\path\to\download"
  Unblock-File .\PASkillsPortable_<version>.paf.exe
  ```
- **Smart App Control** (a stricter Windows 11 mode, on by default on some new
  PCs): it blocks all unsigned apps and has no per-app "Run anyway". If it
  blocks the app, either use a PC without Smart App Control, or turn Smart App
  Control off in **Windows Security -> App & browser control -> Smart App
  Control** (note: turning it back **on** later requires a Windows reset, so
  only do this if you understand the trade-off).

This warning is expected for a new, unsigned build and does not indicate the
app is unsafe — the source and build are public in this repo.

## Quick start — developer (source mode)

```powershell
# 1. Clone with LFS (vendor/ binaries are LFS-tracked)
git clone https://github.com/harshalambani/platform-agnostic-skills-portable.git
cd platform-agnostic-skills-portable
git lfs pull

# 2. Create venv and install dependencies
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Run the Gradio UI (from the repo root — webui.py adds src/ to sys.path)
python -m ui.webui
```

### Requirements

- **Python 3.13+** (source mode only; the frozen build bundles its own runtime)
- **Ollama** or any OpenAI-compatible LLM endpoint
- **Git LFS** — the `vendor/` directory uses LFS for native binaries

The following native binaries are vendored in the frozen build but must be
on PATH (or installed) for source-mode use of certain skills:

- **Tesseract OCR 5.x** — required by the HSBC skill
  ([UB-Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki))
- **Poppler** — required by the HSBC skill for PDF rendering
  ([oschwartz10612 builds](https://github.com/oschwartz10612/poppler-windows/releases))
- **qpdf** — required by the CC Sort skill for PDF decryption
  ([qpdf releases](https://github.com/qpdf/qpdf/releases))

## Running tests

```powershell
cd src
python -m pytest ..\tests\ -v
```

Currently ~160 tests covering smoke imports, registry discovery, config
adapter, runner, health check, streaming, history tab, skill logic, and the
MSG parser.

## Building the frozen release

See [BUILDING.md](BUILDING.md) for the full build guide. The short version:

```powershell
python bundling\build.py --version 0.5.0 --skip-pull
```

This produces `dist/PASkillsPortable_0.5.0.zip` containing the complete
PortableApps.com package. CI runs this automatically on every `v*` tag push.

## Architecture

```
platform-agnostic-skills-portable/
├── src/
│   └── agents/              ← skill implementations (mirrored from upstream
│       ├── registry.py         platform-agnostic-skills at build time)
│       ├── base_agent.py    ← LLM integration (LangGraph agent + direct mode)
│       └── skill_*/         ← one directory per skill (skill.yaml + agent.py)
├── ui/                      ← Gradio web UI
│   ├── webui.py             ← app entry point, tab assembly, port management
│   ├── _config.py           ← multi-endpoint config ↔ legacy adapter
│   ├── _runner.py           ← background-thread executor + streaming
│   ├── _health.py           ← endpoint health checker
│   ├── _native.py           ← native binary resolver (Tesseract, Poppler, qpdf)
│   └── tabs/                ← Home, Settings, History, per-skill generic tabs
├── bundling/                ← build orchestration
│   ├── build.py             ← 11-step build pipeline
│   ├── paskills.spec        ← PyInstaller spec file
│   ├── templates/           ← INI templates, DefaultData, icons
│   ├── launcher-gen/        ← self-hosted PortableApps Launcher Generator
│   └── binaries.toml        ← native binary download URLs + SHA256 hashes
├── vendor/                  ← LFS-tracked native binaries (Tesseract, Poppler, qpdf)
├── tests/                   ← pytest suite
├── staging/                 ← intermediate build tree (gitignored)
└── dist/                    ← release zip output (gitignored)
```

### Config flow

The app uses a multi-endpoint config at `Data/settings/config.yaml`:

```yaml
active_endpoint: local_ollama
endpoints:
  local_ollama:
    provider: ollama
    base_url: http://localhost:11434
    default_model: gemma3
    temperature: 0.0
```

The `_config.py` adapter materialises a transient legacy-format config that
`base_agent.load_model()` consumes. Users manage endpoints through the
Settings tab — no manual YAML editing needed.

### Adding a new skill

1. Create `src/agents/skill_yourskill/` with three files:
   - `skill.yaml` — manifest (name, mode, inputs, output config, dependencies)
   - `agent.py` — must export a `run()` function
   - `AGENT.md` — system prompt (for LLM-backed skills)

2. The registry auto-discovers skills via `skill.yaml`. No changes to
   `webui.py` needed — the generic tab system builds UI from the manifest.

3. Choose a mode:
   - `direct` — single prompt → LLM → response (or pure extraction, no LLM)
   - `agent` — multi-step LangGraph agent with tools

4. Add a `help:` block to `skill.yaml` (overview, inputs, steps, outputs, tips,
   troubleshooting) and run `python scripts/gen_docs.py`. This powers the
   in-app Help and the user guide — see
   [docs/dev/help-block-schema.md](docs/dev/help-block-schema.md). The coverage
   test fails if a UI skill ships without help.

5. Run tests to confirm discovery: `python -m pytest ../tests/test_smoke.py -v`

See any existing skill directory (e.g., `skill_summarize/` for direct mode,
`skill_csv_analyzer/` for agent mode) as a template.

## Documentation & help

User-facing help and developer docs are generated from a single source: the
`help:` block in each skill's `skill.yaml`. One command rebuilds everything:

```powershell
python scripts\gen_docs.py          # regenerate; --check fails if stale (CI)
```

- **End users:** in-app **Help** tab and a collapsible help panel on every skill
  tab; per-skill guides in [docs/user-guide/](docs/user-guide/); and a
  self-contained [docs/USER-GUIDE.html](docs/USER-GUIDE.html) bundled with the
  portable package.
- **Developers:** [help-block-schema.md](docs/dev/help-block-schema.md) (the
  schema), [editing-help.md](docs/dev/editing-help.md) (how to change help), and
  the auto-generated [skills-reference.md](docs/dev/skills-reference.md)
  (every UI skill + the internal pipeline steps).

## CI / CD

The GitHub Actions workflow (`.github/workflows/release.yml`) triggers on
`v*` tag pushes and:

1. Checks out with LFS
2. Builds the frozen exe via `build.py`
3. Smoke-tests the exe (launches, verifies HTTP 200, kills)
4. Creates a GitHub Release with the zip attached

## License

Apache License 2.0 — see `LICENSE`. Bundled third-party binaries
(Tesseract, Poppler, qpdf, PortableApps Launcher) retain their upstream
licenses and are documented in `NOTICE`.
