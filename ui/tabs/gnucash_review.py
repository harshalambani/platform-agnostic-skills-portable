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
import json
import re
import shutil
from pathlib import Path

import gradio as gr

from ui import _config as _config_mod
from ui._review_engine import (
    Column,
    PickerItem,
    ReviewSpec,
    build_html,
    parse_payload,
    payload_box_css,
)

APP_ID = "rv"
TARGET_COL = "Account"
PAYLOAD_VAR = "_rvSavePayload"

# Worst-first confidence order used for the Confidence column's "order" sort
# and matches the mapper's own report ordering (see skill_gnucash_account_mapper).
CONF_ORDER = ("suspense", "none", "low", "smart", "medium", "llm", "override", "high")


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


def _row_presentation(row: dict, contra: dict | None) -> None:
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

    if confidence == "suspense":
        badges[TARGET_COL] = {"text": "SUSPENSE", "cls": "red"}

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
            Column("Description", "Description"),
            Column(TARGET_COL, "Account"),
            Column("Transfer Account", "Transfer Acct", sortable=False),
            Column(deposit_key, "Deposit", sort="number"),
            Column(withdrawal_key, "Withdrawal", sort="number"),
            Column("Balance", "Balance", sortable=False),
            Column("Confidence", "Conf", sort="order", order=CONF_ORDER),
            Column("MatchReason", "Reason"),
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
            ("smart", "Smart"),
            ("override", "Override"),
            ("medium", "Medium"),
            ("high", "High"),
            ("contra", "Contra"),
        ],
        status_label="Filter:",
        default_sort="Confidence",
        apply_matching_on="Description",
        apply_matching_label="Apply to matching",
        also_set={"Confidence": "override", "MatchReason": "User override (review)"},
        also_set_matching={"Confidence": "override", "MatchReason": "User override (batch match)"},
        context={"csv_path": csv_path, "gnucash_file": gnucash_path},
    )


