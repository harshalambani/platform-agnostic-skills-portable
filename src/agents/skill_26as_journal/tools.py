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
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

SCRIPT = Path(__file__).parent / "scripts" / "build_tds_journals.py"

# build_tds_journals.py normally runs as a stand-alone subprocess (below), but
# final_summary() needs its read-only reconcile_s194t()/S194TReco directly
# (not scraped from subprocess stdout) so the reco appears in the single
# authoritative summary agent.py returns -- see final_summary's docstring.
# scripts/ is not a package (mirrors that module's own sys.path-insert
# comment for tds_learnings.py), so import it the same way.
_SCRIPTS_DIR = str(Path(__file__).parent / "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)
import build_tds_journals as _BTJ  # noqa: E402


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
    """Accept overrides as a dict (object), a JSON / python-dict string, or
    None, and return {str(sr): account_path}, or an error string. A missing /
    None / empty payload normalizes to {} — a no-op — because some tool-calling
    models invoke this with no arguments, which must degrade gracefully rather
    than hard-fail the run."""
    if overrides is None:
        return {}
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
    # Keys are deductor Sr numbers. Tool-calling models frequently echo the
    # display label ("Sr 7") or pass an int; normalize each key to the bare
    # number so the builder can int() it, and drop any key with no number
    # (rather than let it crash the subprocess with int("Sr 7")).
    out: dict[str, str] = {}
    for k, v in overrides.items():
        if not v:
            continue
        m = re.search(r"\d+", str(k))
        if m:
            out[m.group(0)] = v
    return out


def _gate_ambiguous_overrides(overrides: dict, output_path: str):
    """LLM overrides for an Ambiguous row (two or more tied credit-account
    candidates) are accepted ONLY if the chosen account is one of that row's
    Tied Candidates. An override to any other account is REJECTED before the
    builder subprocess ever runs -- the row keeps its existing first-wins
    account and Needs Review flag, exactly as if no override had been
    supplied for it. This gate applies only to Ambiguous rows: Suspense and
    every other row pass through untouched, and the human Review tab
    (skill_26as_journal's Gradio review UI, `_apply_changes`) is a completely
    separate code path this gate does not reach -- a person can still assign
    an Ambiguous row to any account there.

    Returns (accepted_dict, []) when every requested override is either
    accepted or not subject to the gate (row missing / not Ambiguous), or a
    "REJECTED: ..." string naming each rejected Sr, its deductor and why, if
    at least one override is rejected -- in which case the whole call is
    short-circuited (no overrides are applied) so the caller can report the
    rejection before touching the subprocess or the output file.
    """
    out = Path(output_path)
    review = out.with_name(out.stem + "-review.csv")
    if not review.is_file():
        return overrides, []

    with review.open(newline="", encoding="utf-8") as f:
        rows_by_sr = {(r.get("Sr") or "").strip(): r for r in csv.DictReader(f)}

    accepted: dict[str, str] = {}
    rejections: list[str] = []
    for sr, account in overrides.items():
        row = rows_by_sr.get(str(sr))
        if row is None or (row.get("Confidence") or "").strip() != "Ambiguous":
            accepted[sr] = account
            continue
        tied = [t.strip() for t in (row.get("Tied Candidates") or "").split(";")
                if t.strip()]
        if account in tied:
            accepted[sr] = account
        else:
            deductor = (row.get("Deductor") or "?").strip()
            rejections.append(
                f"Sr {sr} ({deductor}): '{account}' is not one of the tied "
                f"candidates ({', '.join(tied) or 'none listed'}); the row "
                f"keeps its existing account and stays flagged for manual "
                f"review in the Review tab."
            )

    if rejections:
        return "REJECTED:\n" + "\n".join(rejections)
    return accepted, rejections


def run_build(xlsx_path: str, gnucash_path: str, output_path: str,
              partner_comp_configured: bool = False,
              tds_expense_account: str = "") -> str:
    """Deterministic build + self-verify. Returns the summary + verification."""
    args = [xlsx_path, gnucash_path, output_path]
    if partner_comp_configured:
        args.append("--partner-comp-configured")
    if tds_expense_account:
        args += ["--tds-expense-account", tds_expense_account]
    out = _run_script(args)
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


def final_summary(output_path: str, gnucash_path: str = "",
                  xlsx_path: str = "", tds_expense_account: str = "") -> str:
    """The single authoritative summary shown to the user, computed from the
    output CSV + review sidecar (NOT from the LLM's narration, which a small
    model gets wrong). Reports the matched total split into parser vs LLM,
    Suspense, and accounts to create.

    TDS-13: when xlsx_path and tds_expense_account are both given (i.e. this
    entity has partner_comp_accounts configured), also appends the read-only
    s.194T reconciliation result -- computed fresh here via
    build_tds_journals.reconcile_s194t() rather than scraped from the build
    subprocess's stdout, so this stays the single authoritative summary."""
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
            ambiguous = [r for r in rows if conf(r) == "Ambiguous"]
            suspense = [r for r in rows if conf(r) == "Suspense"
                        or "Suspense" in (r.get("Credit Account") or "")]
            n = len(rows)
            lines.append(
                f"- Matched: {len(parser) + len(llm)} of {n} "
                f"({len(parser)} by the parser, {len(llm)} resolved by the LLM)"
            )
            if ambiguous:
                # Ambiguous rows already have a real (first-wins) credit
                # account posted — they are not Suspense and the LLM must
                # not try to resolve them (two or more candidates tied on
                # score); only the user, in the Review tab, can pick the
                # right one from "Tied Candidates".
                amb_line = (f"- Ambiguous (tied candidates — tag manually in "
                            f"the Review tab, not for the LLM): {len(ambiguous)}")
                amb_line += " — " + ", ".join(
                    (r.get("Deductor") or "?").strip() for r in ambiguous)
                lines.append(amb_line)
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

    if xlsx_path and tds_expense_account:
        try:
            reco = _BTJ.reconcile_s194t(Path(xlsx_path), Path(gnucash_path),
                                        tds_expense_account)
        except Exception as e:
            reco = _BTJ.S194TReco(
                applicable=True, status="VARIANCE",
                message=f"could not complete the s.194T reconciliation: {e}",
            )
        if reco is not None:
            marker = "*** " if reco.loud else ""
            lines.append(f"- {marker}s.194T reconciliation (26AS vs Partner "
                         f"Comp journal): {reco.status} -- {reco.message}")

    return "\n".join(lines)


def run_apply(xlsx_path: str, gnucash_path: str, output_path: str,
              overrides, partner_comp_configured: bool = False,
              tds_expense_account: str = "") -> str:
    """Re-build applying credit-account overrides, then self-verify."""
    norm = _normalize_overrides(overrides)
    if isinstance(norm, str):       # error message
        return norm
    if not norm:
        return "No overrides supplied; the existing CSV is unchanged and valid."
    gated = _gate_ambiguous_overrides(norm, output_path)
    if isinstance(gated, str):      # one or more overrides rejected
        return gated
    norm, _rejections = gated
    if not norm:
        return "No overrides supplied; the existing CSV is unchanged and valid."
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as f:
        json.dump(norm, f)
        ov_path = f.name
    args = [xlsx_path, gnucash_path, output_path, ov_path]
    if partner_comp_configured:
        args.append("--partner-comp-configured")
    if tds_expense_account:
        args += ["--tds-expense-account", tds_expense_account]
    out = _run_script(args)
    if out.startswith("ERROR"):
        return out
    return out + "\n\n" + _verify(output_path)
