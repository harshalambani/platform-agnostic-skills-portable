"""Credit Card - Book Spends (cash basis).

Reads the sorted card statements, matches each bank payment to a card
(Expense:Withdrawals:Credit Card Payment, "CCP") to the statement it settles,
and writes an import-ready journal CSV that books that statement's spends,
refunds, cashback and fees against CCP. Read-only on the book.

Nothing is guessed: a payment that matches no statement, an ambiguous payment,
a statement that does not tie out, a part payment and every EMI conversion, principal and unclassified row are
reported and NOT booked. The run never says it completed successfully while
any of those is open.
"""
from __future__ import annotations

import csv
import sys
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path

from . import journal as J
from . import matcher as M
from .bookio import CCP_PATH, read_book
from .report import RunReport, build_message, write_workbook
from .settings_io import read_large_spend_threshold

_AGENTS_ROOT = Path(__file__).resolve().parent.parent
_MAPPER_FEE_REASON = "Fixed account: card fee or fee reversal -> the entity's Bank Service Charge account"
_MAPPER_DRAW_REASON = "Fixed account: card cashback or CRED overage -> the entity's Drawings account"


def _error(text: str):
    from agents.outputs import ReplyWithOutputs
    return ReplyWithOutputs(text, (), True)


def _strip_root(p: str) -> str:
    p = (p or "").strip()
    return p[len("Root Account:"):] if p.startswith("Root Account:") else p


def _extract(pdf_dir, xlsx, start, end, label):
    """(script module, Result). A seam so tests can supply synthetic statements."""
    from agents.skill_cc_transactions.agent import _load_script
    mod = _load_script()
    return mod, mod.run_extraction(Path(pdf_dir), Path(xlsx), start, end, label)


def _run_mapper(gnucash_path, canonical_csv, out_csv, config_path, drawings, default_account):
    """The existing account mapper over the canonical CSV: history across ALL
    banks (decision 6.1), no bank account (Transfer Account is set here)."""
    if str(_AGENTS_ROOT) not in sys.path:
        sys.path.insert(0, str(_AGENTS_ROOT))
    from skill_gnucash_account_mapper.agent import run as map_run
    return map_run(str(gnucash_path), str(canonical_csv), str(out_csv), config_path=config_path,
                   bank_name=None, gnucash_bank_account=None,
                   drawings_accounts=list(drawings or []), card_default_account=default_account or None)


def _payment_pool(view, start: date, end: date) -> list:
    pad = timedelta(days=M.PART_WINDOW_DAYS)
    pays = []
    for t in view.txns:
        if t.num.startswith(J.NUM_PREFIX):
            continue
        amt = t.amount_on(CCP_PATH)
        if amt <= 0.004 or not (start - pad <= t.date <= end + pad):
            continue
        bank = next((p for p, v in t.splits if p != CCP_PATH and v < 0), "")
        pays.append(M.BookPayment(t.guid, t.date, amt, t.description, bank, start <= t.date <= end))
    return pays


def _canonical_csv(rows, path: Path) -> None:
    from agents.canonical_io import CANONICAL_FIELDS
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(CANONICAL_FIELDS))
        w.writeheader()
        for j in rows:
            w.writerow({"Date": j.date.strftime("%d/%m/%Y"), "Transaction ID": j.num,
                        "Description": j.raw_description or j.description, "Account": "",
                        "Deposit": f"{j.amount:.2f}" if j.side == "D" else "",
                        "Withdrawal": f"{j.amount:.2f}" if j.side == "W" else "",
                        "Balance": "", "Currency": "INR"})


