"""
agent.py — Bank of Baroda LangGraph agent.

Supports both single-file and multi-file (directory) input:
  - If pdf_path is a file, process it directly (original behaviour).
  - If pdf_path is a directory, iterate over every .pdf inside,
    produce one CSV per file in a temp location, then concatenate
    them (skipping duplicate headers) into the final output_path.
"""
import tempfile
from pathlib import Path

from agents.base_agent import build_agent
from agents.skill_bob.tools import extract_bob

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")
TOOLS = [extract_bob]


def _run_single(pdf: Path, csv_out: Path, config_path: str, model_override: str | None) -> str:
    """Run the agent on a single PDF and return the reply."""
    agent = build_agent(TOOLS, SYSTEM_PROMPT, config_path, model_override)
    result = agent.invoke({
        "messages": [(
            "user",
            f"Extract transactions from this Bank of Baroda PDF to CSV.\n"
            f"Input PDF:  {pdf}\n"
            f"Output CSV: {csv_out}"
        )]
    })
    return result["messages"][-1].content


def _merge_csvs(csv_files: list[Path], output_path: Path) -> None:
    """Concatenate multiple CSVs into one, keeping the header only from the first."""
    with open(output_path, "w", encoding="utf-8", newline="") as out:
        for idx, csv_file in enumerate(csv_files):
            with open(csv_file, "r", encoding="utf-8") as inp:
                for line_no, line in enumerate(inp):
                    # Skip header row for all files after the first.
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
    Run the BoB agent and return the final response.

    Args:
        pdf_path:       Path to a single BoB PDF **or** a directory containing
                        one or more BoB statement PDFs.
        output_path:    Path where the output .csv should be saved.
        config_path:    Path to config.yaml.
        model_override: Optional model name, e.g. 'llama3.1', 'qwen3', 'phi4-mini'.
    """
    src = Path(pdf_path)

    # --- Single file: original behaviour ---
    if src.is_file():
        return _run_single(src, Path(output_path), config_path, model_override)

    # --- Directory: batch processing ---
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
        reply = _run_single(pdf, part_csv, config_path, model_override)
        replies.append(f"**{pdf.name}:** {reply}")
        if part_csv.is_file():
            csv_parts.append(part_csv)

    if not csv_parts:
        return "ERROR: no CSVs were produced from any of the input PDFs."

    _merge_csvs(csv_parts, Path(output_path))
    summary = (
        f"Batch complete — processed {len(pdfs)} PDF(s), "
        f"produced {len(csv_parts)} CSV(s), merged into {output_path}.\n\n"
        + "\n\n".join(replies)
    )
    return summary
