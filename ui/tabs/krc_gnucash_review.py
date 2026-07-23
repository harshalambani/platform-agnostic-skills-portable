"""
ui/tabs/krc_gnucash_review.py — KRChoksey GnuCash Import: Review tab.

Built on the shared ui._review_engine skeleton — see ui/tabs/gnucash_review.py
(closest analogue: a picker that reassigns one column) and
ui/tabs/itr_mapping_review.py (whose backup-before-write discipline this
mirrors) for the sibling consumers this follows.

What "a row needing review" means here: skill_krc_gnucash's build script
(src/agents/skill_krc_gnucash/scripts/build_krc_gnucash.py) writes a
Review.csv row (columns: CN No, Type, Security, Net, Reason) for exactly four
reasons, which fall into three fundamentally different kinds — and only ONE
of those kinds may be edited from this screen:

  - account_mapping (EDITABLE) — Reason starts with "no security account
    match": the Bills sheet named a security build_krc_gnucash.py could not
    find (or fuzzy-match >= 0.5) among the book's STOCK/MUTUAL leaf accounts.
    This is a reversible user preference: picking the right account here
    writes {security_name: account_path} into the `security_aliases` map in
    Data/settings/krc_gnucash_config.yaml, which match_security() consults
    (exact-match-first) on the next run.

  - data_value (LOCKED) — Reason starts with "no net amount" or "sale
    quantity could not be read from the contract note": the Bills workbook
    itself is missing/unreadable source data. There is no "account" to pick
    here — the fix is to correct the Bills workbook (or re-run Part II
    Reconcile) and re-run this skill, not to edit anything in this UI.

  - judgment (LOCKED) — Reason starts with "insufficient FIFO lots": the
    build script's own FIFO cost-basis engine determined the prior purchase
    lots in the .gnucash book don't cover this sale. That is a derived fact
    about the book's transaction history, not a mapping decision — letting
    this UI "fix" it by picking an account would silently corrupt the
    pipeline's own cost-basis output.

  - Any OTHER/future Reason text (not one of the four known strings) is
    treated as judgment (locked) by default — see _classify_reason(). An
    unrecognised reason is never assumed safe to edit.

Locking is enforced server-side at save time (_save_changes), not merely by
hiding/greying the row in the client: _classify_reason() is re-run against
the Reason text read FRESH from Review.csv on disk (never trusted from the
client's payload), and any change whose row does not classify as
account_mapping is rejected and reported, never silently applied. See the
module docstring on why this matters — the engine's client-side `assign()`
already skips `r._locked` rows, but a hand-crafted payload posted directly
to `_save_changes` (bypassing the UI) must be rejected too.

Save discipline (mirrors itr_mapping_review._save_changes): write a
timestamped backup of the config file BEFORE any in-place rewrite; no
changes (or nothing valid to apply) never touches the filesystem at all —
not even to open the file for a backup.
"""
from __future__ import annotations

import csv
import datetime
import shutil
from pathlib import Path

import gradio as gr
import yaml

from .. import _config as _config_mod
from .._review_engine import (
    Column,
    PickerItem,
    ReviewSpec,
    build_html,
    parse_payload,
    payload_box_css,
)

APP_ID = "krc"
TARGET_COL = "Account"
PAYLOAD_VAR = "_krcSavePayload"

REVIEW_COLUMNS = ("CN No", "Type", "Security", "Net", "Reason")

_KIND_ORDER = ("account_mapping", "data_value", "judgment")


# ---------------------------------------------------------------------------
# Reason -> kind classification (the critical behavioural rule — see module
# docstring). Deliberately conservative: anything not explicitly recognised
# as an account_mapping reason is locked.
# ---------------------------------------------------------------------------

def _classify_reason(reason: str) -> str:
    r = (reason or "").strip()
    if r.startswith("no security account match"):
        return "account_mapping"
    if r.startswith("no net amount"):
        return "data_value"
    if r.startswith("sale quantity could not be read from the contract note"):
        return "data_value"
    if r.startswith("insufficient FIFO lots"):
        return "judgment"
    return "judgment"  # unknown reason text -> safe default: locked


# ---------------------------------------------------------------------------
# Output-folder Review.csv scanner (mirrors gnucash_review's
# _scan_import_ready_csvs — same output_dir()/glob/mtime-sort pattern).
# ---------------------------------------------------------------------------

