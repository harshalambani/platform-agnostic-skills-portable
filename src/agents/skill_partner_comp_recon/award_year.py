"""H35-14 -- award-year documents for prior-year incentive instalments.

An incentive instalment paid in the reporting year was AWARDED in an earlier
year, and its figures (gross, firm's tax, capital deducted) are printed in
the Compensation summary (Advisory) of that AWARD year -- not in the
reporting year's. This module reads any number of such documents, picks the
right one per award year, and checks each paid instalment against it.

Three different years are kept apart everywhere here and in the output:
  * award FY      -- the year whose Advisory carries the instalment;
  * firm's-tax FY -- the year the schedule's firm's-tax row is charged to,
                     which is the PAYMENT year (it is that year's schedule);
  * payment FY    -- the year the cash was paid.

Recon only. Nothing here reaches the journal.

Rules that are never relaxed:
  * the award year is read from the document's BODY, never its file name;
  * a document for any other year is never used for a cohort;
  * the latest revision wins, found from the letter date, else an explicit
    revision number; when neither settles it the row says CANNOT RECONCILE
    and names the problem -- it never picks one;
  * a Target compensation letter is recorded but is not an instalment
    source;
  * a missing document is a named CANNOT RECONCILE, never an AGREE.
"""
from __future__ import annotations

import re
from datetime import date, datetime

from . import precheck as _precheck

KIND_ADVISORY = "compensation_summary"
KIND_TARGET = "target_comp_letter"

_DATE_RE = re.compile(
    r"\bdate[d]?\s*[:\-]?\s*(\d{1,2})[\s/\-.]+([A-Za-z]{3,9}|\d{1,2})[\s/\-.,]+(\d{4})",
    re.IGNORECASE,
)
_REVISION_RE = re.compile(
    r"\b(?:revision|revised|rev\.?|version)\s*(?:no\.?|number)?\s*[:#\-]?\s*(\d+)\b",
    re.IGNORECASE,
)


def parse_letter_date(text: str) -> str | None:
    """ISO date of the letter, from a 'Date : 12 April 2025' style line, or
    None. Best effort: None means 'not found', never a guess."""
    m = _DATE_RE.search(text or "")
    if not m:
        return None
    d, mon, y = m.group(1), m.group(2), m.group(3)
    try:
        if mon.isdigit():
            return date(int(y), int(mon), int(d)).isoformat()
        return datetime.strptime(f"{d} {mon[:3].title()} {y}", "%d %b %Y").date().isoformat()
    except ValueError:
        return None


def parse_revision(text: str) -> int | None:
    m = _REVISION_RE.search(text or "")
    return int(m.group(1)) if m else None


def read_award_year_document(path: str, password: str | None, advisory_parser) -> dict:
    """Read one award-year document into a plain dict. Never raises: a file
    that cannot be read comes back with `error` set so the run can name it."""
    from pathlib import Path  # noqa: PLC0415
    name = Path(path).name
    doc = {"name": name, "kind": "unknown", "fy": None, "letter_date": None,
           "revision": None, "instalments": None, "error": None}
    text = _precheck.read_first_page_text(path, password)
    if text is None:
        doc["error"] = (_precheck.pdf_open_problem(path, password)
                        or "could not be opened (not a readable PDF)")
        return doc
    doc["kind"] = _precheck.classify_advisory_text(text)
    doc["letter_date"] = parse_letter_date(text)
    doc["revision"] = parse_revision(text)
    if doc["kind"] == KIND_TARGET:
        return doc
    try:
        rec = advisory_parser.parse(path, password)
    except Exception as e:  # noqa: BLE001
        doc["error"] = f"could not be parsed ({_precheck.explain_pdf_error(e, password)})"
        return doc
    doc["fy"] = rec.get("financial_year")
    # Numbered instalments plus the "Arrears for FY .." line: a cohort payment
    # can be the arrears and an instalment paid together. Opening, TOTAL,
    # additions and projected-closing rows never come through here.
    doc["instalments"] = [
        i for i in (rec.get("schedule_instalments") or [])
        if i.get("instalment_no") is not None or _is_arrears(i)
    ]
    return doc


