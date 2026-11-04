"""Fast, cached YAML reads for the UI-build path.

Start-up reads the same few hundred small YAML files (skill manifests, bank
manifests, the portable config, tax-rule metadata).  Two things make that
cheaper without changing what any caller sees:

* the libyaml-backed ``CSafeLoader`` is used when PyYAML ships it (it parses
  the same documents as ``SafeLoader``, several times faster), with a silent
  fall back to the pure-Python ``SafeLoader``;
* parsed results are cached in-process, keyed by the resolved path plus the
  file's ``st_mtime_ns`` and ``st_size``, so a file that is edited or saved
  (including through the UI) is read again on the next call.  Every call
  returns a deep copy, so a caller that mutates the result cannot change
  what the next caller gets.

Read paths only.  Anything that writes YAML keeps using ``yaml.safe_dump``
directly.
"""
from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

# Resolved once at import; tests monkeypatch this to prove the fallback.
_LOADER: type = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

_cache: dict[tuple[str, int, int], Any] = {}


def _parse(text: str) -> Any:
    return yaml.load(text, Loader=_LOADER)  # noqa: S506 - safe loader only


def load_yaml_file(path: str | Path) -> Any:
    """Parse the YAML file at ``path`` (utf-8) and return a private copy.

    Raises ``OSError`` if the file cannot be read and ``yaml.YAMLError`` if it
    cannot be parsed -- the same failures ``yaml.safe_load`` would raise.
    Failures are never cached.
    """
    p = Path(path)
    st = p.stat()
    key = (str(p.resolve()), st.st_mtime_ns, st.st_size)
    if key in _cache:
        return copy.deepcopy(_cache[key])
    value = _parse(p.read_text(encoding="utf-8"))
    # Drop stale generations of the same path so the cache cannot grow
    # without bound when a file is saved repeatedly.
    for old in [k for k in _cache if k[0] == key[0]]:
        del _cache[old]
    _cache[key] = value
    return copy.deepcopy(value)


def clear_cache() -> None:
    _cache.clear()
