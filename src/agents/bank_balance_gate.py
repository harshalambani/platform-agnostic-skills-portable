"""bank_balance_gate.py -- BNK-09: the book must end at the statement balance.

Three pieces, all pure functions over plain data (no GnuCash writes, ever):

1. ``evaluate_gate`` -- the closing-balance gate that runs BEFORE an import file
   is released. Projected book balance = the book's balance for THIS bank
   account just before the statement's first date, plus every row to be
   imported, plus every row skipped as already booked. It must equal the
   statement's running balance at the end date and at every month end.
   A row may be skipped ONLY if its twin is in THIS bank's own account: a split
   on this bank account with the same amount within the matcher's day window.
   A same-amount entry in a shared category (Cash, Drawings, Credit Card
   Payment) booked from ANOTHER bank is never a twin.
2. ``post_import_check`` -- read-only comparison of the bank's daily book
   balance with the statement's running balance, naming the first day they
   drift apart and the closing difference, plus the rows absent from this
   bank's account (written as a "missing rows" CSV by ``write_missing_rows``).
3. ``to_bank_base_rows`` -- the import orientation GnuCash's duplicate matcher
   compares against the BANK account (bank = base Account, category = Transfer
   Account), with the amount columns re-signed for that orientation.
"""
from __future__ import annotations

import csv
import json
import os
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

from agents.canonical_io import (
    IMPORT_READY_FIELDS,
    IMPORT_DEPOSIT_HEADER,
    IMPORT_WITHDRAWAL_HEADER,
    row_deposit,
    row_withdrawal,
)
from agents.skill_gnucash_reconciler.agent import (
    BOOKED_TRANSFER_TOLERANCE_DAYS,
    _norm_account_path,
    _parse_date,
)

TOLERANCE = 0.01
GATE_SUFFIX = ".gate.json"

# Bank-as-base orientation: the amount columns swap meaning.
BANK_BASE_DEPOSIT_HEADER = "Amount (Deposit)"
BANK_BASE_WITHDRAWAL_HEADER = "Amount Negated (Withdrawal)"


class UnsafeOutputPath(ValueError):
    """A gate file path fell outside the folder it must stay in."""


def _confine(path, root) -> str:
    """Return the normalised ``path`` only if it lies directly inside ``root``
    (the folder of the already-validated import file). Anything else -- another
    folder, a ``..`` traversal, a NUL byte -- raises and nothing is touched.
    Written as a plain string-prefix test on the real path (the form CodeQL's
    py/path-injection query recognises as a sanitiser)."""
    raw, base = str(path or ""), str(root or "")
    if not raw or not base or "\x00" in raw or "\x00" in base:
        raise UnsafeOutputPath("No usable path given.")
    norm = os.path.normcase(os.path.realpath(os.path.abspath(raw)))
    top = os.path.normcase(os.path.realpath(os.path.abspath(base)))
    if os.path.dirname(norm) == top and norm.startswith(top.rstrip(os.sep) + os.sep):
        return norm
    raise UnsafeOutputPath(f"{os.path.basename(raw) or raw}: outside the outputs folder; refused.")


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _num(v):
    s = str(v if v is not None else "").replace(",", "").strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _iso(s) -> str | None:
    d = _parse_date(str(s or ""))
    return d.strftime("%Y-%m-%d") if d else None


def point_key(iso: str, amount: float) -> str:
    return f"{iso}|{amount:.2f}"


def gate_sidecar_path(output_path) -> Path:
    """<stem>.gate.json next to the mapped import-ready CSV."""
    return Path(output_path).with_suffix(GATE_SUFFIX)


def row_amount(row: dict) -> float:
    """Signed statement amount of an import-ready or canonical row
    (deposit positive, withdrawal negative), any amount-header spelling."""
    return round((_num(row_deposit(row)) or 0.0) - (_num(row_withdrawal(row)) or 0.0), 2)


def statement_points(rows: list[dict]) -> list[dict]:
    """One point per dated statement row, file order kept."""
    pts = []
    for r in rows:
        iso = _iso(r.get("Date"))
        if iso is None:
            continue
        pts.append({
            "date": iso,
            "date_raw": str(r.get("Date") or ""),
            "txn_id": str(r.get("Transaction ID") or ""),
            "description": str(r.get("Description") or ""),
            "amount": row_amount(r),
            "balance": _num(r.get("Balance")),
            "currency": str(r.get("Currency") or ""),
        })
    return pts


