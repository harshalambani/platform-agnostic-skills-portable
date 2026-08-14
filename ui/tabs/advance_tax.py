"""
ui/tabs/advance_tax.py -- "Advance Tax" tab (ITR > Advance Tax).

Manual-input mode is the PRIMARY path: every field on this tab is a plain,
editable value and the tab is fully usable with NO GnuCash book, NO mapping
and NO entity registry at all -- see advance_tax.py's own module docstring
("MANUAL -- the caller constructs EstimateInput directly... works
standalone"). "Start from book" (below the manual form) is a pure
convenience: it pre-fills the SAME editable fields via advance_tax.from_book,
never a requirement, and its own failure never blocks manual use of the tab.

Follows the established hand-written-tab conventions:
  - ui/tabs/itr_entities.py: the "one key = value per line" / "one item per
    line" textbox editors (simpler and more testable than a Dataframe for a
    handful of rows), the lazy `_ensure_itr_scripts_importable()` sys.path
    shim, and the named-item CRUD shape (dropdown + Load/Save/Delete,
    archive-not-delete on removal).
  - ui/tabs/itr_mapping_review.py: path anchoring exclusively via
    ui._config.data_root_dir() (never a bare "Data/" prefix -- see
    test_path_anchoring.py), and the entity-choices delegation to
    ._generic._options_from_itr_entities().
  - ui/tabs/_generic.py: the download-staging + open-folder pattern (copy
    the produced file into _config.download_staging_dir() before handing it
    to gr.DownloadButton, since Gradio's allowed_paths would otherwise expose
    the whole outputs/ folder -- security finding #5).

Persistence: named estimates are saved via advance_tax.save_estimate() to
Data/advance_tax/<name>.json, ALWAYS with an explicit
data_root=str(_config_mod.data_root_dir()) (never CWD-relative -- see
advance_tax.py's _estimate_dir() docstring, which calls this UI out by name
as the intended caller of that override).
"""
from __future__ import annotations

import datetime
import os
import re
import shutil
import sys
from pathlib import Path

import gradio as gr

from .. import _config as _config_mod

# ---------------------------------------------------------------------------
# Import the ITR Workbook skill's flat (non-package) scripts/ modules, same
# pattern as itr_entities.py / itr_mapping_review.py.
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


def _adv_mod():
    _ensure_itr_scripts_importable()
    import advance_tax  # noqa: PLC0415
    return advance_tax


_STATUS_CHOICES = ("Individual", "HUF")
_REGIME_CHOICES = ("old", "new")
_GAIN_TYPES = ("STCG_111A", "LTCG_112A", "OTHER")

_HEAD_NAMES = ("salary", "remuneration", "rent", "interest")
_HEAD_LABELS = {
    "salary": "Salary",
    "remuneration": "Remuneration + interest on capital (PGBP)",
    "rent": "House property (net)",
    "interest": "Other-sources interest",
}


# ---------------------------------------------------------------------------
# Estimate persistence paths (anchored via data_root_dir()).
# ---------------------------------------------------------------------------

def _estimates_dir() -> Path:
    return _config_mod.data_root_dir() / "advance_tax"


def _archive_dir() -> Path:
    return _estimates_dir() / "_archive"


# Any free-text name coming from the UI (saved-estimate name, entity name
# used to look up a mapping file) MUST pass through _sanitize_name() before
# it is used in ANY filesystem path construction below -- closes the
# py/path-injection dataflow CodeQL flagged from these values into
# save/load/delete and the book-prefill mapping lookup.
_SAFE_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,119}$")


