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
    # NOTE: agents.* modules are NOT listed as hidden imports.
    # They are bundled as raw .py data files via the datas line below
    # (SRC_AGENTS -> "agents") and imported from _MEIPASS/agents/ at
    # runtime.  Listing them as hidden imports causes a "partially
    # compiled package" conflict: PyInstaller compiles __init__ but
    # fails to compile base_agent/registry (complex transitive imports
    # from langgraph break analysis), so at runtime Python finds the
    # compiled package but can't find the uncompiled sub-modules.
    #
    # Transitive deps of agents/ modules — since agents is excluded from
    # Analysis, PyInstaller won't discover these automatically.
    "yaml",
    "pandas",
    "langchain_core",
    "langchain_core.tools",
    # MSG parser dependency (local import inside _parse_msg).
    "extract_msg",
    # Native window (v2, #40). ui/webui.py imports these LAZILY (inside
    # _run_native_window / _native_window_available), so PyInstaller's static
    # analysis never sees them and their bundled hooks (collect the WebView2
    # managed DLLs + pythonnet runtime) would not fire. Declaring them here
    # pulls them into the graph. pywebview loads its Windows backend
    # (winforms/edgechromium) by name at runtime — collect_submodules below
    # grabs every webview.platforms.* module so the backend import resolves.
    "clr",
    "webview",
]

# ---------------------------------------------------------------------------
# Packages with data files / dynamic imports PyInstaller misses on its own.
# collect_all() returns (datas, binaries, hiddenimports) for each.
# ---------------------------------------------------------------------------
from PyInstaller.utils.hooks import collect_all, collect_submodules  # noqa: E402

_extra_datas = []
_extra_binaries = []
_extra_hiddenimports = []

# pywebview picks its backend via importlib at runtime (webview.platforms.*),
# which static analysis can't follow. Pull in every submodule so whichever
# backend the WebView2/EdgeChromium runtime selects is present in the bundle.
try:
    hiddenimports += collect_submodules("webview")
except Exception as _e:
    print(f"[paskills.spec] collect_submodules('webview') skipped: {_e}")

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
    # Standalone offline user guide (generated by scripts/gen_docs.py). Bundled
    # so the package ships a self-contained HTML guide alongside the in-app Help.
    (str(PROJECT_ROOT / "docs" / "USER-GUIDE.html"), "docs"),
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
    runtime_hooks=[str(PROJECT_ROOT / "bundling" / "rthook_agents.py")],
    excludes=[
        # Heavy optional deps we never call but PyInstaller may pull in.
        "tkinter",
        "matplotlib.tests",
        "numpy.tests",
        "pandas.tests",
        "scipy",
        # --- agents/ is shipped as raw .py data files (datas line above),
        # NOT as compiled modules.  Excluding the package from Analysis
        # prevents PyInstaller's FrozenImporter from claiming ownership
        # of the 'agents' namespace; at runtime PathFinder discovers
        # the .py files from _MEIPASS/agents/ instead.
        "agents",
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
    console=False,                 # Phase 2b: PortableApps launcher wraps the exe and hides the window
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