def _write_import_csv(ready, path: Path) -> None:
    from agents.canonical_io import IMPORT_DEPOSIT_HEADER, IMPORT_WITHDRAWAL_HEADER, order_import_ready_headers
    rows = []
    for j in sorted(ready, key=lambda x: (x.date, x.num)):
        rows.append({"Date": j.date.strftime("%d/%m/%Y"), "Transaction ID": j.num, "Description": j.description,
                     "Account": j.account, "Transfer Account": CCP_PATH,
                     IMPORT_DEPOSIT_HEADER: f"{j.amount:.2f}" if j.side == "D" else "",
                     IMPORT_WITHDRAWAL_HEADER: f"{j.amount:.2f}" if j.side == "W" else "",
                     "Balance": "", "Currency": "INR", "Confidence": j.confidence,
                     "MatchReason": j.match_reason})
    headers = order_import_ready_headers(rows[0].keys())
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def run(pdf_dir: str, entity_key: str, gnucash_path: str, output_path: str, period: str = "",
        custom_start: str = "", custom_end: str = "", entities_path: str = "", settings_path: str = "",
        config_path: str = None, model_override: str = None):
    from agents.period_picker import resolve_period
    from agents.outputs import ReplyWithOutputs, extra_output

    # ---- inputs -----------------------------------------------------------
    try:
        start, end, label = resolve_period(period, custom_start, custom_end)
    except ValueError as e:
        return _error(f"ERROR: {e}")
    if not pdf_dir or not Path(pdf_dir).is_dir():
        return _error(f"ERROR: PDF folder not found: {pdf_dir}")
    if not entity_key:
        return _error("ERROR: pick the entity (it names the Bank Service Charge and Drawings accounts).")
    if not gnucash_path or not Path(gnucash_path).is_file():
        return _error("ERROR: GnuCash book not found. Pick the entity and financial year so the book is filled in, "
                      "or choose the .gnucash file.")
    try:
        import configs
        entities = configs.load_entities(entities_path)
    except Exception as e:  # noqa: BLE001
        return _error(f"ERROR: could not load entities.yaml at {entities_path} ({e})")
    ent = entities.get(entity_key)
    if ent is None:
        return _error(f"ERROR: entity {entity_key!r} is not in entities.yaml.")

    # ---- the book (read-only) ---------------------------------------------
    try:
        view = read_book(gnucash_path)
    except Exception as e:  # noqa: BLE001
        return _error(f"ERROR: could not read the GnuCash book {gnucash_path} ({type(e).__name__}: {e}). "
                      f"Nothing was booked.")
    if CCP_PATH not in view.paths:
        return _error(f"ERROR: the book has no account {CCP_PATH!r}. Nothing was booked.")
    try:
        from agents.gnucash_accounts import TargetGuard
        guard = TargetGuard.from_book(gnucash_path)
    except Exception as e:  # noqa: BLE001
        return _error(f"ERROR: could not read the account list of the book ({type(e).__name__}: {e}). "
                      f"Nothing was booked.")
    why = guard.blocked_target_reason(CCP_PATH)
    if why:
        return _error(f"ERROR: {CCP_PATH} cannot be used: {why}. Nothing was booked.")

    threshold = read_large_spend_threshold(settings_path)
    rep = RunReport(label=label, threshold=threshold, ccp_path=CCP_PATH, entity=entity_key)

    # ---- statements -------------------------------------------------------
    with tempfile.TemporaryDirectory() as tmp:
        mod, res = _extract(pdf_dir, Path(tmp) / "extraction.xlsx", start, end, label)
        rep.skipped, rep.duplicates = list(res.skipped), list(res.duplicates)
        rep.issues = [i for i in res.issues if not (i.startswith("fee charged") or i.startswith("EMI row (")
                                                    or i.startswith("tie-out "))]
        rep.statements_read = len(res.statements)
        ties_by_id = {id(s): t for s, t in zip(res.statements, res.ties)}
        rep.refused = [(s, t) for s, t in zip(res.statements, res.ties) if not mod.is_pass(t.result)]

        # ---- payments ------------------------------------------------------
        pool_pay = _payment_pool(view, start, end)
        passing = [s for s in res.statements if mod.is_pass(ties_by_id[id(s)].result)]
        lines = M.pay_lines(res.statements)          # lines on every statement in range; refusal is decided at settlement
        outcome = M.match_payments(lines, pool_pay)
        rep.pairings = outcome.pairings
        rep.lines_unmatched = outcome.lines_unmatched
        rep.ambiguous = outcome.ambiguous
        settlements = M.settle(outcome.pairings, res.statements, res.pool, ties_by_id, mod.find_prior, mod.is_pass)
        rep.settlements = settlements

        flagged = {p.guid: (p, outcome.unmatched_reason[p.guid]) for p in outcome.payments_unmatched if p.in_range}
        for s in settlements:
            if s.status in (M.S_REFUSED, M.S_BEFORE, M.S_NO_PRIOR):
                for p in s.payments:
                    if p.in_range:
                        flagged[p.guid] = (p, s.reason)
        rep.unmatched_payments = list(flagged.values())

        settled_ids = {id(s.target) for s in settlements if s.target is not None}
        run_ids = {id(s) for s in res.statements}
        for st in passing:
            if id(st) in settled_ids:
                continue
            nxt = next((o for o in res.pool if o is not st and mod.find_prior(o, res.pool) is st), None)
            (rep.unpaid if nxt is not None and id(nxt) in run_ids else rep.awaiting).append(st)

        # ---- fixed accounts: usable test, and the EMI interest account -----------
        def _usable(path):
            p = _strip_root(path)
            if p not in view.paths:
                return f"{path!r} is not an account in the book"
            r = guard.blocked_target_reason(p)
            return f"{path!r} cannot be used: {r}" if r else None

        emi_acct = _strip_root(getattr(ent, "card_emi_interest_account", "") or "")
        if not emi_acct:
            emi_bad = "the entity has no card_emi_interest_account in entities.yaml"
        else:
            emi_bad = _usable(emi_acct)
            if emi_bad:
                emi_bad = f"card_emi_interest_account: {emi_bad}"
        rep.emi_interest_reason = emi_bad or ""

        # ---- journals ------------------------------------------------------
        journals, per_statement = [], {}
        for s in settlements:
            if s.status != M.S_SETTLED:
                continue
            js, _emi, iss = J.journals_for_settlement(s.target, s.pay_date, mod.FEE_REVERSAL_RX, threshold,
                                                      book_interest=not emi_bad)
            rep.issues += iss
            for pr in s.pairings:
                ov = J.overage_journal(pr, min(p.date for p in pr.payments))
                if ov is not None:
                    js.append(ov)
            per_statement[id(s.target)] = js
            journals += js
        rep.issues += J.withhold_emi_originals(journals, res.emi_rows)
        J.pair_fee_reversals(journals)
        booked_rows = {j.row_id for j in journals if j.emi}
        rep.emi = J.emi_summary(res.emi_rows)
        rep.emi["rows"] = [r for r in res.emi_rows if id(r) not in booked_rows]
        left = {id(r) for r in rep.emi["rows"]}
        for s in settlements:
            if s.status == M.S_SETTLED and any(id(r) in left for r in s.target.parsed.rows):
                rep.partly_booked.append((s.target, "has EMI rows that are not booked; only its other rows are booked"))
        rep.journals = journals

        # ---- fixed accounts --------------------------------------------------
        if any(j.kind in (J.K_FEE, J.K_FEE_REV) for j in journals):
            bsc = _strip_root(ent.bank_service_charge_account)
            if not bsc:
                return _error(f"ERROR: entity {entity_key!r} has no bank_service_charge_account in entities.yaml, "
                              f"and this run has card fees to book. Add it (the account fees, interest and GST on "
                              f"fees go to). Nothing was booked.")
            bad = _usable(bsc)
            if bad:
                return _error(f"ERROR: entity {entity_key!r} bank_service_charge_account: {bad}. Nothing was booked.")
            for j in journals:
                if j.kind in (J.K_FEE, J.K_FEE_REV):
                    j.account, j.confidence, j.match_reason = bsc, "high", _MAPPER_FEE_REASON
        for j in journals:
            if j.kind == J.K_EMI_INT:
                j.account, j.confidence, j.match_reason = emi_acct, "high", "entity card_emi_interest_account"
        if any(j.kind in (J.K_CASHBACK, J.K_OVERAGE) for j in journals):
            draw = [_strip_root(d) for d in ent.drawings_accounts]
            if len(draw) != 1:
                return _error(f"ERROR: entity {entity_key!r} needs exactly one drawings_accounts entry for card "
                              f"cashback / CRED overage ({len(draw)} found). Nothing was booked.")
            bad = _usable(draw[0])
            if bad:
                return _error(f"ERROR: entity {entity_key!r} drawings account: {bad}. Nothing was booked.")
            for j in journals:
                if j.kind in (J.K_CASHBACK, J.K_OVERAGE):
                    j.account, j.confidence, j.match_reason = draw[0], "high", _MAPPER_DRAW_REASON

        # ---- mapper for spends and refunds ---------------------------------
        to_map = [j for j in journals if j.needs_mapping and j.status == J.ST_READY]
        if to_map:
            cin, cout = Path(tmp) / "cc_canonical.csv", Path(tmp) / "cc_mapped_GnuCash_import_ready.csv"
            _canonical_csv(to_map, cin)
            try:
                _run_mapper(gnucash_path, cin, cout, config_path, [_strip_root(d) for d in ent.drawings_accounts],
                            _strip_root(getattr(ent, "card_spend_default_account", "")))
                with open(cout, newline="", encoding="utf-8") as f:
                    mapped = {r.get("Transaction ID", ""): r for r in csv.DictReader(f)}
            except Exception as e:  # noqa: BLE001
                mapped = {}
                rep.issues.append(f"the account mapper failed ({type(e).__name__}: {e}); spends and refunds are "
                                  f"withheld, nothing was guessed")
            for j in to_map:
                row = mapped.get(j.num)
                if row:
                    j.account = _strip_root(row.get("Account", ""))
                    j.confidence = row.get("Confidence", "") or ""
                    j.match_reason = row.get("MatchReason", "") or ""
            # a refund goes back to the account its identical purchase went to
            by_desc = {(j.card, j.raw_description): j for j in journals
                       if j.kind == J.K_SPEND and j.account}
            for j in to_map:
                if j.kind == J.K_REFUND:
                    orig = by_desc.get((j.card, j.raw_description))
                    if orig is not None:
                        j.account, j.confidence = orig.account, "high"
                        j.match_reason = "refund of an identical purchase in this run"

        # ---- guard on every account ------------------------------------------
        for j in journals:
            if j.status != J.ST_READY:
                continue
            if not j.account:
                j.status, j.why = J.ST_NO_ACCOUNT, j.why or "the mapper found no account"
                continue
            bad = _usable(j.account)
            if bad:
                j.status, j.why = (J.ST_BLOCKED if "cannot be used" in bad else J.ST_NO_ACCOUNT), bad

        J.mark_booked(journals, view.txns, CCP_PATH)

        # ---- CCP tie-out -------------------------------------------------------
        before = round(sum(t.amount_on(CCP_PATH) for t in view.txns if start <= t.date <= end), 2)
        effect = round(sum(j.ccp_effect for j in journals if j.status == J.ST_READY or
                           (j.status == J.ST_BOOKED and not (start <= j.date <= end))), 2)
        unmatched = rep.unmatched_total
        not_booked = round(sum(p.amount for s in settlements if s.status == M.S_PARTLY
                               for p in s.payments if p.in_range), 2)
        after = round(before + effect, 2)
        n_settled = sum(1 for s in settlements if s.status == M.S_SETTLED)
        rep.tie = {"before": before, "effect": effect, "after": after, "unmatched": unmatched,
                   "not_booked": not_booked, "other": round(after - unmatched - not_booked, 2),
                   "tol": max(1.0, float(n_settled))}

        # ---- output ------------------------------------------------------------
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        ready = rep.ready
        if ready:
            _write_import_csv(ready, out)
            rep.output_csv = str(out)
        xlsx = out.with_name(out.stem + "_report.xlsx")
        write_workbook(rep, str(xlsx))
        rep.output_xlsx = str(xlsx)

    msg = build_message(rep)
    return ReplyWithOutputs(msg, [extra_output("workbook", rep.output_xlsx, "Journals, payments, fees, EMI rows, CCP tie-out")],
                            withhold_primary=not ready)
