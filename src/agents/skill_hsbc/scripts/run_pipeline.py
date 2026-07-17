"""End-to-end orchestrator: PDFs in, enriched XLSX out.

Runs the four stages in sequence:
  1. ocr_to_tsv.py   — PDFs → PNGs → Tesseract TSVs
  2. parse_tsv.py    — TSVs → cleaned.json (coordinate-aware + reconciled)
  3. enrich.py       — cleaned.json → enriched.json (IDs, dates, description cleanup)
  4. build_xlsx.py   — enriched.json → final .xlsx (main + Summary sheets)

Each stage writes files into `--work-dir`, so if one step fails you can rerun
just that stage independently without redoing the (slow) OCR step.

Usage:
    python run_pipeline.py \\
      --pdf-dir /path/to/hsbc_pdfs \\
      --work-dir /path/to/scratch \\
      --out /path/to/output.xlsx \\
      [--title "HSBC Savings Apr2025-Mar2026"] \\
      [--skip-ocr]   # reuse existing TSVs if OCR already ran
"""
import argparse
import subprocess
import sys
from pathlib import Path


SCRIPTS = Path(__file__).parent


def run(cmd):
    print(f"\n$ {' '.join(str(c) for c in cmd)}", flush=True)
    res = subprocess.run(cmd)
    if res.returncode != 0:
        sys.exit(f"Stage failed: {cmd[1] if len(cmd) > 1 else cmd[0]}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pdf-dir", type=Path, required=True,
                    help="Folder containing HSBC statement PDFs.")
    ap.add_argument("--work-dir", type=Path, required=True,
                    help="Scratch folder for intermediate files (PNGs, TSVs, JSON).")
    ap.add_argument("--out", type=Path, required=True,
                    help="Final .xlsx output path.")
    ap.add_argument("--title", default="HSBC Savings",
                    help="Title for the main Excel sheet.")
    ap.add_argument("--skip-ocr", action="store_true",
                    help="Reuse existing TSVs in <work-dir>/tsv/ (skip the OCR step).")
    ap.add_argument("--dpi", type=int, default=300,
                    help="OCR raster DPI (default 300; do not go below 250).")
    ap.add_argument("--password", default=None,
                    help="User password for encrypted PDFs (passed to pdftoppm).")
    args = ap.parse_args()

    args.work_dir.mkdir(parents=True, exist_ok=True)

    if not args.skip_ocr:
        ocr_cmd = [sys.executable, str(SCRIPTS / "ocr_to_tsv.py"),
                   "--pdf-dir", str(args.pdf_dir),
                   "--work-dir", str(args.work_dir),
                   "--dpi", str(args.dpi)]
        if args.password:
            ocr_cmd += ["--password", args.password]
        run(ocr_cmd)
    else:
        print("[skip-ocr] reusing existing TSVs", flush=True)

    cleaned = args.work_dir / "cleaned.json"
    enriched = args.work_dir / "enriched.json"

    run([sys.executable, str(SCRIPTS / "parse_tsv.py"),
         "--work-dir", str(args.work_dir),
         "--out", str(cleaned)])

    run([sys.executable, str(SCRIPTS / "enrich.py"),
         "--in", str(cleaned),
         "--out", str(enriched)])

    run([sys.executable, str(SCRIPTS / "build_xlsx.py"),
         "--in", str(enriched),
         "--out", str(args.out),
         "--title", args.title])

    print(f"\nPipeline complete. Output: {args.out}")


if __name__ == "__main__":
    main()
