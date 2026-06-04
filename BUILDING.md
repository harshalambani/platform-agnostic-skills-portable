# Building PA Skills Portable from Source

This is the canonical build guide. It covers prerequisites, source-mode
development, the frozen build pipeline, and CI.

## Prerequisites

**Required:**

- **Windows 10/11** — the build pipeline, PyInstaller spec, and PortableApps
  tooling are Windows-only. Source-mode development of the Python code works
  on any OS, but the frozen build must run on Windows.
- **Python 3.13+** on PATH (`py -3.13 --version`).
- **Git** on PATH, with **Git LFS** installed (`git lfs install`).
  The `vendor/` directory uses LFS for native binaries (~115 MB).

**Automatically handled:**

- The build script creates its own isolated venv in `build_pyinstaller/venv/`
  and installs all pip dependencies (including PyInstaller) there. Your dev
  `.venv` is not touched.
- The PortableApps.com Launcher Generator v2.2.4 is self-hosted at
  `bundling/launcher-gen/2.2.4/` — no separate download needed.

## Source-mode development

This is the fastest feedback loop — no freeze step, changes take effect
immediately.

```powershell
cd platform-agnostic-skills-portable
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt

# Run the UI (opens browser automatically)
cd src
python -m ui.webui
```

The Home tab shows endpoint health (green/amber/red). Configure endpoints
in the Settings tab or edit `Data/settings/config.yaml` directly.

### Native binaries for source mode

Some skills need native binaries on PATH. In the frozen build these are
vendored automatically, but for source-mode development you need:

- **Tesseract 5.x** — HSBC skill
  ([UB-Mannheim installer](https://github.com/UB-Mannheim/tesseract/wiki))
- **Poppler** — HSBC skill
  ([oschwartz10612 builds](https://github.com/oschwartz10612/poppler-windows/releases))
- **qpdf** — CC Sort skill
  ([qpdf releases](https://github.com/qpdf/qpdf/releases))

Or use the vendor refresh script to download them into `vendor/`:

```powershell
python bundling\refresh_binaries.py --target all
```

The `_native.py` resolver checks `vendor/` before PATH, so once vendored
they work in source mode too.

## Frozen build

From the repo root, with Git available:

```powershell
python bundling\build.py
```

### What the script does

| Step | Action | Output |
|------|--------|--------|
| 1 | Read version from `git describe --tags`, write `ui/_buildinfo.py` | Version + SHA + dirty flag |
| 2 | Wipe `staging/`, create PortableApps.com folder skeleton | `staging/App/...`, `staging/Data/`, `staging/Other/` |
| 3 | Create build venv, install `requirements.txt` + PyInstaller | `build_pyinstaller/venv/` |
| 4 | Run PyInstaller via `bundling/paskills.spec` | `staging/App/PASkills/pa_skills.exe` |
| 5 | Copy Tesseract from `vendor/tesseract/` into staging | Native binary in frozen output |
| 6 | Copy Poppler from `vendor/poppler/` into staging | Native binary in frozen output |
| 7 | Verify `src/agents/` is populated (build-time pull or in-tree) | Logs file count |
| 8 | Render `appinfo.ini` + Launcher INI from templates | `staging/App/AppInfo/appinfo.ini` |
| 9 | Copy `DefaultData/` into staging | Default config for first-run |
| 10 | Invoke PortableApps.com Launcher Generator | `staging/PASkillsPortable.exe` |
| 11 | Zip `staging/` into `dist/PASkillsPortable_<version>.zip` | Release archive |

### Useful flags

| Flag | Effect |
|------|--------|
| `--version 0.5.0` | Force version string (ignores git tag) |
| `--allow-dirty` | Proceed with uncommitted changes |
| `--skip-venv` | Reuse existing build venv (saves ~3-5 min) |
| `--skip-pull` | Don't re-pull agents from upstream (use in-tree `src/agents/`) |
| `--skip-launcher` | Skip steps 10-11 (no wrapper exe or zip) |
| `--launcher-gen <path>` | Point to a specific Launcher Generator exe |
| `--clean` | Delete `build_pyinstaller/.agents_cache/` and exit |
| `--no-color` | Strip ANSI codes from output |

### Smoke-testing the frozen output

```powershell
$env:PA_SKILLS_NO_BROWSER = "1"
staging\App\PASkills\pa_skills.exe
```

Check that:

1. Console prints `Running on local URL: http://127.0.0.1:<port>`
2. `ui_port.json` appears with the port and version
3. All tabs render, Home shows endpoint health, skills are discoverable

## CI pipeline

The GitHub Actions workflow (`.github/workflows/release.yml`) triggers on
`v*` tag pushes. It:

1. Checks out with LFS (`git lfs pull`)
2. Sets up Python 3.13
3. Runs `python bundling/build.py --version <tag> --skip-pull --allow-dirty`
4. Smoke-tests the frozen exe (launches, waits for port file, GETs root URL,
   verifies HTTP 200, kills)
5. Uploads the zip as a build artifact
6. Creates a GitHub Release with the zip attached

The build uses `--skip-pull` because CI has no sibling `platform-agnostic-skills`
folder — `src/agents/` is committed in-tree.

### Releasing a new version

```powershell
git tag v0.5.0
git push origin v0.5.0
```

This triggers the CI pipeline. If the tag contains a hyphen (e.g., `v0.5.0-rc1`),
the release is marked as a prerelease.

## Vendoring native binaries

Native binaries are managed through `bundling/binaries.toml`, which records
download URLs and SHA-256 hashes. The `bundling/refresh_binaries.py` script
downloads, verifies, and extracts them into `vendor/`.

```powershell
# Refresh all binaries
python bundling\refresh_binaries.py --target all

# Refresh only Tesseract (from local install instead of download)
python bundling\refresh_binaries.py --target tesseract --from-tesseract "C:\Program Files\Tesseract-OCR"
```

After refreshing, commit the updated `vendor/` contents. Git LFS handles
the large binary files automatically.

## Troubleshooting

**`build.py` exits with code 1 (dirty tree):** Either commit your changes
or pass `--allow-dirty`.

**PyInstaller missing modules:** Check `bundling/paskills.spec` for
`hiddenimports`. Gradio and LangGraph pull in many transitive dependencies
that PyInstaller can't detect statically.

**Frozen exe crashes on startup with no console:** The spec uses
`console=False` (windowless). To debug, temporarily set `console=True` in
`paskills.spec` and rebuild. See also `webui.py`'s `sys.stdout`/`sys.stderr`
redirect for the `None`-stream pitfall.

**Launcher Generator not found:** It's self-hosted at
`bundling/launcher-gen/2.2.4/`. If this path is missing, run `git lfs pull`.

## Historical notes

The date-stamped build notes from earlier phases are archived in
`docs/history/` for reference. This document supersedes all of them.
