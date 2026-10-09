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
the generic book_from wiring and matters for PyInstaller-frozen builds,
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
        # An explicit FY is exact. resolve_book() falls back to the newest
        # registered FY when the asked-for one is missing; for a box that is
        # filled from entity + FY that would hand over another year's book.
        if resolved is not None and fy:
            registered = _book_registry.list_books(entity_key)
            if registered and fy not in registered:
                return ""
    except Exception:
        return ""
    if resolved is not None and resolved.is_file():
        return str(resolved)
    return ""


def _is_registered(path: str | None) -> bool:
    """True when `path` is a registered book of any entity or FY. Read-only;
    False on any failure (cannot tell, so treat it as hand-picked)."""
    if not (path or "").strip():
        return False
    try:
        from .. import _book_registry  # noqa: PLC0415 -- see module docstring
        return _book_registry.is_registered_book(path)
    except Exception:
        return False


def _label(entity_key: str | None, fy: str | None) -> str:
    return f"{entity_key} {fy}" if fy else f"{entity_key}"


def book_update(entity_key: str | None, fy: str | None = None, current: str | None = None):
    """Gradio update for a path box, driven by `resolve_for_ui()`.

    Returns `gr.update(value=<path>)` on a hit.

    On a miss it depends on `current`, the box's present value:
      * `current` is None (caller does not pass it) or blank: a bare
        `gr.update()` -- no change.
      * `current` is a registered book of some entity or FY (what an earlier
        pick filled, or a path typed by hand that happens to be registered):
        it belongs to another entity or FY, so the box is cleared
        (`value=""`). Keeping it would feed that book to this selection.
      * `current` is not a registered book at all: a hand-picked path, kept.
    """
    resolved = resolve_for_ui(entity_key, fy)
    if resolved:
        return gr.update(value=resolved)
    if _is_registered(current):
        return gr.update(value="")
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


def _merge_books(entity_keys, fy, current):
    """(lines, dropped, kept_by_hand) for a multi-book box.

    `lines` = the registered book of each selected entity, then any line the
    box already holds that is NOT a registered book (hand-picked). `dropped`
    = lines that were registered books of entities or FYs no longer selected.
    """
    resolved = [p for p in (resolve_for_ui(k, fy) for k in _as_keys(entity_keys)) if p]
    lines = list(dict.fromkeys(resolved))
    dropped: list[str] = []
    kept: list[str] = []
    for raw in str(current or "").splitlines():
        line = raw.strip()
        if not line or line in lines or line in kept:
            continue
        if line in resolved:
            continue
        if _is_registered(line):
            dropped.append(line)
        else:
            kept.append(line)
    return lines + kept, dropped, kept


def books_update(entity_keys, fy: str | None = None, current: str | None = None):
    """Gradio update for a multi-book path textbox -- one path per line.

    The multi-book counterpart of `book_update()`, for inputs that take
    several books at once (Inter-entity Matrix). Entities with no resolvable
    book are skipped rather than written as a blank line, so the field only
    ever holds paths that exist; `books_status()` names the ones left out.

    `current` is the box's present text. When given, exactly the lines that
    are registered books of entities or FYs no longer selected are dropped,
    and hand-picked lines (not registered anywhere) are kept. When `current`
    is None and nothing resolves, a bare `gr.update()` -- no change.
    """
    if current is None:
        resolved = [p for p in (resolve_for_ui(k, fy) for k in _as_keys(entity_keys)) if p]
        if resolved:
            return gr.update(value="\n".join(resolved))
        return gr.update()
    lines, dropped, _kept = _merge_books(entity_keys, fy, current)
    new = "\n".join(lines)
    if new == str(current).strip() and not dropped:
        return gr.update()
    return gr.update(value=new)


def books_status(entity_keys, fy: str | None = None, current: str | None = None) -> str:
    """One-line Markdown naming the picked entities with no registered book.

    Same principle as `book_status()`: resolved books land in a visible field
    and need no announcement, so only the gap is worth saying. The partial
    case is the one this exists for -- pick three people, get two paths, and
    without this the third's absence is invisible.

    With `current`, it also says when book lines were dropped (registered
    books of entities no longer selected) and when hand-picked lines were kept.
    """
    keys = _as_keys(entity_keys)
    missing = [k for k in keys if not resolve_for_ui(k, fy)]
    parts: list[str] = []
    if missing:
        names = ", ".join(f"**{k}**" for k in missing)
        if len(missing) == len(keys):
            parts.append(
                f"No registered book for {names} -- add the books below, or "
                "register them once on the **Entities** tab."
            )
        else:
            parts.append(
                f"Filled in {len(keys) - len(missing)} of {len(keys)} books. No "
                f"registered book for {names} -- add those below, or register them "
                "once on the **Entities** tab."
            )
    if current is not None:
        _lines, dropped, kept = _merge_books(entity_keys, fy, current)
        if dropped:
            parts.append(
                f"Removed {len(dropped)} book line(s) of entities no longer selected."
            )
        if kept:
            parts.append(
                "Books picked by hand are kept. Check that they belong to the "
                "selected entities."
            )
    return " ".join(parts)


def books_status_update(entity_keys, fy: str | None = None, current: str | None = None):
    """`books_status()` as a Gradio update, hiding the row when it is empty."""
    msg = books_status(entity_keys, fy, current)
    return gr.update(value=msg, visible=bool(msg))


def book_status(entity_key: str | None, fy: str | None = None, current: str | None = None) -> str:
    """One-line Markdown for the *miss* case only -- otherwise "".

    A registry hit says nothing, deliberately. The book field is a path
    textbox, so a hit writes the resolved path in plain sight: the filled
    field is its own acknowledgement, and a line underneath announcing that
    it filled is just repeating what the user can already read.

    Worse, that line went stale the moment anyone used Browse... -- it went
    on claiming the registry had the book covered while the field beside it
    held a completely different one. A status that can contradict the field
    it describes is worse than no status.

    A miss is the case with no visible evidence, so that one is said out
    loud. What it says depends on `current`, the box's present value (see
    `book_update()`): a registered book there was cleared; a hand-picked
    path was kept and the user is told to check it; otherwise the box was
    empty and the user is asked to pick one.
    """
    if not entity_key or resolve_for_ui(entity_key, fy):
        return ""
    label = _label(entity_key, fy)
    cur = (current or "").strip()
    if cur and _is_registered(cur):
        return f"No registered book for {label}; book box cleared."
    if cur:
        return f"Book picked by hand is kept. Check that it belongs to {label}."
    return (
        f"No registered book for **{entity_key}** -- pick the GnuCash book "
        "below. Register it once on the **Entities** tab to skip this step "
        "next time."
    )


def book_status_update(entity_key: str | None, fy: str | None = None, current: str | None = None):
    """`book_status()` as a Gradio update, hiding the row when it is empty."""
    msg = book_status(entity_key, fy, current)
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
