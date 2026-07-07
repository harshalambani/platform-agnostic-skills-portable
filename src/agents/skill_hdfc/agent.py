#!/usr/bin/env python3
"""
HDFC Bank statement -> canonical 8-column CSV.

Supports two input formats:
  PDF (digital or scanned):
    1. (Primary) pdfplumber text extraction
    2. (Fallback) OCR with pytesseract
    3. Regex-parse transaction lines from either source
    4. Statement summary validation (DrCount, CrCount, Opening, Closing)

  XLS/XLSX (net-banking download):
    1. Read with xlrd (.xls) or openpyxl (.xlsx)
    2. Auto-detect header row
    3. Deterministic column mapping

Canonical output columns:
  Date, Transaction ID, Description, Account, Deposit, Withdrawal, Balance, Currency
"""
import csv
import json
import logging
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from agents.balance_utils import (
    verify_running_balance,
    verify_closing_balance,
    format_balance_summary,
)
from agents.canonical_io import CANONICAL_FIELDS

log = logging.getLogger(__name__)


# Single source of truth for the canonical schema lives in canonical_io; keep
# the local name as an alias so existing references don't churn.
CANONICAL_COLS = list(CANONICAL_FIELDS)


def _clean_amount(s):
    s = str(s).replace(",", "").strip()
    try:
        return "" if float(s) == 0.0 else s
    except ValueError:
        return s


def _normalise_date(d):
    parts = d.strip().split("/")
    if len(parts) != 3:
        return d
    dd, mm, yy = parts[0], parts[1], parts[2]
    if len(yy) == 2:
        yy = "20" + yy
    return yy + "-" + mm + "-" + dd


def _extract_statement_summary(text):
    m = re.search(
        r'STATEMENT\s*SUMMARY\s*[:\-]*\s*'
        r'(?:Opening\s*Balance\s*Dr\s*Count\s*Cr\s*Count\s*Debits\s*Credits\s*Closing\s*Bal\w*\s*)?'
        r'([\d,]+\.?\d*)\s+(\d+)\s+(\d+)\s+([\d,]+\.?\d*)\s+([\d,]+\.?\d*)\s+([\d,]+\.?\d*)',
        text, re.IGNORECASE,
    )
    if not m:
        return {}
    return {
        "opening": float(m.group(1).replace(",", "")),
        "dr_count": int(m.group(2)),
        "cr_count": int(m.group(3)),
        "debits": float(m.group(4).replace(",", "")),
        "credits": float(m.group(5).replace(",", "")),
        "closing": float(m.group(6).replace(",", "")),
    }


_PB_DATE_RE = re.compile(r'^(\d{2}/\d{2}/\d{2})\s+(.+)')
_PB_TAIL_RE = re.compile(
    r'(\S+)\s+'
    r'(\d{2}/\d{2}/\d{2})\s+'
    r'([\d,]+\.\d{2})\s+'
    r'([\d,]+\.\d{2})\s+'
    r'([\d,]+\.\d{2})\s*$'
)

_PB_SKIP_RE = re.compile(r'|'.join([
    r'closing\s*balance', r'opening\s*balance', r'page\s*no',
    r'statement\s*of\s*account', r'generated\s*on', r'generated\s*by',
    r'hdfc\s*bank\s*limited', r'registered\s*office',
    r'contents\s*of\s*this', r'state\s*account\s*branch',
    r'gstin\s*number', r'narration', r'chq\.\s*/?\s*ref',
    r'withdrawal\s*amt', r'deposit\s*amt', r'statement\s*summary',
    r'earmarked\s*for\s*hold', r'considered\s*correct',
    r'branch\s*gstn', r'senapati\s*bapat', r'account\s*branch',
    r'preferred\s*customer', r'account\s*type', r'branch\s*code',
    r'nomination', r'rtgs.*ifsc', r'joint\s*holders',
    r'a/c\s*open\s*date', r'account\s*status', r'account\s*no',
    r'cust\s*id', r'od\s*limit', r'currency\s*:', r'email\s*:',
    r'phone\s*no', r'city\s*:', r'address\s*:', r'landmark',
    r's\.v\.\s*road', r'requesting\s*branch', r'from\s*:\s*\d{2}/\d{2}',
    r'^\d{2}/\d{2}/\d{4}$',
    r'^\d[\d,]+\.\d{2}\s+\d+\s+\d+\s+\d[\d,]+\.\d{2}',
    r'^MAHARASHTRA$', r'^MUMBAI$', r'^INDIA$',
]), re.IGNORECASE)

