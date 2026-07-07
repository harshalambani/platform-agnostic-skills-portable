# bundling/icons/

Application icons referenced by `appinfo.ini` and used as the PyInstaller
exe icon (`bundling/paskills.spec`).

These are the **real v2 designer artwork** (dropped in for the v2 release).
The build copies them into `staging/App/AppInfo/` verbatim; when real
artwork is present it overrides the placeholder generator in
`bundling/build.py` (`_ensure_appinfo_icons`).

Files (PortableApps.com Format):

- `appicon.ico`     — multi-resolution ICO (16/32/48/64/128/256 px)
- `appicon_16.png`
- `appicon_32.png`
- `appicon_75.png`  — used by the PortableApps menu (downscaled from the master)
- `appicon_128.png`
- `appicon_1024.png` — full-resolution master, kept for regenerating sizes

## Regenerating sizes

`appicon_16/32/128.png` are the designer's per-size PNG exports;
`appicon_75.png` is downscaled from `appicon_1024.png` with LANCZOS.

> **Do NOT run `bundling/generate_icons.py` against this directory.** That
> script renders the old placeholder motif (gear + sparkle on #0A0A0A) from
> a Pillow-drawn master and will **overwrite** the real artwork. To resize
> from the master instead, downscale `appicon_1024.png` with Pillow LANCZOS.
