"""
ui/tabs/gnucash_review.py — Review & Edit Account Mappings tab.

Interactive table for reviewing mapped CSVs before GnuCash import. Built on
the shared ui._review_engine skeleton (searchable assign picker, multi-select,
sort/filter, save payload bridge) — see ui/tabs/tds_journal_review.py for the
sibling consumer this mirrors.

Supports:
  - Sortable columns (click header)
  - Shift-click / Ctrl-click multi-select
  - Searchable account picker dropdown
  - Batch "Apply to selected" and "Apply to matching" (same Description)
  - Save corrections → per-GnuCash override YAML + re-export CSV

Contra (cross-bank transfer) rows are flagged by a `<csv-stem>.contra.json`
sidecar the pipeline writes next to the CSV. Presentation for those rows
(tags, row tone, badge) is computed server-side in `_row_presentation()` per
the engine's design rule — no bespoke JS for this screen anymore.
"""
from __future__ import annotations

import csv
from datetime import datetime
from html import escape as html_escape
import json
import logging
import re
from pathlib import Path

import gradio as gr

from ui import _config as _config_mod
from ui import _filedialog
from ui import _safe_paths
from ui.tabs import _entity_book
from ui._review_engine import (
    Column,
    PickerItem,
    ReviewSpec,
    build_html,
    parse_payload,
    payload_box_css,
)
from ui.tabs._generic import _MAX_UPLOAD_SIZE_BYTES

APP_ID = "rv"
TARGET_COL = "Account"
PAYLOAD_VAR = "_rvSavePayload"

# Worst-first confidence order used for the Confidence column's "order" sort
# and matches the mapper's own report ordering (see skill_gnucash_account_mapper).
CONF_ORDER = ("suspense", "none", "low", "weak", "smart", "medium", "history", "llm", "override", "high")

# UI-05: match-type bands. (colour, legend text, match types). One colour per
# match type -- a type may appear in exactly one band. A type that is in no band
# gets NO colour (never red by default: red must mean "needs you").
# Violet is deliberately absent: it belongs to the IMP-09 DORMANT? badge.
MATCH_BANDS = (
    ("blue", "Override", ("override",)),
    ("green", "High / history", ("high", "history")),
    ("amber", "Medium / AI", ("medium", "llm", "smart")),
    ("orange", "Weak / low", ("weak", "low")),
    ("red", "Suspense / unmatched", ("suspense", "none")),
)


def match_band(confidence: str) -> str | None:
    """Colour name for a match type, or None when the type is unknown."""
    c = (confidence or "").strip().lower()
    for colour, _label, types in MATCH_BANDS:
        if c in types:
            return colour
    return None


def _band_classes() -> dict[str, str]:
    return {t: f"accent-{colour}" for colour, _l, types in MATCH_BANDS for t in types}


def _legend_html() -> str:
    items = "".join(
        f'<span><span class="sw {colour}"></span>{label}</span>'
        for colour, label, _t in MATCH_BANDS
    )
    return f'<div class="legend" title="Left-edge colour = match type">{items}</div>'


# ---------------------------------------------------------------------------
# Output-folder CSV scanner
# ---------------------------------------------------------------------------

