"""
agent.py — Bank of Baroda statement → CSV — DIRECT mode, no LLM.

Single file or a directory of PDFs (each parsed, then merged into one CSV).
Calls the deterministic parser extract_bob_statement.py; no language model.

Also exposes :class:`BoBSkill`, the ``BankSkill`` implementation the GnuCash
pipeline (and, from step 5, the bank registry) dispatches on. ``run()`` keeps
emitting the native CSV for the standalone UI tab; ``BoBSkill.parse()`` folds in
the former ``adapter_bob`` mapping and returns a canonical :class:`BankResult`.
"""
from __future__ import annotations

import csv
import logging
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Optional

from agents.bank_common import normalize as _normalize
from agents.bank_contract import BankResult, BankStatementMeta
from agents.canonical_io import run_balance_check

log = logging.getLogger(__name__)

SCRIPT = Path(__file__).parent / "scripts" / "extract_bob_statement.py"

BANK_KEY = "bob"


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


# ---------------------------------------------------------------------------
# BankSkill implementation
# ---------------------------------------------------------------------------
#
# The native CSV → canonical mapping below is folded verbatim from the retired
# ``adapter_bob`` skill: same date/number parsing, same "Opening Balance" row
# skip, same column order. Both old and new paths feed ``write_canonical_csv``,
# so the canonical CSV stays byte-for-byte identical.

# Header tokens that identify a Bank of Baroda statement PDF.
_BOB_MARKERS = ("BANK OF BARODA", "WITHDRAWALS", "DEPOSITS")

# Statement front-matter anchors (page 1) for BankStatementMeta extraction.
_ACCOUNT_RE = re.compile(r'A/C\s*Number\s*:\s*([\d\s]+)', re.IGNORECASE)
_PERIOD_RE = re.compile(
    r'Statement of account for the period of\s*(\d{2}-\d{2}-\d{4})\s*to\s*(\d{2}-\d{2}-\d{4})',
    re.IGNORECASE,
)

_BOB_DATE_SHAPE_RE = re.compile(r'^\d{2}-\d{2}-\d{2,4}$')


def _parse_date_bob(date_str: str) -> Optional[str]:
    """Parse BoB date format DD-MM-YY (or DD-MM-YYYY) to ISO YYYY-MM-DD."""
    if not date_str or not str(date_str).strip():
        return None
    date_str = str(date_str).strip()
    if not _BOB_DATE_SHAPE_RE.match(date_str):
        return None
    return _normalize.normalise_date(date_str)


def _parse_indian_number(num_str: str) -> str:
    """Parse Indian number format (1,23,456.78) to a plain decimal string."""
    if not num_str or not str(num_str).strip():
        return "0"
    cleaned = _normalize.clean_amount(num_str, blank_zero=False)
    try:
        return str(float(cleaned))
    except ValueError:
        return "0"


