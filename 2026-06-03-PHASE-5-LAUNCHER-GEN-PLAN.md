# Phase 5 — Launcher Generator CI plan (stashed)

> **Status:** stashed, not active. Drafted 2026-06-03 in a session that was
> incorrectly oriented to Phase 4 priorities and instead designed a CI fix
> that belongs in Phase 5. Reroute is per the 2026-05-27 roadmap doc set
> (`2026-05-27-PHASE-4B-PLAN.md`, `2026-05-27-PHASE-4B-STARTER-PROMPT.md`,
> `2026-05-27-ROADMAP-BEYOND-4B.md`). Current sequencing: **4B → 4C → 4D →
> Phase 5 → 6 → 7**. Phase 4C is complete in a sibling session; 4D is next.
> Do not start this work until 4D has landed and Phase 5 is formally opened.

## 1. Problem (carry-over from Phase 3 CI work)

`.github/workflows/release.yml` tries to download
`https://download.portableapps.com/.../PortableApps.comLauncher_2.2.4.paf.exe`
during CI so the build can produce the `PASkillsPortable.exe` wrapper.
The download is unreliable on GitHub-hosted Windows runners (TLS handshake
rejected by the portableapps.com CDN, per `CmdResult/2026-05-24-next-session-prompt.md`).
Commit `0791ac3` added a curl-then-Invoke-WebRequest fallback, and if both
fail the workflow sets `SKIP_LAUNCHER=true` and emits a wrapperless zip via
a `Compress-Archive` fallback step. Result: CI release zips may be missing
the `PASkillsPortable.exe` wrapper, which defeats the PortableApps Format
contract for end users.

Goal: make CI produce a complete-with-wrapper zip every time, with zero
dependency on the portableapps.com CDN at build time.

## 2. Inspection checklist before executing

1. Re-read `.github/workflows/release.yml` and confirm the curl/IWR/skip
   plumbing is still in place (it was at the time this plan was drafted).
2. Open the most recent v0.3.x (or later) workflow run on GitHub and grep
   the log for `Could not download Launcher Generator` or
   `Build will use --skip-launcher` to confirm whether the fallback is
   firing routinely vs. occasionally. Drives urgency, not strategy.
3. Verify the v0.3.1 (or whatever the latest is at Phase-5 start) release
   asset on GitHub: does the zip contain `PASkillsPortable.exe` at root?
   If yes, CI got lucky on the download. If no, the fix is necessary.
4. Measure the extracted size of the Launcher Generator and its supporting
   files. Drives the Option A vs Option B decision (plain git vs LFS).
5. Confirm GPL-v2 redistribution requirement for the Launcher Generator
   binary is satisfied by a NOTICE update plus a link to upstream source.

## 3. Options considered

**A. Self-host the extracted Launcher Generator in the repo, plain git.**
Pros: deterministic, fully offline-capable build, no CDN dependency,
simplest CI change (delete the download step). Cons: bloats the repo by
the extracted size; small binary-churn risk on PA Launcher upgrades; GPL
credit needed in NOTICE. **Default choice if extracted size ≤ ~10 MB.**

**B. Self-host the extracted Launcher Generator via Git LFS.**
Same robustness as A, no repo-clone bloat. Costs LFS bandwidth on every
CI run; slightly more setup. **Fallback for A if extracted size is large.**

**C. Commit only the `.paf.exe` installer (plain or LFS), extract in CI.**
Smaller committed payload (`.paf.exe` is ~3–5 MB). Adds a silent-extract
step in CI (already wired, so cheap) but introduces a moving part if the
installer's extraction layout changes across PA Launcher versions.
Rejected vs A/B because the saving is marginal and the moving part isn't.

**D. Mirror the Launcher Generator to a GitHub Release on this repo
(e.g. tag `tooling-launcher-gen-2.2.4`), CI downloads from there.**
Keeps the source tree clean; GitHub's CDN is reliable from Actions; can
be updated independently. Still requires a network call at build time;
one more thing to maintain. **Fallback for A if you want zero binaries
in the source tree.**

**E. Mirror to an external CDN** (Cloudflare R2, S3, GitHub Pages).
Strictly worse than D for our scale (cost, infra, secrets).

**F. Use a package manager** (Chocolatey / Scoop / winget) in CI.
Possibly available via Scoop's `extras` bucket; not confirmed. Still a
network dep on a third-party CDN; same flake surface as today, just
relocated. Worth a five-minute check during execution, not a strategy.

**G. `actions/cache`** the downloaded binary after first successful pull.
Useful complement, useless as a standalone fix — needs at least one
successful download to seed the cache.

**H. Accept the fallback permanently.** Off the table by Phase 5 scope.

## 4. Recommendation

**Option A** (self-host extracted, plain git), with **Option B** (LFS)
as the escalation if extracted size is too large, and **Option D**
(GitHub-release mirror) as the second-choice if you'd rather keep the
source tree clean and tolerate a residual network dep. Optionally pair
with **Option G** (`actions/cache`) purely as a speedup, not as a fix.