def _scan_import_ready_csvs() -> list[tuple[str, str]]:
    """Find *GnuCash_import_ready.csv files in the output dir, newest first.

    Returns (label, value) pairs: label is the file NAME (so the dropdown shows
    the name rather than a long truncated path), value is the full path.
    """
    try:
        out_dir = _config_mod.output_dir()
    except Exception:
        return []
    csvs = sorted(
        out_dir.glob("*GnuCash_import_ready*.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [(p.name, str(p)) for p in csvs[:20]]


# ---------------------------------------------------------------------------
# GnuCash account tree extraction (lightweight — no full parse)
# ---------------------------------------------------------------------------

def _extract_account_tree(gnucash_file: str) -> list[str]:
    """Extract the user-pickable account full-paths from a .gnucash file.

    Placeholder / hidden / other "special type" accounts are excluded: they are
    not valid posting targets, so offering them in the review dropdown would let
    a user assign a transaction to an account GnuCash then refuses on import.
    Delegates to the shared placeholder-aware reader
    (``agents.gnucash_accounts``).
    """
    try:
        from agents.gnucash_accounts import load_accounts, postable_accounts
        accounts = postable_accounts(load_accounts(gnucash_file))
    except Exception:
        return []
    # Only multi-level paths are pickable (skip the root and top-level groups),
    # matching the historical behaviour of this picker.
    return sorted({a.path for a in accounts if a.path and ":" in a.path})


# ---------------------------------------------------------------------------
# Contra sidecar + row presentation (computed in Python, per the engine's
# core design rule: presentation logic lives here, not in eval'd JS).
# ---------------------------------------------------------------------------

def _load_contra_sidecar(csv_p: Path) -> dict:
    """Read the `<csv-stem>.contra.json` sidecar if present.

    Keys are stringified row indices → per-row {reason, confidence, status}.
    Missing or unreadable sidecar is treated as "no contras" rather than an
    error — this is optional review-hint data, not required for the CSV to
    load.
    """
    contra_path = csv_p.with_suffix(".contra.json")
    if not contra_path.is_file():
        return {}
    try:
        with open(contra_path, "r", encoding="utf-8") as cf:
            raw = json.load(cf)
        return {str(k): v for k, v in raw.items()}
    except Exception:
        return {}


ADVISORY_SUFFIX = ".advisory.json"


def _load_advisory_sidecar(csv_p: Path) -> dict:
    """IMP-14: `<stem>.advisory.json` -- advisory-only row hints (own-name
    payment OUT whose other side is not in the book). Keys are stringified row
    indices. Missing or unreadable -> {}. Never changes a row's account."""
    path = csv_p.with_suffix(ADVISORY_SUFFIX)
    if not path.is_file():
        return {}
    try:
        with open(path, "r", encoding="utf-8") as af:
            raw = json.load(af)
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    except Exception:
        return {}


BOOKED_SUFFIX = ".matched.json"


def _load_booked_sidecar(csv_p: Path) -> list[dict]:
    """IMP-11: rows the pipeline set aside because they are own transfers
    already booked from the other bank's statement (`<stem>.matched.json`).
    Missing or unreadable -> []."""
    path = csv_p.with_suffix(BOOKED_SUFFIX)
    if not path.is_file():
        return []
    try:
        with open(path, "r", encoding="utf-8") as bf:
            raw = json.load(bf)
        return [e for e in raw if isinstance(e, dict) and isinstance(e.get("row"), dict)]
    except Exception:
        return []


def _booked_row(entry: dict) -> dict:
    """A set-aside row as the review grid shows it: struck through, unticked
    (`_excluded`), with the matching book entry as its Reason."""
    row = dict(entry["row"])
    reason = entry.get("reason") or str(row.get("MatchReason") or "")
    row["MatchReason"] = reason
    row["_excluded"] = True
    return row


def _booked_presentation(row: dict, contra: dict | None) -> None:
    _row_presentation(row, contra)
    row["_tags"] = list(row.get("_tags") or []) + ["booked"]
    row["_rowclass"] = "tone-amber"
    badges = dict(row.get("_badges") or {})
    badges["Date"] = {"text": "BOOKED", "cls": "amber"}
    row["_badges"] = badges
    row["_note"] = row.get("MatchReason") or "Already booked"


def _write_booked_sidecar(csv_p: Path, excluded: list[dict], old: list[dict]) -> None:
    """Rewrite the sidecar with the rows still excluded after a save, so a
    reload neither loses them nor duplicates a re-ticked one."""
    def key(r: dict) -> tuple:
        return tuple(str(r.get(k, "")) for k in
                     ("Date", "Description", "Deposit", "Withdrawal",
                      "Amount Negated (Deposit)", "Amount (Withdrawal)"))
    by_key = {key(e["row"]): e for e in old}
    out = []
    for row in excluded:
        prev = by_key.get(key(row), {})
        entry = {k: v for k, v in prev.items() if k not in ("row", "reason")}
        entry["reason"] = str(row.get("MatchReason") or prev.get("reason") or "")
        entry["row"] = row
        out.append(entry)
    with open(csv_p.with_suffix(BOOKED_SUFFIX), "w", encoding="utf-8") as bf:
        json.dump(out, bf, indent=2, default=str)


def _row_presentation(row: dict, contra: dict | None,
                      advisory: dict | None = None) -> None:
    """Fill in the engine's _tags / _rowclass / _badges / _note keys in place.

    - _tags always carries the row's confidence tier (drives the "Filter:"
      dropdown), plus "contra" when the row was flagged by the sidecar — a
      row can be both (e.g. "suspense" AND "contra"), which is exactly why
      _tags is a list.
    - Suspense rows get a red SUSPENSE badge on the Account column (the
      column that actually holds the value that needs reassigning).
    - Contra rows get the confirmed/possible distinction preserved via
      _rowclass (tone-green / tone-amber, matching the old contra-row
      background colours exactly) and a TRANSFER/POSSIBLE badge on the
      leftmost (Date) column so it's never clipped by a narrow Reason cell.
    """
    confidence = (row.get("Confidence") or "").strip().lower()
    tags = [confidence or "none"]
    badges: dict = {}
    rowclass = ""
    note = ""

    reason = (row.get("MatchReason") or "")
    if confidence == "suspense":
        badges[TARGET_COL] = {"text": "SUSPENSE", "cls": "red"}
        if reason.startswith("blocked: "):
            # IMP-09: the target the matcher wanted is hidden/placeholder.
            badges[TARGET_COL]["title"] = reason
    elif "looks dormant" in reason:
        # IMP-09: advisory only -- still mapped. Violet is deliberately not
        # one of the match-type band colours.
        badges[TARGET_COL] = {"text": "DORMANT?", "cls": "violet", "title": reason}

    if contra:
        tags.append("contra")
        status = contra.get("status") or (
            "confirmed" if contra.get("confidence") == "high" else "possible"
        )
        rowclass = "tone-green" if status == "confirmed" else "tone-amber"
        badges["Date"] = {
            "text": "TRANSFER" if status == "confirmed" else "POSSIBLE",
            "cls": "green" if status == "confirmed" else "amber",
        }
        note = contra.get("reason") or "Possible contra"

    elif advisory:
        # IMP-14: advisory only -- the row keeps its account; a contra flag
        # (other side found) always wins over this hint.
        tags.append("ownxfer")
        badges["Date"] = {"text": "OWN?", "cls": "violet",
                          "title": advisory.get("reason") or ""}
        note = advisory.get("reason") or "Possible own transfer, other side not found"

    band = match_band(confidence)
    # UI-05: the band rides in its own key so _rowclass keeps meaning "tone" only.
    row["_band"] = f"accent-{band}" if band else ""
    row["_tags"] = tags
    row["_rowclass"] = rowclass
    if badges:
        row["_badges"] = badges
    row["_note"] = note


def _generalize_pattern(desc: str) -> str:
    """Turn a bank description into a broader regex that catches variants.

    Strips trailing reference numbers (5+ digits), dates (DD-MM-YYYY,
    DD/MM/YYYY), and trailing whitespace/punctuation so that future
    transactions with different ref numbers still match. Applied only at
    SAVE time (when persisting an override), not at "Apply to matching"
    time — that button matches on the exact Description text, same as
    before.
    """
    s = desc.strip()
    # Strip trailing reference numbers (e.g. -5150102, /8089934)
    s = re.sub(r'[\s/\-]*\d{5,}\s*$', '', s)
    # Strip trailing dates (DD-MM-YYYY or DD/MM/YYYY)
    s = re.sub(r'[\s/\-]*\d{2}[\-/]\d{2}[\-/]\d{4}\s*$', '', s)
    # Strip trailing punctuation and whitespace
    s = s.rstrip(' -/')
    if len(s) < 6:
        # Too short after stripping — fall back to exact match
        return re.escape(desc.strip())
    # Escape for regex, then allow flexible trailing content
    return re.escape(s) + r'.*'


# ---------------------------------------------------------------------------
# UI-06: editable Description
#
# The Review table lets the user reword a narration ("NEFT-XYZ-123 ..." ->
# "Rent - June"). The reworded text REPLACES Description in the exported CSV,
# but it must never become the key anything is learned or matched on:
#
#   * the row keeps its ORIGINAL narration in `Description` all the way through
#     the review, so overrides / persistent rules / "apply to matching" are keyed
#     on what the bank actually wrote, and a re-import of the same statement
#     still hits them;
#   * the reworded text travels in EDIT_KEY (review only, never exported);
#   * at export, an edited row gets Description = edited text, ORIG_KEY = the
#     original (so re-loading the file restores the original for learning) and
#     NOTES_KEY = the original (GnuCash "Notes"), so the bank's own reference
#     stays on the transaction for reference-based matching (contra detection)
#     and for the human reading the register.
#
# Import duplicate check: the pipeline's check keys on (date, amount) within the
# bank account -- see skill_gnucash_reconciler.reconcile -- and never reads the
# description, so rewording cannot make an already-posted row look new.
# ---------------------------------------------------------------------------

EDIT_KEY = "Edited Description"
ORIG_KEY = "Original Description"
NOTES_KEY = "Notes"


def _clean_edit(text: str) -> str:
    """Single-line, trimmed form of an edit ('' when there is nothing to keep)."""
    return " ".join((text or "").split())


def _restore_description_edit(row: dict) -> None:
    """Undo the export transform on load: a row exported with an edit carries
    the original in ORIG_KEY; put it back in Description (the learning key) and
    show the exported text as the edit. Idempotent for rows without ORIG_KEY."""
    orig = (row.get(ORIG_KEY) or "").strip()
    if not orig:
        return
    row[EDIT_KEY] = row.get("Description", "")
    row["Description"] = orig
    if (row.get(NOTES_KEY) or "").strip() == orig:
        row[NOTES_KEY] = ""      # we wrote it; the next export writes it again
    row.pop(ORIG_KEY, None)


def _apply_description_edits(all_rows: list[dict]) -> tuple[list[dict], int]:
    """Return (rows ready to write, number of rows whose Description changed).

    Pure: the input rows are not mutated. With no edit anywhere the returned rows
    equal the input rows exactly (minus a bare EDIT_KEY), so an unedited export
    is byte-identical to the pre-UI-06 one.
    """
    out: list[dict] = []
    n = 0
    for src in all_rows:
        row = dict(src)
        edited = _clean_edit(row.pop(EDIT_KEY, ""))
        original = row.get("Description", "") or ""
        if edited and edited != " ".join(original.split()):
            n += 1
            existing = (row.get(NOTES_KEY) or "").strip()
            if not existing:
                row[NOTES_KEY] = original
            elif original.strip() not in existing:
                row[NOTES_KEY] = f"{original} | {existing}"
            row[ORIG_KEY] = original
            row["Description"] = edited
        out.append(row)
    return out, n


def _export_headers(rows: list[dict]) -> list[str]:
    """Union of every row's keys, first-seen order (the first row alone would
    lose a column only later rows carry -- e.g. Notes on the one edited row)."""
    seen: dict[str, None] = {}
    for r in rows:
        for k in r:
            seen.setdefault(k)
    return list(seen)


# ---------------------------------------------------------------------------
# Spec + load
# ---------------------------------------------------------------------------

def _spec(
    picker_items: list[PickerItem], csv_path: str, gnucash_path: str,
    deposit_key: str, withdrawal_key: str,
) -> ReviewSpec:
    return ReviewSpec(
        app_id=APP_ID,
        columns=[
            Column("Date", "Date"),
            # UI-06: double-click to edit. Description itself stays the ORIGINAL
            # narration; the text lands in EDIT_KEY and replaces it only at export.
            Column("Description", "Description", edit_key=EDIT_KEY),
            Column(TARGET_COL, "Account"),
            Column(deposit_key, "Deposit", sort="number"),
            Column(withdrawal_key, "Withdrawal", sort="number"),
            Column("Balance", "Balance", sortable=False),
            # UI-05: label only. The key -- and so the CSV column -- stays "Confidence".
            Column("Confidence", "Match type", sort="order", order=CONF_ORDER),
            Column("MatchReason", "Reason"),
            # Transfer Acct holds the same bank account on nearly every row
            # -- displayed last so Description and Reason get the width
            # instead. This is DISPLAY order only; the re-exported CSV keeps
            # Transfer Account immediately after Account via
            # agents.canonical_io.order_import_ready_headers (see the
            # re-export block below) and must not be changed to match.
            Column("Transfer Account", "Transfer Acct", sortable=False),
        ],
        target_col=TARGET_COL,
        payload_var=PAYLOAD_VAR,
        picker_label="Assign account:",
        picker_placeholder="Type to search accounts…",
        picker_items=picker_items,
        status_options=[
            ("suspense", "Suspense"),
            ("low", "Low"),
            ("none", "Unmatched"),
            ("weak", "Weak"),
            ("smart", "Smart"),
            ("override", "Override"),
            ("medium", "Medium"),
            ("high", "High"),
            ("contra", "Contra"),
            ("ownxfer", "Own transfer?"),
            ("booked", "Already booked"),
        ],
        status_label="Filter:",
        default_sort="Confidence",
        apply_matching_on="Description",
        apply_matching_label="Apply to matching",
        also_set={"Confidence": "override", "MatchReason": "User override (review)"},
        also_set_matching={"Confidence": "override", "MatchReason": "User override (batch match)"},
        context={"csv_path": csv_path, "gnucash_file": gnucash_path},
        status_col="Confidence",  # UI-04: an assigned row loses its stale badge
        status_classes=_band_classes(),  # UI-05: ...and its band follows the new type
        override_status="override",  # UI-14: ...and a flag tone gives way to the override colour
        extra_panel_html=_legend_html(),
        allow_exclude=True,  # IMP-11: already-booked rows start unticked
    )


def _load_review_data(csv_path: str, gnucash_path: str) -> str:
    """Load mapped CSV + GnuCash account tree, return interactive HTML."""
    if not csv_path or not gnucash_path:
        return "<p>Select both a mapped CSV and a GnuCash file, then click Load.</p>"

    # SEC-19: resolve + refuse non-files, wrong types and paths outside the
    # known folders before anything is opened.
    try:
        csv_p = _safe_paths.resolve_input_file(csv_path, (".csv",))
    except _safe_paths.UnsafePathError as e:
        return f"<p>{html_escape(str(e))}</p>"
    gc_p = Path(gnucash_path)

    if not gc_p.is_file():
        return f"<p>GnuCash file not found: {gc_p.name}</p>"

    with open(csv_p, "r", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))

    booked = _load_booked_sidecar(csv_p)
    if not rows and not booked:
        return "<p>CSV is empty — no rows to review.</p>"

    accounts = _extract_account_tree(str(gc_p))
    if not accounts:
        accounts = sorted({r.get(TARGET_COL, "") for r in rows if r.get(TARGET_COL)})

    contra_flags = _load_contra_sidecar(csv_p)
    advisory_flags = _load_advisory_sidecar(csv_p)
    for i, row in enumerate(rows):
        _restore_description_edit(row)
        _row_presentation(row, contra_flags.get(str(i)), advisory_flags.get(str(i)))
    for entry in booked:
        brow = _booked_row(entry)
        _booked_presentation(brow, entry.get("contra"))
        rows.append(brow)

    # Fallback column keys for older CSVs.
    # MAP-27: the shared helper accepts the new "Amount Negated (Deposit)" /
    # "Amount (Withdrawal)" pair, the 1092991 pair and the plain names.
    from agents.canonical_io import find_deposit_key, find_withdrawal_key
    deposit_key = find_deposit_key(rows[0]) or "Deposit"
    withdrawal_key = find_withdrawal_key(rows[0]) or "Withdrawal"

    picker_items = [PickerItem(value=a, primary=a) for a in accounts]
    spec = _spec(picker_items, str(csv_p), str(gc_p), deposit_key, withdrawal_key)
    return payload_box_css(spec.payload_box_id) + build_html(spec, rows)


# ---------------------------------------------------------------------------
# BNK-09: closing-balance gate on export + read-only check after import
# ---------------------------------------------------------------------------

def _gate_on_export(csv_p: Path, rows: list[dict]) -> tuple[str | None, str, str | None]:
    """Re-run the closing-balance gate for the rows the user is about to export.

    Returns (blocked_markdown, note, bank_base_path). ``blocked_markdown`` is
    set when the export must not be released (the gate failed, or the
    safe-orientation file cannot be built); the bank-base file is then written
    header-only so a stale one cannot be imported. With no gate sidecar (an
    older run) nothing is checked and the old export behaviour stays.
    """
    from agents import bank_balance_gate as bg
    doc = bg.read_gate_sidecar(bg.gate_sidecar_path(csv_p))
    if doc is None:
        return None, "", None
    dst = bg.bank_base_name(csv_p)
    res = bg.reevaluate(doc, rows)
    if res["status"] == "fail":
        bg.write_bank_base_csv([], dst, blocked=True)
        lines = ["**EXPORT BLOCKED - the book would not end at the statement balance.**", ""]
        lines += [f"- {ln.strip()}" for ln in bg.format_gate_failure(res) if ln.strip()]
        return chr(10).join(lines), "", None
    n, problems = bg.write_bank_base_csv(rows, dst)
    if problems:
        return ("**EXPORT BLOCKED - the bank-base file cannot be built.**" + chr(10)
                + chr(10).join(f"- {x}" for x in problems[:20])), "", None
    note = f"; bank-base import file written: {dst.name} ({n} rows)"
    if res["status"] == "unverified":
        note += f" (gate unverified: {res['message']})"
    risks = res.get("double_booking_risk") or []
    if risks:
        note += (f"; RED FLAG: {len(risks)} imported row(s) already have a same-amount entry "
                 f"in this bank's account (possible double booking)")
    return None, note, str(dst)


def _check_after_import(csv_path: str, gnucash_path: str):
    """Read-only: compare this bank's daily book balance with the statement's
    running balance, name the first day they drift apart and the closing
    difference, and write a missing-rows CSV (only rows absent from this bank's
    account). Never writes to the book."""
    from agents import bank_balance_gate as bg
    from agents.skill_gnucash_reconciler.agent import parse_gnucash_for_reconcile
    if not csv_path or not gnucash_path:
        return "Select a mapped CSV and a GnuCash book first.", gr.update(interactive=False, value=None)
    try:
        csv_p = _safe_paths.resolve_input_file(csv_path, (".csv",))
    except _safe_paths.UnsafePathError as e:
        return f"Check refused: {e}", gr.update(interactive=False, value=None)
    gc_p = Path(gnucash_path)
    if not gc_p.is_file():
        return f"GnuCash file not found: {gc_p.name}", gr.update(interactive=False, value=None)
    doc = bg.read_gate_sidecar(bg.gate_sidecar_path(csv_p))
    if doc is None:
        return ("No statement record (.gate.json) next to this CSV, so there is nothing to "
                "compare the book with. Re-run the bank import."), gr.update(interactive=False, value=None)
    try:
        book = parse_gnucash_for_reconcile(str(gc_p), account_filter=doc["book_filter_path"])
        res = bg.post_import_check(doc["points"], bg.own_splits(book, doc["book_filter_path"]))
    except Exception as e:
        return f"Check failed: {e}", gr.update(interactive=False, value=None)
    lines = [("**Book matches the statement.**" if res["status"] == "clean"
              else "**Book and statement differ.**"), "", res["message"]]
    if res["opening_offset"]:
        lines.append(f"- Opening difference carried from before the statement: {res['opening_offset']:.2f} (pre-existing, excluded from the drift).")
    download = gr.update(interactive=False, value=None)
    if res["missing"]:
        mapped = []
        try:
            with open(csv_p, "r", encoding="utf-8", errors="replace", newline="") as f:
                mapped = list(csv.DictReader(f))
        except OSError:
            pass
        out_p = csv_p.with_name(csv_p.stem + "_missing_rows.csv")
        n = bg.write_missing_rows(res["missing"], doc["bank_account"], mapped,
                                  doc.get("intended") or {}, out_p)
        base_p = bg.bank_base_name(out_p)
        mrows = []
        with open(out_p, "r", encoding="utf-8", newline="") as f:
            mrows = list(csv.DictReader(f))
        bg.write_bank_base_csv(mrows, base_p)
        lines.append("")
        lines.append(f"**{n} row(s) are absent from this bank's account:**")
        for m in res["missing"][:60]:
            lines.append(f"- {m['date']} | {m['description'][:60]} | {m['amount']:.2f}")
        lines.append("")
        lines.append(f"Written: `{out_p.name}` (same layout as the import file) and `{base_p.name}` "
                     f"(bank as the base account - import this one).")
        try:
            download = gr.update(value=str(_safe_paths.stage_copy(base_p, _config_mod.download_staging_dir())),
                                 interactive=True)
        except Exception:
            download = gr.update(value=str(base_p), interactive=True)
    return chr(10).join(lines), download


# ---------------------------------------------------------------------------
# Save logic
# ---------------------------------------------------------------------------

def _override_accounting(stats: dict) -> str:
    """UI-12(b): the truth about what a save did with the account changes it
    received -- received / learned / not learned, by reason."""
    learned = stats["learned"] + stats["updated"]
    skipped = []
    if stats["suspense"]:
        skipped.append(f"{stats['suspense']} to a Suspense account (never learned, by design)")
    if stats["empty"]:
        skipped.append(f"{stats['empty']} with an empty narration or no account chosen")
    if stats["same"]:
        skipped.append(f"{stats['same']} already saved to the same account")
    not_learned = stats["suspense"] + stats["empty"] + stats["same"]
    line = (f"Account changes received: {stats['received']}; learned: {learned}"
            f" ({stats['learned']} new, {stats['updated']} updated to a different account);"
            f" not learned: {not_learned}")
    if skipped:
        line += " (" + "; ".join(skipped) + ")"
    return line


def _learn_overrides(changes: list[dict], existing: list[dict]) -> tuple[list[dict], dict]:
    """UI-12(b)/(c): fold the review's account changes into the override rules.

    Pure: `existing` is not mutated. Dedupe is by pattern. A same-pattern,
    same-account repeat is not duplicated; a same-pattern, different-account
    correction UPDATES that rule's account (a rule that carries several
    patterns has just this pattern split off into a new rule, so its other
    patterns keep their account). Suspense is never learned. Returns
    (all_rules, stats) with stats keys: received, learned, updated, same,
    suspense, empty.
    """
    rules = [dict(r, patterns=list(r.get("patterns") or
                                   ([r["pattern"]] if r.get("pattern") else [])))
             for r in existing]
    stats = {"received": len(changes), "learned": 0, "updated": 0,
             "same": 0, "suspense": 0, "empty": 0}
    today = datetime.now().strftime("%Y-%m-%d")

    def _find(pattern: str):
        for r in rules:
            if pattern in r["patterns"]:
                return r
        return None

    for ch in changes:
        desc = ch.get("Description", "")
        account = ch.get(TARGET_COL, "")
        if not desc or not account:
            stats["empty"] += 1
            continue
        # Never save overrides that map to Suspense -- those are unresolved rows.
        if "Suspense" in account:
            stats["suspense"] += 1
            continue
        # Generalize pattern -- strip trailing refs/dates for broader matching.
        pattern = _generalize_pattern(desc)
        if not pattern:
            stats["empty"] += 1
            continue
        hit = _find(pattern)
        if hit is None:
            rules.append({"pattern": pattern, "patterns": [pattern], "account": account})
            stats["learned"] += 1
        elif hit.get("account") == account:
            stats["same"] += 1
        elif len(hit["patterns"]) == 1:
            hit["account"] = account
            hit["added"] = today
            stats["updated"] += 1
        else:
            hit["patterns"].remove(pattern)
            rules.append({"pattern": pattern, "patterns": [pattern], "account": account})
            stats["updated"] += 1
    return rules, stats


_LOAD_LABEL = "Load for Review"
_RESET_LABEL = "Reset"
_RESET_CONFIRM_LABEL = "Discard changes and Reset"
_CONFIRM_BAR = (
    "**Unsaved changes.** {what} would discard your account overrides, exclusions "
    "and description edits. Click **{what}** again to discard them, or click "
    "**Save & Export** first."
)
DIRTY_VAR = PAYLOAD_VAR + "Dirty"
_ARM_VAR = PAYLOAD_VAR + "Armed"


def _guard_js(action: str) -> str:
    """UI-12: page-side js for a Load / Reset click. First click while the
    review has unsaved work arms the action and sends 'confirm'; any later click
    (or a click with nothing unsaved) sends 'go' and drops the work flag. Load
    passes (csv, book, mode), Reset passes (mode)."""
    inputs = "(csv, gc, g)" if action == "load" else "(g)"
    ret = "[csv, gc, mode]" if action == "load" else "mode"
    clear = f"window.{PAYLOAD_VAR} = '';" if action == "reset" else ""
    return (
        f"{inputs} => {{ let mode = 'go'; "
        f"if (window.{DIRTY_VAR} && window.{_ARM_VAR} !== '{action}') "
        f"{{ window.{_ARM_VAR} = '{action}'; mode = 'confirm'; }} "
        f"else {{ window.{_ARM_VAR} = ''; "
        f"if (window.{DIRTY_VAR}) {{ window.{DIRTY_VAR} = false; }} {clear} }} "
        f"return {ret}; }}"
    )


def _load_guarded(csv_file, gnucash_file, mode=""):
    """Load handler with the UI-12 discard guard. `mode` is 'confirm' on the
    first click over unsaved work (nothing is loaded; the confirm bar shows),
    anything else loads as before."""
    if mode == "confirm":
        return (
            gr.update(),
            _CONFIRM_BAR.format(what="Load for Review"),
            gr.update(value="Discard changes and Load"),
            gr.update(value=_RESET_LABEL),
        )
    return (
        _load_review_data(csv_file, gnucash_file),
        "",
        gr.update(value=_LOAD_LABEL),
        gr.update(value=_RESET_LABEL),
    )


def _save_changes(changes_json: str) -> tuple[str, "gr.update"]:
    """Process save from the review UI — write overrides + re-export CSV.

    `changes_json` is the engine's syncPayload() shape: {context, changes,
    all_rows}. Each `changes` entry carries {_idx, _orig, <one key per
    declared Column>, plus guid/Sr/'Transaction ID'/'CN No' when present on
    the row} — `_orig` is the Account value BEFORE this edit.

    Returns (status_markdown, download_file_update).
    """
    if not changes_json or not changes_json.strip():
        return "No changes to save.", gr.update(interactive=False, value=None)

    try:
        payload = parse_payload(changes_json)
    except ValueError as e:
        return f"Error parsing changes: {e}", gr.update(interactive=False, value=None)

    changes = payload["changes"]
    context = payload["context"]
    all_rows = payload["all_rows"]
    gnucash_file = context.get("gnucash_file", "")
    csv_path = context.get("csv_path", "")
    if csv_path:
        # SEC-19: the context rides in the page payload, so it is client input:
        # never write to a path the loader would have refused.
        try:
            csv_path = str(_safe_paths.resolve_input_file(csv_path, (".csv",)))
        except _safe_paths.UnsafePathError as e:
            return (f"Save refused: {e}", gr.update(interactive=False, value=None))

    # UI-06: description edits are export-only; overrides above are learned from
    # the ORIGINAL narration (Description is never overwritten in the payload).
    all_rows, n_desc_edits = _apply_description_edits(all_rows)

    excluded_dirty = bool(payload.get("excluded_dirty"))
    if not changes and not n_desc_edits and not excluded_dirty:
        return "No changes to save.", gr.update(interactive=False, value=None)

    # ── Save overrides YAML ──
    # (With no account changes -- description edits only -- the loop below finds
    # nothing to learn and writes nothing.)
    try:
        # Import via the `agents` package so it resolves in both source and
        # frozen (PyInstaller) builds. The old bare-name import relied on
        # inserting <repo>/src/agents into sys.path, which is a no-op in the
        # frozen app — there the tree lives at _MEIPASS/agents, not
        # _MEIPASS/src/agents — so saving overrides failed with
        # "No module named 'skill_gnucash_account_mapper'".
        from agents.skill_gnucash_account_mapper.persistent_rules import (
            load_overrides,
            rules_path,
            save_overrides_batch,
        )

        _cfg_path = str(_config_mod.PORTABLE_CONFIG_PATH)
        existing = load_overrides(gnucash_file, config_path=_cfg_path)
        all_overrides, stats = _learn_overrides(changes, existing)
        wrote = bool(stats["learned"] or stats["updated"])

        if wrote:
            save_overrides_batch(gnucash_file, all_overrides, config_path=_cfg_path)
            _rp = rules_path(gnucash_file, config_path=_cfg_path)
            override_msg = (
                f"Saved {stats['learned']} new override(s) "
                f"({len(all_overrides)} total) → {_rp}"
            )
            if stats["updated"]:
                override_msg += (
                    f"; {stats['updated']} existing rule(s) updated to a different account"
                )
        else:
            override_msg = "No override rules were written"
        if changes:
            override_msg += "\n\n" + _override_accounting(stats)

    except Exception as e:
        override_msg = f"Warning: could not save overrides — {e}"

    # ── Re-export CSV ──
    download_path: str | None = None
    excluded_rows = payload.get("excluded") or []
    if excluded_dirty and csv_path and not all_rows:
        # Every row is now excluded: keep the header, import nothing.
        try:
            with open(csv_path, "r", encoding="utf-8", errors="replace", newline="") as rf:
                hdr = csv.DictReader(rf).fieldnames or []
            with open(csv_path, "w", newline="", encoding="utf-8") as wf:
                csv.DictWriter(wf, fieldnames=list(hdr)).writeheader()
        except Exception as e:
            logging.getLogger(__name__).warning("could not blank %s: %s", csv_path, e)
    if all_rows and csv_path:
        try:
            csv_p = Path(csv_path)
            # Normalize to the shared import-ready column order so a re-saved CSV
            # matches the mapper's layout (Transfer Account right after Account,
            # not appended last). Single source of truth in canonical_io. Falls
            # back to preserving the original header + appending new keys if the
            # shared schema helper can't be imported.
            try:
                from agents.canonical_io import import_ready_rows, order_import_ready_headers
                # MAP-27: a re-save writes the self-describing amount headers
                # whichever spelling the loaded file used (values do not move).
                all_rows = import_ready_rows(all_rows)
                headers = order_import_ready_headers(_export_headers(all_rows))
            except Exception:
                try:
                    with open(csv_p, "r", encoding="utf-8", errors="replace") as rf:
                        original_headers = csv.DictReader(rf).fieldnames or []
                    headers = list(original_headers)
                    # Add any new keys from payload that aren't in original
                    payload_keys = set(all_rows[0].keys())
                    for k in payload_keys - set(headers):
                        headers.append(k)
                except Exception:
                    headers = list(all_rows[0].keys())
            with open(csv_p, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
                writer.writeheader()
                writer.writerows(all_rows)
            export_msg = f"CSV re-exported: {csv_p.name} ({len(all_rows)} rows)"
            if n_desc_edits:
                n_kept = sum(1 for r in all_rows if (r.get(NOTES_KEY) or "").strip()
                             and (r.get(ORIG_KEY) or "").strip())
                n_empty_orig = n_desc_edits - sum(1 for r in all_rows if (r.get(ORIG_KEY) or "").strip())
                export_msg += f"; {n_desc_edits} description(s) reworded"
                if n_kept:
                    export_msg += f" (original kept in Notes for {n_kept})"
                if n_empty_orig:
                    export_msg += f" ({n_empty_orig} had no original description to keep)"
            # BNK-09: the closing-balance gate decides whether this export may be
            # released, and what is released is the bank-as-base file.
            gate_block, gate_note, bank_base = _gate_on_export(csv_p, all_rows)
            if gate_block:
                return (gate_block + chr(10) * 2 + override_msg,
                        gr.update(interactive=False, value=None))
            export_msg += gate_note
            release_p = Path(bank_base) if bank_base else csv_p
            # Copy to download staging dir so Gradio's file server can serve it
            try:
                staged = _safe_paths.stage_copy(release_p, _config_mod.download_staging_dir())
                download_path = str(staged)
            except Exception:
                download_path = str(release_p)
        except Exception as e:
            export_msg = f"Warning: could not re-export CSV — {e}"
    else:
        export_msg = "CSV not re-exported (no row data)"

    if excluded_dirty and csv_path:
        # IMP-11: persist which already-booked rows are still excluded.
        try:
            csv_p = Path(csv_path)
            _write_booked_sidecar(csv_p, excluded_rows, _load_booked_sidecar(csv_p))
            export_msg += f"; {len(excluded_rows)} row(s) left out of the import (not imported)"
        except Exception as e:
            export_msg += f"; Warning: could not record excluded rows - {e}"

    msg = f"✅ **Saved**\n\n{override_msg}\n\n{export_msg}"
    if download_path:
        return msg, gr.update(value=download_path, interactive=True)
    return msg, gr.update(interactive=False, value=None)


# ---------------------------------------------------------------------------
# Gradio tab renderer
# ---------------------------------------------------------------------------

def render(container_tab=None) -> None:
    """Render the Banks > Review tab. Must be called inside gr.Tab(). Pass that
    gr.Tab as ``container_tab`` so the Mapped-CSV picker re-scans and auto-selects
    the newest import-ready CSV whenever the tab is opened."""

    gr.Markdown("## Review & Edit Account Mappings\n\nSelect a mapped CSV and GnuCash book, then click Load.")

    initial_csvs = _scan_import_ready_csvs()

    with gr.Row():
        entity_dd = gr.Dropdown(
            label="Entity (optional -- auto-fills the GnuCash book from the registry)",
            choices=_entity_book.entity_choices(),
            value=None,
            allow_custom_value=True,
            interactive=True,
            scale=4,
        )
        entity_refresh_btn = gr.Button("↻", scale=0, min_width=40)

    # A registry hit writes the path into the field below in plain sight and
    # says nothing here. A miss leaves that field exactly as it was, which is
    # indistinguishable from "nothing happened" -- that case gets a line.
    # See _entity_book.book_status().
    book_status_md = gr.Markdown("", visible=False, elem_classes=["pa-book-status"])

    with gr.Row():
        csv_dropdown = gr.Dropdown(
            label="Mapped CSV",
            choices=initial_csvs,
            value=initial_csvs[0][1] if initial_csvs else None,
            allow_custom_value=True,
            scale=4,
        )
        refresh_btn = gr.Button("↻", scale=0, min_width=40)
        # A path textbox, not a gr.File: the book is a live file opened
        # read-only in place, never an upload. A gr.File would try to move it
        # into Gradio's cache and serve it to the browser, which fails
        # outright for any book outside the working directory. See the
        # matching comment in ui/tabs/_generic.py for the full reasoning.
        gnucash_file = gr.Textbox(
            label="GnuCash book (.gnucash)",
            placeholder="Pick an entity above, or Browse… to a .gnucash file",
            lines=1,
            max_lines=1,
            scale=4,
        )
        gnucash_browse_btn = gr.Button("Browse...", scale=0, min_width=110)

    entity_dd.change(
        fn=lambda entity_val: (
            _entity_book.book_update(entity_val, None),
            _entity_book.book_status_update(entity_val, None),
        ),
        inputs=[entity_dd],
        outputs=[gnucash_file, book_status_md],
    )

    # ...and the moment the field holds a path -- from Browse..., typing, or
    # the prefill above -- the "pick a book" line has been answered and goes
    # away, rather than sitting there contradicting the field beside it.
    gnucash_file.change(
        fn=_entity_book.book_status_clear_if_filled,
        inputs=[gnucash_file],
        outputs=[book_status_md],
    )
    entity_refresh_btn.click(
        fn=lambda: gr.update(choices=_entity_book.entity_choices()),
        inputs=[],
        outputs=[entity_dd],
    )

    load_btn = gr.Button("Load for Review", variant="primary")

    refresh_btn.click(
        fn=lambda: gr.update(choices=_scan_import_ready_csvs()),
        inputs=[],
        outputs=[csv_dropdown],
    )

    # Native OS file picker for the GnuCash book — a bare gr.File stages a
    # *temp copy* in the browser and hands the handler that temp path, which
    # is catastrophic for a live .gnucash (wrong path, breaks the book GUID).
    # This mirrors ui/tabs/_generic.py's "Browse..." wiring exactly: it opens
    # at the box's remembered folder, validates the pick server-side (the
    # browser's type filter and upload caps are bypassed by a native pick),
    # and sets gnucash_file's value to the REAL absolute path. Drag-drop into
    # the box is untouched — this is additive.
    # NOTE: the Entity dropdown above resolves a registered book via
    # _entity_book.book_update() (registry hit fills gnucash_file; a miss
    # leaves it untouched). Browse still wins if used afterward — it always
    # overwrites with the picked path, no validation blocks that. The picked
    # path itself is still not written back into ui/_book_registry.py; that
    # remains the Entities registration UI's job — these tabs only consume
    # the registry, they don't update it.
    def _browse_gnucash_book():
        valid, warnings = _filedialog.pick_files(
            f"{APP_ID}.gnucash_book",
            multiple=False,
            file_types=(".gnucash",),
            max_size_bytes=_MAX_UPLOAD_SIZE_BYTES,
            title="Select the GnuCash book (.gnucash)",
        )
        for w in warnings:
            gr.Warning(w)
        if not valid:
            # Cancelled, or the pick was rejected — keep the current value.
            return gr.update()
        return gr.update(value=valid[0])

    gnucash_browse_btn.click(fn=_browse_gnucash_book, inputs=[], outputs=[gnucash_file])

    # On tab open, re-scan and auto-select the newest import-ready CSV so a file
    # just produced by a bank/KRChoksey step is picked up without manual refresh.
    if container_tab is not None:
        def _rescan_newest():
            choices = _scan_import_ready_csvs()
            return gr.update(choices=choices,
                             value=(choices[0][1] if choices else None))
        container_tab.select(fn=_rescan_newest, inputs=[], outputs=[csv_dropdown])

    review_html = gr.HTML(value="<p><em>Load a CSV to begin reviewing.</em></p>")

    with gr.Row():
        save_btn = gr.Button("Save & Export", variant="primary")
        reset_btn = gr.Button("Reset", variant="secondary")
    save_result = gr.Markdown("")
    check_btn = gr.Button("Check book after import (read-only)", variant="secondary")
    check_result = gr.Markdown("")
    check_download = gr.DownloadButton(
        label="Download missing rows (bank as base)", visible=True, interactive=False,
        variant="secondary",
    )
    # Created visible=True/interactive=False rather than visible=False:
    # Gradio 6's frontend does not reliably reveal a DownloadButton that
    # starts hidden and is later toggled to visible=True. Toggling
    # `interactive` instead keeps the component always mounted.
    download_file = gr.DownloadButton(
        label="Download corrected CSV", visible=True, interactive=False, variant="primary",
    )

    # Real textbox (visible=True so it's in the DOM) hidden via CSS.
    # gr.State has no frontend element, so the js parameter can't inject into it.
    _payload_box = gr.Textbox(
        value="", show_label=False, container=False, lines=1,
        elem_id=f"{APP_ID}-payload-box",
    )

    check_btn.click(
        fn=_check_after_import,
        inputs=[csv_dropdown, gnucash_file],
        outputs=[check_result, check_download],
    )

    # UI-12: Load and Reset ask before discarding unsaved work. A second click
    # confirms. The page-side js reads the engine's dirty flag and passes
    # "confirm" (first click while dirty) or "go" to the handler through a
    # CSS-hidden carrier box -- an in-page confirm bar, never window.confirm.
    gr.HTML(payload_box_css(f"{APP_ID}-guard-box"))
    _guard_box = gr.Textbox(
        value="", show_label=False, container=False, lines=1,
        elem_id=f"{APP_ID}-guard-box",
    )
    guard_md = gr.Markdown("")

    load_btn.click(
        fn=_load_guarded,
        inputs=[csv_dropdown, gnucash_file, _guard_box],
        outputs=[review_html, guard_md, load_btn, reset_btn],
        js=_guard_js("load"),
    )
    save_btn.click(
        fn=_save_changes,
        inputs=[_payload_box],
        outputs=[save_result, download_file],
        js=f"(x) => window.{PAYLOAD_VAR} || ''",
    ).then(
        fn=None,
        inputs=[save_result],
        outputs=[guard_md],
        js=(f"(m) => {{ if (String(m || '').indexOf('**Saved**') >= 0) "
            f"window.{DIRTY_VAR} = false; return ''; }}"),
    )

    # ── Reset: clear the loaded review + logs, reset pickers to defaults.
    # Leaves output files (CSVs, contra sidecars) on disk untouched. Also
    # clears the pending-save payload so a stale edit set can't be re-saved.
    def _handle_reset_review(mode=""):
        if mode == "confirm":
            return (
                gr.update(), gr.update(), gr.update(), gr.update(),
                gr.update(), gr.update(), gr.update(), gr.update(),
                _CONFIRM_BAR.format(what="Reset"),
                gr.update(value=_LOAD_LABEL), gr.update(value=_RESET_CONFIRM_LABEL),
            )
        choices = _scan_import_ready_csvs()
        return (
            gr.update(choices=choices, value=(choices[0][1] if choices else None)),
            gr.update(value=""),                                     # gnucash_file
            gr.update(value=None),                                   # entity_dd
            gr.update(value="", visible=False),                       # book_status_md
            "<p><em>Load a CSV to begin reviewing.</em></p>",        # review_html
            "",                                                       # save_result
            gr.update(interactive=False, value=None),                 # download_file
            "",                                                       # _payload_box
            "",                                                       # guard_md
            gr.update(value=_LOAD_LABEL), gr.update(value=_RESET_LABEL),
        )

    reset_btn.click(
        fn=_handle_reset_review,
        inputs=[_guard_box],
        outputs=[csv_dropdown, gnucash_file, entity_dd, book_status_md, review_html,
                 save_result, download_file, _payload_box, guard_md, load_btn, reset_btn],
        js=_guard_js("reset"),
    )
