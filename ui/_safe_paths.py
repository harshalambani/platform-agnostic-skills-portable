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

import os
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


def _norm(p) -> str:
    """Absolute, symlink-free, case-normalised string form of a path."""
    return os.path.normcase(os.path.realpath(str(p)))


def resolve_input_file(path, extensions: Iterable[str],
                       folders: Iterable[Path] | None = None) -> Path:
    """Resolve `path`; return it only if it is an existing regular file, has
    one of `extensions`, and lies inside a known folder. Else UnsafePathError.

    The containment test is written as a plain string-prefix check on the
    normalised path, with every file-system use inside the guarded branch --
    the form CodeQL's py/path-injection query recognises as a sanitiser."""
    exts = tuple(e.lower() for e in extensions)
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        raise UnsafePathError("No file path given.")
    name = raw.replace("\\", "/").rsplit("/", 1)[-1] or raw     # display only
    try:
        norm = _norm(raw)
    except (OSError, RuntimeError, ValueError):
        raise UnsafePathError("That path could not be resolved.") from None
    if os.path.splitext(norm)[1].lower() not in exts:
        ext = os.path.splitext(norm)[1]
        raise UnsafePathError(
            f"{name}: expected a {' / '.join(exts)} file, not a {ext or 'extension-less'} file.")
    roots = []
    for f in (folders if folders is not None else known_folders()):
        try:
            roots.append(_norm(f))
        except (OSError, RuntimeError, ValueError):
            continue
    for root in roots:
        if norm.startswith(root.rstrip(os.sep) + os.sep):
            if not os.path.exists(norm):
                raise UnsafePathError(f"File not found: {name}")
            if not os.path.isfile(norm):
                raise UnsafePathError(f"{name} is not a regular file.")
            return Path(norm)
    raise UnsafePathError(
        f"{name} is outside the folders this tab may read from. Move it into the "
        f"Data or outputs folder (or list its folder under "
        f"'{KNOWN_FOLDERS_SETTING}' in the portable config).")


def resolve_run_target(path, suffix: str, review_name: str,
                       folders: Iterable[Path] | None = None) -> tuple[Path, Path | None]:
    """Resolve a run-folder-or-review-file value from a UI box.

    Accepts only (a) an existing folder whose name ends with `suffix`, or
    (b) an existing file called `review_name` directly inside such a folder,
    and only when it lies inside a known folder. Returns (run_dir, review_file
    or None). Anything else raises UnsafePathError before any file access.
    Same string-prefix sanitiser form as resolve_input_file."""
    raw = str(path or "").strip()
    if not raw or "\x00" in raw:
        raise UnsafePathError("No review file or run folder given.")
    try:
        norm = _norm(raw)
    except (OSError, RuntimeError, ValueError):
        raise UnsafePathError("That path could not be resolved.") from None
    roots = []
    for f in (folders if folders is not None else known_folders()):
        try:
            roots.append(_norm(f))
        except (OSError, RuntimeError, ValueError):
            continue
    for root in roots:
        if norm.startswith(root.rstrip(os.sep) + os.sep):
            if os.path.basename(norm) == os.path.normcase(review_name):
                run_s = os.path.dirname(norm)
                review_s = norm
            else:
                run_s, review_s = norm, None
            if not os.path.basename(run_s).endswith(os.path.normcase(suffix)):
                raise UnsafePathError(
                    f"That is not a {suffix} run folder (or its {review_name}).")
            if review_s is not None:
                if not os.path.isfile(review_s):
                    raise UnsafePathError(f"{review_name} not found in that run folder.")
                return Path(run_s), Path(review_s)
            if not os.path.isdir(run_s):
                raise UnsafePathError("That run folder does not exist.")
            return Path(run_s), None
    raise UnsafePathError(
        "That location is outside the folders this tab may read from. Pick a run "
        "from the list (it must be inside the outputs folder).")


def safe_staged_name(name: str) -> str:
    """A bare file name: no directory part, never '', '.', '..' or a drive."""
    bare = str(name or "").replace("\\", "/").rsplit("/", 1)[-1].strip()
    if bare in ("", ".", "..") or ":" in bare or "\x00" in bare:
        raise UnsafePathError("The file name cannot be used for a download copy.")
    return bare


def stage_copy(src: Path, staging: Path, folders: Iterable[Path] | None = None) -> Path:
    """Copy `src` into `staging`. Both ends are checked as normalised strings
    before the copy: the source must sit inside a known folder, the copy
    directly inside the staging folder."""
    src_s = _norm(os.fspath(src))
    roots = []
    for f in (folders if folders is not None else known_folders()):
        try:
            roots.append(_norm(f))
        except (OSError, RuntimeError, ValueError):
            continue
    staging_s = _norm(staging)
    os.makedirs(staging_s, exist_ok=True)
    dest = _norm(os.path.join(staging_s, safe_staged_name(src.name)))
    if not (dest.startswith(staging_s.rstrip(os.sep) + os.sep)
            and os.path.dirname(dest) == staging_s):
        raise UnsafePathError("The download copy would land outside the staging folder.")
    for root in roots:
        if src_s.startswith(root.rstrip(os.sep) + os.sep):
            shutil.copy2(src_s, dest)
            return Path(dest)
    raise UnsafePathError("The source file is outside the folders this tab may read from.")