def own_splits(scoped_data: dict, target_account: str) -> list[tuple[str, float]]:
    """(iso date, amount) of every split sitting in THIS bank's own account."""
    tn = _norm_account_path(target_account)
    out = []
    for t in (scoped_data or {}).get("transactions", []):
        if _norm_account_path(t.get("account", "")) != tn:
            continue
        iso = _iso(t.get("date"))
        if iso:
            out.append((iso, round(float(t["amount"]), 2)))
    return out


def book_balance_before(splits, first_iso: str) -> float:
    return round(sum(a for d, a in splits if d < first_iso), 2)


def statement_opening(points: list[dict]) -> float | None:
    if not points or points[0]["balance"] is None:
        return None
    return round(points[0]["balance"] - points[0]["amount"], 2)


# ---------------------------------------------------------------------------
# twins: the ONLY thing that may justify skipping a statement row
# ---------------------------------------------------------------------------

def assign_twins(points: list[dict], splits, tol_days: int = BOOKED_TRANSFER_TOLERANCE_DAYS):
    """For each statement point, its twin in THIS bank's own account, or None.

    A twin is a split in the bank's own account with the same signed amount
    within ``tol_days``. ONE-TO-ONE: a book split serves at most one row, and
    nearest date wins (an exact date first), so two identical rows against one
    book entry leave the second without a twin.
    """
    by_amt = defaultdict(list)
    for si, (sd, sa) in enumerate(splits):
        by_amt[round(sa, 2)].append(si)
    cands = []
    for ri, p in enumerate(points):
        if p["amount"] == 0:
            continue
        rd = date.fromisoformat(p["date"])
        for si in by_amt.get(round(p["amount"], 2), ()):
            dist = abs((date.fromisoformat(splits[si][0]) - rd).days)
            if dist <= tol_days:
                cands.append((dist, ri, si))
    cands.sort()
    twins = [None] * len(points)
    used = set()
    for dist, ri, si in cands:
        if twins[ri] is not None or si in used:
            continue
        used.add(si)
        twins[ri] = {"date": splits[si][0], "days_off": dist}
    return twins


def mark_imported(points: list[dict], twins: list, imported_rows: list[dict]):
    """Decide which statement points the import file actually carries.

    Matching is by (date, signed amount), multiset. Within one key the points
    WITHOUT a twin are taken as the imported ones first (a row that has a twin
    in the book is the one that was skipped). Returns (flags, extras): flags is
    parallel to points; extras are imported rows with no statement point.
    """
    want = Counter()
    raw = {}
    for r in imported_rows:
        iso = _iso(r.get("Date"))
        if iso is None:
            continue
        a = row_amount(r)
        want[point_key(iso, a)] += 1
        raw.setdefault(point_key(iso, a), r)
    groups = defaultdict(list)
    for i, p in enumerate(points):
        groups[point_key(p["date"], p["amount"])].append(i)
    flags = [False] * len(points)
    extras = []
    for k, n in want.items():
        idxs = sorted(groups.get(k, []), key=lambda i: (twins[i] is not None, i))
        for i in idxs[:n]:
            flags[i] = True
        for _ in range(max(0, n - len(idxs))):
            iso, amt = k.split("|")
            r = raw[k]
            extras.append({"date": iso, "amount": float(amt),
                           "description": str(r.get("Description") or "")})
    return flags, extras


def _month_ends(first_iso: str, last_iso: str) -> list[str]:
    from calendar import monthrange
    f, l = date.fromisoformat(first_iso), date.fromisoformat(last_iso)
    out, y, m = [], f.year, f.month
    while (y, m) <= (l.year, l.month):
        d = date(y, m, monthrange(y, m)[1]).isoformat()
        out.append(min(d, last_iso))
        m += 1
        if m > 12:
            y, m = y + 1, 1
    return sorted(set(out))


def _statement_at(points: list[dict], d: str):
    """Running balance at the end of day ``d``: the last row (file order) of the
    latest dated row on or before ``d`` that carries a balance."""
    best = None
    for i, p in enumerate(points):
        if p["date"] <= d and p["balance"] is not None:
            if best is None or (p["date"], i) >= (best[0], best[1]):
                best = (p["date"], i, p["balance"])
    return None if best is None else best[2]


# ---------------------------------------------------------------------------
# 1. the gate
# ---------------------------------------------------------------------------

