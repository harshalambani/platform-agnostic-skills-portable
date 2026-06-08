# Phase 2a — Hand-off notes (2026-05-22)

> _Phase 2a delivers Tesseract + Poppler vendored under `vendor/` via Git LFS,
> wires the BoB + HSBC tabs into the UI, and updates the build pipeline to
> ship those binaries alongside `pa_skills.exe`. The launcher generator
> (`PASkillsPortable.exe`) and the final release zip are Phase 2b._

What changed in the sandbox (sources only — vendor binaries you fetch on Windows):

| File | Change |
|---|---|
| `requirements.txt` + `pyproject.toml` | Pin `gradio>=6.0,<7.0` |
| `bundling/build.py` | `datetime.utcnow()` → `datetime.now(timezone.utc)`; dirty-tree check excludes `ui/_buildinfo.py`; step5/step6 now copy `vendor/*` into `staging/App/PASkills/` |
| `bundling/refresh_binaries.py` | NEW — download + SHA-256 verify + extract Tesseract/Poppler into `vendor/` |
| `ui/_native.py` | NEW — resolves native binary paths, prepends to `PATH`, configures `pytesseract`. Idempotent |
| `ui/tabs/skill_bob.py` | NEW — BoB tab; pdfplumber only, no native binaries needed |
| `ui/tabs/skill_hsbc.py` | NEW — HSBC tab; calls `_native.ensure_native_path()` at import time; surfaces a clear UI error if Tesseract or Poppler are missing |
| `ui/webui.py` | Adds BoB + HSBC tabs to the sidebar |
| `tests/test_smoke.py` | Three new tests: BoB import, HSBC import, `_native` resolver |

---

## 0. Prereqs (one-time per developer machine)

```powershell
# Git LFS — only needed if you don't already have it installed
git lfs install
```

You can verify with `git lfs version` (should print a version, not "not a git command").

The `.gitattributes` LFS rules (from Phase 1) already track `vendor/**` and
`*.exe`, `*.dll`, `*.traineddata` — so adding the binaries below will route
them through LFS automatically.

---

## 1. Fetch the native binaries (~115 MB, ~5 min on first run)

From the project root, with the dev venv active:

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
.\.venv\Scripts\Activate.ps1
python bundling\refresh_binaries.py --target all
```

**First-run bootstrap behaviour:** `bundling\binaries.toml` ships with
placeholder SHAs. The script will download each archive, compute its actual
SHA-256, print a line like:

```
⚠  binaries.toml has a placeholder SHA for 'tesseract'.
   Copy this line into binaries.toml under [tesseract]:

       sha256 = "abc123…"

   Then re-run: python bundling\refresh_binaries.py --target tesseract
```

Open `bundling\binaries.toml`, paste each `sha256` value into the right
section, save, and re-run the same command. The second pass will verify
against your pinned SHAs and populate `vendor/tesseract/` and
`vendor/poppler/bin/`.

Expected `vendor/` layout when done:

```
vendor/
├── README.md
├── tesseract/
│   ├── tesseract.exe
│   ├── *.dll                 (Leptonica, OpenMP, MSVC runtime, etc.)
│   └── tessdata/
│       └── eng.traineddata
└── poppler/
    └── bin/
        ├── pdftoppm.exe
        ├── pdfinfo.exe
        └── *.dll
```

---

## 2. Commit the vendor tree

```powershell
git add vendor/ bundling/binaries.toml
git status   # sanity-check what's staged
git commit -m "Phase 2a: vendor Tesseract + Poppler, pinned SHAs"
```

LFS automatically replaces the binary files with pointer files in git
history while keeping the real bytes in `.git/lfs/objects/`. The repo
working tree still shows the real files. To verify the routing:

```powershell
git lfs ls-files
```

Should list each `vendor/tesseract/*.exe`, `*.dll`, `*.traineddata`, and
`vendor/poppler/bin/*.exe`. If LFS isn't picking them up, the most common
cause is `git lfs install` was never run on this machine — re-run it and
then `git add --renormalize vendor/`.

---

## 3. Rebuild

```powershell
python bundling\build.py --skip-venv
```

`--skip-venv` reuses the existing build venv from Phase 1, saving ~3 min.
Expected new lines in the output:

```
[step 5] Native binaries — Tesseract
       tesseract: 7 files copied to staging\App\PASkills\tesseract
  ok   tesseract bundled (7 files)
[step 6] Native binaries — Poppler
       poppler: 14 files copied to staging\App\PASkills\poppler
  ok   poppler bundled (14 files)
```

(File counts will vary slightly depending on the upstream zip contents.)

If you see `vendor source missing: ...` warnings, step 1 didn't complete —
verify `vendor/tesseract/tesseract.exe` and `vendor/poppler/bin/pdftoppm.exe`
both exist.

---

## 4. Smoke test the frozen build

```powershell
Get-Process pa_skills -ErrorAction SilentlyContinue | Stop-Process -Force
cd staging\App\PASkills
.\pa_skills.exe --no-browser
```

Open the printed URL in a browser. Expected:

1. **Home tab** — same as Phase 1: green dot for `local_ollama`, two red dots
   for the unconfigured presets.
2. **26AS tab** — unchanged.
3. **BoB tab** — file upload + model dropdown + Run. Drop a Bank of Baroda
   transaction-statement PDF, click Run, expect a date-stamped `.csv` in
   `staging\App\PASkills\outputs\` and a download link.
4. **HSBC tab** — header should read _"Native OCR binaries detected (frozen
   mode)."_ Drop an HSBC statement PDF (preferably one with some scanned
   pages so the OCR path exercises). The agent will print pages-progressed
   info to the console as it runs.

If the HSBC tab header reads _"Native binaries missing"_, step 1 or step 3
silently fell through — re-run `refresh_binaries.py`, verify `vendor/`,
rebuild.

---

## 5. Commit the Phase 2a code changes

Once both BoB and HSBC pass the smoke test:

```powershell
git add -A
git commit -m "Phase 2a: BoB + HSBC tabs, _native resolver, vendor wiring"
git tag -f v0.2.0
```

Then we move on to Phase 2b: AppInfo INI rendering, PortableApps Launcher
Generator invocation, zip distribution.

---

## Known follow-ups for Phase 2b

- Render `appinfo.ini` + `PASkillsPortable.ini` from templates (`bundling/templates/`) using version + commit from `_buildinfo.py`.
- Invoke the PortableApps.com Launcher Generator on the rendered `appinfo.ini` → produces `PASkillsPortable.exe` that wraps `pa_skills.exe` and hides the console window (so we can flip `console=False` back in the spec).
- Zip `staging/` → `dist/PASkillsPortable_<version>.zip` with deterministic timestamps for reproducible builds.
- Smoke-test by unzipping into a fresh PortableApps menu profile.