def _is_arrears(line: dict) -> bool:
    return (line.get("instalment_no") is None
            and str(line.get("label") or "").strip().lower().startswith("arrears"))


def _line_name(a: dict) -> str:
    if a.get("instalment_no") is not None:
        n = a["instalment_no"]
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n if n % 100 not in (11, 12, 13) else 0, "th")
        return f"{n}{suffix} instalment"
    return "Arrears"


def _line_figs(a: dict):
    g = _abs(a.get("gross")) or 0.0
    t = _abs(a.get("firms_tax")) or 0.0
    c = _abs(a.get("capital_contribution")) or 0.0
    n = _abs(a.get("net"))
    return g, t, c, (n if n is not None else round(g - t - c, 2))


def _yielded(adv: list[dict]) -> str:
    if not adv:
        return "none parsed"
    parts = []
    for a in adv:
        g, t, c, _n = _line_figs(a)
        parts.append(f"{_line_name(a)} gross {g:,.2f}, firm's tax {t:,.2f}, capital {c:,.2f}")
    return "; ".join(parts)


def _fingerprint(doc: dict):
    return tuple(
        (i.get("instalment_no"), i.get("gross"), i.get("firms_tax"),
         i.get("capital_contribution"), i.get("net"))
        for i in (doc.get("instalments") or [])
    )


def select_award_year_documents(docs: list[dict] | None) -> dict:
    """{award_fy: {"status", "doc", "note"}} -- pure.

    status: "ok" (doc is set), "ambiguous" (several differing documents and
    the latest cannot be determined), "target_only" (only Target letters),
    never anything that guesses."""
    by_fy: dict = {}
    for d in docs or []:
        if d.get("error") or not d.get("fy"):
            continue
        by_fy.setdefault(d["fy"], []).append(d)
    out: dict = {}
    for fy, group in by_fy.items():
        usable = [d for d in group if d.get("kind") != KIND_TARGET and d.get("instalments")]
        targets = [d for d in group if d.get("kind") == KIND_TARGET]
        if not usable:
            out[fy] = {"status": "target_only" if targets else "none", "doc": None,
                       "note": "only a Target compensation letter was supplied for this year"}
            continue
        distinct: dict = {}
        for d in usable:
            distinct.setdefault(_fingerprint(d), d)
        cands = list(distinct.values())
        if len(cands) == 1:
            out[fy] = {"status": "ok", "doc": cands[0], "note": cands[0]["name"]}
            continue
        names = ", ".join(d["name"] for d in cands)
        picked = None
        why = ""
        dates = [d.get("letter_date") for d in cands]
        if all(dates):
            top = max(dates)
            if dates.count(top) == 1:
                picked, why = cands[dates.index(top)], "latest letter date"
        if picked is None:
            revs = [d.get("revision") for d in cands]
            if all(r is not None for r in revs):
                top = max(revs)
                if revs.count(top) == 1:
                    picked, why = cands[revs.index(top)], "highest revision number"
        if picked is None:
            out[fy] = {"status": "ambiguous", "doc": None,
                       "note": f"{len(cands)} different documents for this year ({names}) and "
                               "the latest revision cannot be determined from their dates or "
                               "revision numbers"}
        else:
            out[fy] = {"status": "ok", "doc": picked,
                       "note": f"{picked['name']} ({why}; {len(cands)} documents supplied)"}
    return out


def _abs(v):
    return None if v is None else abs(float(v))