def _sanitize_name(raw: str, *, what: str = "name") -> str:
    """Validate + sanitize a user-supplied name before it is used in ANY
    filesystem path join below. os.path.basename() strips any directory
    components first (this is the specific operation static analysis for
    path-injection recognizes as taint-clearing, since it discards
    everything up to and including the last path separator), and the
    regex then further rejects anything but a short run of plain
    characters -- so a value like '../../etc/passwd' is reduced to
    'passwd' by basename() and then still has to match the allowlist."""
    name = os.path.basename((raw or "").strip())
    if not name or not _SAFE_NAME_RE.match(name):
        raise ValueError(
            f"{what} {raw!r} is not valid -- use only letters, digits, spaces, "
            "'.', '_', '-' (no path separators)."
        )
    return name


def _safe_path_in(base_dir: Path, filename: str) -> Path:
    """Join filename onto base_dir and verify the resolved result is still
    contained inside base_dir -- same is_relative_to() containment check
    ui/tabs/_generic.py uses for staged downloads. base_dir need not exist
    yet (estimates/archive dirs are created at runtime)."""
    base = base_dir.resolve()
    candidate = (base_dir / filename).resolve()
    if not candidate.is_relative_to(base):
        raise ValueError(f"Resolved path {candidate} escapes {base}.")
    return candidate


def _estimate_choices() -> list[tuple[str, str]]:
    adv = _adv_mod()
    names = adv.list_estimates(data_root=str(_config_mod.data_root_dir()))
    return [("(new estimate)", "")] + [(n, n) for n in names]


# ---------------------------------------------------------------------------
# "one item per line" text editors for the list-shaped fields.
# ---------------------------------------------------------------------------

def _format_paid_lines(paid: list) -> str:
    return "\n".join(f"{d.isoformat()} = {amt}" for (d, amt) in (paid or []))


def _parse_paid_lines(text: str) -> tuple[list, list[str]]:
    """Parse "YYYY-MM-DD = amount" lines -> [(date, float), ...]. Returns
    (items, errors); blank lines and '#' comments are ignored."""
    out: list = []
    errors: list[str] = []
    for lineno, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            errors.append(f"line {lineno} ({raw_line!r}): expected 'YYYY-MM-DD = amount'.")
            continue
        d_text, amt_text = line.split("=", 1)
        d_text = d_text.strip()
        amt_text = amt_text.strip()
        try:
            d = datetime.date.fromisoformat(d_text)
        except ValueError:
            errors.append(f"line {lineno} ({raw_line!r}): {d_text!r} is not a YYYY-MM-DD date.")
            continue
        try:
            amt = float(amt_text)
        except ValueError:
            errors.append(f"line {lineno} ({raw_line!r}): {amt_text!r} is not a number.")
            continue
        out.append((d, amt))
    return out, errors


def _format_cg_lines(items: list) -> str:
    lines = []
    for i in items or []:
        sale = i.sale_date.isoformat() if i.sale_date else ""
        note = i.note or ""
        lines.append(f"{i.amount}, {i.gain_type}, {sale}, {note}")
    return "\n".join(lines)