Reasoning: every option that keeps the download in the build (C, D, E,
F) leaves a flake surface — D and F move it to better CDNs but don't
eliminate it. A and B eliminate it entirely by making the build
self-contained, which also makes local builds reproducible without an
install step. The Launcher updates rarely (current 2.2.4 has been
stable), so the maintenance cost of vendoring it is low. A is preferred
over B because we already have LFS overhead on `vendor/tesseract/` and
`vendor/poppler/`; adding more LFS objects increases CI bandwidth usage.

## 5. Concrete steps when Phase 5 opens (Option A path)

1. Locally: download `PortableApps.comLauncher_2.2.4.paf.exe` from
   portableapps.com, run silently (`/S /D=<scratch>`) to extract, and
   identify the minimum set of files needed for
   `PortableApps.comLauncherGenerator.exe` to run (its own folder plus
   any NSIS plugins it loads).
2. Measure extracted size. If ≤ ~10 MB, commit plain. If larger, add an
   LFS pattern for `bundling/launcher-gen/**` to `.gitattributes`.
3. Drop the extracted tree under `bundling/launcher-gen/<version>/`,
   with a `README.md` documenting upstream URL, version, installer
   SHA-256, license, and the re-vendor procedure for upgrades.
4. Add `bundling/launcher-gen/<version>/` to `_find_launcher_generator`'s
   search hints in `bundling/build.py` (or have the workflow export
   `PASKILLS_LAUNCHER_GEN`), so both local and CI builds find it without
   a flag.
5. Update `release.yml`: delete the entire "Install PortableApps.com
   Launcher Generator" step; delete the `SKIP_LAUNCHER` / fallback-zip
   plumbing; simplify the build invocation. Drop the conditional
   Release-body text noting the fallback.
6. Update `NOTICE` to credit the PortableApps.com Launcher and link
   the upstream source / GPL-v2 license.
7. Update `CHANGELOG.md` for the Phase-5 entry.
8. New dated notes file `YYYY-MM-DD-PHASE-5-NOTES.md` summarising the
   vendor location, upgrade procedure, and why the CI download path was
   removed.

If Option B is chosen instead: same as A plus the `.gitattributes` LFS
pattern. If Option D is chosen instead: create the tooling release
manually (one-time), edit `release.yml` to pull from that asset URL,
delete the fallback/skip plumbing, no source-tree binary commit.

## 6. Verification

1. Land on a branch; push tag `vX.Y.Z-rc1`. The existing workflow treats
   anything with a hyphen as `prerelease: true`.
2. Workflow on Actions: pass criteria is finishes green; `step 10` log
   line shows the generator being invoked; `step 11` writes the zip;
   the "Fallback zip" step is skipped because `env.SKIP_LAUNCHER` is
   empty or false.
3. Download the resulting zip from the prerelease, unzip, confirm
   `PASkillsPortable.exe` is present at the root next to `App/` and
   `Data/`. Sanity-check size > 0.
4. Optional second layer: on Windows, `Expand-Archive` and double-click
   `PASkillsPortable.exe` to confirm it launches the wrapped
   `pa_skills.exe` with no console window.
5. Delete the rc tag and prerelease once green; promote to the real
   `vX.Y.Z` only when ready to ship.

## 7. Open questions (TBD — needed before execution)

1. **A vs B vs D?** Default A unless told otherwise; will measure
   extracted size first and only escalate to B if plain git is too big.
   **TBD.**
2. **Recent CI run log** confirming the fallback is actually firing
   today, vs. acting on a yesterday-stated assumption. (Drives urgency,
   not strategy.) **TBD.**
3. **Tooling release-tag namespace** if going with D. Proposal:
   `tooling-launcher-gen-2.2.4`, clearly distinct from the `v*` pattern
   used for user-facing releases. **TBD.**
4. **PortableApps Launcher version pin.** Plan currently assumes
   2.2.4 (the version the CI workflow hard-codes today). Confirm that's
   still the intended pin at Phase-5 start, or pick a newer one if
   upstream has shipped. **TBD.**

## 8. Cross-references

- Original plan was drafted in a Phase-4-misrouted Cowork session on
  2026-06-03. The full plan text and tradeoffs live in this file; the
  upstream chat that produced it does not need to be re-read.
- Roadmap doc set committed 2026-05-27:
  - `2026-05-27-PHASE-4B-PLAN.md`
  - `2026-05-27-PHASE-4B-STARTER-PROMPT.md`
  - `2026-05-27-ROADMAP-BEYOND-4B.md`
- Relevant files at time of stashing:
  - `.github/workflows/release.yml` (steps "Install PortableApps.com
    Launcher Generator", "Run build.py", "Fallback zip (if launcher was
    skipped)").
  - `bundling/build.py` lines 681–714 (`_find_launcher_generator`),
    717–750 (`step10_launcher_gen`), 759–801 (`step11_zip`).
  - `CmdResult/2026-05-24-next-session-prompt.md` (original failure-mode
    description).
