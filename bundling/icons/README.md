# bundling/icons/

Application icons referenced by `appinfo.ini`. Phase 1 ships placeholder
files; Phase 2 swaps in the final art.

Required files (PortableApps.com Format):

- `appicon.ico`     — multi-resolution ICO (16/32/48/256 px)
- `appicon_16.png`
- `appicon_32.png`
- `appicon_75.png`  — used by the PortableApps menu
- `appicon_128.png`

Until the real artwork is supplied, `bundling/build.py` will warn and
generate solid-colour placeholder PNGs/ICO so the build still completes.
