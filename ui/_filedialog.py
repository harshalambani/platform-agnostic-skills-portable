"""
ui/_filedialog.py — native OS "Browse…" file picker with per-box path memory.

WHY THIS EXISTS
---------------
The file-input boxes on every skill tab are ``gr.File`` components, rendered in
the browser as ``<input type=file>``. Browser security strips the real
filesystem path (JavaScript only ever sees the bare filename) and forbids
setting the dialog's initial folder — so the app can never learn, or reopen at,
the folder the user actually picked from. There is nothing to remember.

This module sidesteps that by opening a **server-side native OS file dialog**.
That is legitimate here because PA Skills Portable is a local desktop app: the
Gradio server runs on the user's own machine (same process that already calls
``os.startfile`` in ``_config.open_in_file_manager``), so the picker opens on
their desktop. Unlike pywebview's ``create_file_dialog`` — which only works in
the native-window shell — the Win32 common dialog works in **both** the native
window and the plain browser-fallback mode.

Each box remembers its own last folder (boxes on the same tab are often
different folders — e.g. a ledger vs. a folder of contract notes), keyed by
``"<skill>.<input>"`` under ``last_dirs`` in ``Data/settings/config.yaml``.

The Win32 call itself (``_native_open_dialog``) is isolated so tests can
monkeypatch it — the dialog is modal and not headless-testable; the persistence
and validation layer around it is.
"""
from __future__ import annotations

import sys
from pathlib import Path

from . import _config

# Config key holding the per-box remembered folders: {"<skill>.<input>": "C:\\..."}.
_LAST_DIRS_KEY = "last_dirs"


# ---------------------------------------------------------------------------
# Per-box folder memory (config.yaml <- last_dirs).
# ---------------------------------------------------------------------------

def last_dir_for(box_key: str, *, path: Path | None = None) -> str | None:
    """Return this box's remembered folder if it still exists on disk, else None."""
    try:
        cfg = _config.load_portable_config(path)
    except Exception:
        return None
    folder = (cfg.get(_LAST_DIRS_KEY) or {}).get(box_key)
    if folder and Path(folder).is_dir():
        return folder
    return None


def remember_dir(box_key: str, folder: str, *, path: Path | None = None) -> None:
    """Persist this box's last folder. Best-effort — a write failure is not fatal
    (the memory just won't carry over); we never let it break a Browse click."""
    if not folder:
        return
    target = path or _config.PORTABLE_CONFIG_PATH
    # Never write into the read-only bundled template fallback.
    if target == getattr(_config, "_DEFAULT_TEMPLATE", None):
        return
    try:
        cfg = _config.load_portable_config(target)
        last = cfg.get(_LAST_DIRS_KEY)
        if not isinstance(last, dict):
            last = {}
        last[box_key] = folder
        cfg[_LAST_DIRS_KEY] = last
        _config.write_portable_config(cfg, target)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Native Win32 open-file dialog (isolated so tests can monkeypatch it).
# ---------------------------------------------------------------------------

def _build_filter(file_types: tuple[str, ...]) -> str:
    """Build a Win32 filter string ("Label\\0*.ext;*.ext\\0All files\\0*.*\\0")."""
    exts = [e if e.startswith(".") else f".{e}" for e in file_types]
    parts: list[str] = []
    if exts:
        pat = ";".join(f"*{e}" for e in exts)
        parts += [f"Supported files ({pat})", pat]
    parts += ["All files (*.*)", "*.*"]
    return "\x00".join(parts) + "\x00"


def _parse_ofn_buffer(raw: str) -> list[str]:
    """Parse the OPENFILENAME result buffer into absolute paths.

    Single selection -> one full path. Multiple selection -> the first entry is
    the directory and the rest are bare filenames to join onto it. (When exactly
    one file is multi-selected, Windows returns just its full path.)
    """
    parts = [p for p in raw.split("\x00") if p]
    if not parts:
        return []
    if len(parts) == 1:
        return [parts[0]]
    directory, names = parts[0], parts[1:]
    return [str(Path(directory) / n) for n in names]


