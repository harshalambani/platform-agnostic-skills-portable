# Phase 2b — Hand-off notes (2026-05-24)

> _Phase 2b lands the four remaining items from v0.2 spec §10.2: render the
> PortableApps INIs from templates, copy DefaultData into staging,
> invoke the PortableApps.com Launcher Generator to produce
> `PASkillsPortable.exe`, and zip the result into a deterministic release
> archive. Also flips `pa_skills.exe` to a windowless build now that the
> launcher hides the console._

What changed in the sandbox:

| File | Change |
|---|---|
| `bundling/build.py` | Real implementations for `step8_render_inis`, `step9_copy_defaults`, `step10_launcher_gen`, `step11_zip`. New CLI flags `--launcher-gen <PATH>` and `--skip-launcher`. `main()` reports the wrapper + zip paths at the end. |
| `bundling/paskills.spec` | `console=True` → `console=False`. `pa_skills.exe` now opens windowless; the PortableApps launcher is what the user double-clicks. |
| `ui/tabs/skill_hsbc.py` | Dropped the `(_NATIVE.mode)` parenthetical from the green banner — the frozen build is the only path users see. |

---

## 0. One-time prereq — install the PortableApps.com Launcher Generator

If you haven't already:

1. Download from https://portableapps.com/apps/development/portableapps.com_launcher (the page links the .paf.exe installer).
2. Install it under your PortableApps menu — the default lands at
   `C:\PortableApps\PortableApps.comLauncher\PortableApps.comLauncherGenerator.exe`
   or somewhere under `C:\PortableApps\PortableApps.com\`. `build.py` searches
   both roots recursively, so either is fine.

Verify with:

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
Get-ChildItem -Path 'C:\PortableApps\' -Recurse -Filter 'PortableApps.comLauncherGenerator.exe' -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty FullName
```

If that prints a path, you're set. If it prints nothing, install it before continuing — or pass `--launcher-gen <path>` to `build.py` (or set `PASKILLS_LAUNCHER_GEN`).

---

## 1. Rebuild — full Phase 2b pass

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
.\.venv\Scripts\Activate.ps1
python bundling\build.py --skip-venv
```

`--skip-venv` reuses the existing build venv so the only slow part is PyInstaller. Expected new output lines after step 7:

```
[step 8] Render appinfo.ini + Launcher INI
       VERSION_3=0.2.0  VERSION_4=0.2.0.0
  ok   wrote staging\App\AppInfo\appinfo.ini
  ok   wrote staging\App\AppInfo\Launcher\PASkillsPortable.ini
[step 9] Copy DefaultData -> staging
  ok   DefaultData copied -> staging\App\DefaultData (1 files)
[step 10] Invoke PortableApps Launcher Generator
       using launcher generator: C:\PortableApps\...\PortableApps.comLauncherGenerator.exe
  ok   wrapper produced: staging\PASkillsPortable.exe
[step 11] Zip staging -> dist
  ok   wrote dist\PASkillsPortable_0.2.0.zip  (N files, X MiB uncompressed)
  ok   build complete (version 0.2.0, sha ...)
```

If step 10 prints `PortableApps.comLauncherGenerator.exe not found.`, `build.py` skips step 11 too (no point zipping without the wrapper). Either install the generator and re-run, or pass it explicitly:

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
python bundling\build.py --skip-venv --launcher-gen 'C:\path\to\PortableApps.comLauncherGenerator.exe'
```

---

## 2. Smoke-test by unzipping into a fresh PortableApps directory

The point of step 11 is that the zip is a drop-in for a PortableApps menu. To verify:

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
$test = "$env:TEMP\paskills-smoke-$([guid]::NewGuid().ToString('N').Substring(0,8))"
New-Item -ItemType Directory -Path $test | Out-Null
Expand-Archive -Path 'dist\PASkillsPortable_0.2.0.zip' -DestinationPath $test
Get-ChildItem -Recurse $test\PASkillsPortable | Select-Object -First 30 FullName
```

Expected top-level layout under `$test\PASkillsPortable\`:

```
App\
├── AppInfo\
│   ├── appinfo.ini
│   └── Launcher\
│       └── PASkillsPortable.ini
├── DefaultData\
│   └── settings\config.yaml
└── PASkills\
    ├── pa_skills.exe
    ├── _internal\
    ├── tesseract\
    └── poppler\
Data\
Other\
PASkillsPortable.exe         ← the wrapper, double-click this
```

Launch test:

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
& "$env:TEMP\paskills-smoke-*\PASkillsPortable\PASkillsPortable.exe"
```

Verify:

1. **No console window** appears (the visible-cmd window from earlier phases is gone — that's the `console=False` flip in `paskills.spec` plus the launcher wrapping the exe).
2. The default browser opens to `http://127.0.0.1:<port>` automatically.
3. **Home tab**: green dot for `local_ollama`. **HSBC tab** header reads _Native OCR binaries detected._ (no parenthetical mode tag).
4. `Data\settings\config.yaml` was copied from `App\DefaultData\settings\config.yaml` by the PA launcher on first run — confirm it exists under `$test\PASkillsPortable\Data\settings\`.

When done:

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
Get-Process pa_skills, PASkillsPortable -ErrorAction SilentlyContinue | Stop-Process -Force
Remove-Item -Recurse -Force "$env:TEMP\paskills-smoke-*"
```

---

## 3. Commit the Phase 2b changes

Once the smoke test passes:

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
git add bundling/build.py bundling/paskills.spec ui/tabs/skill_hsbc.py 2026-05-24-PHASE-2B-NOTES.md
git status   # sanity-check what's staged
git commit -m "Phase 2b: INI rendering, launcher generator, deterministic zip; console=False"
git tag -f v0.2.0
```

---

## 4. What the build script does + does not do now

Does:
* Renders `appinfo.ini` (PackageVersion 4-tuple, DisplayVersion dotted-3) and the Launcher INI from `bundling/templates/*.tmpl`, stripping any `+dirty`/`+sha` version suffix because PA's INI parser dislikes `+`.
* Copies `bundling/templates/DefaultData/` into `staging/App/DefaultData/` so the PA launcher's first-run copy populates `Data\settings\` with our `config.yaml` defaults.
* Searches four places for the Launcher Generator (CLI flag → env var → PATH → `C:\PortableApps\*`), invokes it with `staging/` as its single positional arg, then verifies `staging/PASkillsPortable.exe` was produced.
* Builds `dist/PASkillsPortable_<v3>.zip` with sorted file order and a fixed `(2026,1,1,0,0,0)` timestamp on every entry — same input bytes produces the same output zip SHA-256. Verified.

Does not (deferred):
* No real `appicon.ico` yet — `bundling/icons/README.md` still describes the placeholder situation. The launcher generator will use a default icon. Drop the four PNGs + ICO into `bundling/icons/` whenever the art is ready.
* No PortableApps update-checker URL.

---

## 5. Open follow-ups for Phase 3

* Real icon artwork.
* Code-sign `pa_skills.exe` and `PASkillsPortable.exe` (PortableApps recommends but doesn't require).
* Switch the agents/ source from "already mirrored" to an actual `git clone --depth 1` per `sources.toml` once the upstream repo is published.
* Set up CI to run `python bundling/build.py --version <tag>` on tag push and attach the zip to a GitHub release.