def _parse_cg_lines(text: str) -> tuple[list, list[str]]:
    """Parse "amount, gain_type, sale_date, note" lines -> [CapitalGainItem,
    ...]. sale_date and note are optional (leave blank). Blank lines and
    '#' comments are ignored."""
    adv = _adv_mod()
    out: list = []
    errors: list[str] = []
    for lineno, raw_line in enumerate((text or "").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 2:
            errors.append(
                f"line {lineno} ({raw_line!r}): expected 'amount, gain_type[, sale_date[, note]]'."
            )
            continue
        amt_text, gain_type = parts[0], parts[1]
        sale_text = parts[2] if len(parts) > 2 else ""
        note = parts[3] if len(parts) > 3 else ""
        try:
            amt = float(amt_text)
        except ValueError:
            errors.append(f"line {lineno} ({raw_line!r}): {amt_text!r} is not a number.")
            continue
        if gain_type not in _GAIN_TYPES:
            errors.append(
                f"line {lineno} ({raw_line!r}): gain_type must be one of {', '.join(_GAIN_TYPES)}."
            )
            continue
        sale_date = None
        if sale_text:
            try:
                sale_date = datetime.date.fromisoformat(sale_text)
            except ValueError:
                errors.append(f"line {lineno} ({raw_line!r}): {sale_text!r} is not a YYYY-MM-DD date.")
                continue
        out.append(adv.CapitalGainItem(amount=amt, gain_type=gain_type, sale_date=sale_date, note=note or None))
    return out, errors


# ---------------------------------------------------------------------------
# Form <-> EstimateInput.
# ---------------------------------------------------------------------------

# Field order shared by every function that returns/consumes the full form
# tuple, so render()'s wiring and these helpers can't drift apart silently.
_FORM_FIELDS = (
    "entity_name", "fy", "regime", "status", "dob", "residency", "as_of",
    "salary_actual", "salary_remainder",
    "remuneration_actual", "remuneration_remainder",
    "rent_actual", "rent_remainder",
    "interest_actual", "interest_remainder",
    "other_sources_other", "deductions_via", "tds_to_date",
    "advance_tax_paid_text", "capital_gains_text",
)


def _blank_form() -> tuple:
    return (
        "", "", "new", "Individual", "", "", datetime.date.today().isoformat(),
        0.0, None, 0.0, None, 0.0, None, 0.0, None,
        0.0, 0.0, 0.0, "", "",
    )


def _input_to_form(input_data) -> tuple:
    return (
        input_data.entity_name, input_data.fy, input_data.regime, input_data.status,
        input_data.dob or "", input_data.residency or "", input_data.as_of.isoformat(),
        input_data.salary.actual_to_date, input_data.salary.projected_remainder,
        input_data.remuneration.actual_to_date, input_data.remuneration.projected_remainder,
        input_data.rent.actual_to_date, input_data.rent.projected_remainder,
        input_data.interest.actual_to_date, input_data.interest.projected_remainder,
        input_data.other_sources_other, input_data.deductions_via, input_data.tds_to_date,
        _format_paid_lines(input_data.advance_tax_paid),
        _format_cg_lines(input_data.capital_gains),
    )


def _form_to_input(*values) -> tuple:
    """Build an EstimateInput from the form's raw component values (positional,
    in _FORM_FIELDS order). Returns (input_data_or_None, errors)."""
    adv = _adv_mod()
    d = dict(zip(_FORM_FIELDS, values))
    errors: list[str] = []

    entity_name = (d["entity_name"] or "").strip()
    fy = (d["fy"] or "").strip()
    if not entity_name:
        errors.append("Entity name is required.")
    if not fy:
        errors.append("Financial year (YYYY-YY) is required.")

    try:
        as_of = datetime.date.fromisoformat((d["as_of"] or "").strip())
    except ValueError:
        errors.append(f"'As of' date {d['as_of']!r} is not a valid YYYY-MM-DD date.")
        as_of = datetime.date.today()

    paid, paid_errors = _parse_paid_lines(d["advance_tax_paid_text"])
    errors.extend(f"Advance tax paid: {e}" for e in paid_errors)

    cg_items, cg_errors = _parse_cg_lines(d["capital_gains_text"])
    errors.extend(f"Capital gains: {e}" for e in cg_errors)

    if errors:
        return None, errors

    def _head(actual_key: str, remainder_key: str):
        return adv.ProjectedHead(
            actual_to_date=float(d[actual_key] or 0.0),
            projected_remainder=(None if d[remainder_key] in (None, "") else float(d[remainder_key])),
        )

    input_data = adv.EstimateInput(
        entity_name=entity_name, fy=fy, regime=d["regime"], status=d["status"],
        dob=(d["dob"] or "").strip() or None, residency=(d["residency"] or "").strip() or None,
        as_of=as_of,
        salary=_head("salary_actual", "salary_remainder"),
        remuneration=_head("remuneration_actual", "remuneration_remainder"),
        rent=_head("rent_actual", "rent_remainder"),
        interest=_head("interest_actual", "interest_remainder"),
        other_sources_other=float(d["other_sources_other"] or 0.0),
        capital_gains=cg_items,
        deductions_via=float(d["deductions_via"] or 0.0),
        tds_to_date=float(d["tds_to_date"] or 0.0),
        advance_tax_paid=paid,
    )
    return input_data, []


# ---------------------------------------------------------------------------
# Handlers.
# ---------------------------------------------------------------------------

def _handle_load(name: str):
    if not name:
        return (*_blank_form(), "Pick a saved estimate, or leave as (new estimate) to start blank.")
    try:
        name = _sanitize_name(name, what="Estimate name")
    except ValueError as e:
        return (*_blank_form(), f"Error: {e}")
    adv = _adv_mod()
    try:
        input_data = adv.load_estimate(name, data_root=str(_config_mod.data_root_dir()))
    except Exception as e:
        return (*_blank_form(), f"Error loading {name!r}: {e}")
    return (*_input_to_form(input_data), f"Loaded estimate **{name}**.")


def _handle_save(name: str, *values):
    try:
        name = _sanitize_name(name, what="Estimate name")
    except ValueError as e:
        return f"Error: {e}"
    input_data, errors = _form_to_input(*values)
    if errors:
        return "Error(s):\n" + "\n".join(f"- {e}" for e in errors)
    adv = _adv_mod()
    try:
        path = adv.save_estimate(name, input_data, data_root=str(_config_mod.data_root_dir()))
    except Exception as e:
        return f"Error saving: {e}"
    return f"Saved to `{path}`."


def _handle_delete(name: str):
    """Archive (never delete) a saved estimate -- same discipline as
    itr_entities.py's mapping/filed-return removal."""
    if not name:
        return "Pick a saved estimate to delete first.", gr.update(choices=_estimate_choices())
    try:
        name = _sanitize_name(name, what="Estimate name")
        src = _safe_path_in(_estimates_dir(), f"{name}.json")
    except ValueError as e:
        return f"Error: {e}", gr.update(choices=_estimate_choices())
    if not src.is_file():
        return f"No saved estimate named {name!r}.", gr.update(choices=_estimate_choices())
    archive_dir = _archive_dir()
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        dest = _safe_path_in(archive_dir, f"{name}.json.deleted-{stamp}")
    except ValueError as e:
        return f"Error: {e}", gr.update(choices=_estimate_choices())
    shutil.move(str(src), str(dest))
    return f"Archived to `{dest}` (not permanently deleted).", gr.update(choices=_estimate_choices())


def _handle_annualise(*values):
    input_data, errors = _form_to_input(*values)
    if errors:
        return (*[gr.update() for _ in _FORM_FIELDS], "Error(s):\n" + "\n".join(f"- {e}" for e in errors))
    adv = _adv_mod()
    annualised = adv.annualise(input_data)
    return (*_input_to_form(annualised), "Annualised the still-blank recurring heads (straight-line).")


def _handle_compute(*values):
    """Compute both regimes and write the .xlsx workbook. Returns
    (status_markdown, download_update, open_folder_path_update)."""
    input_data, errors = _form_to_input(*values)
    if errors:
        return "Error(s):\n" + "\n".join(f"- {e}" for e in errors), gr.update(interactive=False, value=None), gr.update(value="")

    adv = _adv_mod()
    _ensure_itr_scripts_importable()
    import rules as rules_engine  # noqa: PLC0415
    import write_advance_tax_workbook as writer  # noqa: PLC0415

    overlay_dir = _config_mod.data_root_dir() / "itr" / "rules"
    try:
        rules = rules_engine.load_rules([overlay_dir, rules_engine.canonical_rules_dir()], input_data.fy)
    except rules_engine.RulesError as e:
        return f"Error: no tax rules found for FY {input_data.fy}: {e}", gr.update(interactive=False, value=None), gr.update(value="")

    try:
        estimate = adv.build_estimate(input_data, rules)
    except Exception as e:
        return f"Error computing estimate: {e}", gr.update(interactive=False, value=None), gr.update(value="")

    # Persisted to Data/advance_tax/ (created at runtime if absent) -- NOT
    # the generic Data/outputs/ folder, so the output workbook lives
    # alongside the saved-estimate JSON for this feature, per the brief.
    out_dir = _estimates_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.datetime.now().strftime("%Y-%m-%d-%H%M%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in input_data.entity_name) or "estimate"
    out_path = out_dir / f"{stamp}-{safe_name}-advance-tax.xlsx"
    try:
        writer.write_workbook(estimate, out_path)
    except Exception as e:
        return f"Error writing workbook: {e}", gr.update(interactive=False, value=None), gr.update(value="")

    # Download-staging (security: same pattern as _generic._make_run_handler
    # -- Gradio's allowed_paths would otherwise expose the whole outputs/
    # folder, so only this one file is copied into the narrower staging dir).
    staging = _config_mod.download_staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    served_path = staging / out_path.name
    shutil.copy2(out_path, served_path)

    lines = [
        "### Done\n",
        f"**New regime tax liability:** {estimate.new_regime.tax_liability:,.2f}",
        f"**Old regime tax liability:** {estimate.old_regime.tax_liability:,.2f}",
        f"**Saved to:** {out_path.resolve()}",
    ]
    if estimate.unprojected_heads:
        lines.append(
            "\n**Flag -- unprojected heads (treated as NOT estimated):** "
            + ", ".join(_HEAD_LABELS.get(h, h) for h in estimate.unprojected_heads)
        )
    if estimate.undated_capital_gains:
        lines.append(
            f"\n**Flag -- {len(estimate.undated_capital_gains)} undated capital-gains item(s)** "
            f"totalling {estimate.undated_cg_amount:,.2f} -- no s.234C first-proviso relief given."
        )
    return (
        "\n\n".join(lines),
        gr.update(value=str(served_path.resolve()), interactive=True),
        gr.update(value=str(out_path.resolve())),
    )


def _open_output_folder(target_path: str):
    if target_path:
        _config_mod.open_in_file_manager(Path(target_path))
    return None


# ---------------------------------------------------------------------------
# "Start from book" -- optional prefill, never required.
# ---------------------------------------------------------------------------

def _handle_prefill_from_book(bs_html, book_file, fy: str, regime: str, status: str, entity_name: str):
    """Pre-fills the SAME editable form fields via advance_tax.from_book().
    Purely a convenience: any failure here leaves the manual form untouched
    and reports an error, it never blocks manual entry."""
    fy = (fy or "").strip()
    if bs_html is None:
        return (*[gr.update() for _ in _FORM_FIELDS], "Pick a balance-sheet HTML export first (or skip this section and enter figures manually).")
    if not fy:
        return (*[gr.update() for _ in _FORM_FIELDS], "Enter the financial year (YYYY-YY) before prefilling from a book.")

    _ensure_itr_scripts_importable()
    try:
        import configs
        import mapping as mapping_engine
        import parse_gnucash as pg
        import rules as rules_engine
        import schedules as sch
        from parse_eguile import parse_file

        bs_path = bs_html.name if hasattr(bs_html, "name") else bs_html
        tree = parse_file(bs_path)

        book = None
        if book_file is not None:
            book_path = book_file.name if hasattr(book_file, "name") else book_file
            book = pg.parse_book(book_path)

        # entity_name is free-text from the UI; sanitize + containment-check
        # before it becomes part of a filesystem path (py/path-injection fix).
        # An entity name that fails validation just means no mapping lookup
        # is attempted -- this whole prefill step is optional, so we fall
        # back to no-mapping rather than raise.
        try:
            entity_key = _sanitize_name(entity_name, what="Entity name") if (entity_name or "").strip() else ""
        except ValueError:
            entity_key = ""
        mapping_path = (
            _safe_path_in(_config_mod.data_root_dir() / "itr" / "mappings", f"{entity_key}.mapping.yaml")
            if entity_key else None
        )
        known_paths = {n.guid: n.path for n in tree.all_nodes() if n.guid}
        if mapping_path is not None and mapping_path.is_file():
            loaded = configs.load_mapping(mapping_path, known_paths=known_paths)
        else:
            loaded = configs.MappingLoadResult(entries={}, warnings=[])
        result = mapping_engine.resolve_tree(tree, loaded)
        node_by_guid = sch._node_by_guid(tree)

        overlay_dir = _config_mod.data_root_dir() / "itr" / "rules"
        rules = rules_engine.load_rules([overlay_dir, rules_engine.canonical_rules_dir()], fy)

        scrips = None
        scrips_path = _config_mod.data_root_dir() / "itr" / "scrips.yaml"
        if scrips_path.is_file():
            scrips = configs.load_scrips(scrips_path)
        fmv_tables = sch.load_fmv_tables()

        input_data = _adv_mod().from_book(
            resolved=result.resolved, node_by_guid=node_by_guid, book=book, fy=fy,
            rules=rules, entity_name=entity_name or "Unnamed", regime=regime, status=status,
            dob=None, scrips=scrips, fmv_tables=fmv_tables,
        )
    except Exception as e:
        return (*[gr.update() for _ in _FORM_FIELDS], f"Error prefilling from book: {e}")

    msg = f"Prefilled from book for FY {fy}. Every field below is still editable."
    if result.unmapped:
        msg += f" ({len(result.unmapped)} unmapped leaf(ves) in the HTML were skipped -- see Review Mapping.)"
    return (*_input_to_form(input_data), msg)


# ---------------------------------------------------------------------------
# render()
# ---------------------------------------------------------------------------

def render(container_tab=None) -> None:
    gr.Markdown(
        "## Advance Tax Estimator\n"
        "Manual entry works fully standalone -- no GnuCash book or mapping "
        "required. Both old and new regime are always computed side by side. "
        "**Advance tax paid** and **capital gains** are always manual input; "
        "capital gains are never projected."
    )

    with gr.Row():
        estimate_dd = gr.Dropdown(choices=_estimate_choices(), value="", label="Saved estimate", scale=3)
        refresh_btn = gr.Button("Refresh", scale=1)
        load_btn = gr.Button("Load", scale=1)

    with gr.Row():
        with gr.Column():
            entity_name = gr.Textbox(label="Entity name")
            fy = gr.Textbox(label="Financial year (YYYY-YY)", placeholder="2025-26")
            regime = gr.Dropdown(choices=_REGIME_CHOICES, value="new", label="Display regime default")
            status = gr.Dropdown(choices=_STATUS_CHOICES, value="Individual", label="Status")
        with gr.Column():
            dob = gr.Textbox(label="Date of birth (YYYY-MM-DD, optional)")
            residency = gr.Textbox(label="Residency (optional)")
            as_of = gr.Textbox(label="As of (YYYY-MM-DD)", value=datetime.date.today().isoformat())

    gr.Markdown("### Recurring heads (actual to date + projected remainder)")
    head_actual_boxes: dict[str, gr.Number] = {}
    head_remainder_boxes: dict[str, gr.Number] = {}
    for head in _HEAD_NAMES:
        with gr.Row():
            head_actual_boxes[head] = gr.Number(label=f"{_HEAD_LABELS[head]} -- actual to date", value=0.0)
            head_remainder_boxes[head] = gr.Number(
                label=f"{_HEAD_LABELS[head]} -- projected remainder (blank = not projected)",
                value=None,
            )
    annualise_btn = gr.Button("Annualise blank remainders (straight-line)")

    with gr.Row():
        other_sources_other = gr.Number(label="Other sources (dividend etc., actual to date only)", value=0.0)
        deductions_via = gr.Number(label="Chapter VI-A deductions (old regime only)", value=0.0)
        tds_to_date = gr.Number(label="TDS credited to date", value=0.0)

    advance_tax_paid_text = gr.Textbox(
        label="Advance tax paid (one 'YYYY-MM-DD = amount' per line)", lines=3,
        placeholder="2025-09-10 = 25000",
    )
    capital_gains_text = gr.Textbox(
        label="Capital gains -- actuals only, never projected "
        "(one 'amount, gain_type, sale_date, note' per line; gain_type is "
        "STCG_111A / LTCG_112A / OTHER; sale_date and note are optional)",
        lines=3, placeholder="150000, LTCG_112A, 2025-11-02, Infosys sale",
    )

    status_md = gr.Markdown()

    with gr.Row():
        save_name = gr.Textbox(label="Save as", scale=3)
        save_btn = gr.Button("Save estimate", scale=1)
        delete_btn = gr.Button("Delete (archive)", scale=1)

    compute_btn = gr.Button("Compute + build workbook", variant="primary")
    run_status_md = gr.Markdown()
    with gr.Row():
        download_btn = gr.DownloadButton("Download workbook", interactive=False)
        open_folder_btn = gr.Button("Open output folder")
    out_path_state = gr.Textbox(visible=False)

    form_components = [
        entity_name, fy, regime, status, dob, residency, as_of,
        head_actual_boxes["salary"], head_remainder_boxes["salary"],
        head_actual_boxes["remuneration"], head_remainder_boxes["remuneration"],
        head_actual_boxes["rent"], head_remainder_boxes["rent"],
        head_actual_boxes["interest"], head_remainder_boxes["interest"],
        other_sources_other, deductions_via, tds_to_date,
        advance_tax_paid_text, capital_gains_text,
    ]
    assert len(form_components) == len(_FORM_FIELDS)

    with gr.Accordion("Start from book (optional -- pre-fills the fields above)", open=False):
        gr.Markdown(
            "Never required. Pre-fills the SAME editable fields above from a "
            "balance-sheet HTML export (and, optionally, the matching "
            ".gnucash book for other-sources/TDS/capital-gains actuals). "
            "Uses this tab's Entity name / FY fields above to locate a "
            "matching mapping file, if one exists; an unmapped or missing "
            "mapping still prefills, just with fewer leaves resolved."
        )
        bs_html_file = gr.File(label="Balance sheet HTML export", file_types=[".html", ".htm"])
        book_file = gr.File(label="GnuCash book (optional)", file_types=[".gnucash"])
        prefill_btn = gr.Button("Prefill from book")

    if container_tab is not None:
        container_tab.select(fn=lambda: gr.update(choices=_estimate_choices()), inputs=[], outputs=[estimate_dd])

    refresh_btn.click(fn=lambda: gr.update(choices=_estimate_choices()), inputs=[], outputs=[estimate_dd])
    load_btn.click(fn=_handle_load, inputs=[estimate_dd], outputs=[*form_components, status_md])

    save_btn.click(fn=_handle_save, inputs=[save_name, *form_components], outputs=[status_md])
    delete_btn.click(fn=_handle_delete, inputs=[estimate_dd], outputs=[status_md, estimate_dd])

    annualise_btn.click(fn=_handle_annualise, inputs=form_components, outputs=[*form_components, status_md])

    compute_btn.click(
        fn=_handle_compute, inputs=form_components,
        outputs=[run_status_md, download_btn, out_path_state],
    )
    open_folder_btn.click(fn=_open_output_folder, inputs=[out_path_state], outputs=[])

    prefill_btn.click(
        fn=_handle_prefill_from_book,
        inputs=[bs_html_file, book_file, fy, regime, status, entity_name],
        outputs=[*form_components, status_md],
    )