_PB_FOOTER_RE = re.compile(
    r'hdfcbank|closingbal|senapati|registeredo|stateaccount'
    r'|gstinnumber|contentsof|thisstatement|taxpayment'
    r'|understand\s*your|\.com/personal'
    r'|computergenerated|doesnotrequire|signature'
    r'|^MR\.|^MRS\.|^MS\.'
    r'|^State\s*:',
    re.IGNORECASE,
)


def _parse_pdf_pdfplumber(pdf_path):
    import pdfplumber

    pdf = pdfplumber.open(pdf_path)
    all_lines = []
    for page in pdf.pages:
        text = page.extract_text(x_tolerance=1) or ""
        for line in text.split("\n"):
            stripped = line.strip()
            if stripped:
                all_lines.append(stripped)
    pdf.close()

    if not all_lines:
        return [], {}

    full_text = "\n".join(all_lines)
    summary = _extract_statement_summary(full_text)

    transactions = []
    i = 0
    while i < len(all_lines):
        line = all_lines[i]
        if _PB_SKIP_RE.search(line) or _PB_FOOTER_RE.search(line):
            i += 1
            continue
        m_date = _PB_DATE_RE.match(line)
        if m_date:
            date_str = m_date.group(1)
            rest = m_date.group(2)
            m_tail = _PB_TAIL_RE.search(rest)
            if m_tail:
                ref = m_tail.group(1)
                ref_pos = rest.rfind(ref)
                desc = rest[:ref_pos].strip() if ref_pos > 0 else ""
                cont = []
                j = i + 1
                while j < len(all_lines):
                    nl = all_lines[j]
                    if _PB_DATE_RE.match(nl):
                        break
                    if _PB_SKIP_RE.search(nl) or _PB_FOOTER_RE.search(nl):
                        j += 1
                        continue
                    if len(nl) < 5 and nl.isdigit():
                        j += 1
                        continue
                    cont.append(nl)
                    j += 1
                full_desc = (desc + " " + " ".join(cont)).strip() if cont else desc
                transactions.append({
                    "Date": _normalise_date(date_str),
                    "Transaction ID": ref,
                    "Description": full_desc,
                    "Account": "",
                    "Deposit": _clean_amount(m_tail.group(4)),
                    "Withdrawal": _clean_amount(m_tail.group(3)),
                    "Balance": m_tail.group(5).replace(",", ""),
                    "Currency": "INR",
                })
                i = j
                continue
        i += 1
    return transactions, summary


_DATE_RE = re.compile(r'^(\d{2}/\d{2}/\d{2})\s*[.\s]*\|')
_TAIL_RE = re.compile(
    r'(\S{10,25})\s*\|\s*'
    r'(\d{2}/\d{2}/\d{2})\s+'
    r'([\d,]+\.?\d*)\s+'
    r'([\d,]+\.?\d*)\s+'
    r'([\d,]+\.?\d*)\s*$'
)


def _ocr_pdf(pdf_path):
    import pypdfium2 as pdfium
    import pytesseract
    pdf = pdfium.PdfDocument(pdf_path)
    pages_text = []
    for i in range(len(pdf)):
        page = pdf[i]
        bitmap = page.render(scale=2.5)
        pil_img = bitmap.to_pil()
        text = pytesseract.image_to_string(pil_img, config="--psm 6")
        pages_text.append(text)
    return "\n".join(pages_text)


def _parse_pdf_transactions(ocr_text):
    lines = ocr_text.splitlines()
    start_idx = 0
    for i, line in enumerate(lines):
        if "statement of account" in line.lower():
            start_idx = i + 1
            break
    transactions = []
    current = None
    current_text = ""
    for line in lines[start_idx:]:
        line = line.strip()
        if not line:
            continue
        low = line.lower()
        if any(k in low for k in [
            "closing balance", "opening balance", "total deposits",
            "total withdrawals", "page no", "statement of account",
            "generated on", "generated by", "statement summary",
        ]):
            continue
        m_date = _DATE_RE.match(line)
        if m_date:
            if current and current_text:
                m_tail = _TAIL_RE.search(current_text)
                if m_tail:
                    transactions.append(_build_pdf_txn(current, current_text, m_tail))
            current = {"date": m_date.group(1)}
            current_text = line
        elif current is not None:
            current_text += " " + line
        if current and current_text:
            m_tail = _TAIL_RE.search(current_text)
            if m_tail:
                transactions.append(_build_pdf_txn(current, current_text, m_tail))
                current = None
                current_text = ""
    if current and current_text:
        m_tail = _TAIL_RE.search(current_text)
        if m_tail:
            transactions.append(_build_pdf_txn(current, current_text, m_tail))
    return transactions