def evaluate_gate(points, twins, flags, extras, book_opening, *,
                  skip_reasons: dict | None = None,
                  intended: dict | None = None,
                  tolerance: float = TOLERANCE) -> dict:
    """Closing-balance gate. See the module docstring.

    ``flags``/``extras`` come from ``mark_imported``. ``skip_reasons`` and
    ``intended`` map ``"<iso>|<amount>"`` to the text shown for a row that is
    not imported (why it was being skipped / which account it was meant for).
    """
    skip_reasons = skip_reasons or {}
    intended = intended or {}
    res = {
        "status": "unverified", "checks": [], "opening": {},
        "unexplained": [], "timing": [], "double_booking_risk": [],
        "extra_imported": list(extras), "message": "",
    }
    if not points:
        res["message"] = "No dated statement rows."
        return res

    s_open = statement_opening(points)
    pre_gap = None if s_open is None else round(book_opening - s_open, 2)
    res["opening"] = {"book": book_opening, "statement": s_open, "gap": pre_gap,
                      "ok": pre_gap is None or abs(pre_gap) <= tolerance}
    offset = pre_gap or 0.0

    counted = [bool(flags[i] or twins[i] is not None) for i in range(len(points))]
    first = min(p["date"] for p in points)
    last = max(p["date"] for p in points)

    for d in _month_ends(first, last):
        stmt = _statement_at(points, d)
        if stmt is None:
            continue
        proj = book_opening + sum(p["amount"] for i, p in enumerate(points)
                                  if counted[i] and p["date"] <= d)
        proj += sum(e["amount"] for e in extras if e["date"] <= d)
        diff = round(proj - offset - stmt, 2)
        res["checks"].append({"date": d, "statement": round(stmt, 2),
                              "projected": round(proj - offset, 2), "diff": diff,
                              "ok": abs(diff) <= tolerance + 1e-9})

    for i, p in enumerate(points):
        if p["amount"] == 0:
            continue
        k = point_key(p["date"], p["amount"])
        row = {"date": p["date"], "description": p["description"], "amount": p["amount"]}
        if not flags[i] and twins[i] is None:
            row["intended_account"] = intended.get(k, "")
            row["reason"] = skip_reasons.get(k, "not in the import file")
            res["unexplained"].append(row)
        elif not flags[i] and twins[i] is not None and twins[i]["days_off"] > 0:
            res["timing"].append(dict(row, twin_date=twins[i]["date"],
                                      days_off=twins[i]["days_off"]))
        elif flags[i] and twins[i] is not None:
            res["double_booking_risk"].append(dict(
                row, twin_date=twins[i]["date"],
                reason="imported although this bank's own account already holds the same amount "
                       "on a nearby date"))

    if not res["checks"]:
        res["status"] = "unverified"
        res["message"] = "The statement has no running balance to check against."
        return res
    bad = [c for c in res["checks"] if not c["ok"]]
    if bad:
        res["status"] = "fail"
        res["message"] = (
            f"The book would NOT end at the statement balance: {len(bad)} check date(s) differ, "
            f"first {bad[0]['date']} by {bad[0]['diff']:.2f}.")
    else:
        res["status"] = "pass"
        res["message"] = "The projected book balance equals the statement balance at every month end and at the end."
    return res


# ---------------------------------------------------------------------------
# 2. read-only check after the import
# ---------------------------------------------------------------------------

