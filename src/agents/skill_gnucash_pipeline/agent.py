#!/usr/bin/env python3
"""
GnuCash Import Pipeline
End-to-end: raw bank statement → GnuCash-ready mapped CSV.

Chain:
  1. Bank parse → canonical. Every dedicated bank (DEDICATED_BANKS, derived
                  from agents.banks.discover() — currently ICICI / Bank of
                  Baroda / HSBC / HDFC / Kotak) is dispatched purely through
                  the agents.banks registry: BankSkill.parse() returns
                  canonical rows in memory, and this module writes the
                  canonical CSV + sidecar once via the shared canonical_io
                  tail — no bank writes its own CSV. "Other Bank (CSV)" is
                  the one path that still uses LLM-assisted column
                  normalisation.
  2. Account mapping   (skill_gnucash_account_mapper)

Public surface:
    run() — PA Skills UI entry point.
"""

import csv
import gzip
import json
import logging
import os
import re
import sys
import tempfile
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from agents.balance_utils import (
    verify_running_balance,
    extract_opening_closing,
    format_balance_summary,
    _safe_float,
)
from agents.banks import discover as discover_banks, load_bank_skill
from agents.skill_gnucash_xml_extractor.agent import _is_structural_bank_account
from agents.canonical_io import (
    read_sidecar as _ci_read_sidecar,
    row_deposit,
    row_withdrawal,
    split_balance_carriers,
    write_canonical_csv,
    write_sidecar,
)
from agents import bank_balance_gate as _bg
from agents.skill_gnucash_reconciler.agent import (
    parse_gnucash_for_reconcile,
    reconcile,
    detect_contra_entries,
    match_booked_own_transfers,
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


def _resolve_hsbc_input(path_or_dir: str, hsbc_skill) -> tuple[str | None, str | None]:
    """Resolve a staged HSBC upload for ``HSBCSkill.parse()``.

    HSBC is the one bank whose ``parse()`` accepts either a directory of PDF
    statements (OCR path) or a single already-enriched ``.xlsx``/``.xlsm``
    workbook (fast path — see MAP-08 sibling task HSB-03). A single-file
    upload must be passed through as-is (never swapped for its parent
    directory, which would sweep every sibling PDF into OCR); a directory
    upload must be inspected to tell which shape it holds.

    Returns ``(resolved_path, None)`` on success, or ``(None, error_markdown)``
    when the upload can't be resolved unambiguously — in the same
    ``## HSBC → ...`` style as the Bank of Baroda branch just below.
    """
    src = Path(path_or_dir)

    if not src.is_dir():
        # Single file (PDF or already-enriched workbook): pass it through
        # unchanged. Never substitute its parent directory.
        return str(src), None

    entries = sorted(src.iterdir())
    pdfs = [p for p in entries if p.is_file() and p.suffix.lower() == ".pdf"]
    workbooks = [p for p in entries if p.is_file() and p.suffix.lower() in (".xlsx", ".xlsm")]

    if pdfs and workbooks:
        return None, (
            "## HSBC → mixed upload\n\n"
            f"❌ The staged upload directory contains both PDF statement(s) "
            f"and enriched workbook(s):\n`{path_or_dir}`\n\n"
            "Upload either PDF statements (for OCR) or a single "
            "already-enriched .xlsx/.xlsm workbook, not both."
        )
    if workbooks:
        if len(workbooks) > 1:
            return None, (
                "## HSBC → multiple workbooks\n\n"
                f"❌ The staged upload directory contains more than one "
                f"enriched workbook:\n`{path_or_dir}`\n\n"
                "Upload just one already-enriched HSBC .xlsx/.xlsm workbook."
            )
        wb = workbooks[0]
        try:
            confidence = hsbc_skill.detect(wb)
        except Exception:
            confidence = 0.0
        if confidence <= 0:
            return None, (
                "## HSBC → unrecognised workbook\n\n"
                f"❌ `{wb.name}` doesn't look like an already-enriched HSBC "
                "workbook. Expected first-sheet headers: `Date, Transaction "
                "Details, Transaction Date, Transaction Number, Extra "
                "Information, Deposit, Withdrawals, Balance`."
            )
        return str(wb), None
    if pdfs:
        return str(src), None
    return None, (
        "## HSBC → no statements found\n\n"
        f"❌ The staged upload directory contains no .pdf or .xlsx/.xlsm "
        f"files:\n`{path_or_dir}`"
    )


def _read_sidecar(canonical_path: str) -> dict | None:
    """Read the _summary.json sidecar if it exists (shared canonical_io tail)."""
    return _ci_read_sidecar(canonical_path)


def _apply_confirmed_contras(output_path: str, contra_flags: dict) -> int:
    """Re-map the Account of confirmed contras to their counterparty bank.

    A confirmed contra (status == "confirmed", i.e. a reference-matched
    cross-bank transfer) must post against the other bank rather than the
    category the mapper guessed. Possible contras are left untouched. Mutates
    ``contra_flags`` in place to record the mapper's original account
    (``mapped_account``) and what was applied (``applied_account``), and
    rewrites ``output_path`` only when at least one row changed.

    contra_flags keys are 0-based row indices into the output CSV (the mapper
    preserves canonical order 1:1). Returns the number of rows re-mapped.
    """
    confirmed = {
        int(idx): c for idx, c in contra_flags.items()
        if isinstance(c, dict) and c.get("status") == "confirmed"
    }
    if not confirmed:
        return 0

    with open(output_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    remapped = 0
    for idx, c in confirmed.items():
        if not (0 <= idx < len(rows)):
            continue
        bank_acct = c.get("contra_account", "") or ""
        if bank_acct.startswith("Root Account:"):
            bank_acct = bank_acct[len("Root Account:"):]
        if not bank_acct:
            continue
        c["mapped_account"] = rows[idx].get("Account", "")
        c["applied_account"] = bank_acct
        rows[idx]["Account"] = bank_acct
        remapped += 1

    if remapped:
        with open(output_path, "w", encoding="utf-8", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    return remapped


BOOKED_SIDECAR_SUFFIX = ".matched.json"


def booked_sidecar_path(output_path: str) -> Path:
    """IMP-11: <stem>.matched.json, next to the mapped CSV."""
    return Path(output_path).with_suffix(BOOKED_SIDECAR_SUFFIX)


def explain_opening_gap(unresolved_gap: float | None, matches: list[dict]) -> list[dict]:
    """IMP-11: the matched rows that pair with book splits dated BEFORE the
    statement, when their net equals the opening gap (to 0.02). Returns [] when
    they do not explain it -- never a partial guess."""
    if unresolved_gap is None or unresolved_gap <= 0.02:
        return []
    pre = [m for m in matches if m.get("pre_period")]
    if not pre:
        return []
    net = sum(float(m["amount"]) for m in pre)
    return pre if abs(abs(net) - unresolved_gap) <= 0.02 else []


def _set_aside_booked_transfers(output_path: str, contra_flags: dict,
                                matches: list[dict]) -> int:
    """IMP-11: take rows already booked from the other bank's statement OUT of
    the importable CSV and park them (with the reason) in <stem>.matched.json.

    Nothing is deleted: the Review tab shows them excluded ("not imported") and
    the user can re-tick one to import it. They are removed from the CSV so
    that, even if Review is never opened, they cannot be imported by accident.
    ``contra_flags`` keys are 0-based row indices into the output CSV; they are
    shifted past the removed rows (a flag on a removed row travels with it).
    The sidecar is always (re)written, so a stale one never survives a re-run.
    """
    import json as _json
    with open(output_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    by_idx = {int(m["row_idx"]): m for m in matches if 0 <= int(m["row_idx"]) < len(rows)}
    parked, kept = [], []
    for i, row in enumerate(rows):
        m = by_idx.get(i)
        if m is None:
            kept.append(row)
            continue
        entry = {k: m[k] for k in ("reason", "book_date", "other_account",
                                   "days_off", "pre_period", "tie") if k in m}
        entry["row"] = row
        if i in contra_flags or str(i) in contra_flags:
            entry["contra"] = contra_flags.get(i, contra_flags.get(str(i)))
        parked.append(entry)
    with open(booked_sidecar_path(output_path), "w", encoding="utf-8") as sf:
        _json.dump(parked, sf, indent=2, default=str)
    if not parked:
        return 0
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)
    gone = {i for i in by_idx}
    remapped = {}
    for key, val in list(contra_flags.items()):
        idx = int(key)
        if idx in gone:
            continue
        remapped[idx - sum(1 for d in gone if d < idx)] = val
    contra_flags.clear()
    contra_flags.update(remapped)
    return len(parked)


def _drop_balance_carriers(output_path: str, contra_flags: dict) -> int:
    """HSB-04: remove balance-carrier rows from the mapped CSV.

    Runs AFTER the running-balance check, the opening-balance reconciliation,
    duplicate detection and contra detection have all used the canonical rows.
    ``contra_flags`` keys are 0-based row indices into the output CSV, so they
    are shifted down past every dropped row (and a flag on a dropped row is
    removed) to keep pointing at the same transactions. Returns rows dropped.
    """
    with open(output_path, "r", encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)
    kept, dropped = split_balance_carriers(rows)
    if not dropped:
        return 0
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(kept)
    gone = set(dropped)
    remapped = {}
    for key, val in list(contra_flags.items()):
        idx = int(key)
        if idx in gone:
            continue
        remapped[idx - sum(1 for d in dropped if d < idx)] = val
    contra_flags.clear()
    contra_flags.update(remapped)
    return len(dropped)


def _run_closing_gate(*, output_path: str, stmt_rows: list[dict], scoped_data,
                      filter_path, bank_account: str) -> tuple[list[str], bool]:
    """BNK-09: closing-balance gate + the safe-orientation (bank-as-base) file.

    Returns (log_lines, blocked). Reads the book only through ``scoped_data``
    (already parsed read-only); writes <stem>.gate.json and the bank-base file.
    On a failed gate the bank-base file is written header-only so a stale one
    from an earlier run cannot be imported.
    """
    lines: list[str] = []
    if scoped_data is None or not filter_path:
        lines.append(
            "⚠️ Closing-balance gate skipped: this bank's account in the book was not "
            "resolved, so the book balance cannot be projected. The bank-base import "
            "file is not produced (no bank account to use as the base).")
        return lines, False
    points = _bg.statement_points(stmt_rows)
    splits = _bg.own_splits(scoped_data, filter_path)
    twins = _bg.assign_twins(points, splits)
    with open(output_path, "r", encoding="utf-8", newline="") as f:
        final_rows = list(csv.DictReader(f))
    flags, extras = _bg.mark_imported(points, twins, final_rows)
    book_open = _bg.book_balance_before(splits, min(p["date"] for p in points)) if points else 0.0

    intended: dict[str, str] = {}
    reasons: dict[str, str] = {}
    sp = booked_sidecar_path(output_path)
    if sp.is_file():
        try:
            for e in json.loads(sp.read_text(encoding="utf-8")):
                row = e.get("row") or {}
                iso = _bg._iso(row.get("Date"))
                if iso:
                    k = _bg.point_key(iso, _bg.row_amount(row))
                    intended[k] = str(row.get("Account") or "")
                    reasons[k] = "set aside as already booked: " + str(e.get("reason") or "")
        except (OSError, ValueError):
            pass
    for p in points:
        reasons.setdefault(
            _bg.point_key(p["date"], p["amount"]),
            "skipped as already booked (same date and amount, or the date-based filter)")
    res = _bg.evaluate_gate(points, twins, flags, extras, book_open,
                            skip_reasons=reasons, intended=intended)
    _bg.write_gate_sidecar(
        _bg.gate_sidecar_path(output_path), root=os.path.dirname(os.path.abspath(output_path)), bank_account=bank_account,
        book_filter_path=filter_path, points=points, twins=twins,
        book_opening=book_open, intended=intended, skip_reasons=reasons, result=res)

    blocked = res["status"] == "fail"
    op = res.get("opening") or {}
    if op and not op.get("ok"):
        lines.append(
            f"⚠️ Opening gap detected (pre-existing, reported separately): the book "
            f"opens at {op['book']:.2f} but the statement at {op['statement']:.2f} "
            f"(difference {op['gap']:.2f}). The gate below checks the movements.")
    if blocked:
        for ln in _bg.format_gate_failure(res):
            lines.append("❌ " + ln.strip())
        lines.append("❌ The bank-base import file was NOT released (written empty).")
    elif res["status"] == "pass":
        lines.append(f"Closing-balance gate -- PASS: {res['message']}")
    else:
        lines.append(f"⚠️ Closing-balance gate -- unverified: {res['message']}")
    if res["timing"]:
        lines.append(
            f"Timing differences (information): {len(res['timing'])} row(s) are booked "
            f"in this bank's account on a date 1-2 days from the statement's; they net "
            f"to zero by the end.")
    for r in res["double_booking_risk"]:
        lines.append(
            f"❌ RED FLAG possible double booking: {r['date']} | {r['description'][:60]} | "
            f"{r['amount']:.2f} -- {r['reason']} (twin dated {r['twin_date']}).")

    dst = _bg.bank_base_name(output_path)
    n, problems = _bg.write_bank_base_csv(final_rows, dst, root=os.path.dirname(os.path.abspath(output_path)), blocked=blocked)
    if blocked:
        pass
    elif problems:
        lines.append(f"❌ Bank-base import file not produced: {problems[0]}"
                     + (f" (+{len(problems) - 1} more)" if len(problems) > 1 else ""))
        blocked = True
    else:
        lines.append(f"Import this file into GnuCash: `{dst.name}` ({n} rows; bank as the "
                     f"base Account, so GnuCash's duplicate check compares against THIS bank).")
    return lines, blocked


def _contra_log_line(contra_flags: dict) -> str:
    """PIPE-09: the run-log line for the contra check, computed from the SAME
    dict that is written to <stem>.contra.json, so the two can never disagree."""
    n = len(contra_flags)
    if not n:
        return "Contra check -- no cross-bank transfers in the final output"
    confirmed = sum(1 for c in contra_flags.values()
                    if (c or {}).get("confidence") == "high")
    possible = n - confirmed
    parts = []
    if confirmed:
        parts.append(f"{confirmed} confirmed (account set to bank)")
    if possible:
        parts.append(f"{possible} possible transfer")
    return (f"Contra check -- {n} cross-bank transfer(s) flagged in contra.json "
            f"({', '.join(parts)}). Review in the **Banks > Review** tab.")


def _write_contra_sidecar(output_path: str, contra_flags: dict) -> None:
    """Write <stem>.contra.json from ``contra_flags``. An empty dict overwrites
    a stale sidecar from an earlier run (only if one exists)."""
    import json as _json
    sidecar = Path(output_path).with_suffix('.contra.json')
    if not contra_flags and not sidecar.exists():
        return
    with open(sidecar, 'w', encoding='utf-8') as cf:
        _json.dump(contra_flags, cf, indent=2, default=str)


# IMP-14: words in an entity's name that are not part of a person's name.
_NAME_STOPWORDS = {"huf", "hindu", "undivided", "family", "and", "of", "the",
                   "mr", "mrs", "ms", "shri", "smt", "dr", "late", "sons", "co"}
ADVISORY_SUFFIX = ".advisory.json"
OWN_TRANSFER_ADVISORY_KIND = "own_transfer_other_side_missing"


def _holder_name_tokens(name: str) -> list[str]:
    """IMP-14: the entity's name as lowercase alphabetic words (initials and
    honorifics dropped). Comes from entity config, never hardcoded."""
    words = re.findall(r"[a-z]+", (name or "").lower())
    return [w for w in words if len(w) >= 2 and w not in _NAME_STOPWORDS]


def _narration_has_holder_name(desc: str, tokens: list[str]) -> bool:
    """True when the narration carries the holder's FIRST and LAST name as
    whole words. A single-word name is too weak to call (returns False): a
    relative sharing only the surname, or only the given name, never matches."""
    if len(tokens) < 2:
        return False
    words = set(re.findall(r"[a-z]+", (desc or "").lower()))
    return tokens[0] in words and tokens[-1] in words


def _own_name_out_advisories(output_path: str, contra_flags: dict,
                             holder_name: str) -> dict:
    """IMP-14: ADVISORY only. Money-OUT rows whose narration carries the
    holder's own name and whose other side was not found in the book (no
    contra flag). Returns {row_index: {kind, reason}}; NEVER touches the CSV,
    never re-maps a row, never looks at money-IN rows."""
    tokens = _holder_name_tokens(holder_name)
    if len(tokens) < 2:
        return {}
    from agents.amount_headers import find_withdrawal_key, find_deposit_key  # noqa: PLC0415
    with open(output_path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        return {}

    def _amt(row, key):
        if not key:
            return 0.0
        try:
            return float(str(row.get(key) or "0").replace(",", "") or 0)
        except ValueError:
            return 0.0

    wkey = find_withdrawal_key(list(rows[0].keys()))
    dkey = find_deposit_key(list(rows[0].keys()))
    flagged = {str(k) for k in (contra_flags or {})}
    out = {}
    for i, row in enumerate(rows):
        if str(i) in flagged:
            continue          # other side found: today's behaviour
        if abs(_amt(row, wkey)) == 0 or abs(_amt(row, dkey)) > 0:  # deposit may be negated
            continue          # money OUT only
        if not _narration_has_holder_name(row.get("Description", ""), tokens):
            continue
        out[i] = {
            "kind": OWN_TRANSFER_ADVISORY_KIND,
            "reason": "Possible own transfer, other side not found in the book",
        }
    return out


def _write_advisory_sidecar(output_path: str, advisories: dict) -> None:
    """Write <stem>.advisory.json. An empty dict overwrites a stale sidecar
    from an earlier run (only if one exists)."""
    sidecar = Path(output_path).with_suffix(ADVISORY_SUFFIX)
    if not advisories and not sidecar.exists():
        return
    with open(sidecar, "w", encoding="utf-8") as af:
        json.dump(advisories, af, indent=2)


def _step3_result_line(output_path: str) -> str:
    """PIPE-09: the Step 3 result, counted from the mapped CSV itself."""
    with open(output_path, "r", encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    suspense = sum(1 for r in rows if (r.get("Confidence") or "").lower() == "suspense")
    unmapped = sum(1 for r in rows if not (r.get("Account") or "").strip())
    return (f"Step 3 result -- {len(rows)} statement row(s) mapped "
            f"({suspense} to Suspense, {unmapped} with no account)")


# Banks with dedicated extraction skills — registry-driven (agents.banks
# discover()) rather than a hardcoded literal, so onboarding a new bank
# (adding its skill.yaml `bank: true` manifest) automatically extends both
# this gating list and the pipeline's `bank` dropdown (ui/tabs/_generic.py's
# "banks" options_from source) without any edit here. See
# 2026-07-18-bank-registry-gating-followup-prompt.md — this list previously
# diverged from the dropdown's options: list (Kotak was added to the
# dropdown in #89 but not here), causing an offer-then-reject bug at the
# SUPPORTED_BANKS guard below.
DEDICATED_BANKS = [b.display_name for b in discover_banks()]
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

def _normalize_digits(s: str | None) -> str:
    """Strip everything but digits, e.g. for comparing account numbers that
    may carry spaces/dashes/masking in either the statement or GnuCash."""
    return "".join(c for c in (s or "") if c.isdigit())


_ROOT_PREFIX = "Root Account:"


def _strip_root(path: str) -> str:
    return path[len(_ROOT_PREFIX):] if path.startswith(_ROOT_PREFIX) else path


def _iso_date(text: str) -> str:
    """DD/MM/YYYY, DD/MM/YY or ISO -> ISO (best effort, '' when unreadable)."""
    t = (text or "").strip()
    try:
        if "/" in t:
            d, m, y = t.split("/")[:3]
            y = y if len(y) == 4 else f"20{y}"
            return f"{y}-{m.zfill(2)}-{d.zfill(2)}"
        return t[:10]
    except ValueError:
        return ""


def _statement_evidence(canonical_rows: list[dict]) -> dict:
    """What the statement itself can say about WHICH own account it belongs to:
    the opening balance, the first transaction date and every narration."""
    if not canonical_rows:
        return {}
    try:
        oc = extract_opening_closing(canonical_rows)
        opening = oc["opening_balance"]
    except Exception:  # noqa: BLE001
        opening = None
    dates = sorted(d for d in (_iso_date(r.get("Date", "")) for r in canonical_rows) if d)
    return {
        "opening_balance": opening,
        "start_date": dates[0] if dates else None,
        "narrations": [str(r.get("Description", "")) for r in canonical_rows],
    }


def _looks_like_year(run: str) -> bool:
    return len(run) == 4 and run[:2] in ("19", "20")


def _narration_pick(pool: list[tuple[str, str]], narrations: list[str]) -> str | None:
    """The id of the ONE pool account whose own account digits appear (as a
    digit run of 4+ digits) in the statement narrations, or None when none or
    more than one does. A run that fits two accounts votes for neither."""
    own: dict[str, list[str]] = {}   # aid -> digit runs in its own name (leaf)
    for aid, full in pool:
        leaf = full.split(":")[-1]
        own[aid] = re.findall(r"\d{4,}", leaf)
    digits = {aid: _normalize_digits(full.split(":")[-1]) for aid, full in pool}
    # a run that appears in more than one candidate's name says nothing
    counts: dict[str, int] = {}
    for runs in own.values():
        for r in set(runs):
            counts[r] = counts.get(r, 0) + 1
    votes: set[str] = set()
    for text in narrations:
        for run in set(re.findall(r"\d{4,}", text or "")):
            if _looks_like_year(run):
                continue
            hit = set()
            for aid, _full in pool:
                if any(r == run and counts.get(r, 0) == 1 for r in own[aid]):
                    hit.add(aid)
                elif digits[aid].endswith(run) and len(run) >= 4:
                    hit.add(aid)
            if len(hit) == 1:
                votes |= hit
    return next(iter(votes)) if len(votes) == 1 else None


def _balance_before(entries, start_date: str | None) -> float:
    """IMP-12: the book balance of ``entries`` ((iso_date, value) splits) dated
    STRICTLY BEFORE ``start_date`` (the statement's first date). With no
    start_date there is no "before": the all-dates balance. The ONE place this
    rule lives -- the evidence picker, the opening-balance comparison and the
    opening-gap explanation all use it."""
    return sum(v for d, v in entries if not start_date or (d and d < start_date))


def _get_gnucash_account_balance(
    gnucash_file: str, bank_name: str, account_number: str | None = None,
    *, opening_balance: float | None = None, start_date: str | None = None,
    narrations: list[str] | None = None, chosen_account: str | None = None,
) -> dict:
    """
    Parse a .gnucash XML file and find the bank account's ledger balance.

    Candidate accounts are those whose name contains bank_name
    (case-insensitive) under Assets. When ``account_number`` is supplied
    (from statement metadata), it is normalised to digits and matched
    against digits embedded in each candidate's account name (e.g.
    "BOB - 100000000001") — this disambiguates multiple accounts at the
    same bank and is preferred over a bare name match.

    If no account number is given, or there is at most one same-bank-name
    candidate, falls back to a name-only match and reports that in
    "match_warning" so callers can surface it. But if an account number
    IS given and matches none of *two or more* same-bank-name candidates,
    a name-only match would be a coin-flip over which account is right —
    so this refuses to guess: it returns ``found: False`` with
    "match_warning" explaining why, rather than silently picking one.

    IMP-08: several own accounts at one bank are NEVER resolved by picking the
    first. Hidden/placeholder accounts are not candidates (IMP-09). With no
    account number the choice is made by evidence -- the statement's opening
    balance equals exactly one candidate's book balance at the statement
    start, or the candidate's own account digits appear in the narrations for
    exactly one candidate (``opening_balance`` / ``start_date`` /
    ``narrations``). If that still leaves a tie the result is
    ``found: False, ambiguous: True`` with the postable ``candidates``; the
    caller must stop and ask. ``chosen_account`` (a full path) is the user's
    explicit pick: it is refused when it is hidden/placeholder, not at this
    bank, or not in the book.

    Returns:
        {
            "found": bool,
            "account_name": str,
            "balance": float,              # ALL dates (the closing check's base)
            "balance_before_start": float, # splits dated before start_date (IMP-12)
            "last_txn_date": str or None,  # YYYY-MM-DD
            "match_warning": str or None,
            "match_note": str or None,     # how an evidence pick was made
            "ambiguous": bool,             # stop and ask
            "refused": bool,               # chosen_account was not acceptable
            "candidates": list[str],       # postable same-bank paths
        }
    """
    try:
        with gzip.open(gnucash_file, 'rt', encoding='utf-8') as f:
            tree = ET.parse(f)
    except Exception:
        return {
            "found": False, "account_name": "", "balance": 0.0,
            "last_txn_date": None, "match_warning": None,
        }

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

    # Candidate accounts: name contains the bank. IMP-10: BANK-type accounts
    # only (the same structural rule the mapper uses for own_bank_accounts);
    # a fixed deposit or mutual fund named after the bank is typically ASSET
    # and must never be offered or accepted as the statement's bank account.
    # ASSET-type accounts are used ONLY when the bank has no BANK-type match,
    # and then a visible warning is attached to the result.
    bank_lower = bank_name.lower()
    _named = [
        (aid, _full_path(aid), info["type"])
        for aid, info in acc_map.items()
        if bank_lower in _full_path(aid).lower() and info["type"] in ("BANK", "ASSET")
    ]
    candidates = [(a, f) for a, f, t in _named if _is_structural_bank_account(t)]
    non_bank_at_bank = {_strip_root(f) for a, f, t in _named if not _is_structural_bank_account(t)}
    fallback_warning = None
    if not candidates and _named:
        candidates = [(a, f) for a, f, t in _named]
        non_bank_at_bank = set()
        fallback_warning = (
            f"No BANK-type account found at '{bank_name}'; using ASSET-type account(s) "
            f"instead: {', '.join(_strip_root(f) for _a, f in candidates)}. Check the "
            f"account type in GnuCash and that this is the right account.")
    all_same_bank = list(candidates)
    # IMP-09: a hidden / placeholder account (or one under a hidden parent) is
    # never a candidate, so it can never be picked even when first in book
    # order.
    _guard = None
    try:
        from agents.gnucash_accounts import TargetGuard  # noqa: PLC0415
        _guard = TargetGuard.from_book(gnucash_file)
        if _guard.known:
            candidates = [(aid, full) for aid, full in candidates
                          if not _guard.is_blocked(full)]
        else:
            _guard = None
    except Exception:  # noqa: BLE001 - the guard must never break the pick
        _guard = None

    def _result(found, name="", balance=0.0, last=None, warn=None, note=None,
                ambiguous=False, refused=False, cands=None, opening=None):
        return {
            "found": found, "account_name": name, "balance": balance,
            # IMP-12: the book balance strictly before the statement's first date
            # (== balance when no start_date was given). The OPENING comparison
            # uses this; the closing check keeps the all-dates "balance".
            "balance_before_start": balance if opening is None else opening,
            "last_txn_date": last,
            "match_warning": (f"{warn} {fallback_warning}".strip() if warn and fallback_warning
                              else (warn or fallback_warning)),
            "match_note": note,
            "ambiguous": ambiguous, "refused": refused,
            "candidates": cands if cands is not None else [],
        }

    postable_paths = [_strip_root(full) for _aid, full in candidates]

    # One pass over the ledger: every split of every same-bank account.
    ids = {aid for aid, _ in all_same_bank}
    splits_by_acc: dict[str, list[tuple[str, float]]] = {aid: [] for aid in ids}
    for trn in root.findall(f'.//{_NS["gnc"]}transaction'):
        date_el = trn.find(f'{_NS["trn"]}date-posted/{_NS["ts"]}date')
        trn_date = date_el.text[:10] if date_el is not None else ""
        for sp in trn.findall(f'{_NS["trn"]}splits/{_NS["trn"]}split'):
            sp_acc = sp.findtext(f'{_NS["split"]}account', '')
            if sp_acc not in ids:
                continue
            val_str = sp.findtext(f'{_NS["split"]}value', '0/1')
            parts = val_str.split('/')
            v = 0.0
            if len(parts) == 2:
                try:
                    v = int(parts[0]) / int(parts[1])
                except (ValueError, ZeroDivisionError):
                    v = 0.0
            splits_by_acc[sp_acc].append((trn_date, v))

    def _ask(reason: str) -> dict:
        msg = (
            f"{reason} Which one is this statement for? Choose it in 'Bank "
            f"account' (or pass bank_account=). Postable accounts at "
            f"'{bank_name}': " + "; ".join(postable_paths) + "."
        )
        return _result(False, warn=msg, ambiguous=True, cands=postable_paths)

    # An explicit choice by the user: verified, never trusted blindly.
    if chosen_account:
        want = _strip_root(chosen_account.strip())
        in_book = {_strip_root(_full_path(a)) for a in acc_map}
        if want not in in_book:
            return _result(False, refused=True, cands=postable_paths, warn=(
                f"bank_account '{want}' is not an account in the GnuCash book."))
        at_bank = {_strip_root(full): (aid, full) for aid, full in all_same_bank}
        if want in non_bank_at_bank:
            return _result(False, refused=True, cands=postable_paths, warn=(
                f"bank_account '{want}' is not a BANK-type account (a fixed deposit, "
                f"fund or other asset); choose one of: {'; '.join(postable_paths)}."))
        if want not in at_bank:
            return _result(False, refused=True, cands=postable_paths, warn=(
                f"bank_account '{want}' is not an account at '{bank_name}'."))
        aid, full = at_bank[want]
        if _guard is not None and _guard.is_blocked(full):
            why = _guard.blocked_target_reason(full) or "target is hidden in the book"
            return _result(False, refused=True, cands=postable_paths, warn=(
                f"bank_account '{want}' was refused: {why}."))
        candidates = [(aid, full)]

    target_id = None
    target_name = ""
    match_warning = None
    match_note = None
    norm_number = _normalize_digits(account_number)

    def _decide(pool):
        """Pick ONE of ``pool`` by evidence, or None (tie / no evidence)."""
        nonlocal match_note
        bal_hits = []
        if opening_balance is not None:
            for aid, full in pool:
                bal = _balance_before(splits_by_acc.get(aid, []), start_date)
                if abs(bal - opening_balance) <= 0.02:
                    bal_hits.append((aid, full))
        n_id = _narration_pick(pool, narrations or [])
        n_hit = next(((a, f) for a, f in pool if a == n_id), None) if n_id else None
        if len(bal_hits) == 1 and (n_hit is None or n_hit == bal_hits[0]):
            match_note = (f"Resolved '{_strip_root(bal_hits[0][1])}' by evidence: the "
                          f"statement's opening balance equals this account's book "
                          f"balance at the statement start.")
            return bal_hits[0]
        if n_hit is not None and not bal_hits:
            match_note = (f"Resolved '{_strip_root(n_hit[1])}' by evidence: its own "
                          f"account number appears in the statement narrations.")
            return n_hit
        return None

    if norm_number and not chosen_account:
        number_matches = [
            (aid, full) for aid, full in candidates
            if _normalize_digits(full) and (
                norm_number in _normalize_digits(full)
                or _normalize_digits(full) in norm_number
            )
        ]
        if len(number_matches) == 1:
            target_id, target_name = number_matches[0]
        elif len(number_matches) > 1:
            picked = _decide(number_matches)
            if picked is None:
                return _ask(
                    f"The account number '{account_number}' fits {len(number_matches)} "
                    f"of your accounts at '{bank_name}' and nothing else on the "
                    f"statement tells them apart.")
            target_id, target_name = picked
        elif len(candidates) > 1:
            # An account number was supplied but matched none of several
            # same-bank-name candidates: with more than one account sharing
            # this bank name, a plain name match is a coin-flip over which
            # one is right, so refuse to guess and report why instead of
            # silently attributing the statement to the wrong ledger.
            match_warning = (
                f"Could not match account number '{account_number}' to any "
                f"of the {len(candidates)} GnuCash accounts named "
                f"'{bank_name}' ({', '.join(name for _, name in candidates)}); "
                f"not resolving to any one of them automatically."
            )
            return _result(False, warn=match_warning, cands=postable_paths)
        # else: zero or exactly one candidate -- handled below.

    if target_id is None and len(candidates) == 1:
        target_id, target_name = candidates[0]
        if norm_number and not chosen_account:
            match_warning = (
                f"Could not match account number '{account_number}' to any "
                f"GnuCash account digits; fell back to name match on "
                f"'{bank_name}' -> '{target_name}'. Verify this is the "
                f"correct account."
            )
    elif target_id is None and len(candidates) > 1:
        picked = _decide(candidates)
        if picked is None:
            return _ask(
                f"{len(candidates)} of your accounts match the bank name "
                f"'{bank_name}' and the statement carries no account number "
                f"or other evidence that picks one.")
        target_id, target_name = picked

    if not target_id:
        return _result(False, cands=postable_paths)

    # Balance of the chosen account (already indexed above).
    entries = splits_by_acc.get(target_id, [])
    balance = sum(v for _d, v in entries)
    last_date = max((d for d, _v in entries if d), default=None)

    return _result(True, target_name, round(balance, 2), last_date,
                   match_warning, match_note, cands=postable_paths,
                   opening=round(_balance_before(entries, start_date), 2))


def postable_bank_accounts(gnucash_file: str, bank_name: str) -> list[str]:
    """Full paths (no "Root Account:" prefix) of YOUR postable accounts at
    ``bank_name``: the choices offered by the import tab's Bank account
    dropdown (IMP-08). Hidden/placeholder accounts are never listed."""
    if not gnucash_file or not bank_name or not Path(str(gnucash_file)).is_file():
        return []
    try:
        res = _get_gnucash_account_balance(gnucash_file, bank_name)
    except Exception:  # noqa: BLE001 - a picker must never raise
        return []
    return list(res.get("candidates") or [])


def _reconcile_opening_balance(
    canonical_rows: list[dict],
    gnucash_file: str,
    bank_name: str,
    account_number: str | None = None,
    *, chosen_account: str | None = None,
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
            "gnucash_balance": float,      # ALL dates: the closing check's base
            "gnucash_opening_balance": float,  # strictly before the first date (IMP-12)
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
            "match_warning": None,
        }

    ev = _statement_evidence(canonical_rows)
    gc = _get_gnucash_account_balance(
        gnucash_file, bank_name, account_number,
        opening_balance=ev.get("opening_balance"), start_date=ev.get("start_date"),
        narrations=ev.get("narrations"), chosen_account=chosen_account,
    )
    if gc.get("ambiguous") or gc.get("refused"):
        # IMP-08: stop and ask -- never continue on a guessed account.
        return {
            "ok": False,
            "stop": True,
            "message": gc.get("match_warning") or "Bank account could not be chosen.",
            "rows_skipped": 0,
            "filtered_rows": canonical_rows,
            "gnucash_balance": 0.0,
            "statement_opening": 0.0,
            "account_found": False,
            "match_warning": None,
            "candidates": gc.get("candidates", []),
        }
    if not gc["found"]:
        log.warning(
            "Could not find %s account in GnuCash — skipping opening balance check",
            bank_name,
        )
        not_found_message = (
            gc.get("match_warning")
            or f"GnuCash account for '{bank_name}' not found — skipping balance reconciliation."
        )
        return {
            "ok": True,
            "message": not_found_message,
            "rows_skipped": 0,
            "filtered_rows": canonical_rows,
            "gnucash_balance": 0.0,
            "statement_opening": 0.0,
            "account_found": False,
            # Already folded into "message" above (a not-found result isn't
            # separately logged via the match_warning line at the call site),
            # so this stays None to avoid printing the same text twice.
            "match_warning": None,
        }

    gc_balance = gc["balance"]
    # IMP-12: the statement's OPENING is a balance at its first date, so it is
    # compared with the book as of just before that date -- not the all-dates
    # balance, which already includes in-period entries (e.g. other-bank
    # transfers IMP-11 sets aside) and would raise a false opening gap.
    gc_open = gc.get("balance_before_start", gc_balance)
    gc_last_date = gc["last_txn_date"]

    # Derive statement opening balance
    oc = extract_opening_closing(canonical_rows)
    stmt_opening = oc["opening_balance"]

    diff = abs(gc_open - stmt_opening)
    _mw = " ".join(x for x in (gc.get("match_warning"), gc.get("match_note")) if x) or None

    if diff <= 0.02:
        # Perfect match — no duplicates, no gap
        return {
            "ok": True,
            "message": (
                f"Opening balance matches GnuCash: "
                f"GnuCash={gc_open:.2f}, Statement={stmt_opening:.2f}"
            ),
            "rows_skipped": 0,
            "filtered_rows": canonical_rows,
            "gnucash_balance": gc_balance,
            "gnucash_opening_balance": gc_open,
            "statement_opening": stmt_opening,
            "account_found": True,
            "match_warning": _mw,
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
                    "gnucash_opening_balance": gc_balance,   # rows up to the book's last date were skipped
                    "statement_opening": new_oc["opening_balance"],
                    "account_found": True,
                    "match_warning": _mw,
                }

    # Scenario B or C — gap we can't resolve
    return {
        "ok": False,
        "message": (
            f"OPENING BALANCE MISMATCH: GnuCash ({gc['account_name']}) "
            f"shows {gc_open:.2f} before the statement's first date but statement opens at {stmt_opening:.2f} "
            f"(diff={diff:.2f}).\n"
            f"Possible causes: (B) prior statement entries were omitted, "
            f"or (C) there's a data error. Please investigate manually."
        ),
        "rows_skipped": 0,
        "filtered_rows": canonical_rows,
        "gnucash_balance": gc_balance,
        "gnucash_opening_balance": gc_open,
        "statement_opening": stmt_opening,
        "account_found": True,
        "match_warning": _mw,
    }


def final_closing_balance_verdict(
    recon: dict,
    final_rows: list[dict],
    stmt_closing: float | None,
    unresolved_opening_gap: float | None,
    explained_opening_rows: list[dict] | None = None,
) -> str:
    """
    Compute the final closing-balance verdict message.

    IMP-11: ``explained_opening_rows`` are the matched (set-aside, unticked)
    rows that pair with book splits dated before the statement. When their net
    IS the opening gap, the gap is not a mystery: the verdict names them. The
    closing figure is always computed from ``final_rows`` only, i.e. as if the
    unticked rows are not imported.

    The verdict compares the actual POST-IMPORT GnuCash balance (pre-import
    book balance + net of the rows just imported) against the STATEMENT's own
    closing balance — an independent source when present, since it comes from
    the bank, not from the book. Comparing the statement-derived canonical CSV
    against itself is circular and can't catch a real opening-balance gap. Any
    gap left unexplained by dedup (``unresolved_opening_gap``) must propagate
    here too — it must never be silently dropped just because dedup found
    zero overlapping rows, and it always wins over a coincidentally-matching
    closing balance (an AMBER/RED verdict, never a false green).
    """
    if recon.get("account_found") is False:
        return "⚠ GnuCash account not found; post-import balance could not be verified."

    if stmt_closing is None:
        return "⚠ No independent statement closing balance available; nothing to verify the book against."

    net_imported = sum(
        _safe_float(row_deposit(r) or 0) - _safe_float(row_withdrawal(r) or 0)
        for r in final_rows
    )
    post_import_balance = recon["gnucash_balance"] + net_imported
    closing_diff = abs(post_import_balance - stmt_closing)

    if (unresolved_opening_gap is not None and unresolved_opening_gap > 0.02
            and explained_opening_rows):
        names = "; ".join(
            f"{m.get('book_date', '?')} {m.get('other_account', '')}".strip()
            for m in explained_opening_rows)
        note = (f"Opening gap {unresolved_opening_gap:.2f} is caused by "
                f"{len(explained_opening_rows)} statement row(s) already booked "
                f"before the statement start ({names}); they are left unticked in Review.")
        if closing_diff <= 0.02:
            return (f"Closing balance VERIFIED (independent): post-import book="
                    f"{post_import_balance:.2f} matches statement closing="
                    f"{stmt_closing:.2f}. {note}")
        return (f"❌ CLOSING BALANCE MISMATCH: post-import book={post_import_balance:.2f}, "
                f"statement closing={stmt_closing:.2f} (diff={closing_diff:.2f}). {note}")
    if unresolved_opening_gap is not None and unresolved_opening_gap > 0.02:
        return (
            f"⚠ unreconciled {unresolved_opening_gap:.2f} — opening-balance adjustment or "
            f"investigate. (Post-import book={post_import_balance:.2f}, "
            f"statement closing={stmt_closing:.2f}, diff={closing_diff:.2f}.)"
        )
    if closing_diff <= 0.02:
        return (
            f"Closing balance VERIFIED (independent): post-import book="
            f"{post_import_balance:.2f} matches statement closing={stmt_closing:.2f}."
        )
    return (
        f"❌ CLOSING BALANCE MISMATCH: post-import book={post_import_balance:.2f}, "
        f"statement closing={stmt_closing:.2f} (diff={closing_diff:.2f})."
    )


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
{{
  "Date": "Txn Date",
  "Transaction ID": "Ref No / Cheque No",
  "Description": "Narration",
  "Account": null,
  "Deposit": "Credit",
  "Withdrawal": "Debit",
  "Balance": "Balance (INR)",
  "Currency": null
}}
"""


def _read_tabular_rows(file_path: str) -> list[list[str]]:
    """Read a CSV or XLS/XLSX file into a flat list of string rows (no header
    split — see _find_generic_header_row for locating the header row)."""
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
        else:
            import openpyxl
            wb = openpyxl.load_workbook(str(p), data_only=True)
            ws = wb.active
            rows = []
            for row in ws.iter_rows(values_only=True):
                rows.append([str(c).strip() if c is not None else "" for c in row])
        return rows

    else:  # CSV
        with open(file_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.reader(fh)
            return [[str(c).strip() for c in row] for row in reader]


_HEADER_DATEISH_RE = re.compile(r'^\d{1,4}[/-]\d{1,2}[/-]\d{1,4}$')


def _find_generic_header_row(rows: list[list[str]]) -> int:
    """Locate the header row in an arbitrary bank CSV/XLS export, tolerant of
    preamble rows (account info, statement period, blank rows, '****'
    separators) above it — mirrors skill_hdfc's header detection but without
    assuming any particular bank's column names. Heuristic: the first row
    with >=2 non-empty cells where most cells look like text labels (contain
    a letter and aren't themselves a date), i.e. a plausible header row."""
    for i, row in enumerate(rows):
        cells = [str(c).strip() for c in row]
        non_empty = [c for c in cells if c]
        if len(non_empty) < 2:
            continue
        text_like = sum(
            1 for c in non_empty
            if re.search(r'[A-Za-z]', c) and not _HEADER_DATEISH_RE.match(c)
        )
        if text_like >= max(2, (len(non_empty) + 1) // 2):
            return i
    return 0  # fallback: no clear header row found, assume the first row


_CANONICAL_MAPPING_KEYS = {
    "Date", "Transaction ID", "Description", "Account",
    "Deposit", "Withdrawal", "Balance", "Currency",
}


def _sanitize_and_validate_mapping(raw_reply: str, headers: list[str]) -> dict:
    """Parse + sanitize an LLM column-mapping reply: strip accidental
    markdown fences, drop unknown keys, and verify every mapped value is
    either null or a header that actually appears (verbatim) in the file.
    Raises ValueError naming the offending keys/values on any violation —
    callers retry once on this error, then hard-fail."""
    raw = raw_reply.strip()
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        mapping = json.loads(raw.strip())
    except json.JSONDecodeError as e:
        raise ValueError(f"LLM reply was not valid JSON: {e}") from e
    if not isinstance(mapping, dict):
        raise ValueError("LLM reply must be a JSON object")

    sign_convention = mapping.get("sign_convention")
    sanitized = {k: v for k, v in mapping.items() if k in _CANONICAL_MAPPING_KEYS}

    header_set = set(headers)
    bad_values = {
        k: v for k, v in sanitized.items()
        if v is not None and v not in header_set
    }
    if bad_values:
        raise ValueError(
            "LLM mapped canonical column(s) to header(s) not present in the "
            f"file: {bad_values}. Real headers are: {headers}"
        )

    sanitized["sign_convention"] = sign_convention
    return sanitized


def _normalise_to_canonical(
    input_file: str,
    output_path: str,
    bank_name: str,
    config_path: str,
    model_override: str,
) -> None:
    """
    LLM-assisted column normalisation: maps arbitrary CSV/XLS to the canonical
    8-column schema and writes the result to output_path. Reserved for
    "Other Bank (CSV)" — HDFC always uses skill_hdfc's deterministic parser.
    """
    agents_root = Path(__file__).resolve().parent.parent
    if str(agents_root) not in sys.path:
        sys.path.insert(0, str(agents_root))
    from agents.base_agent import run_direct  # noqa: E402

    rows = _read_tabular_rows(input_file)
    if not rows:
        raise ValueError(f"Could not read any data from {input_file}")

    header_idx = _find_generic_header_row(rows)
    headers = rows[header_idx]
    sample = rows[header_idx + 1: header_idx + 6]
    all_rows = rows[header_idx + 1:]
    if not headers or not any(str(h).strip() for h in headers):
        raise ValueError(f"Could not find a header row in {input_file}")

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

    mapping = None
    last_error = None
    for attempt in range(2):  # one retry after an invalid/hallucinated reply
        user_message = prompt
        if attempt == 1:
            user_message = (
                prompt
                + f"\n\nYour previous reply was rejected: {last_error}\n"
                + "Only use header names that appear verbatim in the headers list above."
            )
        raw = run_direct(
            user_message=user_message,
            system_prompt=system,
            config_path=config_path,
            model_override=model_override,
        )
        try:
            mapping = _sanitize_and_validate_mapping(raw, headers)
            break
        except ValueError as e:
            last_error = e
            mapping = None

    if mapping is None:
        raise ValueError(
            f"LLM column mapping failed validation after retry: {last_error}"
        )

    sign_convention = mapping.pop("sign_convention", None)
    col_idx = {h: i for i, h in enumerate(headers)}

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


_ITR_SCRIPTS = Path(__file__).resolve().parent.parent / "skill_itr_workbook" / "scripts"


def _load_entity_profile(entity, entities_path):
    """(profile, error). No entity picked -> (None, None): the entity is
    optional and nothing entity-driven applies. An entity picked but not
    resolvable is an error, never a silent fallback to 'no config'."""
    entity = (entity or "").strip()
    if not entity:
        return None, None
    if str(_ITR_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_ITR_SCRIPTS))
    try:
        import configs  # noqa: PLC0415
        entities = configs.load_entities(entities_path)
    except Exception as e:  # noqa: BLE001
        return None, (f"entities.yaml could not be read ({type(e).__name__}) -- "
                      f"fix it or clear the Entity field. Nothing was run.")
    profile = entities.get(entity)
    if profile is None:
        return None, f"Entity '{entity}' is not in entities.yaml -- pick another or clear the field."
    return profile, None


class _PasswordRuleError(ValueError):
    pass


def _derive_bank_password(bank_info, profile):
    """BNK-05: (password, rule_summary) for a bank that declares a
    `password_rule` module next to its skill, else ("", ""). Raises
    _PasswordRuleError when the rule applies but the record lacks a field."""
    import importlib  # noqa: PLC0415
    modname = f"{bank_info.package}.password_rule"
    try:
        rule = importlib.import_module(modname)
    except ModuleNotFoundError as e:
        if e.name == modname:
            return "", ""          # this bank declares no rule
        raise
    try:
        return rule.derive_password(profile), rule.RULE_SUMMARY
    except ValueError as e:
        raise _PasswordRuleError(str(e)) from None


def run(
    bank: str,
    statement_files: str,
    gnucash_file: str,
    output_path: str,
    config_path: str = None,
    model_override: str = None,
    pdf_password: str = None,
    bank_account: str = None,
    entity: str = None,
    entities_path: str = None,
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
        pdf_password:    Optional statement password, forwarded to skill_hdfc
                         for password-protected HDFC PDFs and the SBM encrypted xlsx (for HDFC often the
                         Cust ID). Never logged.
        bank_account:    Optional full path of YOUR account at this bank, used
                         when several match the bank name and the statement
                         does not say which (IMP-08). Refused when hidden,
                         placeholder, not at this bank, or not in the book.

        entity:          Optional entities.yaml key. Supplies the entity's configured
                         Drawings accounts and card-spend default (MAP-34/35) and,
                         for a bank that declares a password rule, the statement
                         password when the box is empty (BNK-05).
        entities_path:   Path to entities.yaml (the `{data_root}/itr/entities.yaml`
                         token).

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

    entity_profile, entity_err = _load_entity_profile(entity, entities_path)
    if entity_err:
        return f"## {bank} → entity error\n\n❌ {entity_err}"

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        canonical_path = str(tmp_path / "canonical.csv")

        # ── Step 1: Bank extraction → canonical CSV ───────────────────────────
        # Every dedicated bank (DEDICATED_BANKS) is dispatched purely through
        # the agents.banks registry: display_name matches this pipeline's
        # `bank` dispatch string exactly (verified 2026-07-17), so no mapping
        # table is needed. Each BankSkill.parse() returns canonical rows in
        # memory only; the canonical CSV + sidecar is written once, here, via
        # the shared canonical_io tail — no bank writes its own CSV anymore.

        bank_info = next((b for b in discover_banks() if b.display_name == bank), None)

        if bank_info is not None:
            bank_input = (
                statement_files[0] if isinstance(statement_files, list)
                else statement_files.split(",")[0].strip()
            )
            # Per-bank input shaping — the one piece of bank-specific logic
            # that can't be pushed into a uniform call, since each skill's
            # parse() expects a different shape for a staged multi-file
            # upload (single resolved file vs. a whole PDF directory).
            if bank == "ICICI":
                bank_input = _resolve_single_file(bank_input, (".xls", ".xlsx"))
            elif bank == "HDFC":
                bank_input = _resolve_single_file(bank_input, (".csv", ".xls", ".xlsx", ".pdf"))
            elif bank == "SBM":
                bank_input = _resolve_single_file(bank_input, (".xlsx",))
            elif bank == "HSBC":
                # A directory may hold PDFs (OCR path) or a single already-
                # enriched workbook (fast path); a single file is passed
                # through as-is, never swapped for its parent directory. See
                # _resolve_hsbc_input (HSB-03).
                hsbc_resolved, hsbc_error = _resolve_hsbc_input(
                    bank_input, load_bank_skill(bank_info),
                )
                if hsbc_error:
                    return hsbc_error
                bank_input = hsbc_resolved
            elif bank == "Bank of Baroda":
                bob_src = Path(bank_input)
                if bob_src.is_dir() and not sorted(bob_src.glob("*.pdf")):
                    return (
                        f"## {bank} → no PDFs found\n\n"
                        f"❌ The staged upload directory contains no .pdf files:\n"
                        f"`{bank_input}`"
                    )
                bank_input = bob_src

            _emit_progress(1, f"{bank}: extracting statement to canonical CSV")
            log_lines.append(f"**Step 1** — {bank}: extracting statement to canonical CSV")
            _derived = ""
            if not (pdf_password or "").strip() and entity_profile is not None:
                # BNK-05: empty box + a bank that declares a rule -> derive.
                # A typed password never reaches here, so it always wins.
                try:
                    _pw, _derived = _derive_bank_password(bank_info, entity_profile)
                except _PasswordRuleError as e:
                    return f"## {bank} → password error\n\n❌ {e}"
                if _pw:
                    pdf_password = _pw
                    del _pw
                if _derived:
                    # The rule is named, the password never is.
                    log_lines.append(
                        f"Statement password derived from the Entities record "
                        f"({_derived}).")
            try:
                skill = load_bank_skill(bank_info)
                bank_result = skill.parse(bank_input, password=pdf_password)
                write_canonical_csv(bank_result.rows, canonical_path)
                write_sidecar(
                    canonical_path, bank_info.display_name, "derived",
                    bank_result.opening_balance, bank_result.closing_balance,
                    bank_result.row_count,
                    account_number=(bank_result.meta.account_number if bank_result.meta else None),
                )
                log.info("%s skill: %d canonical rows (balance_ok=%s)",
                         bank, bank_result.row_count, bank_result.balance_check.ok)
                # Surface the bank skill's own non-fatal warnings (missing/
                # overlapping statement gaps, extracted-vs-expected count
                # mismatches, unparseable rows, running-balance mismatches,
                # …) instead of discarding them — previously bank_result.warnings
                # was computed and attached by every bank skill but never read
                # by this pipeline, so the user never saw it.
                if bank_result.warnings:
                    log_lines.append(
                        f"**Step 1 warnings** — {bank}: {len(bank_result.warnings)} "
                        f"warning(s) from statement parsing:"
                    )
                    for w in bank_result.warnings:
                        log_lines.append(f"⚠ {w}")
            except Exception as e:
                log.error("%s extraction failed: %s", bank, e, exc_info=True)
                _hint = (
                    "\n\nThe password was derived from the Entities record "
                    f"({_derived}) and did not open the file -- type the "
                    "statement password in the password box instead."
                ) if _derived else ""
                return (
                    f"## {bank} → extraction error\n\n"
                    f"❌ {bank} skill raised an exception:\n```\n{e}\n```{_hint}"
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
        # BNK-09: the FULL statement, before any row is skipped, for the
        # closing-balance gate (running balances + every row's amount).
        stmt_all_rows = list(canonical_rows)

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
        # Prefer the account number from statement metadata (if the adapter
        # captured one) over a bare bank-name match — disambiguates multiple
        # accounts at the same bank.
        stmt_sidecar = _read_sidecar(canonical_path)
        stmt_account_number = stmt_sidecar.get("account_number") if stmt_sidecar else None
        bank_account = (bank_account or "").strip() or None
        # Same evidence for both picks below (before any duplicate rows are
        # trimmed), so the two resolutions can never land on different accounts.
        _ev2 = _statement_evidence(canonical_rows)
        recon = _reconcile_opening_balance(
            canonical_rows, gnucash_file, bank, stmt_account_number,
            chosen_account=bank_account)
        if recon.get("stop"):
            # IMP-08: several postable accounts and no evidence (or a refused
            # choice). No output file is written; the user picks and re-runs.
            cands = recon.get("candidates") or []
            listing = chr(10).join(f"- `{c}`" for c in cands)
            nl2 = chr(10) * 2
            return (
                f"**Which {bank} account is this statement for?**{nl2}"
                f"{recon['message']}{nl2}"
                + (f"Postable accounts:{chr(10)}{listing}{nl2}" if listing else "")
                + "Nothing was written. Pick the account in **Bank account** "
                  "and run again."
            )
        if recon.get("match_warning"):
            log_lines.append(f"⚠ Account match: {recon['match_warning']}")

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

        # Track any unexplained opening-balance gap so it survives into the
        # final closing-balance verdict below, instead of silently evaporating
        # if dedup happens to find zero overlapping rows.
        unresolved_opening_gap = None
        if recon["ok"]:
            log_lines.append(f"**Balance check** — {recon['message']}")
        else:
            unresolved_opening_gap = abs(
                recon.get("gnucash_opening_balance", recon["gnucash_balance"])
                - recon["statement_opening"])
            log_lines.append(
                f"**Balance check** — ⚠ Opening balance gap detected "
                f"(GnuCash before the statement={recon.get('gnucash_opening_balance', recon['gnucash_balance']):.2f}, "
                f"statement={recon['statement_opening']:.2f}, diff={unresolved_opening_gap:.2f}). "
                f"Dedup below will attempt to reconcile overlapping transactions; "
                f"if it doesn't, this gap carries into the final verdict."
            )

        # ── Resolve GnuCash bank account (needed to scope dedup below, and
        # for the CSV Account column) ──────────────────────────────────────
        # Must pass stmt_account_number here too — the opening-balance
        # reconciliation above (_reconcile_opening_balance) already resolves
        # the account by account number when one is available. Without
        # passing it here as well, this second, independent resolution could
        # fall back to a bare bank-name match and land on a *different*
        # account than the one just reconciled whenever several accounts
        # share the same bank name (e.g. four Bank of Baroda accounts) —
        # silently scoping dedup / the CSV Account column / contra detection
        # to the wrong ledger. Any ambiguity here is already surfaced via
        # recon['match_warning'] above (same bank/account_number/file inputs
        # → same resolution), so it is not re-logged a second time.
        gc_info = _get_gnucash_account_balance(
            gnucash_file, bank, stmt_account_number,
            opening_balance=_ev2.get("opening_balance"),
            start_date=_ev2.get("start_date"),
            narrations=_ev2.get("narrations"), chosen_account=bank_account)
        account_filter_path = gc_info["account_name"] if gc_info["found"] else None

        # ── Duplicate detection (Phase 4 Lite) ─────────────────────────────────
        # Compare canonical CSV against GnuCash book to flag duplicates
        _emit_progress(4, f"{bank}: checking for duplicates in GnuCash")

        gnucash_data = None  # unfiltered whole-book parse; contra detection needs it below
        gnucash_data_scoped = None  # BNK-09: this bank's own account only (closing gate)
        booked_matches: list[dict] = []  # IMP-11
        try:
            if account_filter_path is None:
                # Without a resolved account we cannot scope the dedup index
                # to the target account, and indexing the whole book by
                # (date, amount) alone lets an unrelated posting elsewhere —
                # a clearing-account leg, an Expense entry, anything on the
                # same date/amount — silently mark a real statement row as
                # already-imported. Skip dedup rather than risk that.
                log_lines.append(
                    f"⚠️ Duplicate check skipped — no matching '{bank}' bank "
                    f"account found in GnuCash book; proceeding with all rows."
                )
            else:
                # Parse the whole book once for contra detection later (it
                # needs to see transactions in OTHER bank accounts too), and
                # separately parse it scoped to just the target account so
                # the dedup index below can't be fooled by same-date/
                # same-amount postings elsewhere in the book.
                gnucash_data = parse_gnucash_for_reconcile(gnucash_file)
                gnucash_data_scoped = parse_gnucash_for_reconcile(
                    gnucash_file, account_filter=account_filter_path
                )

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

                # Run reconciliation, scoped to the target account only
                report, dedup_summary = reconcile(reconcile_rows, gnucash_data_scoped)

                matched_count = dedup_summary.get('matched', 0)
                duplicate_count = dedup_summary.get('duplicates', 0)
                new_count = dedup_summary.get('new', 0)
                total_duplicates = matched_count + duplicate_count

                # IMP-11: own transfers already booked from the OTHER bank's
                # statement, dated 1-2 days differently. Matched on the FULL
                # statement (before the exact dedup drops rows) so an exact pair
                # consumes its book split first; book splits dated before the
                # statement start are included.
                try:
                    _raw_matches = match_booked_own_transfers(
                        reconcile_rows, gnucash_data_scoped, account_filter_path)
                except Exception as e:  # never let the new check break the import
                    log.warning(f"Booked-transfer check failed: {e}")
                    _raw_matches = []

                # Filter to keep only "New" rows
                new_rows = [
                    canonical_rows[i] for i, r in enumerate(report)
                    if r.get('status') == 'New'
                ]

                _new_pos, _n = {}, 0
                for _i, _r in enumerate(report):
                    if _r.get('status') == 'New':
                        _new_pos[_i] = _n
                        _n += 1
                booked_matches = [dict(m, row_idx=_new_pos[m["row_idx"]])
                                  for m in _raw_matches if m["row_idx"] in _new_pos]

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
        gnucash_bank_account = ""
        if gc_info["found"]:
            raw_path = gc_info["account_name"]
            # Strip "Root Account:" prefix — GnuCash CSV importer doesn't want it
            if raw_path.startswith("Root Account:"):
                gnucash_bank_account = raw_path[len("Root Account:"):]
            else:
                gnucash_bank_account = raw_path
            log_lines.append(f"Bank account: `{gnucash_bank_account}`")
        else:
            # No matching bank account in the .gnucash book. This quietly turns
            # off three things, so say so plainly rather than leaving the user to
            # wonder why this bank's output looks reduced: (1) Transfer Account is
            # left blank, (2) cross-bank transfer (contra) detection is skipped,
            # (3) the opening-balance reconciliation check is skipped.
            log_lines.append(
                f"⚠️ **Couldn't find a '{bank}' bank account in the GnuCash book.** "
                f"Transfer Account is left blank, and cross-bank transfer (contra) "
                f"detection and the opening-balance check are skipped. To enable them, "
                f"add or rename a bank-typed account in your `.gnucash` that matches "
                f"'{bank}' (e.g. `Assets:…:Cash and Bank:{bank} - <account-number>`)."
            )

        if booked_matches:
            _pre = sum(1 for m in booked_matches if m.get("pre_period"))
            log_lines.append(
                f"⚠️ Already booked — {len(booked_matches)} statement row(s) are own "
                f"transfers whose other leg is already in the book (dated 1-2 days "
                f"differently{f', {_pre} before the statement start' if _pre else ''}). "
                f"They are left UNTICKED (not imported) in **Banks > Review**; "
                f"re-tick one there to import it anyway."
            )

        # ── Contra detection (cross-bank transfer matching) ──────────────────
        contra_flags: dict[int, dict] = {}  # row_idx → contra info
        contra_ran = False
        try:
            if gnucash_bank_account and gnucash_data:
                contra_ran = True
                contras = detect_contra_entries(
                    canonical_rows, gnucash_data, gnucash_bank_account
                )
                if contras:
                    for c in contras:
                        contra_flags[c["row_idx"]] = c
                    # PIPE-09: the count line is logged AFTER the sidecar is
                    # written (set-aside / carrier removal can still change it).
                    _emit_progress(4, f"{bank}: {len(contras)} contra(s) flagged")
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
            drawings_accounts=list(getattr(entity_profile, "drawings_accounts", None) or []),
            card_default_account=(getattr(entity_profile, "card_spend_default_account", "") or None),
        )
        try:
            log_lines.append(_step3_result_line(output_path))
        except Exception as e:
            log_lines.append(f"Step 3 result -- could not read the mapped CSV: {e}")

        # ── Apply confirmed contras to the mapped output ────────────────────
        # For confirmed (high-confidence, reference-matched) transfers, book the
        # row against the counterparty bank instead of whatever category the
        # mapper guessed — a genuine bank-to-bank transfer must not land in
        # income/expense/investment. Possible (medium) contras are left alone:
        # they stay a review hint and keep the mapper's account. contra row_idx
        # is 0-based into canonical_rows, which the mapper preserves 1:1.
        if contra_flags:
            try:
                remapped = _apply_confirmed_contras(output_path, contra_flags)
                if remapped:
                    log_lines.append(
                        f"Contra — booked {remapped} confirmed transfer(s) "
                        f"to the counterparty bank account."
                    )
            except Exception as e:
                log.warning(f"Could not apply confirmed contras: {e}")
                log_lines.append(f"⚠️ Contra remap skipped — {e}")

        # IMP-11: park the already-booked rows (unticked) before anything shifts.
        try:
            _parked = _set_aside_booked_transfers(output_path, contra_flags, booked_matches)
            if _parked:
                log_lines.append(
                    f"Already-booked rows set aside unticked: {_parked} "
                    f"(see Banks > Review)")
        except Exception as e:
            log.warning(f"Could not set aside booked transfers: {e}")
            log_lines.append(f"⚠️ Already-booked rows not set aside — {e}")

        # HSB-04: the opening-balance carrier row (e.g. HSBC "BALANCE BROUGHT
        # FORWARD") has done its job -- balance check, opening reconciliation,
        # dedup and contra detection all read it above. Drop it now so it does
        # not reach GnuCash as a zero-amount transaction.
        try:
            n_carrier = _drop_balance_carriers(output_path, contra_flags)
            if n_carrier:
                log_lines.append(
                    f"Opening-balance row (no money movement) left out of the "
                    f"import file: {n_carrier}"
                )
        except Exception as e:
            log.warning(f"Could not drop balance-carrier rows: {e}")
            log_lines.append(f"⚠️ Balance-carrier row not removed — {e}")

        # Write contra flags sidecar (if any) alongside the output CSV
        try:
            _write_contra_sidecar(output_path, contra_flags)
        except Exception as e:
            log.warning(f"Could not write contra sidecar: {e}")
            log_lines.append(f"⚠️ Contra sidecar not written — {e}")
        if contra_ran:
            log_lines.append(_contra_log_line(contra_flags))

        # IMP-14: advisory badge for own-name payments OUT whose other side is
        # not in the book yet. Never re-maps a row; written to its own sidecar.
        try:
            _adv = _own_name_out_advisories(
                output_path, contra_flags,
                getattr(entity_profile, "name", "") or "")
            _write_advisory_sidecar(output_path, _adv)
            if _adv:
                log_lines.append(
                    f"Advisory -- {len(_adv)} payment(s) out carry the holder's own "
                    f"name and have no other side in the book (possible own "
                    f"transfer); shown in **Banks > Review**, accounts unchanged.")
        except Exception as e:
            log.warning(f"Own-name advisory skipped: {e}")

        # BNK-09: closing-balance gate. The book must END at the statement
        # balance, whatever was skipped or flagged. Blocks the safe-orientation
        # import file when it would not.
        _gate_blocked = False
        try:
            _gl, _gate_blocked = _run_closing_gate(
                output_path=output_path, stmt_rows=stmt_all_rows,
                scoped_data=gnucash_data_scoped, filter_path=account_filter_path,
                bank_account=gnucash_bank_account)
            log_lines.extend(_gl)
        except Exception as e:
            _gate_blocked = True
            log.warning(f"Closing-balance gate failed: {e}")
            log_lines.append(
                f"❌ Closing-balance gate could not run ({e}). The bank-base import "
                f"file was NOT released; do not import until this is checked.")
            try:
                _bg.write_bank_base_csv([], _bg.bank_base_name(output_path),
                                        root=os.path.dirname(os.path.abspath(output_path)), blocked=True)
            except Exception:
                pass

        _emit_progress(6, f"{bank}: final balance verification")
        try:
            with open(output_path, "r", encoding="utf-8") as f:
                final_rows = list(csv.DictReader(f))
            sidecar = _read_sidecar(canonical_path)
            stmt_closing = sidecar.get("closing_balance") if sidecar else None
            log_lines.append(
                "**Step 6 result** — final balance check: "
                + final_closing_balance_verdict(
                    recon, final_rows, stmt_closing, unresolved_opening_gap,
                    explain_opening_gap(unresolved_opening_gap, booked_matches))
            )
        except Exception as e:
            log_lines.append(
                f"**Step 6 result** — final balance check: Could not verify closing balance: {e}")

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
    if _gate_blocked:
        return (
            f"## 🔴 {bank} → GnuCash pipeline: IMPORT BLOCKED (closing-balance gate)\n\n"
            f"The book would not end at the statement closing balance. Nothing may be "
            f"imported until the rows below are resolved.\n\n"
            f"{steps_summary}\n\n"
            f"---\n\n"
            f"{mapping_result}\n\n"
            f"**Next:** resolve the rows listed above in **Banks > Review**, then re-run."
        )
    return (
        f"## {bank} → GnuCash pipeline complete\n\n"
        f"{steps_summary}\n\n"
        f"---\n\n"
        f"{mapping_result}\n\n"
        f"**Next:** check the **Banks > Review** tab to verify/correct account assignments, then import into GnuCash."
    )