def _load_review_data(csv_path: str, gnucash_path: str) -> str:
    """Load mapped CSV + GnuCash account tree, return interactive HTML."""
    if not csv_path or not gnucash_path:
        return "<p>Select both a mapped CSV and a GnuCash file, then click Load.</p>"

    csv_p = Path(csv_path)
    gc_p = Path(gnucash_path)

    if not csv_p.is_file():
        return f"<p>CSV not found: {csv_p.name}</p>"
    if not gc_p.is_file():
        return f"<p>GnuCash file not found: {gc_p.name}</p>"

    with open(csv_p, "r", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return "<p>CSV is empty — no rows to review.</p>"

    accounts = _extract_account_tree(str(gc_p))
    if not accounts:
        accounts = sorted({r.get(TARGET_COL, "") for r in rows if r.get(TARGET_COL)})

    contra_flags = _load_contra_sidecar(csv_p)
    for i, row in enumerate(rows):
        _row_presentation(row, contra_flags.get(str(i)))

    # Fallback column keys for older CSVs.
    deposit_key = "Deposit - Amount Negated" if "Deposit - Amount Negated" in rows[0] else "Deposit"
    withdrawal_key = "Withdrawal - Amount" if "Withdrawal - Amount" in rows[0] else "Withdrawal"

    picker_items = [PickerItem(value=a, primary=a) for a in accounts]
    spec = _spec(picker_items, str(csv_p), str(gc_p), deposit_key, withdrawal_key)
    return payload_box_css(spec.payload_box_id) + build_html(spec, rows)


# ---------------------------------------------------------------------------
# Save logic
# ---------------------------------------------------------------------------

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

    if not changes:
        return "No changes to save.", gr.update(interactive=False, value=None)

    # ── Save overrides YAML ──
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
        existing_patterns: set[str] = set()
        for o in existing:
            for p in o.get("patterns", []):
                existing_patterns.add(p)

        new_overrides = []
        for ch in changes:
            desc = ch.get("Description", "")
            account = ch.get(TARGET_COL, "")
            if not desc or not account:
                continue
            # Never save overrides that map to Suspense — those are unresolved rows.
            if "Suspense" in account:
                continue
            # Generalize pattern — strip trailing refs/dates for broader matching.
            pattern = _generalize_pattern(desc)
            if pattern not in existing_patterns:
                new_overrides.append({"pattern": pattern, "account": account})
                existing_patterns.add(pattern)

        if new_overrides:
            all_overrides = existing + new_overrides
            save_overrides_batch(gnucash_file, all_overrides, config_path=_cfg_path)
            _rp = rules_path(gnucash_file, config_path=_cfg_path)
            override_msg = f"Saved {len(new_overrides)} new override(s) ({len(all_overrides)} total) → {_rp}"
        else:
            override_msg = "No new overrides needed (all patterns already saved)"

    except Exception as e:
        override_msg = f"Warning: could not save overrides — {e}"

    # ── Re-export CSV ──
    download_path: str | None = None
    if all_rows and csv_path:
        try:
            csv_p = Path(csv_path)
            # Normalize to the shared import-ready column order so a re-saved CSV
            # matches the mapper's layout (Transfer Account right after Account,
            # not appended last). Single source of truth in canonical_io. Falls
            # back to preserving the original header + appending new keys if the
            # shared schema helper can't be imported.
            try:
                from agents.canonical_io import order_import_ready_headers
                headers = order_import_ready_headers(all_rows[0].keys())
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
            # Copy to download staging dir so Gradio's file server can serve it
            try:
                staging = _config_mod.download_staging_dir()
                staging.mkdir(parents=True, exist_ok=True)
                staged = staging / csv_p.name
                shutil.copy2(csv_p, staged)
                download_path = str(staged.resolve())
            except Exception:
                download_path = str(csv_p)
        except Exception as e:
            export_msg = f"Warning: could not re-export CSV — {e}"
    else:
        export_msg = "CSV not re-exported (no row data)"

    msg = f"✅ **Saved**\n\n{override_msg}\n\n{export_msg}"
    if download_path:
        return msg, gr.update(value=download_path, interactive=True)
    return msg, gr.update(interactive=False, value=None)


# ---------------------------------------------------------------------------
# Gradio tab renderer
# ---------------------------------------------------------------------------

def render(container_tab=None) -> None:
    """Render the Review Mappings tab. Must be called inside gr.Tab(). Pass that
    gr.Tab as ``container_tab`` so the Mapped-CSV picker re-scans and auto-selects
    the newest import-ready CSV whenever the tab is opened."""

    gr.Markdown("## Review & Edit Account Mappings\n\nSelect a mapped CSV and GnuCash book, then click Load.")

    initial_csvs = _scan_import_ready_csvs()

    with gr.Row():
        csv_dropdown = gr.Dropdown(
            label="Mapped CSV",
            choices=initial_csvs,
            value=initial_csvs[0][1] if initial_csvs else None,
            allow_custom_value=True,
            scale=4,
        )
        refresh_btn = gr.Button("↻", scale=0, min_width=40)
        gnucash_file = gr.File(
            label="GnuCash book (.gnucash)",
            file_types=[".gnucash"],
            type="filepath",
        )

    load_btn = gr.Button("Load for Review", variant="primary")

    refresh_btn.click(
        fn=lambda: gr.update(choices=_scan_import_ready_csvs()),
        inputs=[],
        outputs=[csv_dropdown],
    )

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

    load_btn.click(
        fn=_load_review_data,
        inputs=[csv_dropdown, gnucash_file],
        outputs=review_html,
    )
    save_btn.click(
        fn=_save_changes,
        inputs=[_payload_box],
        outputs=[save_result, download_file],
        js=f"(x) => window.{PAYLOAD_VAR} || ''",
    )

    # ── Reset: clear the loaded review + logs, reset pickers to defaults.
    # Leaves output files (CSVs, contra sidecars) on disk untouched. Also
    # clears the pending-save payload so a stale edit set can't be re-saved.
    def _handle_reset_review():
        choices = _scan_import_ready_csvs()
        return (
            gr.update(choices=choices, value=(choices[0][1] if choices else None)),
            gr.update(value=None),                                   # gnucash_file
            "<p><em>Load a CSV to begin reviewing.</em></p>",        # review_html
            "",                                                       # save_result
            gr.update(interactive=False, value=None),                 # download_file
            "",                                                       # _payload_box
        )

    reset_btn.click(
        fn=_handle_reset_review,
        inputs=[],
        outputs=[csv_dropdown, gnucash_file, review_html, save_result,
                 download_file, _payload_box],
        js=f"() => {{ window.{PAYLOAD_VAR} = ''; }}",
    )
