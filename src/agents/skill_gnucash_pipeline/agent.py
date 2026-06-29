#!/usr/bin/env python3
"""
GnuCash Import Pipeline
End-to-end: raw bank statement → GnuCash-ready mapped CSV.

Chain:
  1. Bank parse → canonical (BankSkill.parse() for ICICI / BoB / HSBC, each of
                  which extracts AND maps to the canonical schema via the shared
                  canonical_io tail; HDFC + Other Bank use LLM-assisted column
                  normalisation; HSBC still runs its OCR pipeline to an enriched
                  workbook first, which HSBCSkill.parse() then maps)
  2. Account mapping   (skill_gnucash_account_mapper)

Public surface:
    run() — PA Skills UI entry point.
"""

import csv
import gzip
import json
import logging
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from agents.balance_utils import (
    verify_running_balance,
    verify_closing_balance,
    extract_opening_closing,
    format_balance_summary,
    _safe_float,
)
from agents.canonical_io import (
    read_sidecar as _ci_read_sidecar,
)
from agents.skill_gnucash_reconciler.agent import (
    parse_gnucash_for_reconcile,
    reconcile,
    detect_contra_entries,
)

log = logging.getLogger(__name__)


def _emit_progress(step: int, message: str) -> None:
    """Push a progress event to the UI streaming queue (if active)."""
    try:
        from agents.base_agent import get_progress_queue
        q = get_progress_queue()
        if q is not None:
            q.put({"step": step, "type": "pipeline", "snippet": message})
    except Exception:
        pass  # queue not available — running outside UI

# GnuCash XML namespaces
_NS = {
    'gnc': '{http://www.gnucash.org/XML/gnc}',
    'act': '{http://www.gnucash.org/XML/act}',
    'trn': '{http://www.gnucash.org/XML/trn}',
    'split': '{http://www.gnucash.org/XML/split}',
    'ts': '{http://www.gnucash.org/XML/ts}',
    'slot': '{http://www.gnucash.org/XML/slot}',
    'cmdty': '{http://www.gnucash.org/XML/cmdty}',
}

def _resolve_single_file(path_or_dir: str, extensions: tuple[str, ...]) -> str:
    """If *path_or_dir* is a directory (staged uploads), return the first
    matching file inside it; otherwise return the path unchanged."""
    p = Path(path_or_dir)
    if p.is_dir():
        for ext in extensions:
            matches = sorted(p.glob(f"*{ext}"))
            if matches:
                return str(matches[0])
        # Fall back to any file at all
        children = sorted(p.iterdir())
        if children:
            return str(children[0])
    return path_or_dir


def _read_sidecar(canonical_path: str) -> dict | None:
    """Read the _summary.json sidecar if it exists (shared canonical_io tail)."""
    return _ci_read_sidecar(canonical_path)


# Banks with dedicated extraction skills
DEDICATED_BANKS = ["ICICI", "Bank of Baroda", "HSBC", "HDFC"]
# Banks that go through the generic CSV normalisation path
CSV_BANKS = ["Other Bank (CSV)"]
SUPPORTED_BANKS = DEDICATED_BANKS + CSV_BANKS

# Canonical schema (order matters — GnuCash importer expects this sequence)
CANONICAL_COLS = [
    "Date",
    "Transaction ID",
    "Description",
    "Account",
    "Deposit",
    "Withdrawal",
    "Balance",
    "Currency",
]

# ---------------------------------------------------------------------------
# GnuCash ledger balance extraction
# ---------------------------------------------------------------------------

