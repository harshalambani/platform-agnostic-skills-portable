"""The run result text and the Excel workbook for the card-spend booking skill.

Block order in the text (decision: payments without a statement first;
  a stopped AI pass, with its Suspense count, comes before all of them):
  1. RED FLAG: bank payments to Credit Card Payment that match no statement
  2. fees booked
  3. large spends (possible assets)
  4. statements refused / not settled / partly settled
  5. EMI rows: not booked, awaiting decision
  6. issues, then the headline, the CCP tie-out and the counts
The headline never says "completed successfully" while anything above is open.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from . import journal as J
from .matcher import BookPayment


def csv_label(st) -> str:
    return f"CSV export {st.card} {st.label}"


def money(v) -> str:
    return f"{v:,.2f}"


@dataclass
class RunReport:
    label: str = ""
    threshold: float = 0.0
    threshold_source: str = ""                                  # CC-14: which value applied
    ccp_path: str = ""
    entity: str = ""
    journals: list = field(default_factory=list)
    settlements: list = field(default_factory=list)
    pairings: list = field(default_factory=list)
    unmatched_payments: list = field(default_factory=list)      # [(BookPayment, reason)]
    ambiguous: list = field(default_factory=list)
    lines_unmatched: list = field(default_factory=list)
    refused: list = field(default_factory=list)                 # [(Statement, TieOut)]
    tie_results: dict = field(default_factory=dict)             # id(Statement) -> its real tie-out result
    awaiting: list = field(default_factory=list)                # Statements not yet paid (no later statement)
    unpaid: list = field(default_factory=list)                  # Statements with a later statement but no payment
    partly_booked: list = field(default_factory=list)           # [(Statement, reason)]
    untied: list = field(default_factory=list)                  # CC-26: [(Statement, TieOut)] imported although the tie-out did not pass
    csv_ties: list = field(default_factory=list)                # CC-24: [(Statement, TieOut)] CSV exports read whole
    csv_refused: list = field(default_factory=list)             # CC-24: [(file name, reason)] CSV exports that book nothing
    emi: dict = field(default_factory=dict)
    emi_interest_reason: str = ""
    issues: list = field(default_factory=list)
    skipped: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)
    statements_read: int = 0
    tie: dict = field(default_factory=dict)
    output_csv: str = ""
    output_xlsx: str = ""
    errors: list = field(default_factory=list)
    ai_stop: str = ""                    # CC-11: why the mapper's AI pass stopped ("" = it did not)
    ai_stop_suspense: int = 0            # rows that went to Suspense because it stopped

    @property
    def ready(self) -> list:
        return [j for j in self.journals if j.status == J.ST_READY]

    @property
    def withheld(self) -> list:
        return [j for j in self.journals if j.status not in (J.ST_READY, J.ST_BOOKED)]

    @property
    def unmatched_total(self) -> float:
        return round(sum(p.amount for p, _r in self.unmatched_payments), 2)

    @property
    def open_items(self) -> bool:
        """True while anything stops the run from being called a success."""
        return bool(self.ai_stop or self.issues or self.unmatched_payments or self.lines_unmatched or self.ambiguous
                    or self.refused or self.untied or self.csv_refused or self.unpaid or self.partly_booked or self.withheld
                    or (self.emi and self.emi.get("rows"))
                    or any(s.status != "SETTLED" for s in self.settlements)
                    or abs(self.tie.get("other", 0.0)) > self.tie.get("tol", 0.5))


def red_flag_block(rep: RunReport) -> str:
    if not rep.unmatched_payments:
        return ""
    lines = [f"RED FLAG: {len(rep.unmatched_payments)} bank payment(s) to {rep.ccp_path} match no statement "
             f"(total {money(rep.unmatched_total)}). Money left the bank for a card and no spend is booked "
             f"against it. Nothing here was matched to the nearest statement."]
    for p, reason in sorted(rep.unmatched_payments, key=lambda x: (x[0].date, x[0].amount)):
        lines.append(f"  {p.date:%d %b %Y} | {p.bank_account or 'bank account not found'} | {money(p.amount)} | "
                     f"{'CRED' if p.cred else 'direct'} | {reason}")
    return "\n".join(lines)


def ai_stop_block(rep: RunReport) -> str:
    """CC-11: a stopped AI pass is the first thing the reader sees, with its Suspense count."""
    if not rep.ai_stop:
        return ""
    return (f"RED FLAG: the AI pass stopped - {rep.ai_stop}. {rep.ai_stop_suspense} row(s) went to Suspense "
            f"instead of an account. What was mapped before it stopped is kept. Fix the cause (see the reason), "
            f"then run again; this result is NOT a plain success.")


def fee_block(rep: RunReport) -> str:
    fees = [j for j in rep.journals if j.kind in (J.K_FEE, J.K_FEE_REV) and j.status == J.ST_READY]
    if not fees:
        return ""
    t = J.fee_totals(rep.journals)
    lines = [f"FEES BOOKED ({t['count']}): charged {money(t['fees'])}, reversed {money(t['reversals'])}, "
             f"net {money(t['net'])} (EMI interest is not in this total; EMI processing fee and GST are)"]
    for j in sorted(fees, key=lambda x: (x.card, x.real_date)):
        tail = f" (reverses {j.paired_with})" if j.paired_with else ""
        lines.append(f"  {j.card} | {j.real_date:%d %b %Y} | {J.KIND_LABEL[j.kind]} | {j.raw_description[:50] or j.description[:50]} "
                     f"| {money(j.amount)}{tail}")
    return "\n".join(lines)


def large_spend_block(rep: RunReport) -> str:
    flagged = [j for j in rep.journals if j.flag]
    if not flagged:
        return ""
    lines = [f"LARGE SPENDS ({len(flagged)}) of {money(rep.threshold)} or more - {J.LARGE_SPEND_FLAG}. "
             f"Mapped as usual, never moved to an asset account:"]
    for j in sorted(flagged, key=lambda x: (x.real_date, x.card)):
        lines.append(f"  {j.card} | {j.real_date:%d %b %Y} | {j.raw_description[:50]} | {money(j.amount)} | "
                     f"{j.account or 'no account'} | {j.status}")
    return "\n".join(lines)


def not_tied_block(rep: RunReport) -> str:
    """CC-26: the first thing the reader sees. These statements did not tie out, their rows are in the
    import file anyway, and the reader must check them before importing."""
    if not rep.untied:
        return ""
    lines = [f"NOT TIED - IMPORTED, CHECK BEFORE IMPORT: {len(rep.untied)} statement(s) did not tie out. "
             f"Their rows are in the import file (and marked NOT TIED in the workbook); the gap may be a row "
             f"the reader missed or misread."]
    for st, t in rep.untied:
        n = sum(1 for j in rep.journals if j.untied and j.card == st.card and j.statement == st.label)
        lines.append(f"  {st.card} {st.label}: {t.result}; {t.gap or t.note}"
                     + (f"; difference {money(t.difference)}" if t.difference else "")
                     + f"; {n} journal(s) from it in the import file")
    return "\n".join(lines)


def statement_block(rep: RunReport) -> str:
    out = []
    if rep.csv_ties:
        out.append("CSV EXPORTS (no balance tie-out; coverage and counts only):\n" + "\n".join(
            f"  CSV export {st.card} {st.label}: {t.note}; "
            f"{sum(1 for j in rep.journals if j.statement == csv_label(st))} journal(s)"
            for st, t in rep.csv_ties) + "\n  CSV payment rows are not matched to bank payments.")
    if rep.csv_refused:
        out.append("REFUSED CSV exports (nothing booked from them):\n" + "\n".join(
            f"  CSV export {name}: {why}" for name, why in rep.csv_refused))
    if rep.refused:
        out.append("REFUSED - statements that read 0 rows (nothing booked from them):\n" + "\n".join(
            f"  {st.card} {st.label}: {t.result}; {t.gap or t.note}" for st, t in rep.refused))
    partly = [s for s in rep.settlements if s.status == "PARTLY SETTLED"]
    if partly:
        out.append("PARTLY SETTLED - paid less than the statement total, nothing booked "
                   "(a NOT TIED statement is booked on its real dates):\n" + "\n".join(
            f"  {s.target.card} {s.target.label}: {s.reason}" for s in partly))
    other = [s for s in rep.settlements if s.status not in ("SETTLED", "PARTLY SETTLED")]
    if other:
        out.append("PAYMENTS THAT DID NOT SETTLE A STATEMENT IN THIS RUN:\n" + "\n".join(
            f"  {(s.target or s.paid_on).card} {(s.target or s.paid_on).label}: {s.status} - {s.reason}"
            for s in other))
    if rep.unpaid:
        out.append("NOT SETTLED - a later statement exists but shows no payment for it, nothing booked:\n" + "\n".join(
            f"  {st.card} {st.label}" for st in rep.unpaid))
    if rep.awaiting:
        out.append("AWAITING PAYMENT (latest statement, not yet paid; nothing booked):\n" + "\n".join(
            f"  {st.card} {st.label}" for st in rep.awaiting))
    if rep.lines_unmatched:
        out.append("STATEMENT PAYMENT LINES WITH NO BANK PAYMENT IN THE BOOK:\n" + "\n".join(
            f"  {ln.stmt.card} {ln.date:%d %b %Y} {money(ln.amount)}" for ln in rep.lines_unmatched))
    if rep.ambiguous:
        out.append("AMBIGUOUS payments (not matched, never guessed):\n" + "\n".join(
            "  " + (f"{a['line'].stmt.card} line {a['line'].date:%d %b %Y} {money(a['line'].amount)}: "
                    if a["line"] is not None else "")
            + f"{a['reason']} ({', '.join(f'{p.date:%d %b} {money(p.amount)}' for p in a['payments'])})"
            for a in rep.ambiguous))
    if rep.withheld:
        out.append("WITHHELD journals (not in the CSV):\n" + "\n".join(
            f"  {j.card} {j.real_date:%d %b %Y} {j.raw_description[:40] or j.description[:40]} {money(j.amount)}: "
            f"{j.status}{' - ' + j.why if j.why else ''}" for j in rep.withheld))
    if rep.partly_booked:
        out.append("PARTLY BOOKED (never presented as settled):\n" + "\n".join(
            f"  {st.card} {st.label}: {why}" for st, why in rep.partly_booked))
    return "\n\n".join(out)


def emi_block(rep: RunReport) -> str:
    rows = (rep.emi or {}).get("rows") or []
    booked = [j for j in rep.journals if j.emi and j.status == J.ST_READY]
    lines = []
    if booked:
        if all(j.emi_validated for j in booked):
            lines.append(f"EMI ROWS BOOKED ({len(booked)}) - HSBC instalment rows: principal as spend, "
                         f"interest to the EMI interest account, fee and GST as fees")
        else:
            lines.append(f"EMI ROWS BOOKED ({len(booked)}) - {J.EMI_UNVALIDATED}")
        for j in booked:
            lines.append(f"  {j.card} | {j.real_date:%d %b %Y} | {J.KIND_LABEL[j.kind]} | "
                         f"{j.raw_description[:50]} | {money(j.amount)} | {j.account}")
    interest = (rep.emi or {}).get("interest") or {}
    interest_lines = []
    if interest:
        interest_lines.append("  EMI interest (its own subtotal, not a fee):")
        for (card, fy), v in interest.items():
            interest_lines.append(f"    {card} | FY {fy} | {money(v)}")
    expl = (rep.emi or {}).get("explained") or 0
    if expl:
        interest_lines.append(f"  EMI offset legs (instalment credits, loan conversion credits): {expl} - "
                              f"understood, never booked")
    if not rows:
        return "\n".join(lines + interest_lines)
    if rep.emi_interest_reason and any(r.kind == "emi_interest" for r in rows):
        lines.append(f"EMI INTEREST NOT BOOKED - {rep.emi_interest_reason}. Nothing was guessed and it was not "
                     f"sent to Bank Service Charge or the mapper.")
    lines.append(f"{J.EMI_UNBOOKED_TITLE.upper()} ({len(rows)}):")
    for r in rows:
        when = r.date.strftime("%d %b %Y") if r.date else "undated"
        extra = f" | {r.instalment}" if getattr(r, "instalment", "") else ""
        lines.append(f"  {r.card} | {r.period} | {when} | {r.kind} | {r.description[:50]}{extra} | "
                     f"{money(r.amount)} {r.direction}")
    return "\n".join(lines + interest_lines)


def tie_block(rep: RunReport) -> str:
    t = rep.tie
    if not t:
        return ""
    lines = ["Credit Card Payment (CCP) tie-out for the range:",
             f"  before (book, as it stands): {money(t['before'])}",
             f"  these journals: {money(t['effect'])}",
             f"  after: {money(t['after'])}  <- residual",
             f"    of which payments with no statement: {money(t['unmatched'])}",
             f"    of which payments on statements not booked: {money(t['not_booked'])}",
             f"    other: {money(t['other'])}"]
    for d, text, amt in t.get("other_lines") or []:
        lines.append(f"      {d:%d %b %Y} " if d else "      ")
        lines[-1] += f"{text[:60]}: {money(amt)}"
    return "\n".join(lines)


def build_message(rep: RunReport) -> str:
    parts = []
    for block in (not_tied_block(rep), ai_stop_block(rep), red_flag_block(rep), fee_block(rep), large_spend_block(rep), statement_block(rep),
                  emi_block(rep)):
        if block:
            parts.append(block)
    if rep.issues:
        parts.append("Issues:\n" + "\n".join(f"  - {i}" for i in rep.issues))
    n_ready = len(rep.ready)
    if rep.open_items:
        n = (1 if rep.ai_stop else 0) + len(rep.issues) + len(rep.unmatched_payments) + len(rep.refused) + len(rep.withheld) \
            + len(rep.lines_unmatched) + len(rep.ambiguous) + len(rep.unpaid) + len(rep.partly_booked)             + len(rep.csv_refused) + len(rep.untied)
        n += sum(1 for s in rep.settlements if s.status != "SETTLED")
        head = f"Credit card spend booking NOT complete: {max(n, 1)} open item(s) above."
    else:
        head = "Credit card spend booking completed successfully."
    parts.append(head)
    tb = tie_block(rep)
    if tb:
        parts.append(tb)
    parts.append(f"Range run: {rep.label}" + (f" | entity {rep.entity}" if rep.entity else ""))
    if rep.threshold_source:
        parts.append(f"Large-spend limit: {money(rep.threshold)} (from {rep.threshold_source})")
    ready_n = n_ready
    booked_n = sum(1 for j in rep.journals if j.status == J.ST_BOOKED)
    parts.append(f"{ready_n} journal(s) ready to import" + (f", {booked_n} already booked" if booked_n else "")
                 + f" from {sum(1 for s in rep.settlements if s.status == 'SETTLED')} settled statement(s); "
                 f"{rep.statements_read} statement(s) read in the range"
                 + (f" and {len(rep.csv_ties)} CSV export(s)" if rep.csv_ties else ""))
    if rep.skipped:
        parts.append(f"Skipped: not a statement ({len(rep.skipped)}):\n" + "\n".join(f"  {d}" for d in rep.skipped))
    if rep.duplicates:
        parts.append("Duplicate statements:\n" + "\n".join(f"  {d}" for d in rep.duplicates))
    parts.append(f"Output: {rep.output_csv}" if rep.output_csv else "No import file written: nothing is ready to import.")
    if rep.output_xlsx:
        parts.append(f"Workbook: {rep.output_xlsx}")
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Excel
# ---------------------------------------------------------------------------

def statement_rows(rep: RunReport) -> list:
    """CC-09: ONE row per statement, with its real tie-out result. A REFUSED statement is never
    also listed as PASS (it used to appear once as refused and again, as a settled target, as PASS)."""
    rows: dict = {}

    def row(st):
        r = rows.get(id(st))
        if r is None:
            res = rep.tie_results.get(id(st), "")
            r = rows[id(st)] = [st.card, st.label, res, "", ""]
        return r

    for st, t in list(rep.refused) + list(rep.untied):
        r = row(st)
        r[2], r[4] = t.result, t.gap or t.note
    for st, t in rep.csv_ties:
        r = rows[id(st)] = [st.card, f"CSV export {st.label}", t.result, "n/a (CSV export)", t.note]
    for name, why in rep.csv_refused:
        rows[("csv", name)] = ["", f"CSV export {name}", "REFUSED (CSV export)", "", why]
    for s in rep.settlements:
        if s.target:
            r = row(s.target)
            r[3], r[4] = s.status, "; ".join(x for x in (r[4], s.reason) if x)
    for st in rep.awaiting:
        row(st)[3] = "AWAITING PAYMENT"
    for st in rep.unpaid:
        r = row(st)
        r[3], r[4] = "NOT SETTLED", "later statement shows no payment"
    return [tuple(r) for r in rows.values()]


def write_workbook(rep: RunReport, path: str) -> str:
    from openpyxl import Workbook
    from openpyxl.styles import Font

    wb = Workbook()

    def sheet(title, header, rows, first=False):
        ws = wb.active if first else wb.create_sheet()
        ws.title = title
        ws.append(header)
        for c in ws[1]:
            c.font = Font(bold=True)
        for r in rows:
            ws.append(list(r))
        for col in ws.columns:
            width = max((len(str(c.value)) for c in col if c.value is not None), default=8)
            ws.column_dimensions[col[0].column_letter].width = min(max(width + 2, 10), 70)
        return ws

    if rep.untied:
        sheet("NOT TIED", ["Card", "Statement", "Tie-out", "Difference", "Detail", "Journals in the import"],
              [(st.card, st.label, t.result, t.difference, t.gap or t.note,
                sum(1 for j in rep.journals if j.untied and j.card == st.card and j.statement == st.label))
               for st, t in rep.untied], first=True)
    sheet("Journals",
          ["Num", "Kind", "Date booked", "Statement date", "Description", "Debit account", "Credit account",
           "Amount", "Card", "Statement", "Status", "Why / note", "Large spend flag", "Confidence", "Match reason",
           "Tie-out"],
          [(j.num, J.KIND_LABEL[j.kind], j.date, j.real_date, j.description,
            j.account if j.side == "W" else rep.ccp_path, rep.ccp_path if j.side == "W" else j.account,
            j.amount, j.card, j.statement, j.status, "; ".join(x for x in (j.why, j.note) if x), j.flag,
            j.confidence, j.match_reason, "NOT TIED" if j.untied else "") for j in rep.journals],
          first=not rep.untied)
    sheet("Payments without statement",
          ["Date", "Bank account", "Amount", "CRED / direct", "Reason"],
          [(p.date, p.bank_account, p.amount, "CRED" if p.cred else "direct", why)
           for p, why in sorted(rep.unmatched_payments, key=lambda x: (x[0].date, x[0].amount))])
    sheet("Payments matched",
          ["Statement with the payment line", "Line date", "Line amount", "Bank date(s)", "Bank amount", "Match",
           "CRED overage", "Settles statement", "Result"],
          [(f"{s.paid_on.card} {s.paid_on.label}", pr.line.date, pr.line.amount,
            ", ".join(f"{p.date:%d %b %Y}" for p in pr.payments), sum(p.amount for p in pr.payments), pr.kind,
            pr.overage, f"{s.target.card} {s.target.label}" if s.target else "none read", s.status)
           for s in rep.settlements for pr in s.pairings])
    sheet("Large spends",
          ["Date", "Card", "Description", "Amount", "Account", "Flag", "Status"],
          [(j.real_date, j.card, j.raw_description, j.amount, j.account, j.flag, j.status)
           for j in rep.journals if j.flag])
    fees = [j for j in rep.journals if j.kind in (J.K_FEE, J.K_FEE_REV)]
    sheet("Fees", ["Date", "Card", "Kind", "Description", "Amount", "Paired with", "Status"],
          [(j.real_date, j.card, J.KIND_LABEL[j.kind], j.raw_description or j.description, j.amount, j.paired_with,
            j.status) for j in fees])
    emi_rows = (rep.emi or {}).get("rows") or []
    sheet("EMI rows not booked",
          ["Card", "Statement", "Date", "Kind", "Description", "Amount", "Direction"],
          [(r.card, r.period, r.date, r.kind, r.description, r.amount, r.direction) for r in emi_rows])
    sheet("EMI interest",
          ["Card", "Financial year", "EMI interest"],
          [(card, fy, v) for (card, fy), v in ((rep.emi or {}).get("interest") or {}).items()])
    sheet("Statements", ["Card", "Statement", "Tie-out", "Settlement", "Detail"], statement_rows(rep))
    t = rep.tie or {}
    sheet("CCP tie-out", ["Line", "Amount"],
          [("Before (book as it stands)", t.get("before")), ("These journals", t.get("effect")),
           ("After (residual)", t.get("after")), ("Payments with no statement", t.get("unmatched")),
           ("Payments on statements not booked", t.get("not_booked")), ("Other", t.get("other"))]
          + [(f"  Other: {d:%d %b %Y} {text}" if d else f"  Other: {text}", amt)
             for d, text, amt in t.get("other_lines") or []])
    sheet("Issues", ["Issue"], ([(f"AI pass stopped: {rep.ai_stop}; {rep.ai_stop_suspense} row(s) in Suspense",)]
                                if rep.ai_stop else []) + [(i,) for i in rep.issues])
    wb.save(path)
    return path
