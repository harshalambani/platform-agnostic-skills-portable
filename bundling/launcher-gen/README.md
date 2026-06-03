# Bundled PortableApps.com Launcher Generator

Self-hosted copy of the PortableApps.com Launcher Generator, used by
`build.py` (step 10) to produce the `PASkillsPortable.exe` wrapper.

## Why self-hosted?

The PortableApps.com CDN (`download.portableapps.com`) rejects TLS
handshakes from GitHub-hosted runner IPs, so CI cannot download the
generator at build time. Bundling it in the repo ensures every build —
local and CI — produces a complete release zip with the wrapper exe.

## Version

- **Version:** 2.2.4
- **Upstream URL:** https://portableapps.com/apps/development/portableapps.com_launcher
- **Download URL:** https://download.portableapps.com/portableapps/PortableApps.comLauncher/PortableApps.comLauncher_2.2.4.paf.exe
- **License:** GPL-2.0 (see `2.2.4/App/NSIS/COPYING`)

## Contents

The `2.2.4/` directory contains the extracted Launcher Generator with
`App/Manual/` and `Data/` excluded (documentation and runtime data not
needed for compilation).

Key files:
- `2.2.4/PortableApps.comLauncherGenerator.exe` — the generator
- `2.2.4/App/NSIS/makensis.exe` — NSIS compiler used internally

## Re-vendoring

To update to a newer version:

1. Download the new `.paf.exe` from the upstream URL
2. Extract: `PortableApps.comLauncher_X.Y.Z.paf.exe /S /D=<scratch>`
3. Copy to `bundling/launcher-gen/X.Y.Z/`, excluding `App/Manual/` and `Data/`
4. Update this README with the new version and URL
5. Update the `LAUNCHER_GEN_HINTS` path in `bundling/build.py`
