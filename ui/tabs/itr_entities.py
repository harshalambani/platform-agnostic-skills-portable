"""
ui/tabs/itr_entities.py -- ITR Entities CRUD tab.

Add/modify/delete taxpayer entities in Data/itr/entities.yaml through the UI
so the roster is managed like ITR Mapping manages tags -- no hand-edited
YAML (see 2026-07-16-itr-entities-crud-tab-handover-prompt.md).

Save discipline mirrors ui/tabs/itr_mapping_review.py (the better current
precedent for anchored-write + backup discipline; ui/tabs/settings.py was
compared too and supplied the "one dropdown + one form + Save/Delete
buttons" CRUD *shape* this tab follows, but it has no backup/anchoring
plumbing of its own to reuse):
  - every write is anchored via ui._config.data_root_dir() (never a bare
    "Data/" prefix -- source vs frozen path layouts, see test_path_anchoring.py)
  - a timestamped backup of entities.yaml is written before every rewrite
  - a blank/invalid submission never touches the filesystem
  - fields are validated (PAN regex, status/regime enums, ISO dates,
    regime_by_ay/audit_case_by_ay value types) before any write happens

Serialization: configs.dump_entities() (added alongside this tab) re-emits
a stable header + yaml.safe_dump(sort_keys=True) rather than round-tripping
via ruamel.yaml -- see configs.py's module-level DECISION note and the
handover prompt's "Decisions -- RESOLVED 2026-07-23" section (do not
re-litigate: ruamel was rejected as a new frozen-build dependency for the
sake of 3 comment lines).

Cascade + integrity (the hard part):
  - Key rename: Data/itr/mappings/<old>.mapping.yaml is renamed to
    <new>.mapping.yaml. If a file already exists at the target name, the
    ENTIRE save (including the entities.yaml rewrite) is BLOCKED with a
    clear message -- never a half-applied rename.
  - Filed-return files: Data/ITRFiled/<entity_key><token>.{json,pdf} (e.g.
    "Harshal2425.json") are matched by entity-key PREFIX only via
    configs.find_filed_returns() -- the trailing <token> is a human
    filing-batch label, never a parsed/trusted assessment year. On rename,
    each matched file's leading entity-key is swapped (token + extension
    preserved) and the ENTIRE save is BLOCKED if any target filename already
    exists (same no-half-apply discipline as the mapping-file rename). On
    delete, each matched file is ARCHIVED (moved, timestamped) to
    Data/itr/_archive/ alongside the mapping-file archive -- never deleted,
    since filed returns are read-only ground truth.
  - Delete (after double-confirm): the entity's .mapping.yaml, if any, is
    ARCHIVED (moved, timestamped) to Data/itr/_archive/ -- never deleted
    (Harshal's 2026-07-23 decision: a mapping file is hours of review-loop
    work, the entity row is thirty seconds of retyping). Filed-return files
    are archived the same way (see above).
  - Atomicity (2026-07-25 review fix): all pre-write collision checks above
    still run FIRST and block before anything is touched. Given that, the
    filesystem cascade (mapping rename/archive + filed-return renames/
    archives) is executed BEFORE entities.yaml is rewritten, via
    _move_with_rollback() -- if any single move in the cascade fails
    partway through, every move already completed is undone (moved back)
    before a clean Gradio-facing error is returned, and entities.yaml is
    never touched. entities.yaml is written LAST, only once the whole
    cascade has succeeded; if that final write itself fails, the cascade is
    rolled back too. Invariant: after any failure, entities.yaml and the
    on-disk mapping/filed-return files are mutually consistent (either
    all-old or all-new), never mixed.
"""
from __future__ import annotations

import datetime
import re
import shutil
import sys
from pathlib import Path

import gradio as gr

from .. import _config as _config_mod
from .. import _filedialog
from ._generic import _MAX_UPLOAD_SIZE_BYTES

# ---------------------------------------------------------------------------
# Import the ITR Workbook skill's flat (non-package) scripts/ modules, same
# pattern as itr_mapping_review.py.
# ---------------------------------------------------------------------------

_SCRIPTS_ON_PATH = False


def _ensure_itr_scripts_importable() -> None:
    global _SCRIPTS_ON_PATH
    if _SCRIPTS_ON_PATH:
        return
    import agents.skill_itr_workbook as _itr_pkg  # noqa: PLC0415

    scripts_dir = Path(_itr_pkg.__file__).resolve().parent / "scripts"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    _SCRIPTS_ON_PATH = True