def _native_open_dialog(
    *,
    initialdir: str | None,
    file_types: tuple[str, ...],
    multiple: bool,
    title: str,
) -> list[str]:
    """Open the OS file-open dialog and return the selected absolute path(s).

    Returns [] on cancel, error, or a non-Windows platform. This is the only
    part that touches Win32; tests monkeypatch it.
    """
    if not sys.platform.startswith("win"):
        return []
    import ctypes
    from ctypes import wintypes

    class OPENFILENAMEW(ctypes.Structure):
        _fields_ = [
            ("lStructSize", wintypes.DWORD),
            ("hwndOwner", wintypes.HWND),
            ("hInstance", wintypes.HINSTANCE),
            ("lpstrFilter", wintypes.LPCWSTR),
            ("lpstrCustomFilter", wintypes.LPWSTR),
            ("nMaxCustFilter", wintypes.DWORD),
            ("nFilterIndex", wintypes.DWORD),
            ("lpstrFile", wintypes.LPWSTR),
            ("nMaxFile", wintypes.DWORD),
            ("lpstrFileTitle", wintypes.LPWSTR),
            ("nMaxFileTitle", wintypes.DWORD),
            ("lpstrInitialDir", wintypes.LPCWSTR),
            ("lpstrTitle", wintypes.LPCWSTR),
            ("Flags", wintypes.DWORD),
            ("nFileOffset", wintypes.WORD),
            ("nFileExtension", wintypes.WORD),
            ("lpstrDefExt", wintypes.LPCWSTR),
            ("lCustData", wintypes.LPARAM),
            ("lpfnHook", wintypes.LPVOID),
            ("lpTemplateName", wintypes.LPCWSTR),
            ("pvReserved", wintypes.LPVOID),
            ("dwReserved", wintypes.DWORD),
            ("FlagsEx", wintypes.DWORD),
        ]

    OFN_EXPLORER = 0x00080000
    OFN_FILEMUSTEXIST = 0x00001000
    OFN_PATHMUSTEXIST = 0x00000800
    OFN_NOCHANGEDIR = 0x00000008        # keep the process CWD stable (app relies on it)
    OFN_ALLOWMULTISELECT = 0x00000200

    # Big buffer: a multi-select result is "dir\0name\0name\0...\0\0".
    buf = ctypes.create_unicode_buffer(1 << 16)

    ofn = OPENFILENAMEW()
    ofn.lStructSize = ctypes.sizeof(OPENFILENAMEW)
    try:
        ofn.hwndOwner = ctypes.windll.user32.GetForegroundWindow()
    except Exception:
        ofn.hwndOwner = None
    ofn.lpstrFilter = _build_filter(file_types)
    ofn.lpstrFile = ctypes.cast(buf, wintypes.LPWSTR)
    ofn.nMaxFile = len(buf)
    ofn.lpstrInitialDir = initialdir or None
    ofn.lpstrTitle = title
    ofn.Flags = OFN_EXPLORER | OFN_FILEMUSTEXIST | OFN_PATHMUSTEXIST | OFN_NOCHANGEDIR
    if multiple:
        ofn.Flags |= OFN_ALLOWMULTISELECT

    ok = ctypes.windll.comdlg32.GetOpenFileNameW(ctypes.byref(ofn))
    if not ok:
        return []
    return _parse_ofn_buffer(buf[:])


# ---------------------------------------------------------------------------
# Public: pick + validate + remember.
# ---------------------------------------------------------------------------

def pick_files(
    box_key: str,
    *,
    multiple: bool,
    file_types: tuple[str, ...],
    max_size_bytes: int,
    title: str = "Select a file",
    path: Path | None = None,
) -> tuple[list[str], list[str]]:
    """Open the native picker at this box's remembered folder, validate the
    picks, and (on success) remember the folder they came from.

    Returns ``(valid_paths, warnings)``. Because the native dialog bypasses the
    browser's file-type filter and the upload staging path, extension and size
    limits are re-enforced here on the real path (mirrors the caps the run
    handler applies to browser uploads). Rejected picks come back as warnings.
    """
    initial = last_dir_for(box_key, path=path)
    picked = _native_open_dialog(
        initialdir=initial, file_types=file_types, multiple=multiple, title=title,
    )

    allowed = {e.lower() if e.startswith(".") else f".{e.lower()}" for e in file_types}
    valid: list[str] = []
    warnings: list[str] = []
    for p in picked:
        pth = Path(p)
        if not pth.is_file():
            warnings.append(f"Not a file: {pth.name}")
            continue
        if allowed and pth.suffix.lower() not in allowed:
            warnings.append(
                f"{pth.name}: unsupported type (allowed: {', '.join(sorted(allowed))})"
            )
            continue
        try:
            size = pth.stat().st_size
        except OSError:
            size = 0
        if max_size_bytes and size > max_size_bytes:
            warnings.append(
                f"{pth.name}: too large ({size // (1024 * 1024)} MB — "
                f"max {max_size_bytes // (1024 * 1024)} MB)"
            )
            continue
        valid.append(str(pth))

    if valid:
        remember_dir(box_key, str(Path(valid[0]).parent), path=path)
    return valid, warnings
