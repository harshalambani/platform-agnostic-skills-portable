"""
agent.py — Bank of Baroda statement → CSV — DIRECT mode, no LLM.

Single file or a directory of PDFs (each parsed, then merged into one CSV).
Calls the deterministic parser extract_bob_statement.py; no language model.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = Path(__file__).parent / "scripts" / "extract_bob_statement.py"


def _run_single(pdf: Path, csv_out: Path) -> str:
    """Parse a single BoB PDF to CSV and return the script summary."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), str(pdf), str(csv_out)],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"ERROR: {(result.stderr or '').strip() or (result.stdout or '').strip()}"
    return (result.stdout or "").strip() or "Extraction complete."


def _merge_csvs(csv_files: list[Path], output_path: Path) -> None:
    """Concatenate multiple CSVs into one, keeping the header only from the first."""
    with open(output_path, "w", encoding="utf-8", newline="") as out:
        for idx, csv_file in enumerate(csv_files):
            with open(csv_file, "r", encoding="utf-8") as inp:
                for line_no, line in enumerate(inp):
                    if idx > 0 and line_no == 0:
                        continue
                    out.write(line)


def run(
    pdf_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Parse a Bank of Baroda statement PDF, or a folder of them, into a CSV at
    output_path. Direct mode — no LLM. config_path / model_override ignored.
    """
    src = Path(pdf_path)
    if src.is_file():
        return _run_single(src, Path(output_path))
    if not src.is_dir():
        return f"ERROR: pdf_path is neither a file nor a directory: {pdf_path}"

    pdfs = sorted(src.glob("*.pdf"))
    if not pdfs:
        return f"ERROR: no .pdf files found in {pdf_path}"

    print(f"[BoB batch] Found {len(pdfs)} PDF(s) in {pdf_path}")
    tmp_dir = Path(tempfile.mkdtemp(prefix="pa-skills-bob-batch-"))
    csv_parts: list[Path] = []
    replies: list[str] = []
    for i, pdf in enumerate(pdfs, 1):
        part_csv = tmp_dir / f"{pdf.stem}.csv"
        print(f"[BoB batch] Processing {i}/{len(pdfs)}: {pdf.name}")
        reply = _run_single(pdf, part_csv)
        replies.append(f"**{pdf.name}:** {reply}")
        if part_csv.is_file():
            csv_parts.append(part_csv)
    if not csv_parts:
        return "ERROR: no CSVs were produced from any of the input PDFs."
    _merge_csvs(csv_parts, Path(output_path))
    return (f"Batch complete — processed {len(pdfs)} PDF(s), produced "
            f"{len(csv_parts)} CSV(s), merged into {output_path}.\n\n"
            + "\n\n".join(replies))