def _configs_mod():
    _ensure_itr_scripts_importable()
    import configs  # noqa: PLC0415
    return configs


# ---------------------------------------------------------------------------
# Path helpers (anchored via data_root_dir() -- works in source + frozen).
# ---------------------------------------------------------------------------

def _entities_path() -> Path:
    return _config_mod.data_root_dir() / "itr" / "entities.yaml"


def _mappings_dir() -> Path:
    return _config_mod.data_root_dir() / "itr" / "mappings"


def _mapping_path(entity_key: str) -> Path:
    return _mappings_dir() / f"{entity_key}.mapping.yaml"


def _archive_dir() -> Path:
    return _config_mod.data_root_dir() / "itr" / "_archive"


def _itrfiled_dir() -> Path:
    return _config_mod.data_root_dir() / "ITRFiled"


# ---------------------------------------------------------------------------
# Loading.
# ---------------------------------------------------------------------------

def _load_entities() -> dict:
    """{entity_key: EntityProfile}, empty dict if entities.yaml is absent
    or malformed (first run / cold start)."""
    configs = _configs_mod()
    path = _entities_path()
    if not path.is_file():
        return {}
    try:
        return configs.load_entities(path)
    except Exception:
        return {}


def _entity_choices() -> list[tuple[str, str]]:
    entities = _load_entities()
    return [("(new entity)", "")] + sorted(
        (f"{key} ({e.status})", key) for key, e in entities.items()
    )


# ---------------------------------------------------------------------------
# Small key->value editors, encoded as "one 'key = value' per line" text --
# simpler and more testable than a nested Dataframe widget for what is, in
# practice, a handful of AY entries.
# ---------------------------------------------------------------------------

def _format_kv_lines(d: dict) -> str:
    return "\n".join(f"{k} = {v}" for k, v in sorted((d or {}).items()))


def _parse_kv_lines(text: str, *, as_bool: bool = False) -> tuple[dict, list[str]]:
    """Parse "key = value" lines. Returns (dict, errors). Blank lines and
    lines starting with '#' are ignored."""
    out: dict = {}
    errors: list[str] = []
    for lineno, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append(f"line {lineno} ({raw_line!r}): expected 'AY = value'.")
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        if not k:
            errors.append(f"line {lineno} ({raw_line!r}): missing key before '='.")
            continue
        if as_bool:
            low = v.lower()
            if low in ("true", "yes", "1"):
                out[k] = True
            elif low in ("false", "no", "0"):
                out[k] = False
            else:
                errors.append(f"line {lineno} ({raw_line!r}): {v!r} must be true/false.")
                continue
        else:
            out[k] = v
    return out, errors


def _format_list_lines(items: list) -> str:
    return "\n".join(str(x) for x in (items or []))


def _parse_list_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Form <-> EntityProfile.
# ---------------------------------------------------------------------------

_STATUS_CHOICES = ("Individual", "HUF")
_REGIME_CHOICES = ("old", "new")


def _entity_to_form(key: str, entities: dict) -> tuple:
    """Return the form field values for `key`, or blanks for a new entity."""
    e = entities.get(key)
    if e is None:
        return (
            key, "", "", "Individual", "Resident", "", "", "", "", "",
            "", "", "new", "", False, "", "", "", "",
        )
    return (
        e.key, e.name, e.pan, e.status, e.residency,
        e.dob or "", e.doi or "", e.address or "", e.father_name or "", e.aadhaar or "",
        e.business_subtree or "", e.workbook_match or "", e.default_regime, _format_kv_lines(e.regime_by_ay),
        e.audit_case, _format_kv_lines(e.audit_case_by_ay),
        _format_list_lines((e.extra_items or {}).get("bf_losses") or []),
        _format_list_lines((e.extra_items or {}).get("clubbing_notes") or []),
        _format_kv_lines(e.books),
    )


# ---------------------------------------------------------------------------
# Per-FY GnuCash book registry: browse-to-append + derived-match preview.
# ---------------------------------------------------------------------------

_FY_KEY_RE = re.compile(r"^\d{4}-\d{2}$")