def post_import_check(points: list[dict], splits, tolerance: float = TOLERANCE,
                      tol_days: int = BOOKED_TRANSFER_TOLERANCE_DAYS) -> dict:
    """Compare this bank's daily book balance with the statement's running
    balance. Reads data only. ``splits`` = own_splits(book, bank account)."""
    res = {"status": "unverified", "first_drift_date": None, "closing_diff": None,
           "opening_offset": 0.0, "timing_only": False, "daily": [], "missing": [],
           "message": ""}
    if not points:
        res["message"] = "No dated statement rows."
        return res
    twins = assign_twins(points, splits, tol_days)
    res["missing"] = [dict(p, index=i) for i, p in enumerate(points)
                      if twins[i] is None and p["amount"] != 0]
    first = min(p["date"] for p in points)
    s_open = statement_opening(points)
    offset = 0.0 if s_open is None else round(book_balance_before(splits, first) - s_open, 2)
    res["opening_offset"] = offset
    days = sorted({p["date"] for p in points if p["balance"] is not None})
    for d in days:
        book = round(sum(a for sd, a in splits if sd <= d), 2)
        stmt = _statement_at(points, d)
        drift = round(book - stmt - offset, 2)
        res["daily"].append({"date": d, "book": book, "statement": round(stmt, 2),
                             "drift": drift})
    if not res["daily"]:
        res["message"] = "The statement has no running balance to check against."
        return res
    drifting = [x for x in res["daily"] if abs(x["drift"]) > tolerance + 1e-9]
    res["closing_diff"] = res["daily"][-1]["drift"]
    if not drifting and not res["missing"]:
        res["status"] = "clean"
        res["message"] = "The book matches the statement on every day."
        return res
    res["status"] = "drift"
    if drifting:
        res["first_drift_date"] = drifting[0]["date"]
        res["timing_only"] = abs(res["closing_diff"]) <= tolerance + 1e-9
        if res["timing_only"] and not res["missing"]:
            res["status"] = "timing_only"
    res["message"] = (
        f"The book differs from the statement"
        + (f"; first day apart {res['first_drift_date']}, closing difference {res['closing_diff']:.2f}"
           + (" (timing only: it nets to zero by the end)" if res["timing_only"] else "")
           if drifting else "")
        + f"; {len(res['missing'])} statement row(s) are absent from this bank's account.")
    return res


def write_missing_rows(missing: list[dict], bank_account: str, mapped_rows: list[dict],
                       intended: dict, out_path, *, root) -> int:
    """Write the rows absent from this bank's account in the import_ready layout
    (Account = category, Transfer Account = bank). The category comes from the
    mapped import-ready rows (matched by date + amount, one-to-one), then from
    ``intended``; with neither it is left BLANK and the MatchReason says so --
    never a guessed account. Never touches the book. Returns the row count."""
    pool = defaultdict(list)
    for r in mapped_rows:
        iso = _iso(r.get("Date"))
        if iso:
            pool[point_key(iso, row_amount(r))].append(r)
    headers = [IMPORT_DEPOSIT_HEADER if f == "Deposit"
               else IMPORT_WITHDRAWAL_HEADER if f == "Withdrawal" else f
               for f in IMPORT_READY_FIELDS]
    out_rows = []
    for m in missing:
        k = point_key(m["date"], m["amount"])
        src = pool[k].pop(0) if pool.get(k) else None
        acct = (src or {}).get("Account") or intended.get(k, "")
        row = {
            "Date": m["date_raw"] or m["date"], "Transaction ID": m.get("txn_id", ""),
            "Description": m["description"], "Account": acct,
            "Transfer Account": bank_account,
            IMPORT_DEPOSIT_HEADER: f"{m['amount']:.2f}" if m["amount"] > 0 else "",
            IMPORT_WITHDRAWAL_HEADER: f"{-m['amount']:.2f}" if m["amount"] < 0 else "",
            "Balance": "" if m.get("balance") is None else f"{m['balance']:.2f}",
            "Currency": m.get("currency") or "INR",
            "Confidence": (src or {}).get("Confidence", "none") if acct else "none",
            "MatchReason": ((src or {}).get("MatchReason") or "Missing from the book") if acct
            else "Missing from the book; no account known - assign one in Review before import",
        }
        out_rows.append(row)
    safe = _confine(out_path, root)
    with open(safe, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers)
        w.writeheader()
        w.writerows(out_rows)
    return len(out_rows)


# ---------------------------------------------------------------------------
# 3. bank-as-base orientation
# ---------------------------------------------------------------------------

def bank_base_name(import_ready_path) -> Path:
    """Sibling file name that does NOT match the Review picker's
    ``*GnuCash_import_ready*.csv`` glob."""
    p = Path(import_ready_path)
    stem = p.stem
    stem = stem.replace("import_ready", "bank_base") if "import_ready" in stem else stem + "_bank_base"
    return p.with_name(stem + p.suffix)


BANK_BASE_HEADERS = [
    BANK_BASE_DEPOSIT_HEADER if f == "Deposit"
    else BANK_BASE_WITHDRAWAL_HEADER if f == "Withdrawal" else f
    for f in IMPORT_READY_FIELDS
]


