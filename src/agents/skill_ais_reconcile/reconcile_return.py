"""
reconcile_return.py -- ITR-21: the fifth check. Ties the FILED RETURN out
against the AIS (and the books, and the 26AS when given). Pure functions over
already-parsed inputs: no I/O, and no import of the return parser (the return
is read through the attributes of the FiledReturn the agent layer passes in).

WHAT IS COMPARED
  * Per head (salary, interest, dividend): return vs AIS, plus books where a
    book was given. The AIS and books figures are the ones the Books
    Reconciliation check already produced (reconcile_books), taken from its
    report or from the same _ais_by_category helper -- never recomputed here --
    so a payer reported under both the TDS and SFT sections is counted once.
  * Per deductor (TAN): TDS claimed in the return vs the AIS TDS credit, and
    vs the 26AS when a 26AS workbook was given.
  * Partner (section 194T / head BP) rows are a separate block, compared to
    the AIS 194T rows only; they are never folded into other sources.

NEVER GUESSED
  * A return figure that could not be read is None and is flagged, not tied.
  * A TDS row whose credit belongs to someone else (TDSCreditName != "S") is
    listed and flagged "credit in another person's hands", and is NOT added to
    the amount claimed.
  * Salary TDS in an ITR-1 has no verified layout: AIS TANs that carry only
    salary TDS are noted, not compared, for an ITR-1.
  * The tolerance is the one the other checks use (reconcile.TOLERANCE).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from agents.skill_ais_reconcile import ais_categories as AC
from agents.skill_ais_reconcile.reconcile import TDS_CREDIT_FIELD_CANDIDATES, TOLERANCE, _as_decimal
from agents.skill_ais_reconcile import reconcile_books as RB
from agents.skill_ais_reconcile.reconcile_26as import _classify_ais_category

NOT_EXTRACTED = "NOT-EXTRACTED"
ZERO = Decimal("0")
_PARTNER_SECTIONS = {"94T", "194T"}
_HEAD_LABELS = {"salary": "Salary (gross)", "interest": "Interest", "dividend": "Dividend"}
_HEAD_ORDER = ("salary", "interest", "dividend")
# the real AIS tdsTcs category for section 194T receipts (matched whole, case/whitespace-normalised)
_PARTNER_CATEGORY = "receipt of amount by partners from partnership firm"


@dataclass
class HeadTieOut:
    head: str
    label: str
    books: Decimal | None       # None = no book given
    ret: Decimal | None         # None = the return figure could not be read
    ais: Decimal
    delta_ais: Decimal | None   # return - AIS
    delta_books: Decimal | None  # return - books
    flagged: bool
    note: str = ""


@dataclass
class DeductorTieOut:
    tan: str
    name: str
    section: str
    claimed: Decimal | None     # None = claimed amount could not be read
    ais: Decimal
    as26: Decimal | None        # None = no 26AS workbook given
    flagged: bool
    note: str = ""


@dataclass
class TcsTieOut:
    """ITR-22: AIS TCS (tax collected from the taxpayer) vs the return's TCS
    schedule total. Total only: the return's per-collector rows carry no
    verified 'claimed this year' column, so a per-TAN comparison is not made."""
    ais: Decimal
    ret: Decimal | None          # None = the return states no readable TCS total
    by_tan: list = field(default_factory=list)    # (tan, name, amount) from the AIS
    flagged: bool = False
    note: str = ""


@dataclass
class ReturnTieOut:
    form: str = ""
    tcs: TcsTieOut | None = None
    # ITR-22: unrecognised AIS tdsTcs categories (label, rows): in no total
    uncounted_tds: list = field(default_factory=list)
    heads: list = field(default_factory=list)
    deductors: list = field(default_factory=list)
    partner_rows: list = field(default_factory=list)
    informational: list = field(default_factory=list)   # (label, text)
    not_extracted: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    books_given: bool = False
    as26_given: bool = False

    @property
    def flagged_heads(self) -> list[str]:
        return [h.head for h in self.heads if h.flagged]

    @property
    def flagged_deductors(self) -> list[str]:
        return [d.tan for d in self.deductors + self.partner_rows if d.flagged]

    @property
    def flagged_count(self) -> int:
        return len(self.flagged_heads) + len(self.flagged_deductors) + (1 if self.tcs is not None and self.tcs.flagged else 0)


# --------------------------------------------------------------------------
# guard
# --------------------------------------------------------------------------

def check_return_guard(fr: Any, entity_pan: str, year_key: str) -> str | None:
    """None when the return may be tied out; otherwise an ERROR line (the
    caller skips the tie-out and writes no sheet). The return's PAN must be the
    entity's PAN and, when the return states one, its assessment year must be
    the FY end year (FY 2025-26 -> AY 2026)."""
    if getattr(fr, "source_format", "") != "json":
        return ("ERROR: the filed return is a PDF, which carries no per-head figures or TDS rows "
                "this check can use; pick the return's JSON. Return tie-out skipped.")
    pan = str(getattr(fr, "pan", NOT_EXTRACTED) or NOT_EXTRACTED).strip().upper()
    want = (entity_pan or "").strip().upper()
    if pan == NOT_EXTRACTED or not want:
        return ("ERROR: could not confirm whose return this is (PAN missing from the return or "
                "from entities.yaml). Return tie-out skipped.")
    if pan != want:
        return (f"ERROR: the filed return is for a different PAN than the selected entity's "
                f"({pan[:3]}XXXX{pan[-1:]} vs {want[:3]}XXXX{want[-1:]}). Return tie-out skipped.")
    ay = str(getattr(fr, "assessment_year", NOT_EXTRACTED) or NOT_EXTRACTED).strip()
    if ay == NOT_EXTRACTED:
        return ("ERROR: the filed return does not state its assessment year, so it cannot be tied "
                "to this FY. Return tie-out skipped.")
    try:
        fy_end = int(str(year_key).split("-")[0]) + 1
    except ValueError:
        return f"ERROR: cannot read the financial year {year_key!r}. Return tie-out skipped."
    if ay[:4] != str(fy_end):
        return (f"ERROR: the filed return is for assessment year {ay}, but the AIS is for "
                f"FY {year_key} (assessment year {fy_end}). Return tie-out skipped.")
    return None


# --------------------------------------------------------------------------
# return side
# --------------------------------------------------------------------------

def _num(v: Any) -> Decimal | None:
    if isinstance(v, bool) or not isinstance(v, (int, float, Decimal)):
        return None
    return Decimal(str(v))


def _sum_keys(block: dict, keys: tuple, label: str, ne: list) -> Decimal | None:
    vals = []
    for k in keys:
        v = _num(block.get(k))
        if v is None:
            ne.append(f"{label}: {k}")
            return None
        vals.append(v)
    return sum(vals, ZERO)


def _return_heads(fr: Any, ne: list, info: list) -> dict:
    so = getattr(fr, "schedule_os", {}) or {}
    form = getattr(fr, "itr_form", "")
    out: dict = {}

    gross = _num((getattr(fr, "salary", {}) or {}).get("gross_salary"))
    if gross is None:
        ne.append("salary: gross salary")
    out["salary"] = gross

    if not so:
        ne.append("interest: other-sources schedule")
        ne.append("dividend: other-sources schedule")
        out["interest"] = out["dividend"] = None
        return out
    keys = ("interest_sb", "interest_td", "interest_refund") + (() if form == "ITR1" else ("interest_other",))
    out["interest"] = _sum_keys(so, keys, "interest", ne)
    if out["interest"] is not None:
        info.append(("Interest in the return, by kind",
                     ", ".join(f"{k.replace('interest_', '')} {_num(so[k]):,.2f}" for k in keys)))

    div = _num(so.get("dividend_gross"))
    if div is None:
        a, b = _num(so.get("dividend_oth_than_22e")), _num(so.get("dividend_22e"))
        if a is not None and b is not None:
            div = a + b
        else:
            ne.append("dividend: dividend total")
    out["dividend"] = div
    if form == "ITR1" and _num(so.get("other_income")):
        info.append(("Other income in the return (ITR-1 code OTH)", f"{_num(so['other_income']):,.2f} -- not compared"))
    return out


# --------------------------------------------------------------------------
# AIS side, per TAN
# --------------------------------------------------------------------------

def _ais_by_tan(normalized: Any):
    """(credit by TAN, names, head categories by TAN, 194T credit by TAN)."""
    names_by_el = RB._element_names(normalized) if normalized is not None else {}
    credit: dict[str, Decimal] = {}
    names: dict[str, str] = {}
    cats: dict[str, set] = {}
    t194: dict[str, Decimal] = {}
    if normalized is None:
        return credit, names, cats, t194
    # ITR-22: only known TDS-credit categories; TCS / payer-side / unknown
    # rows are handled by _ais_other_by_tan and never summed here.
    for r in AC.credit_rows(AC.tdstcs_l1_rows(normalized)):
        tan = str(r.info_src_id or "").strip().upper()
        if not tan:
            continue
        amt = None
        for cand in TDS_CREDIT_FIELD_CANDIDATES:
            amt = _as_decimal(r.fields.get(cand))
            if amt is not None:
                break
        if " ".join((r.category or "").lower().split()) == _PARTNER_CATEGORY:
            t194[tan] = t194.get(tan, ZERO) + (amt or ZERO)
            names.setdefault(tan, names_by_el.get(RB._element_key(r), "").title())
            continue
        credit[tan] = credit.get(tan, ZERO) + (amt or ZERO)
        cats.setdefault(tan, set()).add(_classify_ais_category(r.category))
        nm = names_by_el.get(RB._element_key(r))
        if nm and not names.get(tan):
            names[tan] = nm.title()
    return credit, names, cats, t194


def _row_amount(r: Any) -> Decimal:
    for cand in TDS_CREDIT_FIELD_CANDIDATES:
        amt = _as_decimal(r.fields.get(cand))
        if amt is not None:
            return amt
    return ZERO


def _ais_other_by_tan(normalized: Any):
    """(tcs rows by TAN, payer-side rows by TAN) -> {tan: [name, amount]}."""
    names_by_el = RB._element_names(normalized) if normalized is not None else {}
    tcs: dict[str, list] = {}
    payer: dict[str, list] = {}
    rows = AC.tdstcs_l1_rows(normalized)
    for kind, bucket in ((AC.TCS, tcs), (AC.PAYER_SIDE, payer)):
        for r in AC.rows_of_kind(rows, kind):
            tan = str(r.info_src_id or "").strip().upper() or "(no TAN)"
            ent = bucket.setdefault(tan, ["", ZERO])
            ent[0] = ent[0] or names_by_el.get(RB._element_key(r), "").title()
            ent[1] += _row_amount(r)
    return tcs, payer


def _tcs_tieout(fr: Any, ais_tcs: dict) -> TcsTieOut | None:
    a = sum((v[1] for v in ais_tcs.values()), ZERO)
    by_tan = [(t, v[0], v[1]) for t, v in sorted(ais_tcs.items())]
    ret = _num(getattr(fr, "tcs_total", None))
    if ret is None:
        if a == ZERO:
            return None
        return TcsTieOut(ais=a, ret=None, by_tan=by_tan, flagged=True,
                         note="TCS in the AIS, but the return has no readable TCS schedule total")
    t = TcsTieOut(ais=a, ret=ret, by_tan=by_tan)
    if abs(ret - a) > TOLERANCE:
        t.flagged = True
        t.note = ("TCS in the AIS, not in the return's TCS schedule" if ret == ZERO
                  else "TCS in the return, absent from the AIS" if a == ZERO
                  else "TCS in the return differs from the AIS")
    return t


def _as26_by_tan(as26: Any):
    dep: dict[str, Decimal] = {}
    names: dict[str, str] = {}
    for t in getattr(as26, "transactions", []) or []:
        if str(t.section or "").strip().upper() in _PARTNER_SECTIONS:
            continue
        tan = str(t.tan or "").strip().upper()
        dep[tan] = dep.get(tan, ZERO) + (_num(t.tds_deposited) or ZERO)
        names.setdefault(tan, t.deductor_name or "")
    return dep, names


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _head_row(head: str, ret: Decimal | None, ais: Decimal, books: Decimal | None) -> HeadTieOut:
    notes: list[str] = []
    flagged = False
    d_ais = d_books = None
    if ret is None:
        flagged = True
        notes.append("return figure NOT-EXTRACTED -- cannot tie out")
    else:
        d_ais = ret - ais
        if abs(d_ais) > TOLERANCE:
            flagged = True
            if ret != ZERO and ais == ZERO:
                notes.append("declared in the return, absent from the AIS")
            elif ais != ZERO and ret == ZERO:
                notes.append("in the AIS, not declared in the return")
            else:
                notes.append("return differs from the AIS")
        if books is not None:
            d_books = ret - books
            if abs(d_books) > TOLERANCE:
                flagged = True
                notes.append("return differs from the books")
    return HeadTieOut(head=head, label=_HEAD_LABELS[head], books=books, ret=ret, ais=ais,
                      delta_ais=d_ais, delta_books=d_books, flagged=flagged, note="; ".join(notes))


def reconcile_return(fr: Any, report: Any, normalized: Any = None,
                     books_report: Any = None, as26: Any = None) -> ReturnTieOut:
    out = ReturnTieOut(form=getattr(fr, "itr_form", ""), books_given=books_report is not None,
                       as26_given=as26 is not None)
    ne: list[str] = list(getattr(fr, "return_not_extracted", []) or [])

    # ---- heads: AIS / books figures are the Books Reconciliation's own ----
    if books_report is not None:
        ais_cat = {c.category: c.ais_income for c in books_report.categories}
        books_cat = {c.category: c.books_income for c in books_report.categories}
        out.notes.extend(books_report.source_notes)
    else:
        total, _tds, _sft, notes, _poss = RB._ais_by_category(report, normalized)
        ais_cat, books_cat = dict(total), None
        out.notes.extend(notes)
    ret_heads = _return_heads(fr, ne, out.informational)
    for head in _HEAD_ORDER:
        ret = ret_heads.get(head)
        a = ais_cat.get(head, ZERO)
        b = None if books_cat is None else books_cat.get(head, ZERO)
        if ret is None or ret != ZERO or a != ZERO or (b is not None and b != ZERO):
            out.heads.append(_head_row(head, ret, a, b))

    # ---- deductors ----
    ais_credit, ais_names, ais_cats, ais_194t = _ais_by_tan(normalized)
    as26_dep, as26_names = _as26_by_tan(as26) if as26 is not None else (None, {})

    claimed: dict[str, Decimal] = {}
    claimed_ne: set[str] = set()
    other_hands: dict[str, list] = {}
    meta: dict[str, dict] = {}
    partner: dict[str, dict] = {}
    for row in getattr(fr, "tds_rows", []) or []:
        tan = str(row.get("tan") or NOT_EXTRACTED).strip().upper()
        section = str(row.get("section") or "").strip().upper()
        is_partner = section in _PARTNER_SECTIONS or str(row.get("head") or "").upper() == "BP"
        bucket = partner if is_partner else meta
        info = bucket.setdefault(tan, {"name": "", "sections": [], "owner_other": False,
                                       "claimed": ZERO, "ne": False})
        info["name"] = info["name"] or row.get("name", "")
        if section and section not in info["sections"]:
            info["sections"].append(section)
        if str(row.get("credit_owner") or "") != "S":
            info["owner_other"] = True      # listed and flagged, NOT added to claimed
            continue
        c = _num(row.get("claimed"))
        if c is None:
            info["ne"] = True
        else:
            info["claimed"] += c

    itr1 = getattr(fr, "itr_form", "") == "ITR1"
    tans = set(meta) | {t for t, v in ais_credit.items() if v != ZERO}
    if as26_dep is not None:
        tans |= {t for t, v in as26_dep.items() if v != ZERO}
    for tan in sorted(tans):
        info = meta.get(tan, {"name": "", "sections": [], "owner_other": False, "claimed": ZERO, "ne": False})
        a = ais_credit.get(tan, ZERO)
        if (itr1 and tan not in meta and ais_cats.get(tan) == {"salary"}):
            out.notes.append(f"{tan}: AIS salary TDS {a:,.2f} not compared (ITR-1 salary TDS layout is unverified)")
            continue
        c = None if info["ne"] else info["claimed"]
        notes: list[str] = []
        flagged = False
        if info["owner_other"]:
            flagged = True
            notes.append("credit in another person's hands -- listed, not compared")
        if info["ne"]:
            flagged = True
            notes.append("claimed amount NOT-EXTRACTED")
        else:
            if abs(c - a) > TOLERANCE:
                flagged = True
                if c == ZERO and not info["owner_other"]:
                    notes.append("TDS in the AIS, not claimed in the return")
                elif tan not in ais_credit or a == ZERO:
                    notes.append("claimed in the return, absent from the AIS")
                else:
                    notes.append("claimed differs from the AIS")
            if as26_dep is not None:
                s = as26_dep.get(tan, ZERO)
                if abs(c - s) > TOLERANCE:
                    flagged = True
                    notes.append("claimed differs from the 26AS")
        out.deductors.append(DeductorTieOut(
            tan=tan, name=info["name"] or ais_names.get(tan) or as26_names.get(tan, ""),
            section=", ".join(info["sections"]), claimed=c, ais=a,
            as26=None if as26_dep is None else as26_dep.get(tan, ZERO),
            flagged=flagged, note="; ".join(notes)))

    for tan in sorted(set(partner) | {t for t, v in ais_194t.items() if v != ZERO}):
        info = partner.get(tan, {"name": "", "sections": [], "owner_other": False, "claimed": ZERO, "ne": False})
        a = ais_194t.get(tan, ZERO)
        c = None if info["ne"] else info["claimed"]
        notes = []
        flagged = False
        if info["owner_other"]:
            flagged = True
            notes.append("credit in another person's hands -- listed, not compared")
        if c is None:
            flagged = True
            notes.append("claimed amount NOT-EXTRACTED")
        elif abs(c - a) > TOLERANCE:
            flagged = True
            notes.append("TDS in the AIS 194T rows, not claimed in the return" if c == ZERO
                         else "claimed in the return, absent from the AIS 194T rows" if a == ZERO
                         else "claimed differs from the AIS 194T rows")
        out.partner_rows.append(DeductorTieOut(
            tan=tan, name=info["name"] or ais_names.get(tan, ""), section=", ".join(info["sections"]) or "194T",
            claimed=c, ais=a, as26=None, flagged=flagged, note="; ".join(notes)))

    for k, label in (("salary", "Salary TDS total in the return"), ("other", "Other TDS total in the return"),
                     ("tds3", "TDS on sale of property (Schedule TDS3) total in the return")):
        v = (getattr(fr, "tds_totals", {}) or {}).get(k)
        if _num(v) is not None:
            out.informational.append((label, f"{_num(v):,.2f}"))
    # ---- TCS (tax collected from the taxpayer): its own line, never a TDS credit ----
    ais_tcs, ais_payer = _ais_other_by_tan(normalized)
    out.tcs = _tcs_tieout(fr, ais_tcs)
    if out.tcs is None and _num(getattr(fr, "tcs_total", None)) is None:
        out.informational.append(("TCS", "the return has no TCS schedule and the AIS shows no TCS"))
    for tan, (nm, amt) in sorted(ais_payer.items()):
        out.informational.append((
            "TDS you deducted as payer - not your credit",
            f"{tan} {nm}".strip() + f": {amt:,.2f} -- not compared"))
    out.uncounted_tds = AC.uncounted_categories(AC.tdstcs_l1_rows(normalized))

    out.not_extracted = ne
    return out