def _browse_append_book(current_books_text: str, fy: str):
    """Browse for a .gnucash book and add/replace its line for `fy` in the
    books textbox. Pure-ish (one native dialog call via _filedialog) --
    returns a gr.update() so the caller can wire it straight to books_box."""
    fy = (fy or "").strip()
    if not _FY_KEY_RE.match(fy):
        gr.Warning("Enter the financial year as YYYY-YY (e.g. 2025-26) before browsing.")
        return gr.update()

    valid, warnings = _filedialog.pick_files(
        "itr_entities.gnucash_book",
        multiple=False,
        file_types=(".gnucash",),
        max_size_bytes=_MAX_UPLOAD_SIZE_BYTES,
        title="Select the GnuCash book (.gnucash)",
    )
    for w in warnings:
        gr.Warning(w)
    if not valid:
        return gr.update()

    books, _errors = _parse_kv_lines(current_books_text)
    books[fy] = valid[0]
    return gr.update(value=_format_kv_lines(books))


def _derived_match_text(books_text: str, override_text: str) -> str:
    """Pure preview of the Layer-A workbook-match token that would be
    effective given the current (unsaved) form state -- mirrors
    configs.effective_workbook_match()'s precedence without needing a
    saved EntityProfile."""
    override = (override_text or "").strip()
    if override:
        return f"**Effective workbook match:** `{override}` (explicit override)"

    books, _errors = _parse_kv_lines(books_text)
    fy_books = {k: v for k, v in books.items() if _FY_KEY_RE.match(k)}
    if fy_books:
        newest_fy = max(fy_books.keys())
        configs = _configs_mod()
        derived = configs.derive_workbook_match(fy_books[newest_fy])
        return f"**Effective workbook match:** `{derived}` (derived from the {newest_fy} book filename)"

    return "**Effective workbook match:** _(none yet -- register a book or set an override above)_"


# ---------------------------------------------------------------------------
# Atomic-enough filesystem cascade: move a batch of (src, dst) pairs, and if
# any move partway through fails, undo every move already completed (best
# effort) before raising -- so a caller that only writes entities.yaml AFTER
# this returns successfully can never end up with entities.yaml pointing at
# files that don't match what's actually on disk.
# ---------------------------------------------------------------------------