def _extract_meta_fields(
    pdf_path: Path, password: Optional[str] = None
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Best-effort extraction of (account_number, period_from, period_to)
    from a BoB statement's page-1 front matter. Never raises."""
    try:
        import pdfplumber  # noqa: PLC0415

        with pdfplumber.open(str(pdf_path), password=password or "") as pdf:
            text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    except Exception:  # noqa: BLE001 — best-effort, must never raise
        return None, None, None

    account_number = None
    m = _ACCOUNT_RE.search(text)
    if m:
        account_number = re.sub(r"\D", "", m.group(1)) or None

    period_from = period_to = None
    m = _PERIOD_RE.search(text)
    if m:
        period_from = _normalize.normalise_date(m.group(1))
        period_to = _normalize.normalise_date(m.group(2))

    return account_number, period_from, period_to


def _native_csv_to_canonical(input_csv: str) -> tuple[list[dict], list[str]]:
    """Map a BoB native CSV (DATE/PARTICULARS/CHQ.NO./WITHDRAWALS/DEPOSITS/
    BALANCE) to canonical 8-column rows. Returns (rows, warnings)."""
    warnings: list[str] = []
    with open(input_csv, "r", encoding="utf-8") as f:
        bob_rows = list(csv.DictReader(f))

    rows: list[dict] = []
    for i, bob_row in enumerate(bob_rows, 1):
        try:
            date_str = _parse_date_bob(bob_row.get("DATE", ""))
            if not date_str:
                warnings.append(
                    f"Row {i}: failed to parse date '{bob_row.get('DATE', '')}'"
                )
                continue

            txn_id = str(bob_row.get("CHQ.NO.", "")).strip() or ""
            description = str(bob_row.get("PARTICULARS", "")).strip()

            # Skip the synthetic "Opening Balance" row (no real transaction).
            if "opening balance" in description.lower():
                continue

            rows.append({
                "Date": date_str,
                "Transaction ID": txn_id,
                "Description": description,
                "Account": "",
                "Deposit": _parse_indian_number(bob_row.get("DEPOSITS", "0")),
                "Withdrawal": _parse_indian_number(bob_row.get("WITHDRAWALS", "0")),
                "Balance": _parse_indian_number(bob_row.get("BALANCE", "0")),
                "Currency": "INR",
            })
        except Exception as e:  # noqa: BLE001 — mirror adapter's row-level guard
            warnings.append(f"Row {i}: {e}")
            continue

    return rows, warnings


def _collect_pdfs(path: Path) -> list[Path]:
    """Return the PDF(s) at ``path`` (single file or directory of PDFs)."""
    if path.is_dir():
        return sorted(path.glob("*.pdf"))
    return [path]


class BoBSkill:
    """Bank of Baroda parser implementing the ``BankSkill`` protocol.

    ``detect`` sniffs a PDF's first page for Bank of Baroda header markers;
    ``parse`` extracts the statement table and maps it to canonical rows.
    """

    bank_key = BANK_KEY

    def formats(self) -> tuple[str, ...]:
        return (".pdf",)

    def detect(self, path: str | Path) -> float:
        """Confidence that ``path`` is a Bank of Baroda statement PDF."""
        p = Path(path)
        if p.is_dir():
            pdfs = sorted(p.glob("*.pdf"))
            if not pdfs:
                return 0.0
            p = pdfs[0]
        if p.suffix.lower() != ".pdf":
            return 0.0
        try:
            import pdfplumber  # noqa: PLC0415

            with pdfplumber.open(str(p)) as pdf:
                text = (pdf.pages[0].extract_text() or "").upper()
        except Exception:  # noqa: BLE001 — detection must never raise
            return 0.0
        if "BANK OF BARODA" in text and all(m in text for m in _BOB_MARKERS):
            return 0.95
        return 0.0

    def parse(
        self,
        path: str | Path,
        password: str | None = None,
    ) -> BankResult:
        """Parse a BoB statement PDF (or directory of PDFs) to a BankResult.

        Returns canonical rows only — writing the CSV/sidecar is the caller's
        job via ``canonical_io`` (the shared tail), per the ``BankSkill``
        protocol.
        """
        from agents.skill_bob.scripts.extract_bob_statement import (  # noqa: PLC0415
            extract,
            write_csv as _write_native_csv,
        )

        pdfs = _collect_pdfs(Path(path))
        if not pdfs:
            raise FileNotFoundError(f"No BoB PDF(s) found at: {path}")

        native_rows = []
        for pdf in pdfs:
            native_rows.extend(extract(pdf, password=password))

        # Round-trip through the native CSV so the canonical mapping sees the
        # exact same representation adapter_bob consumed (keeps tie-out exact).
        with tempfile.TemporaryDirectory(prefix="pa-skills-bob-parse-") as tmp:
            native_csv = Path(tmp) / "bob_raw.csv"
            _write_native_csv(native_rows, native_csv)
            rows, warnings = _native_csv_to_canonical(str(native_csv))

        balance_check = run_balance_check(rows)
        if not balance_check.ok:
            warnings.extend(f"Balance: {d}" for d in balance_check.details)

        account_number, period_from, period_to = _extract_meta_fields(pdfs[0], password)
        meta = BankStatementMeta(
            bank_key=BANK_KEY,
            account_number=account_number,
            period_from=period_from,
            period_to=period_to,
            source_format="pw-pdf" if password else "pdf",
            fidelity="exact",
            password_used=bool(password),
        )

        return BankResult(
            rows=rows,
            bank_key=BANK_KEY,
            account_label="Bank of Baroda",
            currency="INR",
            opening_balance=balance_check.opening_balance,
            closing_balance=balance_check.closing_balance,
            balance_check=balance_check,
            sidecar_path=None,
            warnings=warnings,
            meta=meta,
        )


bank_skill = BoBSkill()
