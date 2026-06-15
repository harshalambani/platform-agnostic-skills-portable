# HDFC Bank Statement PDF Skill — Build Plan

**Date:** 2026-06-13  
**Goal:** Add `skill_hdfc` — OCR + deterministic parser, no LLM needed.  
**Also:** Wire HDFC PDF into `skill_gnucash_pipeline/agent.py`.

---

## 1. Confirmed HDFC PDF Format (from OCR of vaikunth ambani.pdf)

Scanned PDF (image-based — pdfplumber returns 0 text chars).  
OCR with pypdfium2 + pytesseract @ 2.5x scale works well.

### Transaction line format:

```
DD/MM/YY | Description [continuation lines...] RefNo(15 digits) | ValueDate Withdrawal Deposit Balance
```

Examples from the actual file:
```
31/05/24 | BALANCE BROUGHT FORWARD 000000000000000 | 31/08/24 0.00 32,743.00 32,743.00
21/06/24 | SELF 1579 - CHQ PAID - GF REGAL APA 0000000000000102 | 21/06/24 10,000.00 0.00 22,743.00
21/06/24 | CHQ DEP MICR 08-MUM CLG - MICR CLG - 0000000000000076 | 24/06/24 0.00 100,000.00 122,743.00
MUM: VAIKUNTH  M MBRANI:BANK OF BARODA
27/06/24 | SELF 1579 - CHQ PAID - GF REGAL APA 0000000000000104 | 27/06/24 15,000.00 0.00 107,743.00
```

Key observations:
- Ref No is always exactly 15 digits (zero-padded)
- Withdrawal / Deposit: one is always 0.00
- Balance is always the last number on the line
- Continuation lines (no leading date) are appended to previous description
- Header block ends at line containing "Statement of account"

---

## 2. Files to Create

### A. `src/agents/skill_hdfc/` (new directory)

#### `src/agents/skill_hdfc/__init__.py`
```python
# HDFC Bank statement parser skill
```

#### `src/agents/skill_hdfc/skill.yaml`
```yaml
name: "skill_hdfc"
display_name: "HDFC"
description: >
  Convert HDFC Bank scanned PDF statements to GnuCash-importable CSV.
  Uses OCR (pypdfium2 + pytesseract) + deterministic regex parsing.
  No LLM required.
category: "banks"
version: "1.0.0"

mode: "direct"
entry_point: "agent:run"

inputs:
  - name: "pdf_path"
    type: "file"
    label: "HDFC statement PDF"
    file_types: [".pdf"]
    required: true

run_args:
  pdf_path: "{inputs.pdf_path}"
  output_path: "{output_path}"
  config_path: "{config_path}"
  model_override: "{model_override}"

output:
  extension: ".csv"
  suffix: "HDFC_canonical"
  download_label: "Download CSV"

requires:
  native_binaries: ["tesseract"]
  external_tools: []
```