def award_year_rows(cohorts_raw: list[dict], reporting_instalments: list, docs: list[dict] | None):
    """One reconciliation row per instalment paid in the reporting year, plus
    one named row per cohort that has no usable award-year document."""
    from .engine import CANNOT_RECONCILE, RECONCILIATION_TOLERANCE, ReconciliationResult  # noqa: PLC0415

    tol = RECONCILIATION_TOLERANCE
    selections = select_award_year_documents(docs)
    rows = []
    for cohort in cohorts_raw:
        award_fy = cohort["award_fy"]
        paid = [i for i in reporting_instalments if i.award_fy == award_fy and i.gross is not None]
        if not paid:
            continue
        sel = selections.get(award_fy)
        base = f"Award-year check (FY{award_fy} award)"
        if sel is None or sel["status"] != "ok":
            if sel is None:
                why = (f"no award-year document for FY{award_fy} was supplied; the FY{award_fy} "
                       "Compensation summary is needed (any other year's cannot stand in)")
            elif sel["status"] == "ambiguous":
                why = sel["note"]
            else:
                why = (f"only a Target compensation letter was supplied for FY{award_fy}; it "
                       "does not carry the instalment schedule, so it cannot be used here")
            rows.append(ReconciliationResult(
                category=f"{base}: paid instalments vs award-year Advisory",
                sources={"Award-year Advisory": None,
                         "Payment schedule (paid this year)": round(sum(abs(i.gross) for i in paid), 2)},
                agree=None,
                note=f"{CANNOT_RECONCILE} -- {why}.",
            ))
            continue
        doc = sel["doc"]
        adv = doc["instalments"]
        used: set = set()  # an Advisory line is used at most once per cohort
        for i in sorted(paid, key=lambda r: r.payment_date):
            pay_fy = i.instalment_fy
            cat = (f"{base}: instalment paid {i.payment_date.isoformat()} "
                   f"(payment FY{pay_fy})")
            sched = {"gross": _abs(i.gross), "tax": _abs(i.firms_tax) or 0.0,
                     "capital": _abs(i.capital) or 0.0}
            sched["net"] = (_abs(i.net) if i.net is not None
                            else round(sched["gross"] - sched["tax"] - sched["capital"], 2))
            avail = [k for k in range(len(adv)) if k not in used]

            def _fits(figs, s=sched):
                return all(abs(figs[x] - s[k]) <= tol for x, k in enumerate(("gross", "tax", "capital", "net")))

            # 1. a single Advisory line that agrees on all four components wins
            single = [k for k in avail if adv[k].get("gross") is not None and _fits(_line_figs(adv[k]))]
            if len(single) >= 1:
                used.add(single[0])
                a = adv[single[0]]
                rows.append(ReconciliationResult(
                    category=cat,
                    sources={"Award-year Advisory (gross - tax - capital)":
                             round(_line_figs(a)[0] - _line_figs(a)[1] - _line_figs(a)[2], 2),
                             "Payment schedule (gross - tax - capital)":
                             round(sched["gross"] - sched["tax"] - sched["capital"], 2)},
                    agree=True,
                    note=("Gross, firm's tax and capital agree. "
                          f"award FY{award_fy}, firm's-tax FY{pay_fy} (payment year), payment FY{pay_fy}; "
                          f"implied firm's-tax rate: schedule "
                          f"{(sched['tax'] / sched['gross'] if sched['gross'] else 0.0):.2%}, Advisory "
                          f"{(_line_figs(a)[1] / _line_figs(a)[0] if _line_figs(a)[0] else 0.0):.2%}; "
                          f"Advisory {_line_name(a)} in {doc['name']}."),
                ))
                continue
            # 2. otherwise a combination of 2-3 lines (arrears + instalment rows)
            #    that agrees on gross, firm's tax, capital AND net
            from itertools import combinations  # noqa: PLC0415
            combos = []
            for size in (2, 3):
                for c in combinations([k for k in avail if adv[k].get("gross") is not None], size):
                    figs = [sum(_line_figs(adv[k])[x] for k in c) for x in range(4)]
                    if _fits(figs):
                        combos.append(c)
            if len(combos) == 1:
                c = combos[0]
                used.update(c)
                names = " + ".join(_line_name(adv[k]) for k in c)
                rows.append(ReconciliationResult(
                    category=cat,
                    sources={"Award-year Advisory (gross - tax - capital)":
                             round(sched["gross"] - sched["tax"] - sched["capital"], 2),
                             "Payment schedule (gross - tax - capital)":
                             round(sched["gross"] - sched["tax"] - sched["capital"], 2)},
                    agree=True,
                    note=(f"Gross, firm's tax, capital and net agree with the sum of {names} "
                          f"(paid together as one payment). award FY{award_fy}, firm's-tax "
                          f"FY{pay_fy} (payment year), payment FY{pay_fy}; in {doc['name']}."),
                ))
                continue
            if len(combos) > 1:
                alts = " or ".join(" + ".join(_line_name(adv[k]) for k in c) for c in combos)
                rows.append(ReconciliationResult(
                    category=cat,
                    sources={"Award-year Advisory (gross)": None,
                             "Payment schedule (gross)": sched["gross"]},
                    agree=None,
                    note=(f"{CANNOT_RECONCILE} -- ambiguous: lines {alts} each add up to this "
                          f"payment, so which is meant cannot be told ({doc['name']}). "
                          f"Advisory yielded: {_yielded(adv)}."),
                ))
                continue
            hits = [a for a in (adv[k] for k in avail)
                    if a.get("gross") is not None and abs(abs(a["gross"]) - abs(i.gross)) <= tol]
            if len(hits) > 1:
                # Several instalments share this gross: prefer the one whose
                # tax and capital also agree; if none does, we cannot tell
                # which one this payment is, so it is not guessed.
                full = [a for a in hits
                        if abs((_abs(a.get("firms_tax")) or 0.0) - sched["tax"]) <= tol
                        and abs((_abs(a.get("capital_contribution")) or 0.0) - sched["capital"]) <= tol]
                if full:
                    hits = full[:1]
            if len(hits) != 1:
                reason = ("no instalment in the award-year Advisory has that gross"
                          if not hits else
                          f"{len(hits)} instalments in the award-year Advisory have that gross "
                          "and none agrees on firm's tax and capital, so which one this payment "
                          "is cannot be told")
                rows.append(ReconciliationResult(
                    category=cat,
                    sources={"Award-year Advisory (gross)": None,
                             "Payment schedule (gross)": sched["gross"]},
                    agree=None,
                    note=(f"{CANNOT_RECONCILE} -- {reason} ({doc['name']}). "
                          f"Advisory yielded: {_yielded(adv)}."),
                ))
                continue
            a = hits[0]
            used.add(adv.index(a))
            adv_tax = _abs(a.get("firms_tax")) or 0.0
            adv_cap = _abs(a.get("capital_contribution")) or 0.0
            gaps = []
            if abs(adv_tax - sched["tax"]) > tol:
                gaps.append(f"firm's tax: Advisory {adv_tax:,.2f} vs schedule {sched['tax']:,.2f}")
            if abs(adv_cap - sched["capital"]) > tol:
                gaps.append(f"capital deducted: Advisory {adv_cap:,.2f} vs schedule {sched['capital']:,.2f}")
            rate_s = (sched["tax"] / sched["gross"]) if sched["gross"] else 0.0
            rate_a = (adv_tax / abs(a["gross"])) if a.get("gross") else 0.0
            detail = (f"award FY{award_fy}, firm's-tax FY{pay_fy} (payment year), payment FY{pay_fy}; "
                      f"implied firm's-tax rate: schedule {rate_s:.2%}, Advisory {rate_a:.2%}; "
                      f"Advisory instalment {a.get('instalment_no')} in {doc['name']}")
            rows.append(ReconciliationResult(
                category=cat,
                sources={"Award-year Advisory (gross - tax - capital)":
                         round(abs(a["gross"]) - adv_tax - adv_cap, 2),
                         "Payment schedule (gross - tax - capital)":
                         round(sched["gross"] - sched["tax"] - sched["capital"], 2)},
                agree=not gaps,
                note=(("DIFFERS -- " + "; ".join(gaps) + ". ") if gaps else "Gross, firm's tax and capital agree. ")
                     + detail + ".",
            ))
    return rows
