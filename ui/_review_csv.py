"""
ui/_review_csv.py — render a skill's "Review.csv" as an inline HTML table.

Several skills (currently skill_krc_gnucash; skill_26as_journal in a planned
follow-up) can finish a run with some rows they couldn't fully process, and
drop those rows into a "Review.csv" inside the run's output directory instead
of (or in addition to) a terse line in the agent reply. This module turns
that CSV into a readable, colour-coded HTML table that ui/tabs/_generic.py
can splice into the run-result markdown — no new Gradio component needed,
since gr.Markdown already renders raw HTML (see _colorize_status()).

Deliberately Gradio-free and import-light (csv + html + pathlib only) so it
can be unit tested directly, without spinning up the UI or importing any
skill's build script.
"""
from __future__ import annotations

import csv
import html
from pathlib import Path
from typing import Literal

ReasonKind = Literal["account_mapping", "data_value", "judgment", "unknown"]

_KIND_LABELS: dict[ReasonKind, str] = {
    "account_mapping": "Account mapping",
    "data_value": "Data value",
    "judgment": "Judgment call",
    "unknown": "Unknown",
}

# amber for "fixable via a UI action" / "needs a human call", red for
# "blocking data problem" / "didn't match a known pattern" — reuses the
# .rag-warn / .rag-error classes already loaded by ui/webui.py's APP_CSS.
_KIND_CSS_CLASS: dict[ReasonKind, str] = {
    "account_mapping": "rag-warn",
    "data_value": "rag-error",
    "judgment": "rag-warn",
    "unknown": "rag-error",
}

_KIND_HINTS: dict[ReasonKind, str] = {
    "account_mapping": (
        "Pick the matching GnuCash stock account for this security. "
        "(Resolving this from here is coming in a follow-up — for now, add "
        "it to the security_aliases mapping and re-run.)"
    ),
    "data_value": (
        "Fix the underlying data (e.g. the Quantity or Net in the source "
        "workbook) and re-run Reconcile. Don't hand-patch this row."
    ),
    "judgment": (
        "Needs a human decision — e.g. add an opening FIFO lot, accept the "
        "shortfall, or investigate further. Not something to auto-fix."
    ),
    "unknown": "Review manually — this reason text didn't match a known pattern.",
}

_REQUIRED_COLUMNS = ("CN No", "Type", "Security", "Net", "Reason")


def find_review_csv(out_dir: Path) -> Path | None:
    """Return the review CSV directly inside out_dir, if any.

    Matches the literal filename "Review.csv" (case-insensitive), which is
    what skill_krc_gnucash writes today. Non-recursive: review CSVs are
    written straight into the run's output directory, not a subfolder.
    """
    if not out_dir.is_dir():
        return None
    for p in out_dir.iterdir():
        if p.is_file() and p.name.lower() == "review.csv":
            return p
    return None


def classify_reason(reason: str) -> ReasonKind:
    """Classify a Review.csv "Reason" string into an action kind.

    Matched against the literal substrings skill_krc_gnucash's
    build_krc_gnucash.py emits (see its review.append(...) call sites).
    Pure string matching — deliberately doesn't import that script, since
    its module state is unrelated and more volatile than this classifier
    needs to be.
    """
    low = reason.lower()
    if "no security account match" in low:
        return "account_mapping"
    if "no net amount" in low or "sale quantity could not be read" in low:
        return "data_value"
    if "insufficient fifo lots" in low:
        return "judgment"
    return "unknown"


def hint_for_reason(kind: ReasonKind) -> str:
    """Plain-language "what to do" text for a classified reason kind."""
    return _KIND_HINTS[kind]


def read_review_rows(csv_path: Path) -> list[dict[str, str]]:
    """Read a Review.csv into a list of plain dicts.

    Requires the header columns skill_krc_gnucash writes: CN No, Type,
    Security, Net, Reason. Missing/extra columns are tolerated by
    csv.DictReader; callers should treat missing fields as empty strings.
    """
    with open(csv_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [dict(row) for row in reader]


def render_review_table_html(rows: list[dict[str, str]]) -> str:
    """Build an escaped, colour-coded HTML table for the given review rows.

    Every field is html.escape()'d before insertion — row values originate
    from parsed PDF bill data (CN No, Security), which is untrusted input.
    """
    if not rows:
        return ""

    def esc(v: object) -> str:
        return html.escape(str(v if v is not None else ""))

    th_style = (
        "text-align:left;padding:4px 10px;border-bottom:2px solid #4B5563;"
        "white-space:nowrap;"
    )
    td_style = "padding:4px 10px;border-bottom:1px solid #374151;vertical-align:top;"

    header_cells = "".join(
        f'<th style="{th_style}">{esc(col)}</th>'
        for col in ("CN No", "Type", "Security", "Net", "Reason", "What to do")
    )

    body_rows: list[str] = []
    for row in rows:
        reason = row.get("Reason", "")
        kind = classify_reason(reason)
        css_class = _KIND_CSS_CLASS[kind]
        label = _KIND_LABELS[kind]
        hint = hint_for_reason(kind)
        body_rows.append(
            "<tr>"
            f'<td style="{td_style}">{esc(row.get("CN No", ""))}</td>'
            f'<td style="{td_style}">{esc(row.get("Type", ""))}</td>'
            f'<td style="{td_style}">{esc(row.get("Security", ""))}</td>'
            f'<td style="{td_style}">{esc(row.get("Net", ""))}</td>'
            f'<td style="{td_style}"><span class="{css_class}">{esc(label)}</span>'
            f"<br>{esc(reason)}</td>"
            f'<td style="{td_style}">{esc(hint)}</td>'
            "</tr>"
        )

    return (
        '<table style="border-collapse:collapse;width:100%;font-size:0.9em;">'
        f"<thead><tr>{header_cells}</tr></thead>"
        f"<tbody>{''.join(body_rows)}</tbody>"
        "</table>"
    )


def render_review_section_html(csv_path: Path) -> str:
    """Build the full "Needs review" section: heading, open-link, table.

    Returns "" if the file has no data rows (nothing to show). Reads the
    file itself, so callers only need the path found by find_review_csv().
    """
    rows = read_review_rows(csv_path)
    if not rows:
        return ""

    table_html = render_review_table_html(rows)
    abs_path = str(csv_path.resolve())
    file_uri = csv_path.resolve().as_uri()
    folder_name = csv_path.resolve().parent.name

    return (
        f"### ⚠️ Needs review ({len(rows)})\n\n"
        f'<a href="{html.escape(file_uri)}">Open Review.csv</a>'
        f" — {html.escape(abs_path)}\n\n"
        f"<div style=\"font-size:0.85em;opacity:0.8;margin-bottom:6px;\">"
        f"From this run's output folder: {html.escape(folder_name)}</div>\n\n"
        f"{table_html}\n\n"
    )
