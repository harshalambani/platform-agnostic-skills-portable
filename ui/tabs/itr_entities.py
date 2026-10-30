"""
ui/tabs/itr_entities.py -- the "Entities" CRUD tab (GnuCash > Entities).

Module name is historical: this started life as an ITR-only editor and was
promoted to a GnuCash-level sub-tab once entities.yaml became app-wide master
data (it feeds every Entity dropdown, and its books: registry auto-fills every
GnuCash book field).

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

import copy
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


def _parse_marker_text(text: str) -> list[str]:
    """SEC-20: markers typed one per line or comma-separated; trimmed, blanks dropped."""
    return [t.strip() for t in (text or "").replace(",", chr(10)).splitlines() if t.strip()]


def _parse_list_lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


# ---------------------------------------------------------------------------
# Form <-> EntityProfile.
# ---------------------------------------------------------------------------

_STATUS_CHOICES = ("Individual", "HUF")
_REGIME_CHOICES = ("old", "new")

#: `audit_case_basis` is optional and a Gradio Dropdown cannot hold "" as a
#: choice cleanly, so the blank option is a sentinel label mapped back to ""
#: on save. Blank means "unstated", which reads as the s.44AB default -- the
#: only meaning `audit_case: true` ever had before this field existed.
_AUDIT_BASIS_BLANK = "(not stated)"
_AUDIT_BASIS_CHOICES = (
    _AUDIT_BASIS_BLANK,
    "self_44ab",
    "partner_of_audited_firm",
)


def _entity_to_form(key: str, entities: dict) -> tuple:
    """Return the form field values for `key`, or blanks for a new entity."""
    e = entities.get(key)
    if e is None:
        return (
            key, "", "", "Individual", "Resident", "", "", "", "", "",
            "", "", "new", "", False, "", _AUDIT_BASIS_BLANK, "", "", "", "",
            "", "", "",
        )
    return (
        e.key, e.name, e.pan, e.status, e.residency,
        e.dob or "", e.doi or "", e.address or "", e.father_name or "", e.aadhaar or "",
        e.business_subtree or "", e.workbook_match or "", e.default_regime, _format_kv_lines(e.regime_by_ay),
        e.audit_case, _format_kv_lines(e.audit_case_by_ay),
        e.audit_case_basis or _AUDIT_BASIS_BLANK,
        getattr(e, "partner_admission_date", None) or "",
        _format_list_lines((e.extra_items or {}).get("bf_losses") or []),
        _format_list_lines((e.extra_items or {}).get("clubbing_notes") or []),
        _format_kv_lines(e.books),
        _format_list_lines(getattr(e, "drawings_accounts", None) or []),
        getattr(e, "card_spend_default_account", "") or "",
        _format_list_lines(getattr(e, "reimbursement_markers", None) or []),
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

# EntityProfile fields with no widget on this tab: a save must preserve them.
# UI-35: every field now has a control, so nothing is hidden any more. The
# preserve loop in _save_entity stays, so a field added to EntityProfile later
# and listed here is carried over until it gets a widget -- and the guard test
# fails until it is listed in _FIELD_CONTROLS or here.
_FORM_HIDDEN_FIELDS: tuple = ()
# MAP-34 / MAP-35 / UI-35: editable on this tab. A save that passes None for
# them (an older caller) preserves the stored value; a value (even empty)
# replaces it.
_FORM_MAPPER_FIELDS = ("drawings_accounts", "card_spend_default_account", "reimbursement_markers")
_FORM_UI35_FIELDS = (
    "bank_service_charge_account", "card_emi_interest_account", "partner_comp_accounts",
    "foreign_dividends_in_book", "foreign_dividends_in_book_by_ay", "partner_firm_pan",
    "cc_large_spend_threshold", "foreign_dividend_cash_account", "foreign_dividend_tax_account",
    "foreign_dividend_income_account", "cc_cred_max_overage",
)

#: UI-35: EntityProfile field -> a fragment of the label of the control that
#: edits it. tests/test_ui35_entities_all_fields.py builds the tab and checks
#: both directions, so a new field cannot be added without a control.
_FIELD_CONTROLS = {
    "key": "Entity key", "name": "Name", "pan": "PAN", "status": "Status", "residency": "Residency",
    "dob": "Date of birth", "doi": "Date of incorporation", "address": "Address",
    "father_name": "Father's name", "aadhaar": "Aadhaar", "business_subtree": "Business subtree",
    "workbook_match": "Workbook match", "books": "Financial-year books",
    "default_regime": "Default regime", "regime_by_ay": "Regime by AY",
    "audit_case": "Extended due date --", "audit_case_by_ay": "Extended due date by AY",
    "audit_case_basis": "Why 31 Oct applies",
    "foreign_dividends_in_book": "Foreign dividends are already in the book",
    "foreign_dividends_in_book_by_ay": "Foreign dividends already in the book by AY",
    "extra_items": "B/f losses", "partner_comp_accounts": "Partner account: bank",
    "drawings_accounts": "Drawings accounts", "reimbursement_markers": "Reimbursement markers",
    "partner_admission_date": "Date admitted as partner",
    "card_spend_default_account": "Card-spend default account",
    "bank_service_charge_account": "Bank service charge account",
    "card_emi_interest_account": "Card EMI interest account",
    "partner_firm_pan": "Firm PAN",
    "cc_large_spend_threshold": "Large-spend limit",
    "cc_cred_max_overage": "CRED tolerance",
    "foreign_dividend_cash_account": "Foreign dividend cash account",
    "foreign_dividend_tax_account": "Foreign dividend tax account",
    "foreign_dividend_income_account": "Foreign dividend income account",
}
#: Fields intentionally without a control. Starts empty.
_FIELD_CONTROL_ALLOW_LIST: tuple = ()

#: The nine keys partner_comp_accounts carries (the eight required by the
#: Partner Compensation skill plus the optional clearing account).
_PARTNER_ACCOUNT_KEYS = (
    "bank", "tds_expense", "interest_on_capital", "current_account",
    "capital_contribution", "medical_expense", "remuneration_income",
    "share_of_profit_income", "salary_clearing",
)

#: extra_items["partner_comp_drivers"][<FY>] -- the keys the Partner
#: Compensation skill reads, and the type each must have.
_DRIVER_RATE_KEYS = ("firms_tax_rate", "capital_rate", "capital_interest_rate",
                     "capital_interest_tds_rate", "remuneration_tds_rate")
_DRIVER_MONTH_KEYS = ("capital_months_total", "capital_months_achieved")
_DRIVER_AMOUNT_KEYS = ("target_compensation",)
_DRIVER_TEXT_KEYS = ("remuneration_tds_section",)
_DRIVER_DATE_KEYS = ("remuneration_tds_start_date",)
_DRIVER_DATEMAP_KEYS = ("capital_interest_from_date_overrides",)
_DRIVER_KEYS = (_DRIVER_RATE_KEYS + _DRIVER_MONTH_KEYS + _DRIVER_AMOUNT_KEYS
                + _DRIVER_TEXT_KEYS + _DRIVER_DATE_KEYS + _DRIVER_DATEMAP_KEYS)


def _is_number(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_iso_date(v) -> bool:
    if isinstance(v, datetime.date):
        return True
    if not isinstance(v, str):
        return False
    try:
        datetime.date.fromisoformat(v.strip())
        return True
    except ValueError:
        return False


def _format_drivers(drivers: dict | None) -> str:
    if not drivers:
        return ""
    import yaml  # noqa: PLC0415
    # H35-23: the capital-rule rate and months have their own per-FY fields,
    # so they are not repeated in this free-text box.
    shown = {}
    for fy, block in drivers.items():
        if isinstance(block, dict):
            block = {k: v for k, v in block.items() if k not in _CAPITAL_RULE_KEYS}
            if not block:
                continue
        shown[fy] = block
    if not shown:
        return ""
    return yaml.safe_dump(shown, sort_keys=True, default_flow_style=False, allow_unicode=True)


#: H35-23: the two capital-rule drivers edited in their own per-FY fields
#: (stored in the same partner_comp_drivers keys the skill already reads).
_CAPITAL_RULE_KEYS = ("capital_rate", "capital_months_total")


def _format_capital_rule_lines(drivers: dict | None, key: str) -> str:
    """One 'FY = value' line per FY that sets `key`. The rate is shown as a percent."""
    lines = []
    for fy in sorted(drivers or {}):
        block = (drivers or {}).get(fy)
        v = block.get(key) if isinstance(block, dict) else None
        if v is None:
            continue
        if key == "capital_rate" and _is_number(v):
            lines.append(f"{fy} = {round(v * 100, 6):g}")
        else:
            lines.append(f"{fy} = {v}")
    return "\n".join(lines)


def _parse_capital_rule_lines(text: str, key: str) -> tuple[dict, list[str]]:
    """Parse 'FY = value' lines. Rate: a percent strictly between 0 and 100
    (a trailing % is accepted); months: a positive whole number. Returns
    ({FY: stored value}, errors); the rate is stored as a fraction."""
    label = "capital rate (percent)" if key == "capital_rate" else "capital months in total"
    out: dict = {}
    errors: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line:
            continue
        if "=" not in line:
            errors.append(f"{label}: '{line}' is not 'YYYY-YY = value'.")
            continue
        fy, _, val = line.partition("=")
        fy, val = fy.strip(), val.strip().rstrip("%").strip()
        if not _FY_KEY_RE.match(fy):
            errors.append(f"{label}: '{fy}' is not a valid financial year (expected YYYY-YY, e.g. 2025-26).")
            continue
        if fy in out:
            errors.append(f"{label}: {fy} is given twice.")
            continue
        if key == "capital_rate":
            try:
                pct = float(val)
            except ValueError:
                errors.append(f"{label} {fy}: '{val}' is not a number -- enter a percent such as 40.")
                continue
            if not 0 < pct < 100:
                errors.append(f"{label} {fy}: {val} must be above 0 and below 100 (40 means 40%).")
                continue
            out[fy] = round(pct / 100, 10)
        else:
            if not val.isdigit() or int(val) < 1:
                errors.append(f"{label} {fy}: '{val}' must be a positive whole number of months.")
                continue
            out[fy] = int(val)
    return out, errors


def _parse_drivers(text: str) -> tuple[dict, list[str]]:
    """UI-35: validate the per-FY partner-compensation drivers typed as YAML
    ({FY: {driver: value}}). Returns (drivers, errors); on any error the caller
    saves nothing."""
    import yaml  # noqa: PLC0415
    text = text or ""
    if not text.strip():
        return {}, []
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        return {}, [f"partner compensation drivers: not valid YAML ({str(exc).splitlines()[0] if str(exc) else exc})."]
    if data is None:
        return {}, []
    if not isinstance(data, dict):
        return {}, ["partner compensation drivers: expected 'YYYY-YY:' blocks of 'driver: value' lines."]
    errors: list[str] = []
    for fy, block in data.items():
        if not isinstance(fy, str) or not _FY_KEY_RE.match(fy):
            errors.append(f"partner compensation drivers: '{fy}' is not a valid financial year (expected YYYY-YY, e.g. 2025-26).")
            continue
        if not isinstance(block, dict):
            errors.append(f"partner compensation drivers {fy}: expected 'driver: value' lines.")
            continue
        for k, v in block.items():
            where = f"partner compensation drivers {fy}: '{k}'"
            if k not in _DRIVER_KEYS:
                errors.append(f"{where} is not a known driver (known: {', '.join(_DRIVER_KEYS)}).")
            elif k in _DRIVER_RATE_KEYS:
                if not _is_number(v) or not 0 <= v <= 1:
                    errors.append(f"{where} must be a fraction between 0 and 1 (0.25 for 25%), got {v!r}.")
            elif k in _DRIVER_MONTH_KEYS:
                if not isinstance(v, int) or isinstance(v, bool) or v < 0:
                    errors.append(f"{where} must be a whole number of months, got {v!r}.")
            elif k in _DRIVER_AMOUNT_KEYS:
                if not _is_number(v) or v < 0:
                    errors.append(f"{where} must be a number of rupees, got {v!r}.")
            elif k in _DRIVER_TEXT_KEYS:
                if not isinstance(v, str) or not v.strip():
                    errors.append(f"{where} must be text such as 194T, got {v!r}.")
            elif k in _DRIVER_DATE_KEYS:
                if not _is_iso_date(v):
                    errors.append(f"{where} must be a date as YYYY-MM-DD, got {v!r}.")
            elif k in _DRIVER_DATEMAP_KEYS:
                if not isinstance(v, dict) or not all(_is_iso_date(d) for d in v.values()):
                    errors.append(f"{where} must be 'month: YYYY-MM-DD' lines, got {v!r}.")
    return data, errors


# ---------------------------------------------------------------------------
# UI-35: account pick-lists read from the entity's own book (read-only).
# ---------------------------------------------------------------------------

_BOOK_CACHE: dict = {}


def _newest_book(books: dict) -> tuple[str, Path] | None:
    fys = sorted((k for k in (books or {}) if _FY_KEY_RE.match(str(k))), reverse=True)
    if not fys:
        return None
    return fys[0], Path(str(books[fys[0]]))


def _read_book(path: Path):
    """(guard, postable account list) for a book, cached on file mtime/size.
    The book is only read, never written. (None, []) when unreadable."""
    try:
        st = path.stat()
    except OSError:
        return None, []
    sig = (str(path), st.st_mtime_ns, st.st_size)
    if sig in _BOOK_CACHE:
        return _BOOK_CACHE[sig]
    try:
        from agents import gnucash_accounts as G  # noqa: PLC0415
        accs = G.load_accounts(path)
        years, bal = G.read_account_activity(path)
        out = (G.TargetGuard(accs, years, bal), G.postable_accounts(accs))
    except Exception:  # noqa: BLE001
        out = (None, [])
    if out[0] is None or not out[0].known:
        out = (None, [])
    _BOOK_CACHE.clear()
    _BOOK_CACHE[sig] = out
    return out


def _book_context(books: dict):
    """(guard, accounts, fy, note). note says, in words, what the pick-lists are
    built from or why they fall back to typing a path."""
    nb = _newest_book(books)
    if nb is None:
        return None, [], "", "No book is registered for this entity, so account paths are typed in (not checked)."
    fy, path = nb
    guard, accs = _read_book(path)
    if guard is None:
        return None, [], fy, (f"The FY {fy} book could not be read at `{path}`, so account paths are typed in (not checked).")
    return guard, accs, fy, f"Pick-lists come from the FY {fy} book. Hidden, placeholder and dormant accounts are not offered."


def _fy_start(fy: str) -> int | None:
    try:
        return int(fy[:4])
    except (ValueError, TypeError):
        return None


def _account_choices(books: dict) -> tuple[list[str], str]:
    guard, accs, fy, note = _book_context(books)
    if guard is None:
        return [], note
    start = _fy_start(fy)
    paths = sorted({a.path for a in accs if a.path and guard.dormant_reason(a.path, start) is None})
    return paths, note


def _check_accounts(books: dict, fields: dict) -> tuple[list[str], list[str]]:
    """(errors, warnings) for {label: path}. A hidden or placeholder account is
    refused; a path the book does not have, or that looks dormant, only warns."""
    fields = {k: str(v).strip() for k, v in fields.items() if v and str(v).strip()}
    if not fields:
        return [], []
    guard, _accs, fy, note = _book_context(books)
    if guard is None:
        return [], [f"Account paths were not checked: {note}"]
    errors: list[str] = []
    warnings: list[str] = []
    for label, path in fields.items():
        reason = guard.blocked_target_reason(path)
        if reason:
            errors.append(f"{label}: '{path}' cannot be used -- {reason}.")
        elif not guard.has_path(path):
            warnings.append(f"{label}: '{path}' is not an account in the FY {fy} book (saved anyway).")
        else:
            dormant = guard.dormant_reason(path, _fy_start(fy))
            if dormant:
                warnings.append(f"{label}: '{path}' looks dormant ({dormant}); saved anyway.")
    return errors, warnings


def _cred_default_text() -> str:
    from agents.skill_cc_spend_booking.matcher import CRED_MAX_OVERAGE
    return f"{CRED_MAX_OVERAGE:g}"


def _cred_text(raw) -> str:
    """CC-31: the CRED tolerance as shown on the form: the stored value; BLANK when the key is absent
    (the default is shown as the placeholder), so a save never writes the default into the yaml."""
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return ""
    try:
        return f"{_configs_mod().parse_cred_max_overage(raw):g}"
    except ValueError:
        return str(raw)           # a bad hand-edited value is shown as it is, so it can be corrected


def _entity_to_new_form(key: str, entities: dict) -> tuple:
    """UI-35 form values: foreign-dividend switches, drivers, the two card
    accounts, then the nine partner accounts in _PARTNER_ACCOUNT_KEYS order."""
    e = entities.get(key)
    if e is None:
        return (False, "", "", "", "", "", "", "", *([""] * len(_PARTNER_ACCOUNT_KEYS)), "", "", "", "", "")
    pa = e.partner_comp_accounts or {}
    return (
        bool(e.foreign_dividends_in_book), _format_kv_lines(e.foreign_dividends_in_book_by_ay),
        _format_drivers((e.extra_items or {}).get("partner_comp_drivers")),
        e.bank_service_charge_account or "", e.card_emi_interest_account or "",
        e.partner_firm_pan or "",
        ("" if e.cc_large_spend_threshold is None else f"{e.cc_large_spend_threshold:g}"),
        _cred_text(e.cc_cred_max_overage),
        *[str(pa.get(k) or "") for k in _PARTNER_ACCOUNT_KEYS],
        e.foreign_dividend_cash_account or "", e.foreign_dividend_tax_account or "",
        e.foreign_dividend_income_account or "",
        _format_capital_rule_lines((e.extra_items or {}).get("partner_comp_drivers"), "capital_rate"),
        _format_capital_rule_lines((e.extra_items or {}).get("partner_comp_drivers"), "capital_months_total"),
    )


def _save_entity(
    orig_key: str, new_key: str, name: str, pan: str, status: str, residency: str,
    dob: str, doi: str, address: str, father_name: str, aadhaar: str,
    business_subtree: str, workbook_match: str, default_regime: str, regime_by_ay_text: str,
    audit_case: bool, audit_case_by_ay_text: str,
    bf_losses_text: str, clubbing_notes_text: str,
    books_text: str = "",
    # Trailing + defaulted deliberately: every existing caller passes this
    # function's arguments positionally, and the basis is optional metadata.
    audit_case_basis: str = "",
    drawings_accounts_text: str | None = None,
    card_spend_default_account: str | None = None,
    reimbursement_markers_text: str | None = None,
    partner_admission_date: str | None = None,
    # UI-35: None == "an older caller that does not know this field": keep the stored value.
    bank_service_charge_account: str | None = None,
    card_emi_interest_account: str | None = None,
    partner_comp_accounts: dict | None = None,
    foreign_dividends_in_book: bool | None = None,
    foreign_dividends_in_book_by_ay_text: str | None = None,
    partner_comp_drivers_text: str | None = None,
    partner_firm_pan: str | None = None,
    cc_large_spend_threshold: str | None = None,
    cc_cred_max_overage: str | None = None,
    foreign_dividend_cash_account: str | None = None,
    foreign_dividend_tax_account: str | None = None,
    foreign_dividend_income_account: str | None = None,
    capital_rate_by_fy_text: str | None = None,
    capital_months_by_fy_text: str | None = None,
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

    # UI-35: the new fields are validated here, before anything is written.
    fdb_by_ay = None
    if foreign_dividends_in_book_by_ay_text is not None:
        fdb_by_ay, fdb_errors = _parse_kv_lines(foreign_dividends_in_book_by_ay_text, as_bool=True)
        kv_errors += [f"foreign dividends by AY: {e}" for e in fdb_errors]
    drivers = None
    if partner_comp_drivers_text is not None:
        drivers, driver_errors = _parse_drivers(partner_comp_drivers_text)
        kv_errors += driver_errors
    # H35-23: the capital-rule rate and months have their own per-FY fields.
    # None == keep what is stored; a value (even empty) replaces it.
    capital_rates = capital_months = None
    if capital_rate_by_fy_text is not None:
        capital_rates, _e = _parse_capital_rule_lines(capital_rate_by_fy_text, "capital_rate")
        kv_errors += _e
    if capital_months_by_fy_text is not None:
        capital_months, _e = _parse_capital_rule_lines(capital_months_by_fy_text, "capital_months_total")
        kv_errors += _e
    for _fy, _blk in (drivers or {}).items():
        for _ck in _CAPITAL_RULE_KEYS:
            if isinstance(_blk, dict) and _ck in _blk:
                kv_errors.append(
                    f"partner compensation drivers {_fy}: '{_ck}' has its own field "
                    "(Capital rule rate / Capital rule total months) -- remove it from this box.")
    if partner_comp_accounts is not None:
        for k in partner_comp_accounts:
            if k not in _PARTNER_ACCOUNT_KEYS:
                kv_errors.append(f"partner accounts: '{k}' is not a known account key.")
    firm_pan = None
    if partner_firm_pan is not None:
        firm_pan = (partner_firm_pan or "").strip().upper()
        if firm_pan and not re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]", firm_pan):
            kv_errors.append(
                f"Firm PAN {partner_firm_pan!r} is invalid -- expected format AAAAA0000A, or leave blank."
            )
    large_spend = None   # None == keep the stored value; "" == clear (use the global setting)
    if cc_large_spend_threshold is not None:
        text = str(cc_large_spend_threshold).strip()
        if text:
            large_spend = _configs_mod()._parse_positive_amount(text)
            if large_spend is None:
                kv_errors.append(
                    f"Large-spend limit {cc_large_spend_threshold!r} is not a positive number "
                    "-- enter rupees (e.g. 20000), or leave blank to use the global setting."
                )
        else:
            large_spend = ""
    cred_max = None   # None == keep the stored value; "" == clear (the default 50 applies)
    if cc_cred_max_overage is not None:
        text = str(cc_cred_max_overage).strip()
        if text:
            try:
                cred_max = _configs_mod().parse_cred_max_overage(text)
            except ValueError as exc:
                kv_errors.append(
                    f"CRED tolerance {cc_cred_max_overage!r} is refused ({exc}) -- enter rupees from 0 to "
                    f"{_configs_mod().CRED_MAX_OVERAGE_LIMIT:g}, or leave blank for the default."
                )
        else:
            cred_max = ""
    acct_fields = {}
    if bank_service_charge_account is not None:
        acct_fields["Bank service charge account"] = bank_service_charge_account
    if card_emi_interest_account is not None:
        acct_fields["Card EMI interest account"] = card_emi_interest_account
    for _label, _val in (("Foreign dividend cash account", foreign_dividend_cash_account),
                         ("Foreign dividend tax account", foreign_dividend_tax_account),
                         ("Foreign dividend income account", foreign_dividend_income_account)):
        if _val is not None:
            acct_fields[_label] = _val
    for k, v in (partner_comp_accounts or {}).items():
        acct_fields[f"Partner account '{k}'"] = v
    acct_errors, acct_warnings = _check_accounts(books, acct_fields)
    kv_errors += acct_errors

    # The dropdown's blank sentinel is a UI affordance, not a stored value.
    audit_case_basis = (audit_case_basis or "").strip()
    if audit_case_basis == _AUDIT_BASIS_BLANK:
        audit_case_basis = ""

    configs = _configs_mod()
    field_errors = configs.validate_entity_fields(
        key=new_key, name=name, pan=pan, status=status,
        dob=dob or None, doi=doi or None, default_regime=default_regime,
        regime_by_ay=regime_by_ay, audit_case_by_ay=audit_case_by_ay,
        audit_case_basis=audit_case_basis,
        partner_admission_date=(partner_admission_date or None),
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

    prior = entities.get(orig_key) if orig_key else None
    # extra_items is only partly owned by this form: start from the stored dict
    # and set or clear just the keys the form edits, so partner_comp_drivers
    # and any key a later version adds survive a save untouched.
    extra_items = copy.deepcopy(prior.extra_items) if prior is not None and prior.extra_items else {}
    bf_losses = _parse_list_lines(bf_losses_text)
    clubbing_notes = _parse_list_lines(clubbing_notes_text)
    for _k, _v in (("bf_losses", bf_losses), ("clubbing_notes", clubbing_notes)):
        if _v:
            extra_items[_k] = _v
        else:
            extra_items.pop(_k, None)
    if drivers is not None:
        # The YAML box never carries the capital-rule keys: keep the stored
        # ones, so saving the box does not wipe a hand-set value.
        _stored = (prior.extra_items or {}).get("partner_comp_drivers") if prior is not None else None
        merged = copy.deepcopy(drivers)
        for _fy, _blk in (_stored or {}).items():
            if not isinstance(_blk, dict):
                continue
            for _ck in _CAPITAL_RULE_KEYS:
                if _blk.get(_ck) is not None:
                    merged.setdefault(_fy, {})[_ck] = _blk[_ck]
        if merged:
            extra_items["partner_comp_drivers"] = merged
        else:
            extra_items.pop("partner_comp_drivers", None)
    for _ck, _new in (("capital_rate", capital_rates), ("capital_months_total", capital_months)):
        if _new is None:
            continue
        blocks = extra_items.get("partner_comp_drivers")
        blocks = copy.deepcopy(blocks) if isinstance(blocks, dict) else {}
        for _fy, _blk in list(blocks.items()):
            if isinstance(_blk, dict):
                _blk.pop(_ck, None)
        for _fy, _v in _new.items():
            blocks.setdefault(_fy, {})[_ck] = _v
        blocks = {k: v for k, v in blocks.items() if v}
        if blocks:
            extra_items["partner_comp_drivers"] = blocks
        else:
            extra_items.pop("partner_comp_drivers", None)

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
        audit_case_basis=audit_case_basis,
        extra_items=extra_items,
    )
    # Fields this form does not expose are carried over from the stored
    # profile, never dropped (a save used to silently erase partner_comp_accounts,
    # foreign_dividends_in_book*, drawings_accounts, card_spend_default_account).
    if prior is not None:
        for _f in _FORM_HIDDEN_FIELDS:
            setattr(new_profile, _f, copy.deepcopy(getattr(prior, _f)))
        if drawings_accounts_text is None:
            new_profile.drawings_accounts = copy.deepcopy(prior.drawings_accounts)
        if card_spend_default_account is None:
            new_profile.card_spend_default_account = prior.card_spend_default_account
        if reimbursement_markers_text is None:
            new_profile.reimbursement_markers = copy.deepcopy(prior.reimbursement_markers)
        if partner_admission_date is None:
            new_profile.partner_admission_date = prior.partner_admission_date
        for _f in _FORM_UI35_FIELDS:
            setattr(new_profile, _f, copy.deepcopy(getattr(prior, _f)))
    if drawings_accounts_text is not None:
        new_profile.drawings_accounts = _parse_list_lines(drawings_accounts_text)
    if card_spend_default_account is not None:
        new_profile.card_spend_default_account = (card_spend_default_account or "").strip()
    if reimbursement_markers_text is not None:
        new_profile.reimbursement_markers = _parse_marker_text(reimbursement_markers_text)
    if partner_admission_date is not None:
        new_profile.partner_admission_date = (partner_admission_date or "").strip() or None
    if bank_service_charge_account is not None:
        new_profile.bank_service_charge_account = bank_service_charge_account.strip()
    if card_emi_interest_account is not None:
        new_profile.card_emi_interest_account = card_emi_interest_account.strip()
    if large_spend is not None:
        new_profile.cc_large_spend_threshold = None if large_spend == "" else float(large_spend)
    if cred_max is not None:
        new_profile.cc_cred_max_overage = None if cred_max == "" else float(cred_max)
    if foreign_dividend_cash_account is not None:
        new_profile.foreign_dividend_cash_account = foreign_dividend_cash_account.strip()
    if foreign_dividend_tax_account is not None:
        new_profile.foreign_dividend_tax_account = foreign_dividend_tax_account.strip()
    if foreign_dividend_income_account is not None:
        new_profile.foreign_dividend_income_account = foreign_dividend_income_account.strip()
    if partner_comp_accounts is not None:
        # the form owns the nine known keys; any other stored key is kept
        merged = dict(new_profile.partner_comp_accounts or {})
        for k in _PARTNER_ACCOUNT_KEYS:
            v = str(partner_comp_accounts.get(k) or "").strip()
            if v:
                merged[k] = v
            else:
                merged.pop(k, None)
        new_profile.partner_comp_accounts = merged
    if foreign_dividends_in_book is not None:
        new_profile.foreign_dividends_in_book = bool(foreign_dividends_in_book)
    if fdb_by_ay is not None:
        new_profile.foreign_dividends_in_book_by_ay = fdb_by_ay
    if firm_pan is not None:
        new_profile.partner_firm_pan = firm_pan

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

    lines += [f"Warning: {w}" for w in acct_warnings]
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
# "Other dropdown data" panel -- read-only provenance for the dropdowns that
# are NOT backed by entities.yaml.
# ---------------------------------------------------------------------------

_DROPDOWN_SOURCES_PLACEHOLDER = "_Open this tab (or click Refresh) to inspect the other dropdown sources._"

_MAX_LISTED = 12


def _fmt_choices(choices, empty: str) -> str:
    """One-line, truncated rendering of an (label, value) option list."""
    if not choices:
        return f"_{empty}_"
    labels = [str(c[0]) if isinstance(c, (list, tuple)) else str(c) for c in choices]
    shown = ", ".join(f"`{lab}`" for lab in labels[:_MAX_LISTED])
    if len(labels) > _MAX_LISTED:
        shown += f", ... (+{len(labels) - _MAX_LISTED} more)"
    return f"{len(labels)} option(s): {shown}"


def _dropdown_sources_md() -> str:
    """Markdown describing every non-entity dropdown source and its current
    values.

    Deliberately read-only. Unlike Entity, none of these is hand-maintained
    data: the AY list is derived from the shipped tax-rules files plus the
    Data\\itr\\rules overlay, the bank list from the parsers actually
    installed, and the model list from the configured LLM endpoint. There is
    no file to edit for any of them -- you change them by shipping a rules
    file, adding a parser, or pulling a model -- so this panel explains where
    each one comes from instead of pretending to be an editor.
    """
    from . import _generic

    try:
        ay = _generic._options_from_itr_ay_years()
    except Exception as exc:  # noqa: BLE001
        ay = []
        ay_err = f" (lookup failed: {exc})"
    else:
        ay_err = ""

    try:
        banks = _generic._options_from_banks()
    except Exception as exc:  # noqa: BLE001
        banks = []
        banks_err = f" (lookup failed: {exc})"
    else:
        banks_err = ""

    try:
        models = _generic._refresh_models(use_cache=True)
    except Exception as exc:  # noqa: BLE001
        models = []
        models_err = f" (lookup failed: {exc})"
    else:
        models_err = ""

    try:
        entities = _entity_choices()
    except Exception:  # noqa: BLE001
        entities = []

    data_root = _config_mod.data_root_dir()
    try:
        rules_base = _config_mod.canonical_itr_rules_dir()
    except Exception:  # noqa: BLE001
        rules_base = "(unavailable)"

    return "\n\n".join([
        "#### Entity  -- *editable, right here*",
        f"Source: `{data_root / 'itr' / 'entities.yaml'}` (this tab).  "
        f"{_fmt_choices(entities, 'no entities defined yet')}",
        "#### Assessment / financial year  -- *derived, not editable*",
        f"Source: `tax_rules_*.yaml` keyed on `meta.fy`, read from the shipped base "
        f"`{rules_base}` **union** the overlay `{data_root / 'itr' / 'rules'}` "
        f"(overlay wins on a collision). Add a year by shipping or dropping in a rules "
        f"file -- there is no list to maintain.{ay_err}  \n{_fmt_choices(ay, 'no rules files found')}",
        "#### Bank  -- *derived, not editable*",
        f"Source: `agents.banks.discover()` -- the statement parsers actually installed, "
        f"by `display_name`, plus a trailing `Other Bank (CSV)` for the generic "
        f"LLM-normalised path. Add a bank by adding its parser.{banks_err}  \n"
        f"{_fmt_choices(banks, 'no parsers discovered')}",
        "#### Model  -- *derived, not editable*",
        f"Source: the active LLM endpoint in `config.yaml` (Settings tab), queried live; "
        f"falls back to the configured default model when the endpoint is unreachable. "
        f"Add a model by pulling it on the endpoint.{models_err}  \n"
        f"{_fmt_choices(models, 'endpoint unreachable and no default configured')}",
    ])


# ---------------------------------------------------------------------------
# Gradio tab renderer.
# ---------------------------------------------------------------------------

def render(container_tab=None) -> None:
    """Render the Entities tab. Must be called inside gr.Tab().

    Despite the module name this is app-wide master data, not an ITR-only
    editor: `entities.yaml` also drives the entity dropdown on every skill
    that asks for a GnuCash book (Banks "Bank - Convert to GnuCash" + Review,
    Inter-entity Reconcile, 26AS Convert to GnuCash + Review, KRChoksey Convert to
    GnuCash + Review, AIS Reconcile), which is why the tab sits alongside those
    workflows rather than inside ITR.
    """
    gr.Markdown(
        "## Entities\n\n"
        "Add, modify, or delete taxpayer entities in `entities.yaml`. "
        "Select an entity below to edit it, or leave `(new entity)` "
        "selected to add one. A new entity has no mapping yet -- that's "
        "fine, the ITR Workbook cold-starts it.\n\n"
        "This is the single source for every **Entity** dropdown in the "
        "app, and the per-financial-year **books** registered below are "
        "what those dropdowns auto-fill the GnuCash book path from.\n\n"
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
                label="Extended due date -- s.139(1) due date is 31 Oct, not 31 Jul",
                value=False,
            )
            audit_case_by_ay_box = gr.Textbox(
                label="Extended due date by AY (one per line, 'AY = true|false')",
                placeholder="2025-26 = false",
                lines=3, interactive=True,
            )
            audit_case_basis_dd = gr.Dropdown(
                label="Why 31 Oct applies (optional -- wording only, never the date)",
                choices=list(_AUDIT_BASIS_CHOICES),
                value=_AUDIT_BASIS_BLANK,
                interactive=True,
                info="s.139(1) Expl. 2 gives 31 Oct by several routes. "
                     "'partner_of_audited_firm' = working partner of a firm liable to "
                     "audit, who is not an audit case himself. Blank reads as s.44AB.",
            )
            drawings_accounts_box = gr.Textbox(
                label="Drawings accounts (GnuCash mapper; one account path per line)",
                placeholder="Equity:Drawings",
                lines=2, interactive=True,
                info="Money IN lands here only via a History match or the employer "
                     "reimbursement rule (and the rule only when exactly one is listed). "
                     "A path that is not in the book, or is hidden/placeholder, is ignored. "
                     "Blank = unconfigured.",
            )
            card_default_box = gr.Textbox(
                label="Card-spend default account (GnuCash mapper; one account path)",
                placeholder="Equity:Drawings",
                lines=1, interactive=True,
                info="Card spends nothing else matched go here at low confidence. Blank = Suspense.",
            )
            reimbursement_markers_box = gr.Textbox(
                label="Reimbursement markers (GnuCash mapper; one per line or comma-separated)",
                placeholder="ZZRB",
                lines=2, interactive=True,
                info="A NEFT FROM credit whose narration contains one of these whole words is "
                     "routed to Drawings (needs exactly one Drawings account above). "
                     "Blank = the rule never fires.",
            )
            partner_admission_box = gr.Textbox(
                label="Date admitted as partner (YYYY-MM-DD, optional)", interactive=True,
                info="Only for a partner of a firm. The Partner Compensation skill counts "
                     "whole months from this date to 31 March of each year for the capital "
                     "rule (40% of target compensation over 48 months). Blank = the "
                     "capital check says the date is not set.",
            )
            bf_losses_box = gr.Textbox(
                label="B/f losses (one per line)", lines=3, interactive=True,
            )
            clubbing_notes_box = gr.Textbox(
                label="Clubbing notes (one per line)", lines=3, interactive=True,
            )

            with gr.Accordion("Foreign dividends", open=False):
                fdb_cb = gr.Checkbox(
                    label="Foreign dividends are already in the book",
                    value=False,
                    info="Off: the broker report's foreign dividends are ADDED to Other Sources. "
                         "On: the book already records them, so they stay out of the total.",
                )
                fdb_by_ay_box = gr.Textbox(
                    label="Foreign dividends already in the book by AY (one per line, 'AY = true|false')",
                    placeholder="2026-27 = true", lines=2, interactive=True,
                )
                fd_cash_dd = gr.Dropdown(
                    label="Foreign dividend cash account", choices=[], value="",
                    allow_custom_value=True, interactive=True,
                    info="Where the broker's net dividend cash sits. Needed for the foreign-dividend journal; blank = it stops and says so.",
                )
                fd_tax_dd = gr.Dropdown(
                    label="Foreign dividend tax account", choices=[], value="",
                    allow_custom_value=True, interactive=True,
                    info="Receives the foreign tax withheld (debit).",
                )
                fd_income_dd = gr.Dropdown(
                    label="Foreign dividend income account", choices=[], value="",
                    allow_custom_value=True, interactive=True,
                    info="Receives the gross foreign dividend (credit).",
                )
            with gr.Accordion("Card accounts (card-spend booking)", open=False):
                accounts_note_md = gr.Markdown("_Select an entity to load account pick-lists from its book._")
                bank_charge_dd = gr.Dropdown(
                    label="Bank service charge account", choices=[], value="",
                    allow_custom_value=True, interactive=True,
                    info="Card fees, interest and GST on fees. Blank = the card-spend booking stops with a named error when a fee row needs it.",
                )
                emi_interest_dd = gr.Dropdown(
                    label="Card EMI interest account", choices=[], value="",
                    allow_custom_value=True, interactive=True,
                    info="Card EMI interest only; never the Bank service charge account.",
                )
                large_spend_box = gr.Textbox(
                    label="Large-spend limit (rupees)", placeholder="20000", interactive=True,
                    info="A card spend of this amount or more is flagged as a possible asset for THIS entity. "
                         "Blank = use the global setting. Zero, negative or text is refused.",
                )
                cred_max_box = gr.Textbox(
                    label="CRED tolerance (rupees)", placeholder=f"{_cred_default_text()} (default)", interactive=True,
                    info="The most a CRED payment line on a statement may exceed the bank amount and still be "
                         "matched, for THIS entity. Blank = the default (50). A number from 0 to 500; text, "
                         "negative or above 500 is refused.",
                )
            with gr.Accordion("Partner of a firm (Partner Compensation)", open=False):
                firm_pan_box = gr.Textbox(
                    label="Firm PAN (the firm/LLP/AOP this entity is a partner of)",
                    placeholder="AAAAA0000A", interactive=True,
                    info="Used by the Schedule AL firm interest line. Blank = none; a malformed PAN is refused.",
                )
                partner_dds = [
                    gr.Dropdown(
                        label=f"Partner account: {k}", choices=[], value="",
                        allow_custom_value=True, interactive=True,
                    )
                    for k in _PARTNER_ACCOUNT_KEYS
                ]
                drivers_box = gr.Textbox(
                    label="Partner compensation drivers (YAML, one block per FY)",
                    placeholder="2025-26:\n  firms_tax_rate: 0.25\n  target_compensation: 1200000\n  remuneration_tds_section: 194T",
                    lines=6, interactive=True,
                    info="Known drivers: " + ", ".join(_DRIVER_KEYS) + ". Anything else, or a wrong type, "
                         "is refused and nothing is saved. Blank removes all drivers.",
                )
                capital_rate_box = gr.Textbox(
                    label="Capital rule rate by FY (percent, one per line 'FY = percent')",
                    placeholder="2025-26 = 40", lines=2, interactive=True,
                    info="Share of target compensation required as capital. Blank = the default, 40%. "
                         "A percent above 0 and below 100. Informational only; no journal uses it.",
                )
                capital_months_box = gr.Textbox(
                    label="Capital rule total months by FY (one per line 'FY = months')",
                    placeholder="2025-26 = 48", lines=2, interactive=True,
                    info="Months over which the capital is built up. Blank = the default, 48. A positive whole number.",
                )
            _new_inputs = [fdb_cb, fdb_by_ay_box, drivers_box, bank_charge_dd, emi_interest_dd, firm_pan_box, large_spend_box, cred_max_box, *partner_dds,
                           fd_cash_dd, fd_tax_dd, fd_income_dd, capital_rate_box, capital_months_box]

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
        audit_case_cb, audit_case_by_ay_box, audit_case_basis_dd,
        partner_admission_box,
        bf_losses_box, clubbing_notes_box,
        books_box,
        drawings_accounts_box, card_default_box, reimbursement_markers_box,
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

    def _new_form_updates(key):
        """UI-35 values for the new controls; the account pick-lists are rebuilt
        from the entity's newest registered book."""
        entities = _load_entities()
        fdb, fdb_ay, drv, bank, emi, fpan, lsp, crm, *partner = _entity_to_new_form(key or "", entities)
        cap_rate, cap_months = partner[-2:]
        partner = partner[:-2]
        books = dict(entities[key].books) if (key or "") in entities else {}
        choices, note = _account_choices(books)
        def dd(v):
            return gr.update(choices=choices, value=v)
        return (fdb, fdb_ay, drv, dd(bank), dd(emi), fpan, lsp, crm, *[dd(v) for v in partner], cap_rate, cap_months, "_" + note + "_")

    _new_outputs = [*_new_inputs, accounts_note_md]

    def _on_select(key):
        entities = _load_entities()
        values = _entity_to_form(key or "", entities)
        status_msg = f"_Loaded `{key}`. Edit and Save, or Delete._" if key else "_New entity -- fill in the fields and Save._"
        return (*values, *_new_form_updates(key), key or "", status_msg)

    entity_dropdown.change(
        fn=_on_select,
        inputs=[entity_dropdown],
        outputs=[*_form_outputs, *_new_outputs, orig_key_state, save_status],
    )

    def _on_save(
        orig_key, new_key, name, pan, status, residency, dob, doi, address,
        father_name, aadhaar, business_subtree, workbook_match, default_regime, regime_by_ay_text,
        audit_case, audit_case_by_ay_text, audit_case_basis,
        bf_losses_text, clubbing_notes_text,
        books_text, drawings_accounts_text, card_spend_default_account,
        reimbursement_markers_text, partner_admission_date,
        fdb, fdb_by_ay_text, drivers_text, bank_charge, emi_interest, firm_pan, large_spend, cred_max, *partner_values,
    ):
        msg = _save_entity(
            orig_key, new_key, name, pan, status, residency, dob, doi, address,
            father_name, aadhaar, business_subtree, workbook_match, default_regime, regime_by_ay_text,
            audit_case, audit_case_by_ay_text,
            bf_losses_text, clubbing_notes_text,
            books_text,
            audit_case_basis=audit_case_basis,
            drawings_accounts_text=drawings_accounts_text,
            card_spend_default_account=card_spend_default_account,
            reimbursement_markers_text=reimbursement_markers_text,
            partner_admission_date=partner_admission_date,
            bank_service_charge_account=bank_charge or "",
            card_emi_interest_account=emi_interest or "",
            partner_comp_accounts=dict(zip(_PARTNER_ACCOUNT_KEYS, partner_values[:len(_PARTNER_ACCOUNT_KEYS)])),
            foreign_dividend_cash_account=(partner_values[len(_PARTNER_ACCOUNT_KEYS)] or ""),
            foreign_dividend_tax_account=(partner_values[len(_PARTNER_ACCOUNT_KEYS) + 1] or ""),
            foreign_dividend_income_account=(partner_values[len(_PARTNER_ACCOUNT_KEYS) + 2] or ""),
            foreign_dividends_in_book=bool(fdb),
            foreign_dividends_in_book_by_ay_text=fdb_by_ay_text or "",
            partner_comp_drivers_text=drivers_text or "",
            capital_rate_by_fy_text=(partner_values[len(_PARTNER_ACCOUNT_KEYS) + 3] or ""),
            capital_months_by_fy_text=(partner_values[len(_PARTNER_ACCOUNT_KEYS) + 4] or ""),
            partner_firm_pan=firm_pan or "",
            cc_large_spend_threshold=large_spend or "",
            cc_cred_max_overage=cred_max or "",
        )
        new_choices = _entity_choices()
        new_orig = new_key.strip() if msg.startswith("**Saved**") else orig_key
        return msg, gr.update(choices=new_choices, value=new_orig), new_orig

    save_btn.click(
        fn=_on_save,
        inputs=[orig_key_state, key_box, name_box, pan_box, status_dd, residency_box,
                dob_box, doi_box, address_box, father_name_box, aadhaar_box,
                business_subtree_box, workbook_match_box, default_regime_dd, regime_by_ay_box,
                audit_case_cb, audit_case_by_ay_box, audit_case_basis_dd,
                bf_losses_box, clubbing_notes_box,
                books_box, drawings_accounts_box, card_default_box, reimbursement_markers_box,
                partner_admission_box, *_new_inputs],
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
            *blanks, *_new_form_updates(new_value), False, False,
        )

    delete_btn.click(
        fn=_on_delete,
        inputs=[entity_dropdown, confirm_1_cb, confirm_2_cb],
        outputs=[save_status, entity_dropdown, orig_key_state, *_form_outputs, *_new_outputs, confirm_1_cb, confirm_2_cb],
    )

    # --- Credit cards (CC-28): settings/cc_cards.yaml, the one card list ----
    with gr.Accordion("Credit cards (the card list every CC skill reads)", open=False):
        from . import _entity_cards  # noqa: PLC0415
        _entity_cards.render(container_tab=container_tab)

    # --- Other dropdown data (read-only) -----------------------------------
    # Entities is the only dropdown source in the app that is hand-maintained
    # data; the other three are DERIVED and have no file to edit. Rather than
    # invent editors for them, surface where each one comes from and what it
    # currently resolves to, so this tab answers "where is this dropdown
    # managed?" for every dropdown, not just Entity.
    with gr.Accordion("Other dropdown data (read-only)", open=False):
        sources_md = gr.Markdown(_DROPDOWN_SOURCES_PLACEHOLDER)
        sources_refresh_btn = gr.Button("Refresh", scale=0, min_width=100)
        sources_refresh_btn.click(fn=_dropdown_sources_md, inputs=[], outputs=[sources_md])

    if container_tab is not None:
        # Populated on tab open rather than at build time: the model list can
        # reach out to the LLM endpoint, and app construction must not block
        # on a network call.
        container_tab.select(fn=_dropdown_sources_md, inputs=[], outputs=[sources_md])
