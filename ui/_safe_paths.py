"""
SEC-19 -- one shared guard for user-supplied file paths (CodeQL py/path-injection).

A path typed or picked in the UI reaches open()/shutil.copy2() in the review
tabs. `resolve_input_file` resolves it and refuses anything that is not a
regular file of the expected type inside a known folder, with a plain message
instead of a traceback. `safe_staged_name` / `stage_copy` keep a staged copy
inside the staging folder whatever the source name contains.

The known folders live in ONE place: `KNOWN_FOLDERS_SETTING` names an optional
list in the portable config; without it the default is the app's Data folder
tree (`data_root_dir()`) plus the configured outputs folder (`output_dir()`).
"""
from __future__ import annotations

import shutil
from pathlib import Path
from typing import Iterable

from ui import _config as _config_mod

# Optional portable-config key: a list of extra folders review files may live in.
KNOWN_FOLDERS_SETTING = "review_known_folders"


class UnsafePathError(ValueError):
    """The path was refused. str(e) is a user-facing message (no traceback)."""


def known_folders() -> list[Path]:
    """The folders a review input/journal may be read from (resolved)."""
    folders: list[Path] = [_config_mod.data_root_dir()]
    try:
        folders.append(_config_mod.output_dir())
    except Exception:  # noqa: BLE001 -- an unreadable config must not widen access
        pass
    try:
        extra = _config_mod.load_portable_config().get(KNOWN_FOLDERS_SETTING) or []
    except Exception:  # noqa: BLE001
        extra = []
    folders.extend(Path(str(p)) for p in extra if str(p).strip())
    out: list[Path] = []
    for f in folders:
        try:
            out.append(f.resolve())
        except OSError:
            continue
    return out


def _inside(path: Path, folder: Path) -> bool:
    try:
        path.relative_to(folder)
        return True
    except ValueError:
        return False


def resolve_input_file(path, extensions: Iterable[str],
                       folders: Iterable[Path] | None = None) -> Path:
    """Resolve `path`; return it only if it is an existing regular file, has
    one of `extensions`, and lies inside a known folder. Else UnsafePathError."""
    exts = tuple(e.lower() for e in extensions)
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        raise UnsafePathError("No file path given.")
    try:
        p = Path(raw).resolve()
    except (OSError, RuntimeError, ValueError):
        raise UnsafePathError("That path could not be resolved.") from None
    name = p.name
    if p.suffix.lower() not in exts:
        raise UnsafePathError(
            f"{name}: expected a {' / '.join(exts)} file, not a {p.suffix or 'extension-less'} file.")
    allowed = [Path(f).resolve() for f in (folders if folders is not None else known_folders())]
    if not any(_inside(p, f) for f in allowed):
        raise UnsafePathError(
            f"{name} is outside the folders this tab may read from. Move it into the "
            f"Data or outputs folder (or list its folder under "
            f"'{KNOWN_FOLDERS_SETTING}' in the portable config).")
    if not p.exists():
        raise UnsafePathError(f"File not found: {name}")
    if not p.is_file():
        raise UnsafePathError(f"{name} is not a regular file.")
    return p


def safe_staged_name(name: str) -> str:
    """A bare file name: no directory part, never '', '.', '..' or a drive."""
    bare = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if bare in ("", ".", "..") or ":" in bare or "\x00" in bare:
        raise UnsafePathError("The file name cannot be used for a download copy.")
    return bare


def stage_copy(src: Path, staging: Path) -> Path:
    """Copy `src` into `staging`; the copy is verified to sit directly in it."""
    staging = Path(staging).resolve()
    staging.mkdir(parents=True, exist_ok=True)
    dest = (staging / safe_staged_name(src.name)).resolve()
    if dest.parent != staging:
        raise UnsafePathError("The download copy would land outside the staging folder.")
    shutil.copy2(src, dest)
    return dest