def _clean_ocr_desc(desc):
    desc = re.sub(r'[\|]+\s*[\-\s]*$', "", desc)
    desc = re.sub(r'\s+0$', "", desc)
    desc = re.sub(r'\s+\d[\d,]*\.\d{2}\s+\d[\d,]*\.\d{2}\s+\d[\d,]*\.\d{2}', "", desc)
    desc = re.sub(r'\s*\|\s*', " ", desc)
    return re.sub(r'\s+', " ", desc).strip(" -|")


def _build_pdf_txn(current, text, m_tail):
    ref_no = m_tail.group(1)
    withdrawal = _clean_amount(m_tail.group(3))
    deposit = _clean_amount(m_tail.group(4))
    balance = m_tail.group(5).replace(",", "")
    pipe_idx = text.find("|")
    if pipe_idx >= 0:
        pipe_idx += 1
        ref_idx = text.rfind(ref_no)
        raw_desc = text[pipe_idx:ref_idx].strip() if ref_idx > pipe_idx else ""
    else:
        raw_desc = ""
    desc = re.sub(r'\s+', " ", raw_desc).strip(" -")
    desc = _clean_ocr_desc(desc)
    return {
        "Date": _normalise_date(current["date"]),
        "Transaction ID": ref_no,
        "Description": desc,
        "Account": "",
        "Deposit": deposit,
        "Withdrawal": withdrawal,
        "Balance": balance,
        "Currency": "INR",
    }


_HDFC_XLS_COLS = {
    "date":       ["date"],
    "value_date": ["value dt", "value date", "val date", "val dt"],
    "narration":  ["narration", "description", "particulars"],
    "nature":     ["nature of exp", "nature", "remarks"],
    "ref":        ["chq./ref.no.", "chq/ref no", "ref no", "cheque no", "reference no"],
    "withdrawal": ["withdrawal amt.", "withdrawal", "debit", "debit amt."],
    "deposit":    ["deposit amt.", "deposit", "credit", "credit amt."],
    "balance":    ["closing balance", "balance"],
}


def _read_xls_rows(file_path):
    p = Path(file_path)
    suffix = p.suffix.lower()
    if suffix == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(str(p), data_only=True)
        ws = wb.active
        rows = []
        for row in ws.iter_rows(values_only=True):
            rows.append([str(c).strip() if c is not None else "" for c in row])
        return rows
    else:
        try:
            import xlrd
        except ImportError:
            raise ImportError("xlrd is required to read .xls files.")
        wb = xlrd.open_workbook(str(p))
        ws = wb.sheet_by_index(0)
        rows = []
        for r in range(ws.nrows):
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
            rows.append(row)
        return rows


def _find_header_row(rows):
    for i, row in enumerate(rows):
        if len(row) >= 2:
            c0 = str(row[0]).strip().lower()
            c1 = str(row[1]).strip().lower()
            if c0 == "date" and any(k in c1 for k in ["narration", "description", "particulars"]):
                return i
    return -1


def _map_columns(header_row):
    headers = [str(h).strip().lower() for h in header_row]
    result = {}
    for field, candidates in _HDFC_XLS_COLS.items():
        idx = None
        for h_idx, h in enumerate(headers):
            if any(c in h for c in candidates):
                idx = h_idx
                break
        result[field] = idx
    return result


def _parse_xls_transactions(rows):
    header_idx = _find_header_row(rows)
    if header_idx == -1:
        raise ValueError("Could not find HDFC header row (expected Date + Narration)")
    col = _map_columns(rows[header_idx])
    for req in ("date", "narration", "ref", "withdrawal", "deposit", "balance"):
        if col.get(req) is None:
            raise ValueError("Required column not found: " + req)
    date_col = col["value_date"] if col.get("value_date") is not None else col["date"]
    transactions = []
    for row in rows[header_idx + 1:]:
        if not row or not str(row[0]).strip():
            continue
        date_raw = str(row[date_col]).strip()
        if not re.match(r'^\d{2}/\d{2}/\d{2,4}$', date_raw):
            continue
        narration = str(row[col["narration"]]).strip() if col["narration"] is not None else ""
        nature = str(row[col["nature"]]).strip() if col.get("nature") is not None else ""
        ref = str(row[col["ref"]]).strip() if col["ref"] is not None else ""
        wdl = _clean_amount(row[col["withdrawal"]]) if col["withdrawal"] is not None else ""
        dep = _clean_amount(row[col["deposit"]]) if col["deposit"] is not None else ""
        bal = _clean_amount(row[col["balance"]]) if col["balance"] is not None else ""
        if nature and nature.lower() not in ("", "none", "nan"):
            desc = narration + " -- " + nature
        else:
            desc = narration
        ref_clean = ref.lstrip("0") or ref
        transactions.append({
            "Date": _normalise_date(date_raw),
            "Transaction ID": ref_clean,
            "Description": desc,
            "Account": "",
            "Deposit": dep,
            "Withdrawal": wdl,
            "Balance": bal,
            "Currency": "INR",
        })
    return transactions


