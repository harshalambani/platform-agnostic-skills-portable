"""
agent.py — Kotak Mahindra Bank statement -> CSV — DIRECT mode, no LLM.

Single PDF (password-optional). Calls the deterministic table-based parser
in ``scripts/extract_kotak_statement.py``; no language model involved.

Also exposes :class:`KotakSkill`, the ``BankSkill`` implementation the bank
registry (``agents/banks.py``) discovers and dispatches on. ``run()`` keeps
emitting a native CSV for standalone UI-skill usage; ``KotakSkill.parse()``
maps the same rows into a canonical :class:`BankResult`.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Optional

from agents.bank_common import normalize as _normalize
from agents.bank_contract import BankResult, BankStatementMeta
from agents.canonical_io import run_balance_check

log = logging.getLogger(__name__)

BANK_KEY = "kotak"

# Header/front-matter markers that identify a Kotak Mahindra Bank statement.
_KOTAK_MARKERS = ("KOTAK MAHINDRA BANK",)
_KOTAK_COLUMN_MARKERS = ("WITHDRAWAL", "DEPOSIT", "BALANCE")

_ACCOUNT_RE = re.compile(r'Account\s*No\s*:\s*(\d+)', re.IGNORECASE)
_PERIOD_RE = re.compile(
    r'Statement Period\s*:\s*(\d{2}\s+[A-Za-z]{3}\s+\d{4})\s+to\s+(\d{2}\s+[A-Za-z]{3}\s+\d{4})',
    re.IGNORECASE,
)


def _extract_meta_fields(
    pdf_path: Path, password: Optional[str] = None
) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """Best-effort extraction of (account_number, period_from, period_to)
    from a Kotak statement's page-1 front matter. Never raises."""
    try:
        import pdfplumber  # noqa: PLC0415

        with pdfplumber.open(str(pdf_path), password=password or "") as pdf:
            text = (pdf.pages[0].extract_text() or "") if pdf.pages else ""
    except Exception:  # noqa: BLE001 -- best-effort, must never raise
        return None, None, None

    account_number = None
    m = _ACCOUNT_RE.search(text)
    if m:
        account_number = re.sub(r"\D", "", m.group(1)) or None

    period_from = period_to = None
    m = _PERIOD_RE.search(text)
    if m:
        period_from = _normalize.parse_space_month_date(m.group(1))
        period_to = _normalize.parse_space_month_date(m.group(2))

    return account_number, period_from, period_to


def _rows_to_canonical(rows) -> tuple[list[dict], list[str]]:
    """Map extract_kotak_statement.Row objects to canonical 8-column rows.

    Mirrors BoB's convention: the "Opening Balance" pseudo-row is excluded
    from canonical transaction rows (it has no date/txn identity to canon-
    icalize), but its balance is still read separately as the statement's
    opening_balance via the balance check over the remaining rows.
    """
    warnings: list[str] = []
    canonical: list[dict] = []
    for i, row in enumerate(rows, 1):
        if row.is_opening_balance:
            continue
        if not row.date:
            warnings.append(f"Row {i}: no parseable date, skipped: {row.description!r}")
            continue
        canonical.append({
            "Date": row.date,
            "Transaction ID": row.chq_ref or "",
            "Description": row.description,
            "Account": "",
            "Deposit": row.deposit or "0",
            "Withdrawal": row.withdrawal or "0",
            "Balance": row.balance or "0",
            "Currency": "INR",
        })
    return canonical, warnings


def run(
    pdf_path: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str = None,
) -> str:
    """
    Parse a Kotak Mahindra Bank statement PDF into a CSV at output_path.
    Direct mode -- no LLM. config_path / model_override ignored.
    """
    from agents.skill_kotak.scripts.extract_kotak_statement import (  # noqa: PLC0415
        extract,
        write_csv,
    )

    src = Path(pdf_path)
    if not src.is_file():
        return f"ERROR: pdf_path is not a file: {pdf_path}"

    try:
        rows = extract(src)
    except RuntimeError as e:
        return f"ERROR: {e}"

    write_csv(rows, Path(output_path))
    return f"Extraction complete -- wrote {len(rows)} row(s) to {output_path}."


# ---------------------------------------------------------------------------
# BankSkill implementation
# ---------------------------------------------------------------------------

class KotakSkill:
    """Kotak Mahindra Bank parser implementing the ``BankSkill`` protocol.

    ``detect`` sniffs a PDF's first page for Kotak header markers; ``parse``
    extracts the ruled transaction table (via pdfplumber's
    ``extract_tables()``, see ``scripts/extract_kotak_statement.py``) and
    maps it to canonical rows.
    """

    bank_key = BANK_KEY

    def formats(self) -> tuple[str, ...]:
        return (".pdf",)

    def detect(self, path: str | Path) -> float:
        """Confidence that ``path`` is a Kotak Mahindra Bank statement PDF."""
        p = Path(path)
        if p.suffix.lower() != ".pdf":
            return 0.0
        try:
            import pdfplumber  # noqa: PLC0415

            with pdfplumber.open(str(p)) as pdf:
                text = (pdf.pages[0].extract_text() or "").upper()
        except Exception:  # noqa: BLE001 -- detection must never raise
            return 0.0
        if any(m in text for m in _KOTAK_MARKERS) and all(m in text for m in _KOTAK_COLUMN_MARKERS):
            return 0.95
        return 0.0

    def parse(
        self,
        path: str | Path,
        password: str | None = None,
    ) -> BankResult:
        """Parse a Kotak statement PDF to a BankResult.

        Returns canonical rows only -- writing the CSV/sidecar is the
        caller's job via ``canonical_io`` (the shared tail), per the
        ``BankSkill`` protocol.
        """
        from agents.skill_kotak.scripts.extract_kotak_statement import extract  # noqa: PLC0415

        p = Path(path)
        if not p.is_file():
            raise FileNotFoundError(f"No Kotak PDF found at: {path}")

        native_rows = extract(p, password=password)
        rows, warnings = _rows_to_canonical(native_rows)

        balance_check = run_balance_check(rows)
        if not balance_check.ok:
            warnings.extend(f"Balance: {d}" for d in balance_check.details)

        account_number, period_from, period_to = _extract_meta_fields(p, password)
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
            account_label="Kotak Mahindra Bank",
            currency="INR",
            opening_balance=balance_check.opening_balance,
            closing_balance=balance_check.closing_balance,
            balance_check=balance_check,
            sidecar_path=None,
            warnings=warnings,
            meta=meta,
        )


bank_skill = KotakSkill()