#### `src/agents/skill_hdfc/agent.py`
```python
#!/usr/bin/env python3
"""
HDFC Bank statement PDF → canonical 8-column CSV.

Pipeline:
  1. Rasterize each PDF page with pypdfium2 at 2.5x (≈225 DPI)
  2. OCR with pytesseract (--psm 6 uniform block)
  3. Regex-parse transaction lines
  4. Write canonical CSV

Canonical output columns:
  Date, Transaction ID, Description, Account, Deposit, Withdrawal, Balance, Currency
"""
import csv
import re
import sys
from pathlib import Path


CANONICAL_COLS = [
    "Date", "Transaction ID", "Description", "Account",
    "Deposit", "Withdrawal", "Balance", "Currency",
]

# Matches a transaction line start: DD/MM/YY |
_DATE_RE = re.compile(r'^(\d{2}/\d{2}/\d{2})\s*\|')

# Matches the tail of a complete transaction:
# ... 15-digit-ref | value_date  withdrawal  deposit  balance
# Numbers may contain commas (1,00,000.00)
_TAIL_RE = re.compile(
    r'(\d{15})\s*\|\s*'
    r'(\d{2}/\d{2}/\d{2})\s+'
    r'([\d,]+\.?\d*)\s+'
    r'([\d,]+\.?\d*)\s+'
    r'([\d,]+\.?\d*)\s*$'
)


def _clean_amount(s: str) -> str:
    """Remove commas from amount strings; return '' for '0.00'."""
    s = s.replace(',', '').strip()
    try:
        v = float(s)
        return '' if v == 0.0 else s
    except ValueError:
        return s


def _ocr_pdf(pdf_path: str) -> str:
    """Rasterize and OCR all pages; return joined text."""
    import pypdfium2 as pdfium
    import pytesseract

    pdf = pdfium.PdfDocument(pdf_path)
    pages_text = []
    for i in range(len(pdf)):
        page = pdf[i]
        bitmap = page.render(scale=2.5)
        pil_img = bitmap.to_pil()
        text = pytesseract.image_to_string(pil_img, config='--psm 6')
        pages_text.append(text)
    return '\n'.join(pages_text)


def _parse_transactions(ocr_text: str) -> list[dict]:
    """
    Parse OCR text into a list of transaction dicts.

    Strategy:
    1. Find "Statement of account" header line; ignore everything before it.
    2. Walk line by line:
       - If line starts with DD/MM/YY |, it's a new transaction (may be incomplete).
       - Append subsequent lines to the current transaction's description buffer
         until _TAIL_RE matches the accumulated text.
    3. Skip summary/footer lines (closing balance, opening balance, page totals).
    """
    lines = ocr_text.splitlines()

    # Find the start marker
    start_idx = 0
    for i, line in enumerate(lines):
        if 'statement of account' in line.lower():
            start_idx = i + 1
            break

    transactions = []
    current: dict | None = None
    current_text = ''

    for line in lines[start_idx:]:
        line = line.strip()
        if not line:
            continue

        # Skip footer/summary keywords
        low = line.lower()
        if any(k in low for k in [
            'closing balance', 'opening balance', 'total deposits',
            'total withdrawals', 'page no', 'statement of account',
            'generated on', 'generated by',
        ]):
            current = None
            current_text = ''
            continue

        m_date = _DATE_RE.match(line)

        if m_date:
            # Save any pending transaction first
            if current and current_text:
                m_tail = _TAIL_RE.search(current_text)
                if m_tail:
                    transactions.append(_build_txn(current, current_text, m_tail))
            # Start new
            current = {'date': m_date.group(1)}
            current_text = line
        elif current is not None:
            # Continuation line — append to buffer
            current_text += ' ' + line

        # Check if current buffer is now complete
        if current and current_text:
            m_tail = _TAIL_RE.search(current_text)
            if m_tail:
                transactions.append(_build_txn(current, current_text, m_tail))
                current = None
                current_text = ''

    # Flush last
    if current and current_text:
        m_tail = _TAIL_RE.search(current_text)
        if m_tail:
            transactions.append(_build_txn(current, current_text, m_tail))

    return transactions


def _build_txn(current: dict, text: str, m_tail) -> dict:
    """Extract fields from a complete transaction buffer."""
    ref_no = m_tail.group(1)
    # withdrawal = m_tail.group(3), deposit = m_tail.group(4), balance = m_tail.group(5)
    withdrawal = _clean_amount(m_tail.group(3))
    deposit = _clean_amount(m_tail.group(4))
    balance = m_tail.group(5).replace(',', '')

    # Description is everything between the opening date| and the ref number
    # Extract from text: after first '|' up to the ref number
    pipe_idx = text.index('|') + 1
    ref_idx = text.rfind(ref_no)
    raw_desc = text[pipe_idx:ref_idx].strip() if ref_idx > pipe_idx else ''
    # Clean up OCR artefacts: collapse whitespace, strip trailing dashes
    desc = re.sub(r'\s+', ' ', raw_desc).strip(' -')

    # Convert DD/MM/YY → DD/MM/YYYY
    d = current['date']
    if len(d) == 8:  # DD/MM/YY
        parts = d.split('/')
        year = '20' + parts[2]
        d = f"{parts[0]}/{parts[1]}/{year}"

    return {
        'Date': d,
        'Transaction ID': ref_no,
        'Description': desc,
        'Account': '',
        'Deposit': deposit,
        'Withdrawal': withdrawal,
        'Balance': balance,
        'Currency': 'INR',
    }


def run(
    pdf_path: str,
    output_path: str,
    config_path: str = None,
    model_override: str = None,
) -> str:
    """
    Entry point: OCR + parse HDFC PDF → canonical CSV.

    Args:
        pdf_path:      Path to HDFC statement PDF.
        output_path:   Destination canonical CSV path.
        config_path:   Unused (no LLM needed).
        model_override: Unused.

    Returns:
        Human-readable summary string.
    """
    pdf_path = str(pdf_path)
    if not Path(pdf_path).is_file():
        return f"❌ File not found: {pdf_path}"

    ocr_text = _ocr_pdf(pdf_path)
    transactions = _parse_transactions(ocr_text)

    if not transactions:
        return (
            "❌ No transactions found in PDF.\n\n"
            "Possible causes: OCR quality issue, unexpected statement format, "
            "or the PDF is not an HDFC bank statement."
        )

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_COLS)
        writer.writeheader()
        writer.writerows(transactions)

    return (
        f"✓ HDFC: extracted {len(transactions)} transactions from "
        f"{Path(pdf_path).name} → canonical CSV"
    )
```