def run(pdf_path, output_path, config_path=None, model_override=None):
    """HDFC statement (PDF or XLS/XLSX) -> canonical CSV."""
    input_path = str(pdf_path)
    if not Path(input_path).is_file():
        return "File not found: " + input_path

    suffix = Path(input_path).suffix.lower()
    summary = {}

    try:
        if suffix == ".pdf":
            transactions, summary = _parse_pdf_pdfplumber(input_path)
            fmt = "PDF (pdfplumber)"
            if not transactions:
                log.info("pdfplumber found 0 transactions, falling back to OCR")
                ocr_text = _ocr_pdf(input_path)
                transactions = _parse_pdf_transactions(ocr_text)
                if not summary:
                    summary = _extract_statement_summary(ocr_text)
                fmt = "PDF (OCR)"
        elif suffix in (".xls", ".xlsx"):
            rows = _read_xls_rows(input_path)
            transactions = _parse_xls_transactions(rows)
            fmt = suffix.upper()
        else:
            return "Unsupported file type: " + suffix
    except Exception as e:
        return "Error processing " + Path(input_path).name + ": " + str(e)

    if not transactions:
        return "No transactions found in " + fmt + " file."

    running = verify_running_balance(transactions)

    if summary and "closing" in summary:
        expected_closing = summary["closing"]
    else:
        expected_closing = running["closing_balance"]

    closing = verify_closing_balance(transactions, expected_closing=expected_closing)

    count_warnings = []
    if summary:
        expected_total = summary.get("dr_count", 0) + summary.get("cr_count", 0)
        actual_total = len(transactions)
        if expected_total > 0 and actual_total != expected_total:
            msg = "TRANSACTION COUNT MISMATCH: extracted %d, statement says %d (Dr=%d + Cr=%d)" % (
                actual_total, expected_total, summary["dr_count"], summary["cr_count"])
            count_warnings.append(msg)
            log.warning(msg)

        actual_dr = sum(1 for t in transactions
                        if float(t.get("Withdrawal", "0").replace(",", "") or "0") > 0)
        actual_cr = sum(1 for t in transactions
                        if float(t.get("Deposit", "0").replace(",", "") or "0") > 0)
        if actual_dr != summary.get("dr_count", actual_dr):
            count_warnings.append("  Debit count: extracted %d, expected %d" % (
                actual_dr, summary["dr_count"]))
        if actual_cr != summary.get("cr_count", actual_cr):
            count_warnings.append("  Credit count: extracted %d, expected %d" % (
                actual_cr, summary["cr_count"]))

    Path(output_path).parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=CANONICAL_COLS)
        writer.writeheader()
        writer.writerows(transactions)

    # Write sidecar summary JSON for pipeline's independent balance verification
    sidecar_path = Path(output_path).with_suffix(".csv_summary.json")
    sidecar_data = {
        "bank": "HDFC",
        "source": "statement_summary" if summary else "derived",
        "opening_balance": summary.get("opening", running["opening_balance"]),
        "closing_balance": summary.get("closing", running["closing_balance"]),
        "dr_count": summary.get("dr_count", 0),
        "cr_count": summary.get("cr_count", 0),
        "row_count": len(transactions),
    }
    try:
        with open(sidecar_path, "w", encoding="utf-8") as sf:
            json.dump(sidecar_data, sf, indent=2)
        log.info("Wrote sidecar summary: %s", sidecar_path)
    except Exception as e:
        log.warning("Could not write sidecar summary: %s", e)

    balance_info = format_balance_summary(running, closing)
    result = "HDFC (%s): extracted %d transactions from %s -> canonical CSV\n%s" % (
        fmt, len(transactions), Path(input_path).name, balance_info)
    if count_warnings:
        result += "\n" + "\n".join(count_warnings)
    return result
