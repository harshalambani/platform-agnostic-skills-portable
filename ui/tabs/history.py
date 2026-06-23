"""
ui/tabs/history.py — Output History tab.

Scans the outputs directory for files produced by previous skill runs,
parses the timestamp and skill suffix from the filename convention
(YYYY-MM-DD-HHMMSS-{stem}-{suffix}{ext}), and presents a table with
download and delete actions.

Gap tracker: C5 — skill output history tab.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

import gradio as gr

from .. import _config


# ---------------------------------------------------------------------------
# Data model.
# ---------------------------------------------------------------------------

# Known suffixes from skill.yaml output.suffix fields.  Used to map
# filenames back to a human-readable skill label.
_SUFFIX_TO_SKILL: dict[str, str] = {
    "26AS":             "Form 26AS Extractor",
    "BoB":              "Bank of Baroda",
    "CC-Sort":          "CC Sort",
    "CC-Transactions":  "CC Transactions",
    "analysis":         "CSV Analyzer",
    "HSBC":             "HSBC Cleanup",
    "summary":          "Summarizer",
    "translation":      "Translator",
}

# Regex for the timestamp prefix: YYYY-MM-DD-HHMMSS
_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})-(.+)$")
_TS_FMT = "%Y-%m-%d-%H%M%S"


@dataclass
class OutputEntry:
    """One parsed output file/directory."""
    path: Path
    filename: str
    timestamp: datetime | None
    skill_label: str
    size_bytes: int
    is_dir: bool


# ---------------------------------------------------------------------------
# Parsing helpers.
# ---------------------------------------------------------------------------

def _human_size(nbytes: int) -> str:
    """Format bytes as a human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if nbytes < 1024:
            return f"{nbytes:.1f} {unit}" if unit != "B" else f"{nbytes} {unit}"
        nbytes /= 1024
    return f"{nbytes:.1f} TB"


def _infer_skill(name_without_ts: str) -> str:
    """
    Try to match a known suffix at the end of the filename stem.

    The filename (after the timestamp prefix) looks like:
        {input_stem}-{suffix}{ext}    for files
        {suffix}                      for directories

    We check if name_without_ts ends with any known suffix (before the
    extension) and return the corresponding skill label.
    """
    # Strip extension for matching.
    stem = Path(name_without_ts).stem if "." in name_without_ts else name_without_ts

    for suffix, label in _SUFFIX_TO_SKILL.items():
        if stem == suffix or stem.endswith(f"-{suffix}"):
            return label
    return "Unknown"


def parse_output_entry(path: Path) -> OutputEntry:
    """Parse an output file or directory into an OutputEntry."""
    name = path.name
    ts: datetime | None = None
    skill_label = "Unknown"

    m = _TS_RE.match(name)
    if m:
        try:
            ts = datetime.strptime(m.group(1), _TS_FMT)
        except ValueError:
            pass
        remainder = m.group(2)
        skill_label = _infer_skill(remainder)
    else:
        # No timestamp prefix — try to infer skill from the whole name.
        skill_label = _infer_skill(name)

    is_dir = path.is_dir()
    if is_dir:
        # Sum all files in the directory.
        size = sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    else:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0

    return OutputEntry(
        path=path,
        filename=name,
        timestamp=ts,
        skill_label=skill_label,
        size_bytes=size,
        is_dir=is_dir,
    )


def scan_outputs() -> list[OutputEntry]:
    """
    Scan the outputs directory and return parsed entries sorted
    newest-first.  Skips hidden files (dotfiles).
    """
    out_dir = _config.output_dir()
    if not out_dir.is_dir():
        return []

    entries: list[OutputEntry] = []
    for item in out_dir.iterdir():
        if item.name.startswith("."):
            continue
        entries.append(parse_output_entry(item))

    # Sort: newest first (entries without a timestamp go to the end).
    entries.sort(
        key=lambda e: (e.timestamp is not None, e.timestamp or datetime.min),
        reverse=True,
    )
    return entries


def delete_output(path: Path) -> bool:
    """
    Delete an output file or directory.  Returns True on success.
    Only deletes items inside the configured output directory (safety).
    """
    out_dir = _config.output_dir().resolve()
    resolved = path.resolve()

    # Safety: refuse to delete anything outside the outputs directory.
    try:
        resolved.relative_to(out_dir)
    except ValueError:
        return False

    if resolved.is_dir():
        import shutil
        shutil.rmtree(resolved, ignore_errors=True)
        return not resolved.exists()
    elif resolved.is_file():
        resolved.unlink()
        return not resolved.exists()
    return False