---

## 3. Wire HDFC PDF into the Pipeline

In `src/agents/skill_gnucash_pipeline/agent.py`, replace the HDFC branch:

**Current (CSV-only):**
```python
elif bank in CSV_BANKS:
    if isinstance(statement_files, list):
        input_file = statement_files[0]
    else:
        input_file = statement_files.split(",")[0].strip()
    log_lines.append(f"**Step 1** — {bank}: reading statement and detecting column layout")
    log_lines.append(f"**Step 2** — {bank}: LLM normalising columns → canonical schema")
    _normalise_to_canonical(input_file=input_file, output_path=canonical_path, ...)
```

**Replace with:**
```python
elif bank == "HDFC":
    input_file = (
        statement_files[0] if isinstance(statement_files, list)
        else statement_files.split(",")[0].strip()
    )
    suffix = Path(input_file).suffix.lower()
    if suffix == ".pdf":
        log_lines.append("**Step 1** — HDFC: OCR scanning PDF statement")
        log_lines.append("**Step 2** — HDFC: parsing transactions to canonical format")
        from skill_hdfc.agent import run as hdfc_run  # noqa: E402
        hdfc_run(
            pdf_path=input_file,
            output_path=canonical_path,
            config_path=config_path,
            model_override=model_override,
        )
    else:
        # CSV/XLS fallback (unchanged)
        log_lines.append("**Step 1** — HDFC: reading CSV/XLS statement")
        log_lines.append("**Step 2** — HDFC: LLM normalising columns → canonical schema")
        _normalise_to_canonical(
            input_file=input_file,
            output_path=canonical_path,
            bank_name=bank,
            config_path=config_path,
            model_override=model_override,
        )

elif bank == "Other Bank (CSV)":
    input_file = (
        statement_files[0] if isinstance(statement_files, list)
        else statement_files.split(",")[0].strip()
    )
    log_lines.append("**Step 1** — Other Bank: reading CSV/XLS statement")
    log_lines.append("**Step 2** — Other Bank: LLM normalising columns → canonical schema")
    _normalise_to_canonical(
        input_file=input_file,
        output_path=canonical_path,
        bank_name=bank,
        config_path=config_path,
        model_override=model_override,
    )
```

Also update `DEDICATED_BANKS` and `CSV_BANKS` at the top of agent.py:
```python
DEDICATED_BANKS = ["ICICI", "Bank of Baroda", "HSBC", "HDFC"]
CSV_BANKS = ["Other Bank (CSV)"]
```

---

## 4. Update skill_gnucash_pipeline/skill.yaml

The dropdown options and file type label stay the same — HDFC is already listed.  
Just ensure `.pdf` is in `file_types` (it already is).

---

## 5. Summary of all file operations

| Operation | File |
|-----------|------|
| CREATE    | `src/agents/skill_hdfc/__init__.py` |
| CREATE    | `src/agents/skill_hdfc/skill.yaml` |
| CREATE    | `src/agents/skill_hdfc/agent.py` |
| EDIT      | `src/agents/skill_gnucash_pipeline/agent.py` (HDFC branch split + Other Bank split) |

---

## 6. Test command (run from project root)

```powershell
cd "C:\Users\inabm\Documents\Cowork Playground\platform-agnostic-skills-portable"
python -c "
import sys; sys.path.insert(0, 'src')
from agents.skill_hdfc.agent import run
result = run(
    pdf_path=r'Data\Vaikunth\vaikunth ambani.pdf',
    output_path=r'Data\Vaikunth\hdfc_canonical_test.csv',
)
print(result)
"
```

Expected output: `✓ HDFC: extracted N transactions from vaikunth ambani.pdf → canonical CSV`

---

## 7. Notes for implementation session

- All file writes must go to `/tmp` first, validate AST (for .py) or lint (for .yaml), then `cp` to mount — never Edit/Write directly on the Cowork mount (null byte corruption risk).
- `skill_hdfc` uses `mode: "direct"` (not `"agent"`) because it needs no LLM — same as any deterministic skill.
- The `_TAIL_RE` regex was designed from real OCR output. If OCR spacing varies, the `\s+` between amounts handles it.
- The `_clean_amount()` function converts `0.00` → `''` to keep the canonical schema clean (only one of Deposit/Withdrawal should be non-empty per row).
