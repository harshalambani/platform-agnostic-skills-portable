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


def _as_keys(entity_keys) -> list[str]:
    """Normalise a multiselect dropdown's value to a list of entity keys.

    Gradio hands back a list when `multiselect=True`, but a bare string when
    the component was built without it (and `None` when nothing is picked),
    so every caller would otherwise have to re-do this dance.
    """
    if not entity_keys:
        return []
    if isinstance(entity_keys, str):
        entity_keys = [entity_keys]
    return [k for k in (str(e).strip() for e in entity_keys) if k]


def books_update(entity_keys, fy: str | None = None):
    """Gradio update for a multi-book path textbox -- one path per line.

    The multi-book counterpart of `book_update()`, for inputs that take
    several books at once (Inter-entity Matrix). Entities with no resolvable
    book are skipped rather than written as a blank line, so the field only
    ever holds paths that exist; `books_status()` names the ones left out.

    Follows `book_update()`'s rule on the empty case: when nothing resolves,
    return a bare `gr.update()` rather than `value=""`, so clearing the
    entity dropdown never wipes paths the user picked by hand.
    """
    resolved = [p for p in (resolve_for_ui(k, fy) for k in _as_keys(entity_keys)) if p]
    if resolved:
        return gr.update(value="\n".join(resolved))
    return gr.update()


def books_status(entity_keys, fy: str | None = None) -> str:
    """One-line Markdown naming the picked entities with no registered book.

    Same principle as `book_status()`: resolved books land in a visible field
    and need no announcement, so only the gap is worth saying. The partial
    case is the one this exists for -- pick three people, get two paths, and
    without this the third's absence is invisible.
    """
    keys = _as_keys(entity_keys)
    missing = [k for k in keys if not resolve_for_ui(k, fy)]
    if not missing:
        return ""
    names = ", ".join(f"**{k}**" for k in missing)
    if len(missing) == len(keys):
        return (
            f"No registered book for {names} -- add the books below, or "
            "register them once on the **Entities** tab."
        )
    return (
        f"Filled in {len(keys) - len(missing)} of {len(keys)} books. No "
        f"registered book for {names} -- add those below, or register them "
        "once on the **Entities** tab."
    )


def books_status_update(entity_keys, fy: str | None = None):
    """`books_status()` as a Gradio update, hiding the row when it is empty."""
    msg = books_status(entity_keys, fy)
    return gr.update(value=msg, visible=bool(msg))


def book_status(entity_key: str | None, fy: str | None = None) -> str:
    """One-line Markdown for the *miss* case only -- otherwise "".

    A registry hit says nothing, deliberately. The book field is a path
    textbox, so a hit writes the resolved path in plain sight: the filled
    field is its own acknowledgement, and a line underneath announcing that
    it filled is just repeating what the user can already read.

    Worse, that line went stale the moment anyone used Browse... -- it went
    on claiming the registry had the book covered while the field beside it
    held a completely different one. A status that can contradict the field
    it describes is worse than no status.

    A miss is the case with no visible evidence: the field simply stays as
    it was, which looks identical to "the registry answered with nothing".
    So that one is said out loud, along with how to stop it recurring.
    """
    if not entity_key or resolve_for_ui(entity_key, fy):
        return ""
    return (
        f"No registered book for **{entity_key}** -- pick the GnuCash book "
        "below. Register it once on the **Entities** tab to skip this step "
        "next time."
    )


def book_status_update(entity_key: str | None, fy: str | None = None):
    """`book_status()` as a Gradio update, hiding the row when it is empty."""
    msg = book_status(entity_key, fy)
    return gr.update(value=msg, visible=bool(msg))


def book_status_clear_if_filled(book_value: str | None):
    """Hide the status line once the book field holds anything at all.

    Wired to the book textbox's own .change(), so it fires however the path
    arrived -- Browse..., typing, pasting. The miss message asks the user to
    pick a book; once they have, it has served its purpose and must not sit
    there still asking.
    """
    if (book_value or "").strip():
        return gr.update(value="", visible=False)
    return gr.update()
