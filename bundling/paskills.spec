# -*- mode: python ; coding: utf-8 -*-
#
# bundling/paskills.spec — PyInstaller spec for PA Skills Portable.
#
# Mode:        --onedir (per spec §10.3 — faster start, easier to debug).
# Entry point: ui/webui.py
# Output:      dist\pa_skills\pa_skills.exe (+ _internal\)
# Hidden imports:  LangChain / LangGraph use lazy provider lookup that
#                  PyInstaller's static analysis misses. Pre-declared per
#                  spec §10.3.
# Data:        agents/ tree (mirrored from upstream by build.py) and
#              ui/ Python sources (already discovered, but bundled
#              explicitly to keep the spec self-documenting).
#
# Invoked by:  bundling/build.py.
# Run directly: pyinstaller bundling\paskills.spec --clean --noconfirm
#               (assumes cwd = repo root, venv with deps + pyinstaller active)

from pathlib import Path

# PyInstaller injects `workpath`, `distpath`, etc. into the spec namespace,
# but not the spec file's own path. Compute the project root from cwd, which
# build.py guarantees is the repo root.
PROJECT_ROOT = Path.cwd()
SRC_AGENTS   = PROJECT_ROOT / "src" / "agents"
UI_DIR       = PROJECT_ROOT / "ui"
ICON_PATH    = PROJECT_ROOT / "bundling" / "icons" / "appicon.ico"

# ---------------------------------------------------------------------------
# Hidden imports — see spec §10.3.
# ---------------------------------------------------------------------------
hiddenimports = [
    # LangChain / LangGraph providers loaded lazily by name.
    "langchain_ollama",
    "langchain_openai",
    "langgraph.prebuilt",
    "langgraph.graph",
    # PDF / OCR.
    "pdfplumber",
    "pypdfium2",
    "pytesseract",
    # Spreadsheets.
    "openpyxl",
    # UI.
    "gradio",
    "gradio_client",
    # Skill modules (imported by string at runtime via the agent loop).
    "agents",
    "agents.base_agent",
    "agents.skill_26as",
    "agents.skill_26as.agent",
    "agents.skill_26as.tools",
    "agents.skill_bob",
    "agents.skill_bob.agent",
    "agents.skill_bob.tools",
    "agents.skill_hsbc",
    "agents.skill_hsbc.agent",
    "agents.skill_hsbc.tools",
]

# ---------------------------------------------------------------------------
# Packages with data files / dynamic imports PyInstaller misses on its own.
# collect_all() returns (datas, binaries, hiddenimports) for each.
# ---------------------------------------------------------------------------
from PyInstaller.utils.hooks import collect_all  # noqa: E402

_extra_datas = []
_extra_binaries = []
_extra_hiddenimports = []

for _pkg in (
    "gradio",
    "gradio_client",
    "safehttpx",
    "groovy",
    "starlette",
    "fastapi",
    "uvicorn",
    "pdfplumber",
):
    try:
        _d, _b, _h = collect_all(_pkg)
        _extra_datas += _d
        _extra_binaries += _b
        _extra_hiddenimports += _h
    except Exception as _e:
        print(f"[paskills.spec] collect_all('{_pkg}') skipped: {_e}")

hiddenimports += _extra_hiddenimports


# ---------------------------------------------------------------------------
# Data files — bundled into _internal\ at runtime.
# ---------------------------------------------------------------------------
datas = [
    # The whole agents/ tree, mirrored verbatim from upstream.
    (str(SRC_AGENTS), "agents"),
    # The ui/ Python sources — already discoverable but bundled explicitly
    # so non-py assets (markdown, etc.) inside ui/ are carried along.
    (str(UI_DIR), "ui"),
    # Template default settings copied to %PAL:DataDir% on first run.
    (str(PROJECT_ROOT / "bundling" / "templates" / "DefaultData"), "DefaultData"),
]

# Drop directories not present (e.g., when running spec in a partial tree).
datas = [(src, dst) for (src, dst) in datas if Path(src).exists()]
datas += _extra_datas


block_cipher = None

a = Analysis(
    [str(PROJECT_ROOT / "ui" / "webui.py")],
    pathex=[str(PROJECT_ROOT / "src"), str(PROJECT_ROOT)],
    binaries=_extra_binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Heavy optional deps we never call but PyInstaller may pull in.
        "tkinter",
        "matplotlib.tests",
        "numpy.tests",
        "pandas.tests",
        "scipy",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="pa_skills",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # PortableApps doesn't require UPX; keep symbols
    console=True,                  # console required for uvicorn logger; PA launcher hides it in Phase 2
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="pa_skills",
)
