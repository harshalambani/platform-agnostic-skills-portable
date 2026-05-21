# Build notes — PA Skills Portable

> _Phase 1 handoff, 2026-05-20. Follow this once on your Windows machine after
> the sandbox has laid the scaffold down. Subsequent builds are just step 4._

---

## 0.  One-time cleanup: re-init the git repo

The sandbox tried `git init` while laying down files, but the Cowork mount
disallows file unlinks, so a stale `.git/config.lock` was left behind. Git
on Windows can't recover from that lock without `Remove-Item`, so the first
thing you do on your Windows box is:

```powershell
cd 'C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable'
Remove-Item .git -Recurse -Force
git init -b main
git config user.email "harshal.subscribe@hotmail.com"
git config user.name  "Harshal"
git add .
git commit -m "Phase 1 scaffold (v0.2 spec)"
git tag v0.1.0
```

The `v0.1.0` tag is what `build.py` reads for its version string. Without
a tag the build still works but stamps `0.0.0+<sha>` into `_buildinfo.py`.

## 1.  Prerequisites (one-time)

- **Python 3.13** on PATH (`py -3.13 --version` should print 3.13.x).
  PyInstaller's freeze targets whatever Python the build venv is created
  with, so 3.13 in → 3.13 out. CPython.org's installer is fine.
- **Git** on PATH (used by `build.py` for the version derivation).
- **No** admin rights, no Visual Studio, no Tesseract or Poppler yet —
  those are Phase 2.

Recommended (not required for Phase 1):

- **Git LFS** — install before you ever commit anything under `vendor/`.
  Phase 2 wires LFS up; in Phase 1 `vendor/` is empty placeholders.
  `git lfs install`.

## 2.  Source-mode run (no freeze, fastest feedback loop)

Use this to sanity-check the UI against your local Ollama before you
spend time freezing. From the repo root:

```powershell
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m ui.webui
```

Expected on launch:

1. Console prints something like `Running on local URL:  http://127.0.0.1:54321`.
2. Default browser opens to that URL.
3. The **Home** tab shows a 🟢 dot next to `local_ollama` if Ollama is
   running on `localhost:11434`, 🔴 otherwise.
4. The **26AS** tab's model dropdown is populated from Ollama's `/api/tags`.

If Ollama is on a non-default port, edit `Data\settings\config.yaml`
(create the file by copying `bundling\templates\DefaultData\settings\config.yaml`
into `Data\settings\config.yaml`) and change `local_ollama.base_url`.

## 3.  Frozen build (PyInstaller `--onedir`)

This is steps 1–4 of spec §10.2. From the repo root, on Windows:

```powershell
python bundling\build.py
```

What the script does, in order:

| step | action | output |
|---|---|---|
| 1 | Read version from `git describe --tags`, write `ui/_buildinfo.py`. | `_buildinfo.py` carries version + 7-char sha + dirty flag. |
| 2 | Wipe `staging/` and recreate the PortableApps.com folder skeleton. | `staging/App/...`, `staging/Data/`, `staging/Other/`. |
| 7 | (logical step 7) Verify `src/agents/` is populated. | Logs the .py file count. |
| 3 | Create a clean venv in `build_pyinstaller/venv/`, install `requirements.txt` + PyInstaller. | `build_pyinstaller/venv/Scripts/python.exe`. |
| 4 | Run `pyinstaller bundling\paskills.spec`. Copy `build_pyinstaller/dist/pa_skills/` → `staging/App/PASkills/`. | `staging/App/PASkills/pa_skills.exe` (+ `_internal/`). |
| 5–11 | Print "Phase 2, skipped" markers. | (no output) |

Useful flags:

- `--allow-dirty` — proceed even if `git status` is not clean. Set this
  while iterating; **do not** set it on tagged release builds.
- `--skip-venv` — reuse the existing build venv (saves ~3–5 min on every
  rebuild after the first).
- `--version 0.1.0` — force a version string, ignoring the git tag.
- `--no-color` — strip ANSI codes if your terminal renders them as `[94m`.

## 4.  Smoke test the frozen output

```powershell
cd staging\App\PASkills
.\pa_skills.exe --no-browser
```

Expected:

1. Console prints `Running on local URL:  http://127.0.0.1:<port>`.
2. A file `staging\App\PASkills\ui_port.json` appears with the port +
   version + commit.
3. Open the URL manually — both tabs should render, Home should report
   the Ollama endpoint's health, and the 26AS dropdown should populate.

Smoke test against a real 26AS PDF:

1. Upload your TRACES 26AS PDF.
2. Pick a tool-calling model from the dropdown (`gemma4`, `llama3.1`,
   `qwen3`, or `phi4-mini`).
3. Click **Run**. The agent loop should print progress to the console,
   then the Excel download link should appear in the right column.

Output lands in `staging\App\PASkills\outputs\` (or `Data\outputs\` when
you eventually launch via `PASkillsPortable.exe` in Phase 2).

## 5.  Known Phase-1 limitations

- **No launcher yet.** You launch the frozen `pa_skills.exe` directly;
  the `PASkillsPortable.exe` shell is generated in Phase 2 by the
  PortableApps Launcher Generator from `bundling\templates\appinfo.ini.tmpl`.
- **No Tesseract / Poppler.** The HSBC skill's OCR path is not yet wired
  end-to-end; HSBC and BoB tabs are intentionally absent from the UI.
- **`vendor/` is empty.** Git LFS still has nothing to track.
- **Icons are placeholders.** `bundling/icons/` has only a `README.md`;
  PyInstaller will warn that no icon was supplied and use its default.

## 6.  Reporting back

Once you've completed steps 0–4, drop a one-liner with:

1. Did step 0 (re-init) complete cleanly?
2. Did `build.py` exit 0?
3. Did the smoke test in §4 reach the "Extraction complete" message?

If any step failed, copy the last ~30 lines of the console output back to
the conversation and we'll triage from there.
