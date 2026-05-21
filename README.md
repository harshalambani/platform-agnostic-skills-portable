# PA Skills Portable

LLM-powered PDF and statement processing skills, packaged as a single portable
folder that conforms to the [PortableApps.com Format](https://portableapps.com/development/portableapps_format).

The package bundles the Python runtime, all dependencies, Tesseract OCR with
English language data, Poppler, the three core skills (26AS, BoB, HSBC), and
a Gradio-based web UI. The Large Language Model is intentionally **not** bundled —
the user runs Ollama (or any OpenAI-compatible endpoint) externally.

## Status

This is a Phase-1 scaffold. End-to-end frozen build is not yet produced;
see `BUILD-NOTES.md` for the per-phase walkthrough and `CHANGELOG.md` for
the release history.

## Quick start (developer, source mode)

```powershell
# 1. Clone with LFS (binaries under vendor/ are LFS-tracked)
git clone https://github.com/<owner>/platform-agnostic-skills-portable.git
cd platform-agnostic-skills-portable

# 2. Create venv and install runtime dependencies
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# 3. Run the Gradio UI in source mode (talks to local Ollama on :11434 by default)
python -m ui.webui
```

## Packaging (developer, frozen build)

```powershell
# Phase 1: produces a PyInstaller --onedir output under staging\App\PASkills\
python bundling\build.py
```

See `BUILD-NOTES.md` for the full per-phase command set, prerequisites, and
expected artifact layout. The end-user `PASkillsPortable_<version>.zip` is
produced by Phase 2 of the plan.

## Repo layout

```
platform-agnostic-skills-portable/
├── src/agents/         ← skill implementations, mirrored from the upstream
│                         platform-agnostic-skills project at build time
├── ui/                 ← Gradio web UI (Home + 26AS in Phase 1)
├── bundling/           ← build.py, paskills.spec, INI templates, icons
├── tests/              ← unit and smoke tests
├── vendor/             ← LFS-tracked native binaries (Phase 2)
├── staging/            ← intermediate build tree (gitignored)
└── dist/               ← release zip lands here (gitignored)
```

## License

Apache License 2.0 — see `LICENSE`. Bundled third-party binaries
(Tesseract, Poppler, PortableApps Launcher) retain their upstream
licenses and are documented in `NOTICE`.