# ---------------------------------------------------------------------------
# Table rendering.
# ---------------------------------------------------------------------------

def _build_table_data() -> list[list[str]]:
    """Build rows for the Gradio Dataframe: [Date, Skill, Filename, Size]."""
    entries = scan_outputs()
    rows = []
    for e in entries:
        date_str = e.timestamp.strftime("%Y-%m-%d %H:%M:%S") if e.timestamp else "—"
        kind = "folder" if e.is_dir else ""
        size_str = _human_size(e.size_bytes)
        if kind:
            size_str += f" ({kind})"
        rows.append([date_str, e.skill_label, e.filename, size_str])
    return rows


def _build_summary_markdown() -> str:
    """One-line summary: count and total size."""
    entries = scan_outputs()
    if not entries:
        return "_No output files found. Run a skill to see results here._"
    total = sum(e.size_bytes for e in entries)
    return (
        f"**{len(entries)}** output(s) · **{_human_size(total)}** total · "
        f"Location: `{_config.output_dir()}`"
    )


def _download_file(filename: str) -> str | None:
    """Return the absolute path for Gradio DownloadButton, or None."""
    if not filename or not filename.strip():
        return None
    out_dir = _config.output_dir()
    target = out_dir / filename.strip()
    if target.is_file():
        return str(target.resolve())
    return None


def _delete_and_refresh(filename: str) -> tuple[Any, ...]:
    """Delete the named file and return refreshed table + summary."""
    if filename and filename.strip():
        target = _config.output_dir() / filename.strip()
        delete_output(target)
    return (
        _build_table_data(),
        _build_summary_markdown(),
        "",                          # clear the filename input
        gr.update(visible=False),    # hide download button
        "Deleted." if filename else "",
    )


# ---------------------------------------------------------------------------
# Public: render the History tab.
# ---------------------------------------------------------------------------

def render() -> None:
    """Render the History tab body; must be called inside a gr.Tab context."""
    gr.Markdown("## Output History\n\nPrevious skill run outputs.")
    summary_md = gr.Markdown(value=_build_summary_markdown())

    table = gr.Dataframe(
        value=_build_table_data(),
        headers=["Date", "Skill", "Filename", "Size"],
        datatype=["str", "str", "str", "str"],
        interactive=False,
        wrap=True,
        row_count=(0, "dynamic"),
    )

    with gr.Row():
        refresh_btn = gr.Button("Refresh", variant="secondary")
        _open_out_btn = gr.Button("Open output folder", variant="secondary")
    _open_out_btn.click(
        fn=lambda: _config.open_in_file_manager(_config.output_dir()),
        inputs=None, outputs=None,
    )

    gr.Markdown("---\n\n**Actions** — type or paste a filename from the table above:")

    with gr.Row():
        filename_input = gr.Textbox(
            label="Filename",
            placeholder="e.g. 2026-06-03-143025-sales-analysis.md",
            scale=3,
        )
        download_btn = gr.DownloadButton(
            label="Download",
            visible=False,
            variant="primary",
            scale=1,
        )

    with gr.Row():
        get_btn = gr.Button("Get download link", variant="secondary")
        delete_btn = gr.Button("Delete", variant="stop")

    status_md = gr.Markdown("")

    # --- Event handlers ---

    refresh_btn.click(
        fn=lambda: (_build_table_data(), _build_summary_markdown()),
        outputs=[table, summary_md],
    )

    def _on_get(filename: str):
        path = _download_file(filename)
        if path:
            return gr.update(value=path, visible=True), ""
        return gr.update(visible=False), "File not found." if filename else ""

    get_btn.click(
        fn=_on_get,
        inputs=[filename_input],
        outputs=[download_btn, status_md],
    )

    delete_btn.click(
        fn=_delete_and_refresh,
        inputs=[filename_input],
        outputs=[table, summary_md, filename_input, download_btn, status_md],
    )

    # Click a table row to populate the filename input.
    def _on_select(evt: gr.SelectData):
        # evt.value is the cell value; evt.index is (row, col).
        # We want the Filename column (index 2).
        if evt.index and len(evt.index) >= 2:
            row_idx = evt.index[0]
            data = _build_table_data()
            if 0 <= row_idx < len(data):
                return data[row_idx][2]  # Filename column
        # Fallback: return the clicked cell value.
        return str(evt.value) if evt.value else ""

    table.select(
        fn=_on_select,
        outputs=[filename_input],
    )