def _get_gnucash_account_balance(gnucash_file: str, bank_name: str) -> dict:
    """
    Parse a .gnucash XML file and find the bank account's ledger balance.

    Searches for an account whose name contains bank_name (case-insensitive)
    under Assets, sums all transaction splits to compute the balance.

    Returns:
        {
            "found": bool,
            "account_name": str,
            "balance": float,
            "last_txn_date": str or None,  # YYYY-MM-DD
        }
    """
    try:
        with gzip.open(gnucash_file, 'rt', encoding='utf-8') as f:
            tree = ET.parse(f)
    except Exception:
        return {"found": False, "account_name": "", "balance": 0.0, "last_txn_date": None}

    root = tree.getroot()

    # Build account map: id → {name, parent_id, type}
    acc_map = {}
    for acc in root.findall(f'.//{_NS["gnc"]}account'):
        aid = acc.findtext(f'{_NS["act"]}id', '')
        aname = acc.findtext(f'{_NS["act"]}name', '')
        atype = acc.findtext(f'{_NS["act"]}type', '')
        parent_el = acc.find(f'{_NS["act"]}parent')
        parent_id = parent_el.text if parent_el is not None else None
        acc_map[aid] = {"name": aname, "parent_id": parent_id, "type": atype}

    def _full_path(aid):
        parts = []
        visited = set()
        while aid and aid in acc_map and aid not in visited:
            visited.add(aid)
            parts.append(acc_map[aid]["name"])
            aid = acc_map[aid]["parent_id"]
        return ":".join(reversed(parts))

    # Find the bank account by name match
    target_id = None
    target_name = ""
    bank_lower = bank_name.lower()

    for aid, info in acc_map.items():
        full = _full_path(aid)
        if bank_lower in full.lower() and info["type"] in ("BANK", "ASSET"):
            target_id = aid
            target_name = full
            break

    if not target_id:
        return {"found": False, "account_name": "", "balance": 0.0, "last_txn_date": None}

    # Sum splits for this account
    balance = 0.0
    last_date = None

    for trn in root.findall(f'.//{_NS["gnc"]}transaction'):
        date_el = trn.find(f'{_NS["trn"]}date-posted/{_NS["ts"]}date')
        trn_date = date_el.text[:10] if date_el is not None else None

        for sp in trn.findall(f'{_NS["trn"]}splits/{_NS["trn"]}split'):
            sp_acc = sp.findtext(f'{_NS["split"]}account', '')
            if sp_acc == target_id:
                val_str = sp.findtext(f'{_NS["split"]}value', '0/1')
                # GnuCash stores values as "num/denom" e.g. "150000/100"
                parts = val_str.split('/')
                if len(parts) == 2:
                    try:
                        balance += int(parts[0]) / int(parts[1])
                    except (ValueError, ZeroDivisionError):
                        pass
                if trn_date:
                    if last_date is None or trn_date > last_date:
                        last_date = trn_date

    return {
        "found": True,
        "account_name": target_name,
        "balance": round(balance, 2),
        "last_txn_date": last_date,
    }


def _reconcile_opening_balance(
    canonical_rows: list[dict],
    gnucash_file: str,
    bank_name: str,
) -> dict:
    """
    Reconcile the canonical CSV's opening balance against GnuCash ledger.

    Three scenarios:
      A. Statement has entries dated before/on GnuCash's last txn date
         → these are duplicates already posted → skip them
      B. GnuCash has entries not in the statement (previous statement omitted)
         → cannot detect without prior statement → flag warning
      C. Some other error → flag error

    Returns:
        {
            "ok": bool,
            "message": str,
            "rows_skipped": int,           # scenario A duplicates removed
            "filtered_rows": list[dict],   # rows after removing duplicates
            "gnucash_balance": float,
            "statement_opening": float,
        }
    """
    if not canonical_rows:
        return {
            "ok": False,
            "message": "No rows in canonical CSV.",
            "rows_skipped": 0,
            "filtered_rows": [],
            "gnucash_balance": 0.0,
            "statement_opening": 0.0,
        }

    gc = _get_gnucash_account_balance(gnucash_file, bank_name)
    if not gc["found"]:
        log.warning(
            "Could not find %s account in GnuCash — skipping opening balance check",
            bank_name,
        )
        return {
            "ok": True,
            "message": f"GnuCash account for '{bank_name}' not found — skipping balance reconciliation.",
            "rows_skipped": 0,
            "filtered_rows": canonical_rows,
            "gnucash_balance": 0.0,
            "statement_opening": 0.0,
        }

    gc_balance = gc["balance"]
    gc_last_date = gc["last_txn_date"]

    # Derive statement opening balance
    oc = extract_opening_closing(canonical_rows)
    stmt_opening = oc["opening_balance"]

    diff = abs(gc_balance - stmt_opening)

    if diff <= 0.02:
        # Perfect match — no duplicates, no gap
        return {
            "ok": True,
            "message": (
                f"Opening balance matches GnuCash: "
                f"GnuCash={gc_balance:.2f}, Statement={stmt_opening:.2f}"
            ),
            "rows_skipped": 0,
            "filtered_rows": canonical_rows,
            "gnucash_balance": gc_balance,
            "statement_opening": stmt_opening,
        }

    # Scenario A check: are there rows in the statement dated on or before
    # GnuCash's last transaction date? If so, they're likely duplicates.
    if gc_last_date:
        filtered = []
        skipped = 0
        running_bal = stmt_opening

        for row in canonical_rows:
            row_date = row.get("Date", "")
            # Normalise date for comparison — handle DD/MM/YYYY and YYYY-MM-DD
            try:
                if "/" in row_date:
                    parts = row_date.split("/")
                    if len(parts[2]) == 4:
                        cmp_date = f"{parts[2]}-{parts[1]}-{parts[0]}"
                    else:
                        cmp_date = f"20{parts[2]}-{parts[1]}-{parts[0]}"
                else:
                    cmp_date = row_date
            except (IndexError, ValueError):
                cmp_date = row_date

            if cmp_date <= gc_last_date:
                skipped += 1
                continue
            filtered.append(row)

        if skipped > 0 and filtered:
            # Re-check: does the new opening balance match GnuCash now?
            new_oc = extract_opening_closing(filtered)
            new_diff = abs(gc_balance - new_oc["opening_balance"])
            if new_diff <= 0.02:
                return {
                    "ok": True,
                    "message": (
                        f"Scenario A: {skipped} entries already in GnuCash (dated ≤ {gc_last_date}) — skipped.\n"
                        f"Opening balance now matches: GnuCash={gc_balance:.2f}, "
                        f"Statement (after skip)={new_oc['opening_balance']:.2f}"
                    ),
                    "rows_skipped": skipped,
                    "filtered_rows": filtered,
                    "gnucash_balance": gc_balance,
                    "statement_opening": new_oc["opening_balance"],
                }

    # Scenario B or C — gap we can't resolve
    return {
        "ok": False,
        "message": (
            f"OPENING BALANCE MISMATCH: GnuCash ({gc['account_name']}) "
            f"shows {gc_balance:.2f} but statement opens at {stmt_opening:.2f} "
            f"(diff={diff:.2f}).\n"
            f"Possible causes: (B) prior statement entries were omitted, "
            f"or (C) there's a data error. Please investigate manually."
        ),
        "rows_skipped": 0,
        "filtered_rows": canonical_rows,
        "gnucash_balance": gc_balance,
        "statement_opening": stmt_opening,
    }


