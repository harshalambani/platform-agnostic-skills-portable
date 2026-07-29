"""
ui/_book_registry.py -- app-side book resolution layer (GnuCash book
registry architecture, Phase 2, 2026-07-29 handover).

Phase 1 (merged) added `EntityProfile.books: dict[str, str]` (FY string,
e.g. "2025-26" -> path to a `.gnucash` file) to
`src/agents/skill_itr_workbook/scripts/configs.py`, round-tripped through
`load_entities()`/`dump_entities()`, plus the pure helpers
`derive_workbook_match()`/`effective_workbook_match()`. That module stays
portable and inert -- nothing there reads/writes the live entities.yaml.

This module is the app-side counterpart: it gives every tab ONE place to
resolve/list/set an entity's book, anchored via `ui._config.data_root_dir()`
the same way every other stateful ui/tabs/*.py helper is (see
`ui/tabs/itr_mapping_review.py`'s `_entity_book_path()`/`_workbook_match_map()`
and `ui/tabs/itr_entities.py`'s save path). Importing the skill's `configs`
module (for `load_entities`/`dump_entities`/`EntityProfile`) from here is
fine and expected -- only `configs.py` itself must stay skill-side/portable.

FY vs AY -- do NOT conflate: `books` is keyed by Financial Year strings like
"2025-26" (zero-padded `YYYY-YY`, so lexical string sort == chronological
sort). entities.yaml separately keys `regime_by_ay`/`audit_case_by_ay` by
Assessment Year ("2026-27" for FY 2025-26) -- those are untouched here.

SCOPE DISCIPLINE (Phase 2): this module is purely additive plumbing. It is
NOT wired into any live tab in this commit -- not `_entity_book_path()`, not
any `gr.File` picker. Nothing's runtime behaviour changes from this commit
alone; rewiring existing call sites onto this module is a later phase.

Legacy `book:` back-compat -- DESIGN DECISION (flagged, not merely assumed):
there is no `book` field on `EntityProfile`; the single-book path lives (if
present at all) as a raw top-level `book:` key in entities.yaml, read today
by `_entity_book_path()` in `ui/tabs/itr_mapping_review.py`. `resolve_book()`
mirrors that function's tolerant raw-YAML read as its last-resort fallback
(after the `books` dict is exhausted), so callers migrating off
`_entity_book_path()` get a strict superset of its behaviour. `list_books()`
deliberately does NOT synthesize a fake FY key for a legacy `book:` value --
it stays truthful to the `books` dict only (empty dict if the entity has no
`books`, regardless of whether a legacy `book:` exists). This asymmetry is
intentional: `list_books()` answers "what FY-keyed books are registered",
`resolve_book()` answers "what book should I use right now" -- and a legacy
single book was never FY-keyed in the first place, so inventing a key for it
in `list_books()` would misrepresent the data. Flag to the requester if this
reading is wrong.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

from . import _config as _config_mod

_FY_RE = re.compile(r"^\d{4}-\d{2}$")

_SCRIPTS_ON_PATH = False


def _ensure_itr_scripts_importable() -> None:
    """Put the ITR Workbook skill's flat scripts/ dir on sys.path once, the
    same way ui/tabs/itr_mapping_review.py and ui/tabs/itr_entities.py do
    (frozen-build-safe: resolved via the agents.skill_itr_workbook package,
    not a source-tree-relative guess)."""
    global _SCRIPTS_ON_PATH
    if _SCRIPTS_ON_PATH:
        return
    import agents.skill_itr_workbook as _itr_pkg  # noqa: PLC0415

    scripts_dir = Path(_itr_pkg.__file__).resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    _SCRIPTS_ON_PATH = True


def _configs_mod():
    """Import and return the skill's configs module (importable in both
    source and frozen builds)."""
    _ensure_itr_scripts_importable()
    import configs  # noqa: PLC0415
    return configs


def _default_entities_path() -> Path:
    return _config_mod.data_root_dir() / "itr" / "entities.yaml"


def _is_valid_fy(fy: str) -> bool:
    """FY strings are zero-padded 'YYYY-YY', e.g. '2025-26'. Deliberately
    narrow -- catches the AY-instead-of-FY mixup ('2026-27' meant as an AY)
    only by shape, not by value; both FY and AY strings share this shape, so
    this cannot tell them apart on its own. Callers are responsible for
    passing an actual FY."""
    return bool(_FY_RE.match(fy))


def _load_entities_tolerant(entities_path: Path) -> dict:
    """{entity_key: EntityProfile}, empty dict if entities_path is absent,
    unreadable, or malformed -- never raises. Mirrors the tolerant-read
    convention used throughout ui/tabs/*.py's own entities.yaml helpers."""
    if not entities_path.is_file():
        return {}
    try:
        return _configs_mod().load_entities(entities_path)
    except Exception:
        return {}


def _legacy_book_path(entity_key: str, entities_path: Path) -> Path | None:
    """Optional per-entity legacy `book:` path, read straight off the raw
    YAML dict -- mirrors `_entity_book_path()` in
    ui/tabs/itr_mapping_review.py verbatim (that function predates `books:`
    and there is no `book` field on EntityProfile, so `load_entities()` never
    surfaces it). Returns None on any missing/malformed file, unknown key, or
    absent/blank `book:` -- never raises."""
    if not entities_path.is_file():
        return None
    try:
        raw = yaml.safe_load(entities_path.read_text(encoding="utf-8")) or {}
    except Exception:
        return None
    if not isinstance(raw, dict):
        return None
    fields = raw.get(entity_key)
    if not isinstance(fields, dict):
        return None
    book = fields.get("book")
    if not book or not str(book).strip():
        return None
    return Path(str(book))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def resolve_book(
    entity_key: str,
    fy: str | None = None,
    entities_path: str | Path | None = None,
) -> Path | None:
    """Resolve the GnuCash book path to use for `entity_key`.

    Precedence:
      1. `books[fy]` if `fy` is given and present in the entity's `books`.
      2. Else the newest FY in `books` (max of the FY keys, via descending
         string sort -- safe because FY keys are zero-padded 'YYYY-YY', so
         lexical order == chronological order), if `books` is non-empty.
      3. Else the entity's legacy single `book:` path, if entities.yaml has
         one (raw top-level key, not an EntityProfile field -- see
         `_legacy_book_path()`).
      4. Else None.

    Returns None gracefully on any missing/malformed file or unknown
    `entity_key` -- never raises.
    """
    path = Path(entities_path) if entities_path is not None else _default_entities_path()
    entities = _load_entities_tolerant(path)
    profile = entities.get(entity_key)

    if profile is not None and profile.books:
        if fy is not None and fy in profile.books:
            return Path(profile.books[fy])
        newest_fy = sorted(profile.books.keys(), reverse=True)[0]
        return Path(profile.books[newest_fy])

    legacy = _legacy_book_path(entity_key, path)
    if legacy is not None:
        return legacy

    return None


def list_books(
    entity_key: str,
    entities_path: str | Path | None = None,
) -> dict[str, str]:
    """{FY: path} for every book registered on `entity_key`, or {} if the
    entity has no `books` (or doesn't exist / the file is missing). A
    legacy single `book:` is deliberately NOT synthesized into a fake FY key
    here -- see the module docstring's "Legacy `book:` back-compat" section.
    """
    path = Path(entities_path) if entities_path is not None else _default_entities_path()
    entities = _load_entities_tolerant(path)
    profile = entities.get(entity_key)
    if profile is None or not profile.books:
        return {}
    return dict(profile.books)


def set_book(
    entity_key: str,
    fy: str,
    path: str | Path,
    entities_path: str | Path | None = None,
) -> None:
    """Register `path` as `entity_key`'s book for financial year `fy`.

    Loads entities.yaml, sets `books[fy] = str(path)` on that entity, and
    persists via `configs.dump_entities()` -- exactly how
    `ui/tabs/itr_entities.py` already saves entity edits. Raises ValueError
    on a malformed `fy` (must match 'YYYY-YY', e.g. "2025-26") or an unknown
    `entity_key`; raises whatever `load_entities`/file I/O raises on a
    missing/malformed entities.yaml (this is a write path, so failures are
    surfaced rather than swallowed, unlike the read-side helpers above).
    """
    if not _is_valid_fy(fy):
        raise ValueError(f"fy {fy!r} is not a valid Financial Year string (expected 'YYYY-YY', e.g. '2025-26').")

    entities_file = Path(entities_path) if entities_path is not None else _default_entities_path()
    configs = _configs_mod()
    entities = configs.load_entities(entities_file)

    profile = entities.get(entity_key)
    if profile is None:
        raise ValueError(f"Unknown entity_key {entity_key!r} -- not found in {entities_file}.")

    profile.books[fy] = str(path)
    entities_file.write_text(configs.dump_entities(entities), encoding="utf-8")
