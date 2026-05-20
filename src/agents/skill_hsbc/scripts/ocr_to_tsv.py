"""Stage 1: Convert HSBC bank-statement PDFs to per-page Tesseract TSV.

Each PDF goes through two steps:

  1. `pdftoppm -r 300 -png <pdf> <work>/png/<name>/page`
     Rasterises the PDF at 300 DPI. High DPI is important: HSBC's printed
     numbers are small and Tesseract loses decimal points below ~250 DPI.
  2. `tesseract <png> <work>/tsv/<name>/page-N --psm 6 tsv`
     TSV mode is crucial because downstream stages use the x/y coordinates
     to classify amounts into Deposit / Withdrawal / Balance columns.
     PSM 6 = "assume a single uniform block of text" — works well for
     statement pages which are essentially one big table.

The filename (without extension) becomes the statement key; the enclosing
script groups pages by this key.

Usage:
    python ocr_to_tsv.py --pdf-dir /path/to/hsbc_pdfs --work-dir /path/to/work
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path


def check_binaries():
    missing = []
    for bin_name in ("pdftoppm", "tesseract"):
        if shutil.which(bin_name) is None:
            missing.append(bin_name)
    if missing:
        sys.exit(
            f"Missing required binaries: {', '.join(missing)}.\n"
            "Install with: apt-get install -y poppler-utils tesseract-ocr"
        )


def ocr_pdf(pdf_path: Path, work_dir: Path, dpi: int = 300):
    """Render one PDF to PNGs then OCR each page to TSV.

    Creates:
      <work_dir>/png/<stem>/page-1.png, page-2.png, ...
      <work_dir>/tsv/<stem>/page-1.tsv, page-2.tsv, ...
    """
    stem = pdf_path.stem
    png_dir = work_dir / "png" / stem
    tsv_dir = work_dir / "tsv" / stem
    png_dir.mkdir(parents=True, exist_ok=True)
    tsv_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{stem}] rasterising at {dpi} DPI...", flush=True)
    subprocess.run(
        ["pdftoppm", "-r", str(dpi), "-png", str(pdf_path), str(png_dir / "page")],
        check=True,
    )

    pngs = sorted(png_dir.glob("page-*.png"))
    # pdftoppm may produce `page-1.png` or `page-01.png` depending on page count.
    # Normalise filenames so downstream glob('page-*.tsv') sorting works by int.
    for png in pngs:
        page_num = int("".join(c for c in png.stem.split("-")[-1] if c.isdigit()))
        tsv_path = tsv_dir / f"page-{page_num}.tsv"
        print(f"  OCR page {page_num}...", flush=True)
        # tesseract <png> <out_stem> <config> -> writes <out_stem>.tsv
        subprocess.run(
            [
                "tesseract", str(png), str(tsv_path.with_suffix("")),
                "--psm", "6", "tsv",
            ],
            check=True,
            stderr=subprocess.DEVNULL,
        )

    return tsv_dir


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--pdf-dir", required=True, type=Path,
                    help="Folder containing HSBC statement PDFs (one file per statement period).")
    ap.add_argument("--work-dir", required=True, type=Path,
                    help="Scratch folder for intermediate PNG/TSV output.")
    ap.add_argument("--dpi", type=int, default=300,
                    help="Raster DPI (default 300; do not go below 250).")
    args = ap.parse_args()

    check_binaries()

    args.work_dir.mkdir(parents=True, exist_ok=True)
    pdfs = sorted(args.pdf_dir.glob("*.pdf"))
    if not pdfs:
        sys.exit(f"No PDFs found in {args.pdf_dir}")

    print(f"Found {len(pdfs)} PDF(s) in {args.pdf_dir}")
    for pdf in pdfs:
        ocr_pdf(pdf, args.work_dir, dpi=args.dpi)

    print(f"\nDone. TSVs under {args.work_dir / 'tsv'}/")


if __name__ == "__main__":
    main()