def _scan_review_csvs() -> list[tuple[str, str]]:
    """Find */Review.csv under KRChoksey GnuCash Import output folders,
    newest first. label = "<run folder name>/Review.csv"."""
    try:
        out_dir = _config_mod.output_dir()
    except Exception:
        return []
    candidates = sorted(
        out_dir.glob("*-KRC-GnuCash/Review.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [(f"{p.parent.name}/{p.name}", str(p)) for p in candidates[:20]]


def _config_path() -> Path:
    """Data/settings/krc_gnucash_config.yaml, anchored via data_root_dir() —
    the same relative path build_krc_gnucash.py's DEFAULT_CONFIG resolves to
    when the build subprocess's cwd is the data root."""
    return _config_mod.data_root_dir() / "settings" / "krc_gnucash_config.yaml"


# ---------------------------------------------------------------------------
# GnuCash STOCK/MUTUAL account picker (mirrors match_security()'s own
# candidate set in build_krc_gnucash.py: postable STOCK/MUTUAL leaves only).
# ---------------------------------------------------------------------------

def _extract_stock_accounts(gnucash_path: str) -> list[str]:
    try:
        from agents.gnucash_accounts import load_accounts
        accounts = load_accounts(gnucash_path)
    except Exception:
        return []
    return sorted({
        a.path for a in accounts
        if a.type in ("STOCK", "MUTUAL") and not a.is_special and a.path
    })


# ---------------------------------------------------------------------------
# Row loading — plain CSV read, no presentation. Kept separate from
# _row_presentation so _save_changes can re-read the file fresh (the source
# of truth for locking) without re-deriving any display state.
# ---------------------------------------------------------------------------

def _load_review_rows(review_csv_path: str) -> list[dict]:
    p = Path(review_csv_path)
    if not p.is_file():
        return []
    with open(p, "r", newline="", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        row = {k: (r.get(k) or "") for k in REVIEW_COLUMNS}
        row["kind"] = _classify_reason(row["Reason"])
        row[TARGET_COL] = ""
        out.append(row)
    return out


# ---------------------------------------------------------------------------
# Row presentation — computed server-side in Python, per the engine's design
# rule: a loader decides what a row looks like and hands the engine plain
# data, instead of re-deriving lock/badge logic in eval'd JS.
# ---------------------------------------------------------------------------

def _row_presentation(row: dict) -> None:
    kind = row.get("kind") or _classify_reason(row.get("Reason", ""))
    row["kind"] = kind
    locked = kind != "account_mapping"
    row["_locked"] = locked
    row["_tags"] = [kind]
    row["_note"] = row.get("Reason") or ""

    if kind == "account_mapping":
        row["_rowclass"] = "accent-amber"
        row["_badges"] = {TARGET_COL: {"text": "NEEDS MAPPING", "cls": "amber"}}
    elif kind == "data_value":
        row["_rowclass"] = "accent-red"
        row["_badges"] = {
            "kind": {"text": "DATA ISSUE (locked)", "cls": "red",
                     "title": "Fix the Bills workbook / contract note and re-run the skill."}
        }
    else:  # judgment (includes any unrecognised future reason)
        row["_rowclass"] = "accent-red"
        row["_badges"] = {
            "kind": {"text": "JUDGMENT (locked)", "cls": "red",
                     "title": "Derived from FIFO lot history in the .gnucash book — "
                              "not editable here."}
        }


# ---------------------------------------------------------------------------
# Spec + load
# ---------------------------------------------------------------------------

def _spec(picker_items: list[PickerItem], review_path: str, gnucash_path: str) -> ReviewSpec:
    return ReviewSpec(
        app_id=APP_ID,
        columns=[
            Column("CN No", "CN No"),
            Column("Type", "Type"),
            Column("Security", "Security"),
            Column("Net", "Net", sort="number"),
            Column("Reason", "Reason"),
            Column("kind", "Row Type", sort="order", order=_KIND_ORDER),
            Column(TARGET_COL, "Account (alias target)"),
        ],
        target_col=TARGET_COL,
        payload_var=PAYLOAD_VAR,
        picker_label="Assign account:",
        picker_placeholder="Type to search STOCK/MUTUAL accounts…",
        picker_items=picker_items,
        status_options=[
            ("account_mapping", "Needs mapping (editable)"),
            ("data_value", "Data issue (locked)"),
            ("judgment", "Judgment (locked)"),
        ],
        status_label="Show:",
        default_sort="kind",
        apply_matching_on="Security",
        apply_matching_label="Apply to same security",
        extra_panel_html=(
            '<div style="margin-bottom:8px;padding:6px 10px;border:1px solid #333;'
            'border-radius:4px;background:#161616;color:#ccc;font-size:12px;'
            'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,sans-serif;">'
            "Only <strong>Needs mapping</strong> rows can be fixed here (they write an "
            "alias into <code>Data/settings/krc_gnucash_config.yaml</code>). "
            "<strong>Data issue</strong> and <strong>Judgment</strong> rows are locked — "
            "fix the Bills workbook or the book's purchase history and re-run the skill."
            "</div>"
        ),
        context={"review_path": review_path, "gnucash_path": gnucash_path},
    )


def _load_review_data(review_path: str, gnucash_path: str) -> str:
    """Load a Review.csv + the GnuCash book's STOCK/MUTUAL accounts, return
    the interactive HTML table."""
    if not review_path or not gnucash_path:
        return "<p>Select both a Review.csv and a GnuCash file, then click Load.</p>"

    review_p = Path(review_path)
    gc_p = Path(gnucash_path)

    if not review_p.is_file():
        return f"<p>Review.csv not found: {review_p}</p>"
    if not gc_p.is_file():
        return f"<p>GnuCash file not found: {gc_p.name}</p>"

    rows = _load_review_rows(str(review_p))
    if not rows:
        return "<p>Review.csv has no rows — nothing needs review.</p>"

    accounts = _extract_stock_accounts(str(gc_p))
    for row in rows:
        _row_presentation(row)

    picker_items = [PickerItem(value=a, primary=a) for a in accounts]
    spec = _spec(picker_items, str(review_p), str(gc_p))
    return payload_box_css(spec.payload_box_id) + build_html(spec, rows)


# ---------------------------------------------------------------------------
# Save handler.
# ---------------------------------------------------------------------------

def _save_changes(changes_json: str) -> str:
    """Process a Save from the review UI.

    Locking is enforced HERE, not trusted from the client: for every change,
    Review.csv is re-read fresh from `context.review_path` (never the
    payload's own field values) and `_idx` is cross-checked against `CN No`
    at that position — a stale/tampered/out-of-range index is rejected
    outright. Only a row whose Reason (read fresh from disk) classifies as
    account_mapping may write an alias; every other row is rejected and
    reported, never silently applied.

    Writes {security: account} into `security_aliases` in
    Data/settings/krc_gnucash_config.yaml, in place, with a timestamped
    backup written BEFORE the rewrite. No changes (or nothing valid to
    apply) never touches the filesystem — not even to open the config file.
    """
    if not changes_json or not changes_json.strip():
        return "No changes to save."

    try:
        payload = parse_payload(changes_json)
    except ValueError as e:
        return f"Error parsing changes: {e}"

    changes = payload["changes"]
    context = payload["context"]

    if not changes:
        return "No changes to save."

    review_path = str(context.get("review_path") or "").strip()
    if not review_path:
        return "Error: no review file in context — nothing was saved."

    review_file = Path(review_path)
    if not review_file.is_file():
        return f"Error: review file not found: {review_file} — nothing was saved."

    fresh_rows = _load_review_rows(str(review_file))

    # The account picker (built from the book's postable STOCK/MUTUAL leaves —
    # see _extract_stock_accounts) means the UI itself cannot produce a bad
    # value, but a hand-crafted payload could. Re-derive the same valid set
    # server-side here (mirrors gnucash_review.py's `existing = {a.path for a
    # in accounts}` account_exists check) rather than trusting ch[TARGET_COL]
    # verbatim, so a forged account is rejected and reported like a locked
    # row, not silently written into security_aliases.
    gnucash_path = str(context.get("gnucash_path") or "").strip()
    valid_accounts = set(_extract_stock_accounts(gnucash_path)) if gnucash_path else set()

    applied = 0
    skipped_blank = 0
    rejected: list[str] = []
    new_aliases: dict[str, str] = {}

    for ch in changes:
        idx = ch.get("_idx")
        cn_no = ch.get("CN No", "")
        if not isinstance(idx, int) or idx < 0 or idx >= len(fresh_rows):
            rejected.append(
                f"CN {cn_no}: row index out of range in current Review.csv "
                "(the file may have changed — reload and retry)"
            )
            continue

        fresh = fresh_rows[idx]
        if str(fresh.get("CN No") or "") != str(cn_no or ""):
            rejected.append(
                f"CN {cn_no}: no longer matches Review.csv row {idx} "
                "(the file changed — reload and retry)"
            )
            continue

        reason = fresh.get("Reason", "")
        kind = _classify_reason(reason)
        if kind != "account_mapping":
            rejected.append(
                f"CN {fresh.get('CN No')} ({fresh.get('Security')}): "
                f"locked ({kind}) — {reason}"
            )
            continue

        security = fresh.get("Security") or ""
        account = str(ch.get(TARGET_COL) or "").strip()
        if not security or not account:
            skipped_blank += 1
            continue

        if account not in valid_accounts:
            rejected.append(
                f"CN {fresh.get('CN No')} ({security}): '{account}' is not a "
                "known STOCK/MUTUAL account in this GnuCash book — rejected"
            )
            continue

        new_aliases[security] = account
        applied += 1

    if not new_aliases:
        lines = ["No changes applied — nothing was saved."]
        if skipped_blank:
            lines.append(f"{skipped_blank} row(s) skipped (blank account selection).")
        if rejected:
            lines.append(f"{len(rejected)} row(s) REJECTED (locked):")
            lines.extend(f"  - {r}" for r in rejected)
        return "\n\n".join(lines)

    cfg_path = _config_path()
    cfg_path.parent.mkdir(parents=True, exist_ok=True)

    backup_msg = "No existing config file — nothing to back up (cold start)."
    existing_cfg: dict = {}
    if cfg_path.is_file():
        try:
            existing_cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            if not isinstance(existing_cfg, dict):
                existing_cfg = {}
        except Exception:
            existing_cfg = {}
        stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_path = cfg_path.with_name(f"{cfg_path.name}.bak-{stamp}")
        shutil.copy2(cfg_path, backup_path)
        backup_msg = f"Backup written: {backup_path}"

    aliases = dict(existing_cfg.get("security_aliases") or {})
    aliases.update(new_aliases)
    existing_cfg["security_aliases"] = aliases
    cfg_path.write_text(yaml.safe_dump(existing_cfg, sort_keys=False), encoding="utf-8")

    lines = [
        "**Saved**",
        "",
        backup_msg,
        f"Applied {applied} alias mapping(s) -> {cfg_path}",
    ]
    if skipped_blank:
        lines.append(f"Skipped {skipped_blank} row(s) (blank account selection).")
    if rejected:
        lines.append(f"{len(rejected)} row(s) REJECTED (locked — not account_mapping rows):")
        lines.extend(f"  - {r}" for r in rejected)
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# Gradio tab renderer.
# ---------------------------------------------------------------------------

def render(container_tab=None) -> None:
    """Render the KRChoksey GnuCash Import Review tab. Must be called inside
    gr.Tab(). Pass that gr.Tab as ``container_tab`` so the Review.csv picker
    re-scans and auto-selects the newest run whenever the tab is opened."""

    gr.Markdown(
        "## Review KRChoksey GnuCash Import\n\n"
        "Select a Review.csv (from a GnuCash Import run) and your GnuCash book, "
        "then click Load. Only **Needs mapping** rows can be fixed here — "
        "**Data issue** and **Judgment** rows are locked; fix the Bills workbook "
        "or the book's purchase history and re-run the skill instead."
    )

    initial_reviews = _scan_review_csvs()

    with gr.Row():
        review_dropdown = gr.Dropdown(
            label="Review.csv",
            choices=initial_reviews,
            value=initial_reviews[0][1] if initial_reviews else None,
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
        fn=lambda: gr.update(choices=_scan_review_csvs()),
        inputs=[],
        outputs=[review_dropdown],
    )

    if container_tab is not None:
        def _rescan_newest():
            choices = _scan_review_csvs()
            return gr.update(choices=choices, value=(choices[0][1] if choices else None))
        container_tab.select(fn=_rescan_newest, inputs=[], outputs=[review_dropdown])

    review_html = gr.HTML(value="<p><em>Load a Review.csv to begin.</em></p>")

    with gr.Row():
        save_btn = gr.Button("Save", variant="primary")
    save_result = gr.Markdown("")

    # Real textbox (visible=True so it's in the DOM) hidden via CSS.
    # gr.State has no frontend element, so the js parameter can't inject into it.
    _payload_box = gr.Textbox(
        value="", show_label=False, container=False, lines=1,
        elem_id=f"{APP_ID}-payload-box",
    )

    load_btn.click(
        fn=_load_review_data,
        inputs=[review_dropdown, gnucash_file],
        outputs=review_html,
    )
    save_btn.click(
        fn=_save_changes,
        inputs=[_payload_box],
        outputs=[save_result],
        js=f"(x) => window.{PAYLOAD_VAR} || ''",
    )