def to_bank_base_rows(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Re-orient import-ready rows so the BANK is the base Account.

    Account (category) and Transfer Account (bank) swap places. The amounts keep
    their numbers; the HEADERS swap meaning: a deposit is now an "Amount" (the
    bank goes up), a withdrawal an "Amount Negated" (the bank goes down). A row
    with no Transfer Account (bank unknown) or no category cannot be oriented
    and is reported in the problem list, never guessed.
    """
    out, problems = [], []
    for i, r in enumerate(rows, 1):
        bank = str(r.get("Transfer Account") or "").strip()
        cat = str(r.get("Account") or "").strip()
        if not bank:
            problems.append(f"row {i} ({r.get('Date')}): no bank account (Transfer Account is blank)")
            continue
        if not cat:
            problems.append(f"row {i} ({r.get('Date')}): no category account")
            continue
        dep, wd = row_deposit(r), row_withdrawal(r)
        new = {}
        for f in IMPORT_READY_FIELDS:
            if f == "Account":
                new[f] = bank
            elif f == "Transfer Account":
                new[f] = cat
            elif f == "Deposit":
                new[BANK_BASE_DEPOSIT_HEADER] = dep if dep is not None else ""
            elif f == "Withdrawal":
                new[BANK_BASE_WITHDRAWAL_HEADER] = wd if wd is not None else ""
            else:
                new[f] = r.get(f, "")
        out.append(new)
    return out, problems


def write_bank_base_csv(import_ready_rows: list[dict], dst_path, *, root, blocked: bool = False):
    """Write the bank-base file. ``blocked`` (the gate failed) writes the header
    only, so a stale file from an earlier run can never be imported by mistake.
    Returns (rows_written, problems)."""
    rows, problems = ([], []) if blocked else to_bank_base_rows(import_ready_rows)
    if problems:
        rows = []
    safe = _confine(dst_path, root)
    with open(safe, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=BANK_BASE_HEADERS)
        w.writeheader()
        w.writerows(rows)
    return len(rows), problems


# ---------------------------------------------------------------------------
# sidecar + re-evaluation (used by the pipeline and the Review tab)
# ---------------------------------------------------------------------------

def write_gate_sidecar(path, *, root, bank_account: str, book_filter_path: str,
                       points, twins, book_opening, intended, skip_reasons,
                       result) -> None:
    doc = {"bank_account": bank_account, "book_filter_path": book_filter_path,
           "points": points, "twins": twins, "book_opening": book_opening,
           "intended": intended, "skip_reasons": skip_reasons, "result": result}
    safe = _confine(path, root)
    with open(safe, "w", encoding="utf-8") as f:
        json.dump(doc, f, indent=2, default=str)


def read_gate_sidecar(path, *, root) -> dict | None:
    try:
        safe = _confine(path, root)
    except UnsafeOutputPath:
        return None
    if not os.path.isfile(safe):
        return None
    try:
        with open(safe, "r", encoding="utf-8") as f:
            doc = json.loads(f.read())
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and "points" in doc else None


def reevaluate(doc: dict, imported_rows: list[dict]) -> dict:
    """Re-run the gate from a sidecar for a different set of imported rows (the
    Review tab, after the user unticked or re-ticked rows)."""
    points, twins = doc["points"], doc["twins"]
    flags, extras = mark_imported(points, twins, imported_rows)
    return evaluate_gate(points, twins, flags, extras, doc["book_opening"],
                         skip_reasons=doc.get("skip_reasons"), intended=doc.get("intended"))


def format_gate_failure(result: dict, limit: int = 40) -> list[str]:
    """Plain-text lines naming every row that explains a failed gate."""
    lines = [result.get("message", "")]
    for c in result.get("checks", []):
        if not c["ok"]:
            lines.append(f"  {c['date']}: statement {c['statement']:.2f}, "
                         f"projected book {c['projected']:.2f}, difference {c['diff']:.2f}")
    for r in result.get("unexplained", [])[:limit]:
        lines.append(
            f"  ROW {r['date']} | {r['description'][:60]} | {r['amount']:.2f} | "
            f"intended account: {r.get('intended_account') or '(not mapped)'} | {r['reason']}")
    if not result.get("unexplained"):
        lines.append("  No single skipped row explains the difference "
                     "(check rows the import file lacks or adds).")
    for r in result.get("extra_imported", []):
        lines.append(f"  EXTRA ROW in the import file but not on the statement: "
                     f"{r['date']} | {r['description'][:60]} | {r['amount']:.2f}")
    return lines