def _move_with_rollback(pairs: list[tuple[Path, Path]]) -> None:
    """Move each (src, dst) in `pairs` in order via shutil.move (works for
    both same-directory renames and cross-directory archive moves). On
    failure, every move already completed is moved back before a clean
    RuntimeError is raised -- never leaves a partial cascade on disk."""
    done: list[tuple[Path, Path]] = []
    try:
        for src, dst in pairs:
            shutil.move(str(src), str(dst))
            done.append((src, dst))
    except Exception as exc:
        rollback_errors: list[str] = []
        for src, dst in reversed(done):
            try:
                shutil.move(str(dst), str(src))
            except Exception as rollback_exc:  # noqa: BLE001
                rollback_errors.append(f"{dst} -> {src}: {rollback_exc}")
        if rollback_errors:
            raise RuntimeError(
                f"filesystem operation failed ({exc}) and the rollback also hit "
                "error(s) -- on-disk state may be inconsistent, please check "
                "manually before retrying: " + "; ".join(rollback_errors)
            ) from exc
        raise RuntimeError(
            f"filesystem operation failed and was fully rolled back -- nothing "
            f"was changed: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Save (Add / Modify / Rename).
# ---------------------------------------------------------------------------

def _save_entity(
    orig_key: str, new_key: str, name: str, pan: str, status: str, residency: str,
    dob: str, doi: str, address: str, father_name: str, aadhaar: str,
    business_subtree: str, workbook_match: str, default_regime: str, regime_by_ay_text: str,
    audit_case: bool, audit_case_by_ay_text: str,
    bf_losses_text: str, clubbing_notes_text: str,
    books_text: str = "",
) -> str:
    orig_key = (orig_key or "").strip()
    new_key = (new_key or "").strip()

    if not new_key:
        return "Error: entity key is required -- nothing was saved."

    regime_by_ay, kv_errors_1 = _parse_kv_lines(regime_by_ay_text)
    audit_case_by_ay, kv_errors_2 = _parse_kv_lines(audit_case_by_ay_text, as_bool=True)
    books, kv_errors_3 = _parse_kv_lines(books_text)
    fy_key_re = re.compile(r"^\d{4}-\d{2}$")
    for k in books:
        if not fy_key_re.match(k):
            kv_errors_3.append(
                f"books: '{k}' is not a valid financial year (expected YYYY-YY, e.g. 2025-26)."
            )
    kv_errors = kv_errors_1 + kv_errors_2 + kv_errors_3

    configs = _configs_mod()
    field_errors = configs.validate_entity_fields(
        key=new_key, name=name, pan=pan, status=status,
        dob=dob or None, doi=doi or None, default_regime=default_regime,
        regime_by_ay=regime_by_ay, audit_case_by_ay=audit_case_by_ay,
    )
    errors = field_errors + kv_errors
    if errors:
        return "**Not saved -- fix the following:**\n\n" + "\n".join(f"- {e}" for e in errors)

    entities = _load_entities()
    is_rename = bool(orig_key) and orig_key != new_key
    is_add = not orig_key

    if is_add and new_key in entities:
        return f"Error: entity '{new_key}' already exists -- select it from the dropdown to Modify instead."
    if is_rename and new_key in entities:
        return f"Error: cannot rename '{orig_key}' -> '{new_key}': '{new_key}' already exists."
    if not is_add and not is_rename and orig_key not in entities:
        return f"Error: entity '{orig_key}' not found -- it may have been deleted elsewhere; refresh and retry."

    # Rename cascade feasibility check BEFORE any write -- never half-apply.
    old_mapping = _mapping_path(orig_key) if is_rename else None
    new_mapping = _mapping_path(new_key) if is_rename else None
    if is_rename and old_mapping.is_file() and new_mapping.is_file():
        return (
            f"Error: rename blocked -- both '{old_mapping.name}' and "
            f"'{new_mapping.name}' already exist in Data/itr/mappings/. "
            "Resolve the conflict manually (archive or delete the stale one) "
            "before renaming; nothing was written."
        )

    filed_renames: list[tuple[Path, Path]] = []
    if is_rename:
        old_filed = configs.find_filed_returns(
            orig_key, _itrfiled_dir(), all_entity_keys=set(entities.keys())
        )
        for src in old_filed:
            token_and_suffix = src.name[len(orig_key):]
            target = src.with_name(f"{new_key}{token_and_suffix}")
            filed_renames.append((src, target))
        collisions = [t for _, t in filed_renames if t.exists()]
        if collisions:
            names = ", ".join(p.name for p in collisions)
            return (
                f"Error: rename blocked -- target filed-return file(s) already "
                f"exist in Data/ITRFiled/: {names}. Resolve the conflict "
                "manually before renaming; nothing was written."
            )

    extra_items = {}
    bf_losses = _parse_list_lines(bf_losses_text)
    clubbing_notes = _parse_list_lines(clubbing_notes_text)
    if bf_losses:
        extra_items["bf_losses"] = bf_losses
    if clubbing_notes:
        extra_items["clubbing_notes"] = clubbing_notes

    new_profile = configs.EntityProfile(
        key=new_key,
        name=name.strip(),
        pan=pan.strip().upper(),
        status=status,
        residency=(residency or "Resident").strip() or "Resident",
        dob=(dob or "").strip() or None,
        doi=(doi or "").strip() or None,
        address=(address or "").strip() or None,
        father_name=(father_name or "").strip() or None,
        aadhaar=(aadhaar or "").strip() or None,
        business_subtree=(business_subtree or "").strip() or None,
        workbook_match=(workbook_match or "").strip() or None,
        books=books,
        default_regime=default_regime,
        regime_by_ay=regime_by_ay,
        audit_case=bool(audit_case),
        audit_case_by_ay=audit_case_by_ay,
        extra_items=extra_items,
    )

    entities[new_key] = new_profile
    if is_rename:
        del entities[orig_key]

    # Filesystem cascade FIRST, entities.yaml LAST (2026-07-25 atomicity
    # fix): if any move in the cascade fails partway through, everything
    # already moved is rolled back and entities.yaml is never touched --
    # see _move_with_rollback() and the module docstring's "Atomicity" note.
    mapping_cascaded = bool(is_rename and old_mapping.is_file())
    cascade_pairs: list[tuple[Path, Path]] = []
    if mapping_cascaded:
        cascade_pairs.append((old_mapping, new_mapping))
    cascade_pairs.extend(filed_renames)

    if cascade_pairs:
        try:
            _move_with_rollback(cascade_pairs)
        except RuntimeError as exc:
            return (
                "**Not saved -- the rename cascade hit a filesystem error and "
                "was rolled back. Nothing changed (entities.yaml untouched, "
                f"files back in their original place):**\n\n- {exc}"
            )

    entities_file = _entities_path()
    entities_file.parent.mkdir(parents=True, exist_ok=True)

    backup_msg = "No existing entities.yaml -- nothing to back up (first entity)."
    if entities_file.is_file():
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = entities_file.with_name(f"{entities_file.name}.bak-{stamp}")
        shutil.copy2(entities_file, backup_path)
        backup_msg = f"Backup written: {backup_path}"

    try:
        entities_file.write_text(configs.dump_entities(entities), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        rolled_back = ""
        if cascade_pairs:
            try:
                _move_with_rollback([(dst, src) for src, dst in reversed(cascade_pairs)])
                rolled_back = " The filesystem cascade was rolled back too -- nothing changed."
            except RuntimeError as rollback_exc:
                rolled_back = f" WARNING: the filesystem cascade rollback also failed ({rollback_exc}) -- check disk state manually."
        return f"**Not saved -- writing entities.yaml failed: {exc}.**{rolled_back}"

    lines = ["**Saved**", "", backup_msg]
    action = "Added" if is_add else ("Renamed + updated" if is_rename else "Updated")
    lines.append(f"{action} entity `{new_key}` -> {entities_file}")

    if is_rename:
        if mapping_cascaded:
            lines.append(f"Cascaded: {old_mapping.name} -> {new_mapping.name}")
        else:
            lines.append("No existing mapping file to cascade (cold-start entity).")

        if filed_renames:
            renamed_names = ", ".join(f"{src.name} -> {target.name}" for src, target in filed_renames)
            lines.append(f"Cascaded filed return(s): {renamed_names}")
        else:
            lines.append("No filed-return files to cascade in Data/ITRFiled/.")

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Delete (double-confirm required; archive, never silent-delete).
# ---------------------------------------------------------------------------

def _delete_entity(key: str, confirm_1: bool, confirm_2: bool) -> str:
    key = (key or "").strip()
    if not key:
        return "Error: select an entity first -- nothing was deleted."
    if not (confirm_1 and confirm_2):
        return "Error: both confirmation checkboxes must be checked to delete -- nothing was deleted."

    entities = _load_entities()
    if key not in entities:
        return f"Error: entity '{key}' not found -- nothing was deleted."

    configs = _configs_mod()
    all_keys = set(entities.keys())
    del entities[key]

    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")

    # Filesystem cascade FIRST, entities.yaml LAST (2026-07-25 atomicity
    # fix, same discipline as _save_entity): archive moves are executed
    # before entities.yaml is rewritten, and rolled back on any failure.
    mapping_file = _mapping_path(key)
    mapping_archived = mapping_file.is_file()
    archive_dir = _archive_dir()
    mapping_target = archive_dir / f"{key}.mapping.yaml.archived-{stamp}"

    filed_returns = configs.find_filed_returns(key, _itrfiled_dir(), all_entity_keys=all_keys)
    filed_targets = [
        (src, archive_dir / f"{src.name}.archived-{stamp}") for src in filed_returns
    ]

    cascade_pairs: list[tuple[Path, Path]] = []
    if mapping_archived:
        cascade_pairs.append((mapping_file, mapping_target))
    cascade_pairs.extend(filed_targets)

    if cascade_pairs:
        archive_dir.mkdir(parents=True, exist_ok=True)
        try:
            _move_with_rollback(cascade_pairs)
        except RuntimeError as exc:
            return (
                "**Not deleted -- the archive cascade hit a filesystem error "
                "and was rolled back. Nothing changed (entities.yaml "
                f"untouched, files back in their original place):**\n\n- {exc}"
            )

    entities_file = _entities_path()
    backup_msg = "No existing entities.yaml -- nothing to back up."
    if entities_file.is_file():
        backup_path = entities_file.with_name(f"{entities_file.name}.bak-{stamp}")
        shutil.copy2(entities_file, backup_path)
        backup_msg = f"Backup written: {backup_path}"

    try:
        entities_file.write_text(configs.dump_entities(entities), encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        rolled_back = ""
        if cascade_pairs:
            try:
                _move_with_rollback([(dst, src) for src, dst in reversed(cascade_pairs)])
                rolled_back = " The archive cascade was rolled back too -- nothing changed."
            except RuntimeError as rollback_exc:
                rolled_back = f" WARNING: the archive cascade rollback also failed ({rollback_exc}) -- check disk state manually."
        return f"**Not deleted -- writing entities.yaml failed: {exc}.**{rolled_back}"

    lines = ["**Deleted**", "", backup_msg, f"Removed `{key}` from {entities_file}"]

    if mapping_archived:
        lines.append(f"Archived mapping file -> {mapping_target} (never deleted).")
    else:
        lines.append("No mapping file existed for this entity -- nothing to archive.")

    if filed_targets:
        archived_names = ", ".join(target.name for _, target in filed_targets)
        lines.append(
            "Archived filed-return file(s) (never deleted) -> " + archived_names
        )
    else:
        lines.append("No filed-return files existed for this entity -- nothing to archive.")

    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Gradio tab renderer.
# ---------------------------------------------------------------------------

def render(container_tab=None) -> None:
    """Render the ITR Entities tab. Must be called inside gr.Tab()."""
    gr.Markdown(
        "## ITR Entities\n\n"
        "Add, modify, or delete taxpayer entities in `entities.yaml`. "
        "Select an entity below to edit it, or leave `(new entity)` "
        "selected to add one. A new entity has no mapping yet -- that's "
        "fine, the ITR Workbook cold-starts it.\n\n"
        "> **Rename note:** changing the entity key on an existing entity "
        "renames its `.mapping.yaml` file and any filed returns in "
        "`Data/ITRFiled/` to match. If a file already exists at any new "
        "name, the rename is blocked rather than half-applied."
    )

    initial_choices = _entity_choices()

    with gr.Row():
        entity_dropdown = gr.Dropdown(
            label="Entity",
            choices=initial_choices,
            value="",
            interactive=True,
            scale=4,
        )
        refresh_btn = gr.Button("↻", scale=0, min_width=40)

    if container_tab is not None:
        def _rescan_entities():
            return gr.update(choices=_entity_choices(), value="")
        container_tab.select(fn=_rescan_entities, inputs=[], outputs=[entity_dropdown])

    with gr.Row():
        with gr.Column(scale=1):
            orig_key_state = gr.State("")
            key_box = gr.Textbox(label="Entity key", placeholder="e.g. SYN-IND", interactive=True)
            name_box = gr.Textbox(label="Name", interactive=True)
            pan_box = gr.Textbox(label="PAN", placeholder="AAAAA0000A", interactive=True)
            status_dd = gr.Dropdown(label="Status", choices=list(_STATUS_CHOICES), value="Individual", interactive=True)
            residency_box = gr.Textbox(
                label="Residency", value="Resident", interactive=True,
                info="R/OR, RNOR, or NR takes statutory effect; anything else (incl. free text) defaults to R/OR.",
            )
            dob_box = gr.Textbox(label="Date of birth (YYYY-MM-DD)", interactive=True)
            doi_box = gr.Textbox(label="Date of incorporation (YYYY-MM-DD, HUF)", interactive=True)
            address_box = gr.Textbox(label="Address", interactive=True)
            father_name_box = gr.Textbox(label="Father's name (optional)", interactive=True)
            aadhaar_box = gr.Textbox(label="Aadhaar (optional, digits only)", interactive=True)
            business_subtree_box = gr.Textbox(
                label="Business subtree (GnuCash account path prefix, optional)", interactive=True,
            )
            workbook_match_box = gr.Textbox(
                label="Workbook match (optional) -- filename name w/o year, e.g. AliceDoe",
                interactive=True,
            )
            books_box = gr.Textbox(
                label=(
                    "Financial-year books (one per line, 'FY = path', e.g. "
                    "2025-26 = C:\\books\\AliceDoe2526.gnucash)"
                ),
                lines=3, interactive=True,
            )
            with gr.Row():
                fy_browse_box = gr.Textbox(label="FY for Browse", placeholder="2025-26", scale=1)
                books_browse_btn = gr.Button("Browse for book...", scale=0, min_width=140)
            derived_match_md = gr.Markdown(
                "**Effective workbook match:** _(none yet -- register a book or set an override above)_"
            )

        with gr.Column(scale=1):
            default_regime_dd = gr.Dropdown(
                label="Default regime", choices=list(_REGIME_CHOICES), value="new", interactive=True,
            )
            regime_by_ay_box = gr.Textbox(
                label="Regime by AY (one per line, 'AY = old|new')",
                placeholder="2025-26 = old\n2026-27 = new",
                lines=3, interactive=True,
            )
            audit_case_cb = gr.Checkbox(
                label="Audit case (s.44AB) -- s.139(1) due date is 31 Oct, not 31 Jul",
                value=False,
            )
            audit_case_by_ay_box = gr.Textbox(
                label="Audit case by AY (one per line, 'AY = true|false')",
                placeholder="2025-26 = false",
                lines=3, interactive=True,
            )
            bf_losses_box = gr.Textbox(
                label="B/f losses (one per line)", lines=3, interactive=True,
            )
            clubbing_notes_box = gr.Textbox(
                label="Clubbing notes (one per line)", lines=3, interactive=True,
            )

            save_status = gr.Markdown("_Fill in the fields and click Save._")
            with gr.Row():
                save_btn = gr.Button("Save Entity", variant="primary")

            gr.Markdown("---\n**Delete** (double-confirm required):")
            with gr.Row():
                confirm_1_cb = gr.Checkbox(label="I understand this removes the entity from entities.yaml", value=False)
                confirm_2_cb = gr.Checkbox(label="I confirm I want to delete this entity", value=False)
            delete_btn = gr.Button("Delete Entity", variant="stop")

    _form_outputs = [
        key_box, name_box, pan_box, status_dd, residency_box,
        dob_box, doi_box, address_box, father_name_box, aadhaar_box,
        business_subtree_box, workbook_match_box, default_regime_dd, regime_by_ay_box,
        audit_case_cb, audit_case_by_ay_box, bf_losses_box, clubbing_notes_box,
        books_box,
    ]

    books_browse_btn.click(
        fn=_browse_append_book,
        inputs=[books_box, fy_browse_box],
        outputs=[books_box],
    )
    books_box.change(
        fn=_derived_match_text,
        inputs=[books_box, workbook_match_box],
        outputs=[derived_match_md],
    )
    workbook_match_box.change(
        fn=_derived_match_text,
        inputs=[books_box, workbook_match_box],
        outputs=[derived_match_md],
    )

    def _on_select(key):
        entities = _load_entities()
        values = _entity_to_form(key or "", entities)
        status_msg = f"_Loaded `{key}`. Edit and Save, or Delete._" if key else "_New entity -- fill in the fields and Save._"
        return (*values, key or "", status_msg)

    entity_dropdown.change(
        fn=_on_select,
        inputs=[entity_dropdown],
        outputs=[*_form_outputs, orig_key_state, save_status],
    )

    def _on_save(
        orig_key, new_key, name, pan, status, residency, dob, doi, address,
        father_name, aadhaar, business_subtree, workbook_match, default_regime, regime_by_ay_text,
        audit_case, audit_case_by_ay_text, bf_losses_text, clubbing_notes_text,
        books_text,
    ):
        msg = _save_entity(
            orig_key, new_key, name, pan, status, residency, dob, doi, address,
            father_name, aadhaar, business_subtree, workbook_match, default_regime, regime_by_ay_text,
            audit_case, audit_case_by_ay_text, bf_losses_text, clubbing_notes_text,
            books_text,
        )
        new_choices = _entity_choices()
        new_orig = new_key.strip() if msg.startswith("**Saved**") else orig_key
        return msg, gr.update(choices=new_choices, value=new_orig), new_orig

    save_btn.click(
        fn=_on_save,
        inputs=[orig_key_state, key_box, name_box, pan_box, status_dd, residency_box,
                dob_box, doi_box, address_box, father_name_box, aadhaar_box,
                business_subtree_box, workbook_match_box, default_regime_dd, regime_by_ay_box,
                audit_case_cb, audit_case_by_ay_box, bf_losses_box, clubbing_notes_box,
                books_box],
        outputs=[save_status, entity_dropdown, orig_key_state],
    )

    def _on_delete(key, confirm_1, confirm_2):
        msg = _delete_entity(key, confirm_1, confirm_2)
        new_choices = _entity_choices()
        deleted = msg.startswith("**Deleted**")
        new_value = "" if deleted else key
        blanks = _entity_to_form(new_value, _load_entities())
        return (
            msg, gr.update(choices=new_choices, value=new_value), new_value,
            *blanks, False, False,
        )

    delete_btn.click(
        fn=_on_delete,
        inputs=[entity_dropdown, confirm_1_cb, confirm_2_cb],
        outputs=[save_status, entity_dropdown, orig_key_state, *_form_outputs, confirm_1_cb, confirm_2_cb],
    )