_NORMALISE_PROMPT = """\
You are a data normalisation assistant. Your job is to map the columns of a bank
statement CSV to a fixed canonical schema.

Canonical columns (in order):
  Date, Transaction ID, Description, Account, Deposit, Withdrawal, Balance, Currency

The user's CSV has these headers:
{headers}

Here are the first few sample rows so you can see the data format:
{sample}

Rules:
- Return ONLY a valid JSON object — no markdown, no explanation, no code fences.
- Map each canonical column name to the best-matching header from the user's CSV.
- If there is no reasonable match for a canonical column, map it to null.
- "Deposit" and "Withdrawal" are credit and debit amounts respectively. They may
  appear as a single "Amount" column with sign — if so, map both to that column
  and include a "sign_convention" key: "positive_is_deposit" or "negative_is_deposit".
- Do not invent column names; only use names that appear verbatim in the headers list.

Example response:
{
  "Date": "Txn Date",
  "Transaction ID": "Ref No / Cheque No",
  "Description": "Narration",
  "Account": null,
  "Deposit": "Credit",
  "Withdrawal": "Debit",
  "Balance": "Balance (INR)",
  "Currency": null
}
"""


def _read_csv_or_xls(file_path: str) -> tuple[list[str], list[list[str]]]:
    """
    Read a CSV or XLS/XLSX file and return (headers, sample_rows).
    sample_rows is at most 5 data rows.
    """
    p = Path(file_path)
    suffix = p.suffix.lower()

    if suffix in (".xls", ".xlsx"):
        if suffix == ".xls":
            import xlrd
            wb = xlrd.open_workbook(str(p))
            ws = wb.sheet_by_index(0)
            rows = []
            for r in range(ws.nrows):
                row = []
                for c in range(ws.ncols):
                    cell = ws.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        import xlrd as _x
                        dt = _x.xldate_as_datetime(cell.value, wb.datemode)
                        row.append(dt.strftime("%d/%m/%Y"))
                    elif cell.ctype == xlrd.XL_CELL_NUMBER:
                        v = cell.value
                        row.append(str(int(v)) if v == int(v) else str(v))
                    elif cell.ctype == xlrd.XL_CELL_EMPTY:
                        row.append("")
                    else:
                        row.append(str(cell.value).strip())
                rows.append(row)
        else:
            import openpyxl
            wb = openpyxl.load_workbook(str(p), data_only=True)
            ws = wb.active
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append([str(c) if c is not None else "" for c in row])

        if not rows:
            return [], []
        headers = rows[0]
        sample = rows[1:6]
        return headers, sample

    else:  # CSV
        with open(file_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            rows = list(reader)
        if not rows:
            return [], []
        headers = rows[0]
        sample = rows[1:6]
        return headers, sample


def _normalise_to_canonical(
    input_file: str,
    output_path: str,
    bank_name: str,
    config_path: str,
    model_override: str,
) -> None:
    """
    LLM-assisted column normalisation: maps arbitrary CSV/XLS to the canonical
    8-column schema and writes the result to output_path.
    """
    agents_root = Path(__file__).resolve().parent.parent
    if str(agents_root) not in sys.path:
        sys.path.insert(0, str(agents_root))
    from agents.base_agent import run_direct  # noqa: E402

    headers, sample = _read_csv_or_xls(input_file)
    if not headers:
        raise ValueError(f"Could not read any data from {input_file}")

    headers_str = json.dumps(headers)
    sample_str = "\n".join(
        "  " + ", ".join(f"{h}={v}" for h, v in zip(headers, row))
        for row in sample
    )

    prompt = _NORMALISE_PROMPT.format(headers=headers_str, sample=sample_str)
    system = (
        f"You are normalising a {bank_name} bank statement CSV to canonical format. "
        "Output ONLY raw JSON — no markdown, no explanation."
    )

    raw = run_direct(
        user_message=prompt,
        system_prompt=system,
        config_path=config_path,
        model_override=model_override,
    )

    # Strip accidental markdown fences if the LLM adds them
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    mapping = json.loads(raw.strip())

    sign_convention = mapping.pop("sign_convention", None)

    # Re-read full file
    all_headers, _ = _read_csv_or_xls(input_file)
    p = Path(input_file)
    if p.suffix.lower() in (".xls", ".xlsx"):
        # Re-read all rows
        if p.suffix.lower() == ".xls":
            import xlrd
            wb = xlrd.open_workbook(str(p))
            ws = wb.sheet_by_index(0)
            all_rows = []
            for r in range(1, ws.nrows):  # skip header
                row = []
                for c in range(ws.ncols):
                    cell = ws.cell(r, c)
                    if cell.ctype == xlrd.XL_CELL_DATE:
                        dt = xlrd.xldate_as_datetime(cell.value, wb.datemode)
                        row.append(dt.strftime("%d/%m/%Y"))
                    elif cell.ctype == xlrd.XL_CELL_NUMBER:
                        v = cell.value
                        row.append(str(int(v)) if v == int(v) else str(v))
                    elif cell.ctype == xlrd.XL_CELL_EMPTY:
                        row.append("")
                    else:
                        row.append(str(cell.value).strip())
                all_rows.append(row)
        else:
            import openpyxl
            wb = openpyxl.load_workbook(str(p), data_only=True)
            ws = wb.active
            all_rows = []
            for i, row in enumerate(ws.iter_rows(values_only=True)):
                if i == 0:
                    continue
                all_rows.append([str(c) if c is not None else "" for c in row])
    else:
        with open(input_file, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            data_rows = list(reader)
        all_rows = data_rows[1:]  # skip header

    col_idx = {h: i for i, h in enumerate(all_headers)}

    def _get(row, col_name):
        if col_name is None:
            return ""
        idx = col_idx.get(col_name)
        if idx is None or idx >= len(row):
            return ""
        return row[idx]

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(CANONICAL_COLS)
        for row in all_rows:
            # Handle single-amount-column case
            deposit_src = mapping.get("Deposit")
            withdrawal_src = mapping.get("Withdrawal")
            deposit_val = _get(row, deposit_src)
            withdrawal_val = _get(row, withdrawal_src)

            if deposit_src and deposit_src == withdrawal_src and sign_convention:
                # Same source column — split by sign
                try:
                    amt = float(deposit_val.replace(",", "") or "0")
                except ValueError:
                    amt = 0.0
                if sign_convention == "positive_is_deposit":
                    deposit_val = str(amt) if amt > 0 else ""
                    withdrawal_val = str(abs(amt)) if amt < 0 else ""
                else:  # negative_is_deposit
                    deposit_val = str(abs(amt)) if amt < 0 else ""
                    withdrawal_val = str(amt) if amt > 0 else ""

            out_row = [
                _get(row, mapping.get("Date")),
                _get(row, mapping.get("Transaction ID")),
                _get(row, mapping.get("Description")),
                _get(row, mapping.get("Account")),
                deposit_val,
                withdrawal_val,
                _get(row, mapping.get("Balance")),
                _get(row, mapping.get("Currency")) or "INR",
            ]
            writer.writerow(out_row)


def run(
    bank: str,
    statement_files: str,
    gnucash_file: str,
    output_path: str,
    config_path: str = None,
    model_override: str = None,
) -> str:
    """
    Run the full GnuCash import pipeline.

    Args:
        bank:            Bank name — one of SUPPORTED_BANKS.
        statement_files: Path or comma-separated paths to uploaded statement file(s).
                         XLS for ICICI; PDF(s) for BoB / HSBC;
                         CSV or XLS/XLSX for HDFC / Other Bank.
        gnucash_file:    Path to .gnucash book (must be closed in GnuCash).
        output_path:     Path for the final mapped CSV.
        config_path:     Passed through to sub-skills and LLM calls.
        model_override:  Passed through to sub-skills and LLM calls.

    Returns:
        Human-readable summary string for the UI.
    """
    agents_root = Path(__file__).resolve().parent.parent
    if str(agents_root) not in sys.path:
        sys.path.insert(0, str(agents_root))

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    bank = (bank or "").strip()
    if bank not in SUPPORTED_BANKS:
        return (
            f"❌ Unknown bank **{bank!r}**.\n\n"
            f"Supported banks: {', '.join(SUPPORTED_BANKS)}"
        )

    log_lines = []

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        canonical_path = str(tmp_path / "canonical.csv")

        # ── Step 1 + 2: Bank extraction → canonical CSV ───────────────────────

        if bank == "ICICI":
            icici_input = (
                statement_files[0] if isinstance(statement_files, list)
                else statement_files.split(",")[0].strip()
            )
            icici_input = _resolve_single_file(icici_input, (".xls", ".xlsx"))
            _emit_progress(1, f"ICICI: extracting statement to canonical CSV")
            log_lines.append("**Step 1** — ICICI: extracting statement to canonical CSV")
            # ICICISkill.parse() writes the canonical CSV (byte-identical to the
            # former run() path) plus the sidecar via the shared canonical_io tail.
            try:
                from skill_icici.agent import ICICISkill  # noqa: E402
                icici_result = ICICISkill().parse(icici_input, output_path=canonical_path)
                log.info("ICICI skill: %d canonical rows (balance_ok=%s)",
                         icici_result.row_count, icici_result.balance_check.ok)
            except Exception as e:
                log.error("ICICI extraction failed: %s", e, exc_info=True)
                return (
                    f"## {bank} → extraction error\n\n"
                    f"❌ ICICI skill raised an exception:\n```\n{e}\n```"
                )

        elif bank == "Bank of Baroda":
            bob_input = (
                statement_files[0] if isinstance(statement_files, list)
                else statement_files.split(",")[0].strip()
            )
            _emit_progress(1, "Bank of Baroda: extracting PDFs to canonical CSV")
            log_lines.append("**Step 1** — Bank of Baroda: extracting PDFs to canonical CSV")
            # skill_bob.BoBSkill.parse() extracts the PDF table AND maps it to the
            # canonical schema (the former adapter_bob step is folded in), writing
            # the canonical CSV + sidecar via the shared canonical_io tail.
            try:
                from skill_bob.agent import BoBSkill  # noqa: E402

                bob_src = Path(bob_input)
                if bob_src.is_dir() and not sorted(bob_src.glob("*.pdf")):
                    return (
                        f"## {bank} → no PDFs found\n\n"
                        f"❌ The staged upload directory contains no .pdf files:\n"
                        f"`{bob_input}`"
                    )
                bob_result = BoBSkill().parse(bob_src, output_path=canonical_path)
                log.info("BoB skill: %d canonical rows (balance_ok=%s)",
                         bob_result.row_count, bob_result.balance_check.ok)
            except Exception as e:
                log.error("BoB extraction failed: %s", e, exc_info=True)
                return (
                    f"## {bank} → extraction error\n\n"
                    f"❌ Bank of Baroda extraction failed:\n```\n{e}\n```"
                )

        elif bank == "HSBC":
            hsbc_xlsx = str(tmp_path / "hsbc_raw.xlsx")
            work_dir = str(tmp_path / "hsbc_work")
            hsbc_input = (
                statement_files[0] if isinstance(statement_files, list)
                else statement_files.split(",")[0].strip()
            )
            # If input is a directory (staged uploads), use it as pdf_dir;
            # if it's a single file, use its parent directory.
            hsbc_src = Path(hsbc_input)
            pdf_dir_for_hsbc = str(hsbc_src) if hsbc_src.is_dir() else str(hsbc_src.parent)

            _emit_progress(1, "HSBC: extracting PDFs to Excel (direct)")
            log_lines.append("**Step 1** — HSBC: extracting PDFs to Excel")
            try:
                # Bypass the LangGraph agent — call the pipeline tool directly.
                # HSBC extraction is pure Python (OCR + parse), no LLM needed.
                from skill_hsbc.tools import run_hsbc_pipeline  # noqa: E402
                hsbc_result = run_hsbc_pipeline.invoke({
                    "pdf_dir": pdf_dir_for_hsbc,
                    "work_dir": work_dir,
                    "output_path": hsbc_xlsx,
                    "title": "HSBC Statement",
                })
                log.info("HSBC pipeline result: %s", hsbc_result[:200] if hsbc_result else "empty")
            except Exception as e:
                log.error("HSBC extraction failed: %s", e, exc_info=True)
                return (
                    f"## {bank} → extraction error\n\n"
                    f"❌ HSBC extraction failed:\n```\n{e}\n```"
                )
            # Check intermediate output before proceeding to adapter
            if not Path(hsbc_xlsx).is_file():
                return (
                    f"## {bank} → extraction produced no output\n\n"
                    f"❌ The HSBC pipeline did not create the Excel file.\n\n"
                    f"**Pipeline result:** {hsbc_result}"
                )
            _emit_progress(2, "HSBC: converting to canonical format")
            log_lines.append("**Step 2** — HSBC: converting to canonical format")
            # HSBCSkill.parse() maps the enriched workbook to the canonical
            # schema (folds in the former adapter_hsbc, with the column-mapping
            # bug fixed) and writes the canonical CSV + sidecar.
            try:
                from skill_hsbc.agent import HSBCSkill  # noqa: E402
                hsbc_result = HSBCSkill().parse(hsbc_xlsx, output_path=canonical_path)
                log.info("HSBC skill: %d canonical rows (balance_ok=%s)",
                         hsbc_result.row_count, hsbc_result.balance_check.ok)
            except Exception as e:
                log.error("HSBC adapter failed: %s", e, exc_info=True)
                return (
                    f"## {bank} → canonical conversion error\n\n"
                    f"❌ HSBC adapter raised an exception:\n```\n{e}\n```"
                )

        elif bank == "HDFC":
            input_file = (
                statement_files[0] if isinstance(statement_files, list)
                else statement_files.split(",")[0].strip()
            )
            input_file = _resolve_single_file(input_file, (".csv", ".xls", ".xlsx", ".pdf"))
            suffix = Path(input_file).suffix.lower()
            if suffix == ".pdf":
                _emit_progress(1, f"HDFC: OCR scanning PDF statement")
                log_lines.append("**Step 1** — HDFC: OCR scanning PDF statement")
                _emit_progress(2, f"HDFC: parsing transactions to canonical format")
                log_lines.append("**Step 2** — HDFC: parsing transactions to canonical format")
                try:
                    from skill_hdfc.agent import run as hdfc_run  # noqa: E402
                    hdfc_result = hdfc_run(
                        pdf_path=input_file,
                        output_path=canonical_path,
                        config_path=config_path,
                        model_override=model_override,
                    )
                    # hdfc_run returns an error string (starting with ❌) on failure
                    if hdfc_result and "❌" in str(hdfc_result):
                        return (
                            f"## {bank} → extraction error\n\n"
                            f"{hdfc_result}"
                        )
                except Exception as e:
                    log.error("HDFC extraction failed: %s", e, exc_info=True)
                    return (
                        f"## {bank} → extraction error\n\n"
                        f"❌ HDFC skill raised an exception:\n```\n{e}\n```"
                    )
            else:
                # CSV/XLS fallback
                _emit_progress(1, f"HDFC: reading CSV/XLS statement")
                log_lines.append("**Step 1** — HDFC: reading CSV/XLS statement")
                _emit_progress(2, f"HDFC: LLM normalising columns → canonical schema")
                log_lines.append("**Step 2** — HDFC: LLM normalising columns → canonical schema")
                try:
                    _normalise_to_canonical(
                        input_file=input_file,
                        output_path=canonical_path,
                        bank_name=bank,
                        config_path=config_path,
                        model_override=model_override,
                    )
                except Exception as e:
                    log.error("HDFC normalisation failed: %s", e, exc_info=True)
                    return (
                        f"## {bank} → normalisation error\n\n"
                        f"❌ HDFC column normalisation raised an exception:\n```\n{e}\n```"
                    )

        elif bank == "Other Bank (CSV)":
            input_file = (
                statement_files[0] if isinstance(statement_files, list)
                else statement_files.split(",")[0].strip()
            )
            input_file = _resolve_single_file(input_file, (".csv", ".xls", ".xlsx"))
            _emit_progress(1, f"Other Bank: reading CSV/XLS statement")
            log_lines.append("**Step 1** — Other Bank: reading CSV/XLS statement")
            _emit_progress(2, f"Other Bank: LLM normalising columns → canonical schema")
            log_lines.append("**Step 2** — Other Bank: LLM normalising columns → canonical schema")
            try:
                _normalise_to_canonical(
                    input_file=input_file,
                    output_path=canonical_path,
                    bank_name=bank,
                    config_path=config_path,
                    model_override=model_override,
                )
            except Exception as e:
                log.error("Other Bank normalisation failed: %s", e, exc_info=True)
                return (
                    f"## {bank} → normalisation error\n\n"
                    f"❌ Column normalisation raised an exception:\n```\n{e}\n```"
                )

        # ── Verify extraction produced output ────────────────────────────────
        if not Path(canonical_path).is_file():
            steps_summary = "\n".join(f"🟢 {line}" for line in log_lines)
            return (
                f"## {bank} → extraction failed\n\n"
                f"{steps_summary}\n\n"
                f"🔴 Bank extraction did not produce a canonical CSV.\n"
                f"Check the console log for errors from the {bank} skill."
            )

        # ── Balance verification on canonical CSV ─────────────────────────────
        _emit_progress(3, f"{bank}: verifying balances")
        # Read the canonical CSV back for balance checks
        with open(canonical_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            canonical_rows = list(reader)

        # Running balance check (Intervention 2)
        running = verify_running_balance(canonical_rows)
        if running["ok"]:
            log_lines.append(
                f"Running balance: OK ({running['opening_balance']:.2f} → "
                f"{running['closing_balance']:.2f})"
            )
        else:
            log_lines.append(
                f"Running balance: {running['mismatches']} mismatch(es)"
            )

        # ── Opening balance reconciliation with GnuCash (Intervention 1) ──
        recon = _reconcile_opening_balance(canonical_rows, gnucash_file, bank)

        if recon["rows_skipped"] > 0:
            log_lines.append(
                f"Skipped {recon['rows_skipped']} duplicate entries "
                f"already in GnuCash (date-based filter)"
            )
            with open(canonical_path, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=CANONICAL_COLS)
                writer.writeheader()
                writer.writerows(recon["filtered_rows"])
            canonical_rows = recon["filtered_rows"]

        if recon["ok"]:
            log_lines.append(f"**Balance check** — {recon['message']}")
        else:
            # Don't flag as error — dedup below will handle overlapping rows
            log_lines.append(
                f"**Balance check** — Opening balance gap detected "
                f"(GnuCash={recon['gnucash_balance']:.2f}, "
                f"statement={recon['statement_opening']:.2f}). "
                f"Dedup will reconcile overlapping transactions."
            )

        # ── Duplicate detection (Phase 4 Lite) ─────────────────────────────────
        # Compare canonical CSV against GnuCash book to flag duplicates
        _emit_progress(4, f"{bank}: checking for duplicates in GnuCash")

        gnucash_data = None  # set here so contra detection can use it even if dup-check fails
        try:
            # Parse GnuCash file to get existing transactions
            gnucash_data = parse_gnucash_for_reconcile(gnucash_file)

            # Convert canonical_rows to reconcile format
            # (canonical_rows are already dicts with 'Date', 'Deposit', 'Withdrawal' keys)
            reconcile_rows = []
            for idx, row in enumerate(canonical_rows, 1):
                try:
                    deposit = _safe_float(row.get('Deposit', 0))
                    withdrawal = _safe_float(row.get('Withdrawal', 0))
                    reconcile_rows.append({
                        'row_num': idx,
                        'date': row.get('Date', ''),
                        'description': row.get('Description', ''),
                        'deposit': deposit,
                        'withdrawal': withdrawal,
                    })
                except (ValueError, KeyError):
                    reconcile_rows.append({
                        'row_num': idx,
                        'date': row.get('Date', ''),
                        'description': row.get('Description', ''),
                        'deposit': 0.0,
                        'withdrawal': 0.0,
                    })

            # Run reconciliation
            report, dedup_summary = reconcile(reconcile_rows, gnucash_data)

            matched_count = dedup_summary.get('matched', 0)
            duplicate_count = dedup_summary.get('duplicates', 0)
            new_count = dedup_summary.get('new', 0)
            total_duplicates = matched_count + duplicate_count

            # Filter to keep only "New" rows
            new_rows = [
                canonical_rows[i] for i, r in enumerate(report)
                if r.get('status') == 'New'
            ]

            # Edge case: all rows are duplicates
            if total_duplicates > 0 and new_count == 0:
                return (
                    f"## {bank} → GnuCash pipeline — all transactions already in GnuCash\n\n"
                    f"Duplicate check — All {len(canonical_rows)} transaction(s) are already "
                    f"in your GnuCash book. Nothing to import.\n\n"
                    f"---\n\n"
                    f"**Next:** If you expected new transactions, check that:\n"
                    f"1. Your GnuCash file is current\n"
                    f"2. Your bank statement covers the right period\n"
                    f"3. Transactions match by date + amount (GnuCash matching logic)"
                )

            # Rewrite canonical CSV with only new rows
            if total_duplicates > 0:
                log_lines.append(
                    f"Duplicate check — {total_duplicates} already in GnuCash "
                    f"(removed), {new_count} new (will be mapped)"
                )
                with open(canonical_path, "w", newline="", encoding="utf-8") as f:
                    writer = csv.DictWriter(f, fieldnames=CANONICAL_COLS)
                    writer.writeheader()
                    writer.writerows(new_rows)
                canonical_rows = new_rows
            else:
                log_lines.append(
                    f"Duplicate check — {new_count} new transactions (none in GnuCash)"
                )

        except Exception as e:
            log_lines.append(
                f"⚠️ Duplicate check skipped — {e}. Proceeding with all rows."
            )
            log.warning(f"Duplicate detection failed: {e}")

        # ── Resolve GnuCash bank account path (for CSV Account column) ────
        gc_info = _get_gnucash_account_balance(gnucash_file, bank)
        gnucash_bank_account = ""
        if gc_info["found"]:
            raw_path = gc_info["account_name"]
            # Strip "Root Account:" prefix — GnuCash CSV importer doesn't want it
            if raw_path.startswith("Root Account:"):
                gnucash_bank_account = raw_path[len("Root Account:"):]
            else:
                gnucash_bank_account = raw_path
            log_lines.append(f"Bank account: `{gnucash_bank_account}`")

        # ── Contra detection (cross-bank transfer matching) ──────────────────
        contra_flags: dict[int, dict] = {}  # row_idx → contra info
        try:
            if gnucash_bank_account and gnucash_data:
                contras = detect_contra_entries(
                    canonical_rows, gnucash_data, gnucash_bank_account
                )
                if contras:
                    for c in contras:
                        contra_flags[c["row_idx"]] = c
                    high = sum(1 for c in contras if c["confidence"] == "high")
                    med = len(contras) - high
                    parts = []
                    if high:
                        parts.append(f"{high} high-confidence")
                    if med:
                        parts.append(f"{med} medium-confidence")
                    log_lines.append(
                        f"Contra check — {len(contras)} possible cross-bank "
                        f"transfer(s) detected ({', '.join(parts)}). "
                        f"Review in **Review Mappings** tab."
                    )
                    _emit_progress(4, f"{bank}: {len(contras)} contra(s) flagged")
                else:
                    log_lines.append("Contra check — no cross-bank transfers detected")
        except Exception as e:
            log.warning(f"Contra detection failed: {e}")
            log_lines.append(f"⚠️ Contra check skipped — {e}")

        # ── Step 3: Account mapping ───────────────────────────────────────────
        if bank == "ICICI":
            step_n = 2
        elif bank in CSV_BANKS:
            step_n = 3
        else:
            step_n = 3

        _emit_progress(5, f"{bank}: mapping accounts from {Path(gnucash_file).name}")
        log_lines.append(
            f"**Step {step_n}** — GnuCash: mapping accounts from "
            f"`{Path(gnucash_file).name}`"
        )
        from skill_gnucash_account_mapper.agent import run as mapper_run  # noqa: E402
        mapping_result = mapper_run(
            gnucash_file=gnucash_file,
            canonical_csv=canonical_path,
            output_path=output_path,
            config_path=config_path,
            model_override=model_override,
            bank_name=bank,
            gnucash_bank_account=gnucash_bank_account,
        )

        # Write contra flags sidecar (if any) alongside the output CSV
        if contra_flags:
            contra_sidecar = Path(output_path).with_suffix('.contra.json')
            try:
                import json as _json
                with open(contra_sidecar, 'w', encoding='utf-8') as cf:
                    _json.dump(contra_flags, cf, indent=2, default=str)
            except Exception as e:
                log.warning(f"Could not write contra sidecar: {e}")

        _emit_progress(6, f"{bank}: final balance verification")
        # ── Final closing balance verification (Intervention 3) ──────────
        # After dedup removes overlapping rows the pre-dedup closing balance
        # no longer matches the output CSV — skip the check in that case.
        if total_duplicates > 0:
            log_lines.append(
                "**Final check** — Closing balance check skipped "
                f"(dedup removed {total_duplicates} overlapping rows)"
            )
        else:
            try:
                with open(output_path, "r", encoding="utf-8") as f:
                    final_rows = list(csv.DictReader(f))

                # Read sidecar for an independent expected closing balance
                sidecar = _read_sidecar(canonical_path)
                if sidecar and "closing_balance" in sidecar:
                    expected_closing = sidecar["closing_balance"]
                    source_label = sidecar.get("source", "unknown")
                else:
                    expected_closing = running["closing_balance"]
                    source_label = "derived"

                closing_check = verify_closing_balance(
                    final_rows,
                    expected_closing=expected_closing,
                )

                if source_label == "statement_summary":
                    tag = "VERIFIED (independent)"
                elif source_label == "derived":
                    tag = "OK (derived — no independent source)"
                else:
                    tag = f"OK ({source_label})"

                if closing_check.get("ok"):
                    log_lines.append(f"**Final check** — Closing balance {tag}: {closing_check['message']}")
                else:
                    log_lines.append(f"**Final check** — ❌ {closing_check['message']}")
            except Exception as e:
                log_lines.append(f"**Final check** — Could not verify closing balance: {e}")

    # Color-code each log line: green = OK, amber = warning, red = error
    _WARN_KEYS = ("mismatch", "gap detected", "skipped", "⚠", "warning")
    _ERR_KEYS = ("❌", "error", "failed", "could not")

    formatted_lines = []
    for line in log_lines:
        # Strip markdown bold for cleaner one-line display
        clean = line.replace("**", "")
        low = clean.lower()
        if any(k in low for k in _ERR_KEYS):
            formatted_lines.append(f"🔴 {clean}")
        elif any(k in low for k in _WARN_KEYS):
            formatted_lines.append(f"🟡 {clean}")
        else:
            formatted_lines.append(f"🟢 {clean}")

    steps_summary = "  \n".join(formatted_lines)  # MD line break (two spaces + \n)
    return (
        f"## {bank} → GnuCash pipeline complete\n\n"
        f"{steps_summary}\n\n"
        f"---\n\n"
        f"{mapping_result}\n\n"
        f"**Next:** check the **Review Mappings** tab to verify/correct account assignments, then import into GnuCash."
    )
