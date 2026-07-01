"""
tools.py — implementation helpers for the 26AS TDS Journal skill.

These are PLAIN functions (not LangChain tools). agent.py wraps them as
closures that capture the file paths, so the LLM never has to pass a path
(small local models garble long Windows paths) — it only decides account
choices. The deterministic builder always writes a valid, balanced CSV
(unmatched deductors go to Suspense), and every call verifies its own output.
"""
import ast
import csv
import gzip
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT = Path(__file__).parent / "scripts" / "build_tds_journals.py"


def _run_script(args: list[str]) -> str:
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return f"ERROR: {result.stderr.strip() or result.stdout.strip()}"
    return result.stdout.strip() or "Done."


def _verify(csv_path: str) -> str:
    """Balance check: every transaction's signed Amount splits must sum to zero
    and no split may have a blank Account."""
    p = Path(csv_path)
    if not p.is_file():
        return f"NOTE: could not re-open {p.name} to verify (it was still saved)."
    groups: dict = {}
    order: list = []
    with p.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            acct = (row.get("Account") or "").strip()
            amt = float(row["Amount"]) if (row.get("Amount") or "").strip() else 0.0
            key = (row.get("Transaction ID") or "").strip() or (
                (row.get("Date") or "") + "|" + (row.get("Description") or ""))
            if key not in groups:
                groups[key] = [0.0, False]
                order.append(key)
            groups[key][0] += amt
            if not acct:
                groups[key][1] = True
    problems = []
    for key in order:
        total, blank = groups[key]
        if abs(total) >= 0.01:
            problems.append(f"{key}: does not balance (sum {total:.2f})")
        if blank:
            problems.append(f"{key}: a split has a blank Account")
    if problems:
        return "VERIFY PROBLEMS:\n" + "\n".join(problems)
    return f"VERIFIED — {len(order)} transactions, all balanced."


def _normalize_overrides(overrides) -> "dict | str":
    """Accept overrides as a dict (object) OR a JSON / python-dict string, and
    return {str(sr): account_path}, or an error string."""
    if isinstance(overrides, str):
        s = overrides.strip()
        if not s:
            return {}
        try:
            overrides = json.loads(s)
        except Exception:
            try:
                overrides = ast.literal_eval(s)
            except Exception:
                return ('ERROR: overrides must be an object like '
                        '{"2": "Income:Interest Income:Interest on HDFC - FD"}.')
    if not isinstance(overrides, dict):
        return 'ERROR: overrides must be an object {sr: account_path}.'
    return {str(k): v for k, v in overrides.items() if v}


def run_build(xlsx_path: str, gnucash_path: str, output_path: str) -> str:
    """Deterministic build + self-verify. Returns the summary + verification."""
    out = _run_script([xlsx_path, gnucash_path, output_path])
    if out.startswith("ERROR"):
        return out
    return out + "\n\n" + _verify(output_path)


def _existing_account_paths(gnucash_path: str):
    """Set of full account paths in the .gnucash book (without 'Root Account:'),
    or None if it can't be read. Used to compute accounts-to-create."""
    try:
        raw = Path(gnucash_path).read_bytes()
        data = gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw
        root = ET.fromstring(data)
    except Exception:
        return None
    ns = {"act": "http://www.gnucash.org/XML/act"}
    by_id = {}
    for a in root.iter("{http://www.gnucash.org/XML/gnc}account"):
        nm = a.find("act:name", ns)
        idv = a.find("act:id", ns)
        if nm is None or idv is None:
            continue
        par = a.find("act:parent", ns)
        by_id[idv.text] = (nm.text, par.text if par is not None else None)

    def full(i):
        parts, cur, seen = [], i, set()
        while cur in by_id and cur not in seen:
            seen.add(cur)
            n, p = by_id[cur]
            parts.append(n)
            cur = p
        parts = list(reversed(parts))
        if parts and parts[0].lower().startswith("root"):
            parts = parts[1:]
        return ":".join(parts)

    return {full(i) for i in by_id}


def final_summary(output_path: str, gnucash_path: str = "") -> str:
    """The single authoritative summary shown to the user, computed from the
    output CSV + review sidecar (NOT from the LLM's narration, which a small
    model gets wrong). Reports the matched total split into parser vs LLM,
    Suspense, and accounts to create."""
    out = Path(output_path)
    review = out.with_name(out.stem + "-review.csv")
    lines = ["**Journals built**"]

    if review.is_file():
        with review.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        if rows:
            def conf(r):
                return (r.get("Confidence") or "").strip()
            parser = [r for r in rows if conf(r) in ("High", "Medium")]
            llm = [r for r in rows if conf(r) == "Override"]
            suspense = [r for r in rows if conf(r) in ("Suspense", "Low")
                        or "Suspense" in (r.get("Credit Account") or "")]
            n = len(rows)
            lines.append(
                f"- Matched: {len(parser) + len(llm)} of {n} "
                f"({len(parser)} by the parser, {len(llm)} resolved by the LLM)"
            )
            susp_line = f"- On Suspense (need manual review): {len(suspense)}"
            if suspense:
                susp_line += " — " + ", ".join(
                    (r.get("Deductor") or "?").strip() for r in suspense)
            lines.append(susp_line)

    existing = _existing_account_paths(gnucash_path) if gnucash_path else None
    if existing is not None and out.is_file():
        with out.open(newline="", encoding="utf-8") as f:
            used = {(r.get("Account") or "").strip() for r in csv.DictReader(f)}
        missing = sorted(a for a in used if a and a not in existing)
        lines.append("- Accounts to create in GnuCash before import: "
                     + (", ".join(missing) if missing else "none"))

    return "\n".join(lines)


def run_apply(xlsx_path: str, gnucash_path: str, output_path: str,
              overrides) -> str:
    """Re-build applying credit-account overrides, then self-verify."""
    norm = _normalize_overrides(overrides)
    if isinstance(norm, str):       # error message
        return norm
    if not norm:
        return "No overrides supplied; the existing CSV is unchanged and valid."
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(norm, f)
        ov_path = f.name
    out = _run_script([xlsx_path, gnucash_path, output_path, ov_path])
    if out.startswith("ERROR"):
        return out
    return out + "\n\n" + _verify(output_path)
