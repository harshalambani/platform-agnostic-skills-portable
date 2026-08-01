"""
ui/tabs/_entity_book.py -- shared entity -> GnuCash book prefill plumbing
(GnuCash book registry architecture, Phase 5 core, 2026-07-30 handover).

Gives the generic skill-tab renderer (ui/tabs/_generic.py) and any
hand-written tab a single place to:

  1. list entity-dropdown choices (`entity_choices()` -- moved here
     verbatim from `_generic._options_from_itr_entities()`, which used to
     live inline in _generic.py and is now a thin delegating wrapper there
     for backward compatibility with existing call sites, e.g.
     ui/tabs/itr_mapping_review.py's `_entity_choices()`);
  2. resolve an entity(+FY)'s registered book to a UI-safe string
     (`resolve_for_ui()`); and
  3. turn that into a Gradio update that never blanks a manually-picked
     path on a registry miss (`book_update()`).

`ui._book_registry.resolve_book()` is imported lazily inside the function
bodies (not at module import time) -- this matches the existing pattern in
`_generic._registry_book_fill()` and matters for PyInstaller-frozen builds,
where eager imports at module load time can pull in more than the frozen
build's import graph expects.
"""
from __future__ import annotations

import gradio as gr

from .. import _config


def entity_choices() -> list[tuple[str, str]]:
    """(label, entity_key) pairs from Data/itr/entities.yaml, for any
    entity dropdown (options_from: itr_entities). Reads fresh on every call
    so entities.yaml edits show up on refresh without a restart; gracefully
    empty when the file is absent (first run) or malformed (caller keeps
    the dropdown usable via allow_custom_value).

    Moved verbatim from `_generic._options_from_itr_entities()` -- same
    behaviour, same return shape (f"{key} ({status})", key) pairs, sorted.
    """
    path = _config.data_root_dir() / "itr" / "entities.yaml"
    if not path.is_file():
        return []
    try:
        import yaml
        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(raw, dict):
            return []
        return sorted(
            (f"{key} ({fields_.get('status', '?')})" if isinstance(fields_, dict) else key, key)
            for key, fields_ in raw.items()
        )
    except Exception:
        return []


def resolve_for_ui(entity_key: str | None, fy: str | None = None) -> str:
    """Resolve `entity_key`'s registered GnuCash book to a UI-safe string.

    Delegates to `_book_registry.resolve_book()`, then re-checks
    `.is_file()` (resolve_book() does not check disk existence). Returns
    `str(path)` on a hit, `""` on any miss -- unknown/empty entity, no
    registered book, or a registered path that no longer exists on disk.
    Never raises: wrapped in try/except so a malformed entities.yaml or an
    import failure degrades to "no prefill" rather than breaking the tab.
    """
    if not entity_key:
        return ""
    try:
        from .. import _book_registry  # noqa: PLC0415 -- see module docstring
        resolved = _book_registry.resolve_book(entity_key, fy or None)
    except Exception:
        return ""
    if resolved is not None and resolved.is_file():
        return str(resolved)
    return ""


def book_update(entity_key: str | None, fy: str | None = None):
    """Gradio update for a file component, driven by `resolve_for_ui()`.

    Returns `gr.update(value=<path>)` on a hit. Returns a bare
    `gr.update()` (no change) on a miss -- deliberately does NOT carry
    `value=""`, so a registry miss never blanks out a path the user already
    picked manually via Browse.
    """
    resolved = resolve_for_ui(entity_key, fy)
    if resolved:
        return gr.update(value=resolved)
    return gr.update()


def book_status(entity_key: str | None, fy: str | None = None) -> str:
    """One-line Markdown telling the user whether the book field below still
    needs their attention.

    The book field stays visible and overridable in every case (Browse always
    wins), but a user who has just picked an entity has no way of knowing
    whether the registry answered -- the file box simply fills, or doesn't.
    This says so out loud:

      - no entity picked        -> "" (renders as nothing)
      - registry hit            -> filled from the registry, nothing more to do
      - registry miss           -> pick the book manually, and how to fix it
                                   permanently (register it on Entities)
    """
    if not entity_key:
        return ""
    if resolve_for_ui(entity_key, fy):
        return (
            f"GnuCash book filled from the registry for **{entity_key}** -- "
            "you do not need to pick a file below."
        )
    return (
        f"No registered book for **{entity_key}** -- pick the GnuCash book "
        "below. Register it once on the **Entities** tab to skip this step "
        "next time."
    )


def book_status_update(entity_key: str | None, fy: str | None = None):
    """`book_status()` as a Gradio update, hiding the row when it is empty."""
    msg = book_status(entity_key, fy)
    return gr.update(value=msg, visible=bool(msg))
