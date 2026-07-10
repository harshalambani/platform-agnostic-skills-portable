#!/usr/bin/env python3
"""
matrix_recon.py -- OPTIONAL all-family intercompany matrix. DIRECT mode, no LLM.

Reconciles EVERY unordered pair among N GnuCash books by reusing the pairwise
engine (reconcile_intercompany.reconcile), then rolls the results up into one
workbook:

    Matrix          -- owner x owner grid of the balance difference per pair
    Pairs           -- one row per pair (balances, difference, counts, status)
    All Exceptions  -- every unmatched item across all pairs, with best hint

Pairs with no mutual contra accounts are reported as "n/a" (not an error).

Run standalone:
    python matrix_recon.py OUT.xlsx BOOK1 BOOK2 [BOOK3 ...] \
        [--period "FY 2025-26" | --start YYYY-MM-DD --end YYYY-MM-DD] [--tol 7]

Exit codes: 0 = every reconciled pair ties, 2 = at least one out of balance /
has exceptions, 1 = error.
"""
from __future__ import annotations

import argparse
import sys
from itertools import combinations
from pathlib import Path

# Reuse the pairwise engine from the sibling skill's scripts folder.
_PAIR_SCRIPTS = (Path(__file__).resolve().parents[2]
                 / "skill_gnucash_intercompany" / "scripts")
if str(_PAIR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_PAIR_SCRIPTS))
if str(Path(__file__).parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).parent))

from reconcile_intercompany import reconcile, derive_owner_and_fy  # noqa: E402


def run_matrix(paths: list[str], period: str = "", start: str = "",
               end: str = "", tol_days: int = 7) -> dict:
    """Reconcile every unordered pair. Returns a dict for the workbook writer."""
    owners = [derive_owner_and_fy(Path(p))[0] for p in paths]
    pair_results: list[dict] = []
    fy_label = ""
    for (ia, pa), (ib, pb) in combinations(list(enumerate(paths)), 2):
        entry = {"ia": ia, "ib": ib, "a_owner": owners[ia], "b_owner": owners[ib]}
        try:
            res = reconcile(pa, pb, period, start, end, tol_days)
            entry.update(ok=True, res=res)
            entry["a_owner"] = res["book_a"].owner
            entry["b_owner"] = res["book_b"].owner
            owners[ia], owners[ib] = entry["a_owner"], entry["b_owner"]
            fy_label = fy_label or res["fy_label"]
        except Exception as e:  # noqa: BLE001  -- no mutual contra, bad period, etc.
            entry.update(ok=False, err=str(e))
        pair_results.append(entry)
    return {
        "paths": paths, "owners": owners, "pairs": pair_results,
        "fy_label": fy_label or period or "(period undetermined)",
        "tol_days": tol_days,
    }


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="All-family intercompany matrix")
    ap.add_argument("out_xlsx")
    ap.add_argument("books", nargs="+")
    ap.add_argument("--period", default="")
    ap.add_argument("--start", default="")
    ap.add_argument("--end", default="")
    ap.add_argument("--tol", type=int, default=7)
    args = ap.parse_args(argv)

    if len(args.books) < 2:
        print("ERROR: need at least two .gnucash books.", file=sys.stderr)
        return 1

    m = run_matrix(args.books, args.period, args.start, args.end, args.tol)

    from matrix_report import write_matrix_workbook
    write_matrix_workbook(m, args.out_xlsx)

    ok = [p for p in m["pairs"] if p.get("ok")]
    na = [p for p in m["pairs"] if not p.get("ok")]
    out_of_balance = [p for p in ok if p["res"]["difference"] != 0]
    with_exc = [p for p in ok if p["res"]["a_exc"] or p["res"]["b_exc"]]

    print(f"Intercompany matrix  [{m['fy_label']}]  --  {len(m['paths'])} books, "
          f"{len(m['pairs'])} pairs")
    for p in ok:
        r = p["res"]
        tie = "TIES" if r["difference"] == 0 else f"OUT by {r['difference']:,.2f}"
        print(f"  {p['a_owner']:<22} <-> {p['b_owner']:<22} "
              f"matched={len(r['pairs']):>2} exc={len(r['a_exc'])}/{len(r['b_exc'])}  {tie}")
    for p in na:
        print(f"  {p['a_owner']:<22} <-> {p['b_owner']:<22} n/a ({p['err']})")
    print(f"Summary: {len(ok)} reconciled, {len(na)} n/a, "
          f"{len(out_of_balance)} out of balance, {len(with_exc)} with exceptions")
    print(f"Workbook: {args.out_xlsx}")

    return 0 if (not out_of_balance and not with_exc) else 2


if __name__ == "__main__":
    sys.exit(main())
