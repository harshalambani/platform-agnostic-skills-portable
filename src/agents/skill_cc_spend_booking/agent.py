"""Credit Card - Book Spends (cash basis).

Reads the sorted card statements, matches each bank payment to a card
(Expense:Withdrawals:Credit Card Payment, "CCP") to the statement it settles,
and writes an import-ready journal CSV that books that statement's spends,
refunds, cashback and fees against CCP. Read-only on the book.

Nothing is guessed: a payment that matches no statement, an ambiguous payment,
a statement that does not tie out, a part payment and every EMI conversion credit and unclassified row are
reported and NOT booked. The run never says it completed successfully while
any of those is open.
"""
from __future__ import annotations

import csv
import sys
import tempfile
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from . import journal as J
from . import matcher as M
from . import split as SP
from .bookio import CCP_PATH, read_book
from .report import RunReport, build_message, csv_label, write_workbook
from .settings_io import resolve_cred_max_overage, resolve_large_spend_threshold

_AGENTS_ROOT = Path(__file__).resolve().parent.parent
_MAPPER_FEE_REASON = "Fixed account: card fee or fee reversal -> the entity's Bank Service Charge account"
_MAPPER_DRAW_REASON = "Fixed account: card cashback or CRED overage -> the entity's Drawings account"


def _fee_row(r, fee_rx) -> bool:
    """A statement row that will become a K_FEE / K_FEE_REV journal (same test as journal.py)."""
    if r.kind in J.EMI_FEE_KINDS and r.direction == "Dr":
        return True
    if r.kind == "fee" and r.direction == "Dr":
        return True
    return bool(r.kind == "refund" and r.direction == "Cr" and fee_rx is not None
                and fee_rx.search(r.description or ""))


def _cashback_row(r) -> bool:
    return r.kind == "cashback" and r.direction in ("Cr", "Dr")


def _impossible_target(guard, j) -> str:
    """CC-10: why journal ``j`` may not be READY on its mapped account, or "". The
    type comes from the book (an EXPENSE account nested under an Income parent is
    a legitimate spend target); an unknown type is never guessed."""
    kind = getattr(guard, "account_type", lambda p: None)(j.account)
    side = "credited" if j.side == "D" else "debited"
    if kind == "INCOME":
        return (f"{'a refund' if j.side == 'D' else 'a spend'} cannot be booked to an income account: "
                f"{j.account} is an income account and would be {side}")
    if kind == "ASSET" and j.confidence != "override":
        return (f"{j.account} is an asset account and was reached by a {j.confidence or 'automatic'} match; "
                f"only a saved rule of yours may put a card spend on an asset account")
    return ""


def _split_target(s, cards, split_from) -> tuple:
    """(sub-account path or "", an Issue text or ""). A statement is split when its card has an
    `account` in the card list, ccp_split_from is set, and every bank payment that settled it is dated on
    or after ccp_split_from. A mixed settlement, or a folder shared by two listed cards, is not split."""
    if split_from is None or not cards or not s.payments:
        return "", ""
    same = [c for c in cards if c.label == s.target.card]
    if not any(c.account for c in same):
        return "", ""
    where = f"{s.target.card} {s.target.label}"
    if len(same) > 1:
        return "", (f"{where}: two listed cards share this card folder, so the statement cannot be tied to one "
                    f"sub-account; it is booked against {CCP_PATH} as before")
    dates = [p.date for p in s.payments]
    if all(d < split_from for d in dates):
        return "", ""
    if any(d < split_from for d in dates):
        return "", (f"{where}: its bank payments fall on both sides of ccp_split_from ({split_from:%d %b %Y}); "
                    f"it is booked against {CCP_PATH} as before")
    return same[0].account, ""


def _reconcile_splits(rep, journals: list) -> None:
    """A statement already in the book against the parent (its rows carry their Num) is never split now:
    that would move its payment onto the sub-account while its spends stayed on the parent."""
    for rec in list(rep.splits):
        moved_booked = [m for m in rec["moves"] if m.status == J.ST_BOOKED]
        if moved_booked:
            continue
        if any(j.status == J.ST_BOOKED for j in rec["journals"]):
            for j in rec["journals"]:
                j.transfer = ""
            drop = {id(m) for m in rec["moves"]}
            journals[:] = [j for j in journals if id(j) not in drop]
            rep.splits.remove(rec)
            rep.issues.append(f"{rec['card']} {rec['statement']}: already in the book against {CCP_PATH}, so it is "
                              f"not split and its payment stays in the pool")


def _log_line(text: str) -> None:
    """One line into the run log: the progress stream the UI writes to the Full log file."""
    try:
        from agents.base_agent import get_progress_queue
        q = get_progress_queue()
        if q is not None:
            q.put({"step": 5, "type": "pipeline", "snippet": f"cc-spend: {text}"})
    except Exception:  # noqa: BLE001
        pass
    print(f"[cc-spend] {text}")


def _error(text: str):
    from agents.outputs import ReplyWithOutputs
    return ReplyWithOutputs(text, (), True)


def _strip_root(p: str) -> str:
    p = (p or "").strip()
    return p[len("Root Account:"):] if p.startswith("Root Account:") else p


def _extract(pdf_dir, xlsx, start, end, label, cards=None, cards_problem=None):
    """(script module, Result). A seam so tests can supply synthetic statements."""
    from agents.skill_cc_transactions.agent import _load_script
    mod = _load_script()
    return mod, mod.run_extraction(Path(pdf_dir), Path(xlsx), start, end, label, cards, cards_problem)


def _run_mapper(gnucash_path, canonical_csv, out_csv, config_path, drawings, default_account, model=None):
    """The existing account mapper over the canonical CSV: history across ALL
    banks (decision 6.1), no bank account (Transfer Account is set here)."""
    if str(_AGENTS_ROOT) not in sys.path:
        sys.path.insert(0, str(_AGENTS_ROOT))
    from skill_gnucash_account_mapper.agent import run as map_run
    return map_run(str(gnucash_path), str(canonical_csv), str(out_csv), config_path=config_path,
                   bank_name=None, gnucash_bank_account=None,
                   drawings_accounts=list(drawings or []), card_default_account=default_account or None,
                   model_override=model or None)


_AI_STOP_MARKER = "AI pass stopped"       # same text the mapper puts in MatchReason (CC-11)


def _mapper_ai_status() -> dict:
    """The mapper's own record of its last AI pass ({} when the module is not loaded)."""
    mod = sys.modules.get("skill_gnucash_account_mapper.agent")
    return dict(getattr(mod, "_LLM_RUN_STATUS", {}) or {}) if mod is not None else {}


def _clear_mapper_ai_status() -> None:
    mod = sys.modules.get("skill_gnucash_account_mapper.agent")
    status = getattr(mod, "_LLM_RUN_STATUS", None) if mod is not None else None
    if isinstance(status, dict):
        status.clear()


def _ai_stop_from(mapped: dict) -> tuple:
    """CC-11: (reason, suspense_count) when the mapper's AI pass stopped, else ("", 0).
    Read from the rows themselves (the rows the stopped pass never reached say so in
    MatchReason) and from the mapper's run status; either one is enough."""
    reason, n = "", 0
    for r in mapped.values():
        why = r.get("MatchReason", "") or ""
        if _AI_STOP_MARKER in why and (r.get("Confidence", "") or "") == "suspense":
            n += 1
            if not reason:
                tail = why.split(_AI_STOP_MARKER + ":", 1)[-1] if (_AI_STOP_MARKER + ":") in why else ""
                reason = tail.split("; review", 1)[0].strip()
    status = _mapper_ai_status()
    if status.get("stopped"):
        reason = reason or str(status.get("reason") or "the AI pass stopped")
    return reason, n


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
                     "Account": j.account, "Transfer Account": j.transfer or CCP_PATH,
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
        config_path: str = None, model_override: str = None, prepayment_marks: str = ""):
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

    # ---- the card list (CC-28) ---------------------------------------------
    # A missing list is reported loudly in the Issues and the run goes on without card digits
    # (pairing is then exactly as before). A list that is present but refused stops the run.
    from agents import cc_cards
    cards, cards_problem = None, None
    if settings_path:
        try:
            cards = cc_cards.load_for_settings(settings_path, entities_path or None)
        except cc_cards.CardListMissing as e:
            cards_problem = str(e)
        except cc_cards.CardListError as e:
            return _error(f"ERROR: the card list cannot be used: {e}. Nothing was booked.")

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

    # ---- CC-39 / CC-29 settings (both come from the card list; both are optional) ----------------
    split_from = getattr(cards, "split_from", None)
    prepay_acct = _strip_root(getattr(cards, "prepayment_account", "") or "")
    mark_tokens = SP.parse_marks(prepayment_marks)
    if mark_tokens:
        if cards is None:
            return _error("ERROR: payments were marked as corporate card prepayments, but the card list "
                          f"({cc_cards.CARDS_FILENAME}) was not found. Nothing was booked.")
        if split_from is None:
            return _error("ERROR: payments were marked as corporate card prepayments, but ccp_split_from is not "
                          "set in the card list; only payments on or after it can be marked. Nothing was booked.")
        if not prepay_acct:
            return _error("ERROR: payments were marked as corporate card prepayments, but prepayment_account is "
                          "not set in the card list (choose it on the Cards screen). Nothing was booked.")
        if prepay_acct not in view.paths:
            return _error(f"ERROR: prepayment_account {prepay_acct!r} is not an account in the book. Nothing was booked.")
        _pw = guard.blocked_target_reason(prepay_acct)
        if _pw:
            return _error(f"ERROR: prepayment_account {prepay_acct!r} cannot be used: {_pw}. Nothing was booked.")
        _bad_tok = [t for t in mark_tokens if not SP.valid_code(t)]
        if _bad_tok:
            return _error("ERROR: a prepayment code is not an 8-character code from the report "
                          f"({len(_bad_tok)} such value(s)). Nothing was booked.")

    threshold, threshold_source = resolve_large_spend_threshold(ent, settings_path)
    try:
        cred_max, cred_src = resolve_cred_max_overage(ent)
    except ValueError as e:
        return _error(f"ERROR: {e}. Nothing was booked.")
    rep = RunReport(label=label, threshold=threshold, ccp_path=CCP_PATH, entity=entity_key,
                    threshold_source=threshold_source, cred_max=cred_max, cred_max_source=cred_src)

    # ---- statements -------------------------------------------------------
    _t0 = time.perf_counter()
    _marks: list = []

    def _mark(name):
        nonlocal _t0
        now = time.perf_counter()
        _marks.append(f"{name} {now - _t0:.2f}s")
        _t0 = now

    with tempfile.TemporaryDirectory() as tmp:
        mod, res = _extract(pdf_dir, Path(tmp) / "extraction.xlsx", start, end, label, cards, cards_problem)
        rep.skipped, rep.duplicates = list(res.skipped), list(res.duplicates)
        rep.issues = [i for i in res.issues if not (i.startswith("fee charged") or i.startswith("EMI row (")
                                                    or i.startswith("tie-out "))]
        rep.statements_read = len(res.statements)
        ties_by_id = {id(s): t for s, t in zip(res.statements, res.ties)}
        # CC-26: a statement that does not tie is IMPORTED and reported loudly (NOT TIED). Only a statement
        # that read 0 rows is still refused: nothing in it can be trusted to a card or a side.
        _untied_all = [(s, t) for s, t in zip(res.statements, res.ties) if not mod.is_pass(t.result)]
        rep.refused = [(s, t) for s, t in _untied_all if not s.parsed.rows]
        rep.untied = [(s, t) for s, t in _untied_all if s.parsed.rows]
        untied_ids = {id(s) for s, _t in rep.untied}
        rep.tie_results = {id(s): t.result for s, t in zip(res.statements, res.ties)}

        _mark("parse")
        passing = [s for s in res.statements if mod.is_pass(ties_by_id[id(s)].result) or id(s) in untied_ids]

        # ---- CC-08: fixed-account checks BEFORE any matching work ----------------
        # Fee / cashback rows on a passing statement need the entity's Bank Service Charge /
        # Drawings account. Say so now, not after the payment matching. The same checks run
        # again below on the journals as a backstop.
        def _usable(path):
            p = _strip_root(path)
            if p not in view.paths:
                return f"{path!r} is not an account in the book"
            r = guard.blocked_target_reason(p)
            return f"{path!r} cannot be used: {r}" if r else None

        # CC-24: a CSV export read whole (card identified, coverage and counts reported) is bookable;
        # its rows take the same checks as PDF rows. A refused CSV is not in csv_statements at all.
        csv_sts = list(getattr(res, "csv_statements", None) or [])
        csv_ties = list(getattr(res, "csv_ties", None) or [])
        rep.csv_ties = list(zip(csv_sts, csv_ties))
        rep.csv_refused = list(getattr(res, "csv_refused", None) or [])
        rep.held = list(getattr(res, "held", None) or [])
        _rows = [r for s in passing for r in s.parsed.rows] + [r for s in csv_sts for r in s.parsed.rows]
        if any(_fee_row(r, mod.FEE_REVERSAL_RX) for r in _rows):
            _bsc = _strip_root(ent.bank_service_charge_account)
            if not _bsc:
                return _error(f"ERROR: entity {entity_key!r} has no bank_service_charge_account in entities.yaml, "
                              f"and this run has card fees to book. Add it (the account fees, interest and GST on "
                              f"fees go to). Nothing was booked.")
            _bad = _usable(_bsc)
            if _bad:
                return _error(f"ERROR: entity {entity_key!r} bank_service_charge_account: {_bad}. Nothing was booked.")
        if any(_cashback_row(r) for r in _rows):
            _draw = [_strip_root(d) for d in ent.drawings_accounts]
            if len(_draw) != 1:
                return _error(f"ERROR: entity {entity_key!r} needs exactly one drawings_accounts entry for card "
                              f"cashback / CRED overage ({len(_draw)} found). Nothing was booked.")
            _bad = _usable(_draw[0])
            if _bad:
                return _error(f"ERROR: entity {entity_key!r} drawings account: {_bad}. Nothing was booked.")

        # ---- payments ------------------------------------------------------
        pool_pay = _payment_pool(view, start, end)
        # CC-29: a payment already booked as a prepayment (its row is in the book) is the user's earlier
        # decision read back, not a new marking. It leaves the pool, so it can never be paired to a card.
        _book_nums = {t.num for t in view.txns if t.num}
        rep.prepaid_earlier = [p for p in pool_pay if J.prepay_num(p.guid) in _book_nums]
        _prepaid_ids = {p.guid for p in rep.prepaid_earlier}
        pool_pay = [p for p in pool_pay if p.guid not in _prepaid_ids]
        lines = M.pay_lines(res.statements)          # lines on every statement in range; refusal is decided at settlement
        # CC-27: the payment rows of a CSV export are matched to the bank payments by the same rule
        # (same amount tolerance, same date window, one-to-one) in the SAME pool as the PDF lines, so a
        # bank payment can never pair with two rows. A CSV pairing settles nothing: it only confirms the
        # payment. The row itself is still never booked.
        csv_lines = M.pay_lines(csv_sts)
        csv_ids = {id(s) for s in csv_sts}
        outcome = M.match_payments(lines + csv_lines, pool_pay, cred_max, cards)
        rep.pairings = [pr for pr in outcome.pairings if id(pr.line.stmt) not in csv_ids]
        rep.csv_pairings = [pr for pr in outcome.pairings if id(pr.line.stmt) in csv_ids]
        rep.lines_unmatched = [ln for ln in outcome.lines_unmatched if id(ln.stmt) not in csv_ids]
        rep.csv_lines_unmatched = [ln for ln in outcome.lines_unmatched if id(ln.stmt) in csv_ids]
        rep.ambiguous = outcome.ambiguous
        settlements = M.settle(rep.pairings, res.statements, res.pool, ties_by_id, mod.find_prior, mod.is_pass,
                               csv_statements=csv_sts, csv_pairings=rep.csv_pairings, range_end=end)
        rep.settlements = settlements

        flagged = {p.guid: (p, outcome.unmatched_reason[p.guid]) for p in outcome.payments_unmatched if p.in_range}
        for s in settlements:
            if s.status in (M.S_REFUSED, M.S_BEFORE, M.S_NO_PRIOR):
                for p in s.payments:
                    if p.in_range:
                        flagged[p.guid] = (p, s.reason)
        rep.unmatched_payments = list(flagged.values())

        # ---- CC-29: payments the user marked as corporate card prepayments -------------------------
        # Only a payment that no statement line took, in range, on or after ccp_split_from. Nothing is
        # marked automatically. A code that cannot be applied is reported and books nothing.
        _paired = {p.guid for pr in outcome.pairings for p in pr.payments}
        _paired |= {p.guid for s in settlements for p in s.payments}
        _unpaired = {p.guid for p in outcome.payments_unmatched if p.in_range and p.guid not in _paired}
        _by_code = {J.prepay_code(p.guid): p for p in pool_pay if p.in_range}
        prepay_js: dict = {}
        for tok in mark_tokens:
            p = _by_code.get(tok)
            if p is None:
                rep.issues.append(f"prepayment code {tok}: no bank payment to the card pool in this range has that "
                                  f"code; nothing was booked for it")
            elif p.guid in _paired:
                rep.issues.append(f"prepayment code {tok}: the {p.date:%d %b %Y} payment of {p.amount:,.2f} is paired "
                                  f"to a statement, so it cannot be a prepayment; nothing was booked for it")
            elif p.date < split_from:
                rep.issues.append(f"prepayment code {tok}: the {p.date:%d %b %Y} payment is before ccp_split_from "
                                  f"({split_from:%d %b %Y}); nothing was booked for it")
            elif p.guid not in _unpaired:
                rep.issues.append(f"prepayment code {tok}: the {p.date:%d %b %Y} payment is not an unmatched "
                                  f"payment; nothing was booked for it")
            else:
                prepay_js[p.guid] = J.prepay_journal(p, prepay_acct)
        rep.unmatched_payments = [(p, why) for p, why in rep.unmatched_payments if p.guid not in prepay_js]
        rep.prepayments = [(p, prepay_js[p.guid]) for p in sorted(_by_code.values(), key=lambda x: (x.date, x.amount))
                           if p.guid in prepay_js]
        rep.unmatched_codes = {p.guid: J.prepay_code(p.guid) for p, _w in rep.unmatched_payments
                               if p.guid in _unpaired and split_from is not None and p.date >= split_from}

        settled_ids = {id(s.target) for s in settlements if s.target is not None}
        run_ids = {id(s) for s in res.statements}
        for st in passing:
            if id(st) in settled_ids:
                continue
            nxt = next((o for o in res.pool if o is not st and mod.find_prior(o, res.pool) is st), None)
            if nxt is not None and id(nxt) in run_ids:
                rep.unpaid.append(st)
                # CC-32: say when the next statement DOES show payment lines that matched no bank payment
                bad = [ln for ln in rep.lines_unmatched if ln.stmt is nxt] +                       [a["line"] for a in rep.ambiguous if a.get("line") is not None and a["line"].stmt is nxt]
                if bad:
                    tot = sum(ln.amount for ln in bad)
                    rep.unpaid_why[id(st)] = (f"the next statement shows {len(bad)} payment line(s) ({tot:,.2f}) "
                                              f"that matched no bank payment (see 'Payments without statement' "
                                              f"and the payment lines with no bank payment)")
            else:
                rep.awaiting.append(st)

        _mark("match")
        # ---- the EMI interest account (soft: a problem withholds the interest rows only) ----
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
            if id(s.target) in untied_ids:
                for j in js:
                    j.untied = ties_by_id[id(s.target)].result
            # CC-39: a statement of a card with its own sub-account, paid on or after ccp_split_from, is
            # booked against that sub-account, and each bank payment is moved onto it
            sub, sub_note = _split_target(s, cards, split_from)
            if sub_note:
                rep.issues.append(sub_note)
            if sub:
                bad_sub = _usable(sub)
                if bad_sub:
                    rep.split_refused.append((s, sub, bad_sub))
                    continue        # nothing is booked for this statement; it never falls back to the parent
                for j in js:
                    j.transfer = sub
                moves = [J.move_journal(p, sub, s.target.card, s.target.label, s.target.source, s.target.card)
                         for p in s.payments]
                rep.splits.append({"card": s.target.card, "statement": s.target.label, "sub": sub,
                                   "payments": list(s.payments), "moves": moves, "journals": js})
                journals += moves
            per_statement[id(s.target)] = js
            journals += js
        # ---- CC-31: a CRED payment line whose prior period is a whole-booked CSV settles no PDF statement,
        # but the card was credited more than the bank sent: its overage is booked once, exactly as for a
        # settled statement (Dr CCP / Cr Drawings), so the CCP balance is not left off by the difference ----
        # (held aside and merged into the view after the CC-26 and CC-24 loops, so the statement that was
        # paid keeps its own rows booked by those loops)
        csv_prior_ov = []
        for s in settlements:
            if s.status != M.S_CSV_PRIOR:
                continue
            for pr in s.pairings:
                ov = J.overage_journal(pr, min(p.date for p in pr.payments))
                if ov is not None:
                    csv_prior_ov.append((id(s.paid_on), ov))
        # ---- CC-26: an untied statement that no payment settled is booked on its real dates ----
        for st in res.statements:
            if id(st) not in untied_ids or id(st) in per_statement:
                continue
            js, _emi, iss = J.journals_for_settlement(st, None, mod.FEE_REVERSAL_RX, threshold,
                                                      book_interest=not emi_bad)
            rep.issues += iss
            for j in js:
                j.untied = ties_by_id[id(st)].result
            per_statement[id(st)] = js
            journals += js
        # ---- CC-24: CSV rows, same classification and guards, no settling payment ----
        for cst in csv_sts:
            js, _emi, iss = J.journals_for_settlement(cst, None, mod.FEE_REVERSAL_RX, threshold,
                                                      book_interest=not emi_bad, num_label="CSV")
            rep.issues += iss
            for j in js:
                j.statement = csv_label(cst)
            per_statement[id(cst)] = js
            journals += js
        for sid, ov in csv_prior_ov:
            per_statement.setdefault(sid, []).append(ov)
            journals.append(ov)
        # ---- CC-33: the CRED difference of a PARTLY SETTLED statement and of a CSV payment row matched to a
        # bank payment is booked too (Dr CCP / Cr Drawings, on the payment date). Held to the end so it can
        # never stop a statement or a CSV from booking its own rows, and kept out of the per-statement view
        # (a PARTLY statement's spends are NOT booked). One journal per pairing: the Num comes from the bank
        # payment(s), the line date and the amount, so a pairing already seen in this run (SETTLED or
        # CSV-prior) is skipped here, and a later run that settles the statement gives the same Num, which
        # the already-booked check catches ----
        _ov_seen = {j.num for j in journals if j.kind == J.K_OVERAGE}
        _not_closed = {id(pr) for s in settlements if s.status not in (M.S_SETTLED, M.S_PARTLY, M.S_CSV_PRIOR)
                       for pr in s.pairings}
        _cc33 = [(pr, s.target) for s in settlements if s.status == M.S_PARTLY for pr in s.pairings]
        _cc33 += [(pr, None) for pr in rep.csv_pairings if id(pr) not in _not_closed]
        for pr, tgt in _cc33:
            ov = J.overage_journal(pr, min(p.date for p in pr.payments))
            if ov is None or ov.num in _ov_seen:
                continue
            _ov_seen.add(ov.num)
            if tgt is not None and id(tgt) in untied_ids:
                ov.untied = ties_by_id[id(tgt)].result
            if id(pr.line.stmt) in csv_ids:
                ov.statement = csv_label(pr.line.stmt)
            journals.append(ov)
        rep.issues += J.withhold_emi_originals(journals, res.emi_rows)
        J.pair_fee_reversals(journals)
        booked_rows = {j.row_id for j in journals if j.emi}
        rep.emi = J.emi_summary(res.emi_rows)
        # an offset leg (instalment credit, loan conversion credit) is understood and never booked:
        # it is counted, not listed as awaiting a decision
        rep.emi["explained"] = sum(1 for r in res.emi_rows if getattr(r, "explained", False))
        rep.emi["rows"] = [r for r in res.emi_rows
                           if id(r) not in booked_rows and not getattr(r, "explained", False)]
        left = {id(r) for r in rep.emi["rows"]}
        for s in settlements:
            if s.status == M.S_SETTLED and any(id(r) in left for r in s.target.parsed.rows):
                rep.partly_booked.append((s.target, "has EMI rows that are not booked; only its other rows are booked"))
        for st, _t in rep.untied:
            settled_targets = {id(s.target) for s in settlements if s.status == M.S_SETTLED}
            if id(st) in per_statement and id(st) not in settled_targets \
                    and any(id(r) in left for r in st.parsed.rows):
                rep.partly_booked.append((st, "has EMI rows that are not booked; only its other rows are booked"))
        rep.journals = journals
        # CC-23: inputs for the read-only payments-and-statements view (it books and changes nothing)
        rep.view_statements = list(res.statements)
        rep.view_per_statement = per_statement
        rep.view_pool = pool_pay
        rep.view_range = (start, end)

        _mark("journals")
        # ---- fixed accounts (backstop: the early check above already covers the normal case) ----
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
        if any(j.kind in (J.K_CASHBACK, J.K_CASHBACK_REV, J.K_OVERAGE) for j in journals):
            draw = [_strip_root(d) for d in ent.drawings_accounts]
            if len(draw) != 1:
                return _error(f"ERROR: entity {entity_key!r} needs exactly one drawings_accounts entry for card "
                              f"cashback / CRED overage ({len(draw)} found). Nothing was booked.")
            bad = _usable(draw[0])
            if bad:
                return _error(f"ERROR: entity {entity_key!r} drawings account: {bad}. Nothing was booked.")
            for j in journals:
                if j.kind in (J.K_CASHBACK, J.K_CASHBACK_REV, J.K_OVERAGE):
                    j.account, j.confidence, j.match_reason = draw[0], "high", _MAPPER_DRAW_REASON

        # ---- already booked (before mapping: an unmapped journal is checked too) ----
        journals += list(prepay_js.values())
        J.mark_booked(journals, view.txns, CCP_PATH)
        _reconcile_splits(rep, journals)
        _mark("already-booked check")

        # ---- mapper for spends and refunds ---------------------------------
        # CC-12: a row withheld as POSSIBLY ALREADY BOOKED is mapped too, so the report still shows
        # the account and confidence the mapper chose; its status stays withheld.
        to_map = [j for j in journals if j.needs_mapping and j.status in (J.ST_READY, J.ST_POSSIBLY)]
        if to_map:
            cin, cout = Path(tmp) / "cc_canonical.csv", Path(tmp) / "cc_mapped_GnuCash_import_ready.csv"
            _canonical_csv(to_map, cin)
            _clear_mapper_ai_status()
            try:
                _run_mapper(gnucash_path, cin, cout, config_path, [_strip_root(d) for d in ent.drawings_accounts],
                            _strip_root(getattr(ent, "card_spend_default_account", "")), model_override)
                with open(cout, newline="", encoding="utf-8") as f:
                    mapped = {r.get("Transaction ID", ""): r for r in csv.DictReader(f)}
            except Exception as e:  # noqa: BLE001
                mapped = {}
                rep.issues.append(f"the account mapper failed ({type(e).__name__}: {e}); spends and refunds are "
                                  f"withheld, nothing was guessed")
            rep.ai_stop, rep.ai_stop_suspense = _ai_stop_from(mapped)
            if rep.ai_stop:
                # written to the run log as well as the result
                _log_line(f"WARNING: AI pass stopped - {rep.ai_stop}. {rep.ai_stop_suspense} row(s) went to Suspense; "
                          f"the result is NOT complete.")
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
                        # never labelled higher than the purchase it mirrors
                        j.account, j.confidence = orig.account, orig.confidence
                        j.match_reason = "refund of an identical purchase in this run"

        _mark("map")
        # ---- guard on every account ------------------------------------------
        suspense = next((p for p in sorted(view.paths) if p.rsplit(":", 1)[-1] == "Suspense"),
                        J.SUSPENSE_DEFAULT)
        for j in journals:
            if j.status != J.ST_READY:
                continue
            if not j.account:
                j.status, j.why = J.ST_NO_ACCOUNT, j.why or "the mapper found no account"
                continue
            if j.emi and j.kind == J.K_SPEND and emi_acct and j.account == emi_acct:
                # principal is the item's cost, never the interest account
                j.status, j.why = J.ST_NO_ACCOUNT, "the mapper chose the EMI interest account for EMI principal"
                continue
            if j.needs_mapping and j.account.startswith(CCP_PATH + ":"):
                j.status, j.why = J.ST_NO_ACCOUNT, "the mapper chose a card payment sub-account as the expense"
                continue
            bad = _usable(j.account)
            if bad:
                j.status, j.why = (J.ST_BLOCKED if "cannot be used" in bad else J.ST_NO_ACCOUNT), bad
                continue
            # CC-10: a card spend can never hit an income account, and only an explicit saved
            # user rule may put one on an asset account. Read from the account TYPE in the book.
            if j.needs_mapping:
                kind_bad = _impossible_target(guard, j)
                if kind_bad:
                    was = j.account
                    j.status, j.why = J.ST_REVIEW, kind_bad
                    j.account, j.confidence = suspense, "suspense"
                    j.match_reason = f"{j.match_reason} (was: {was})".strip()

        # ---- CCP tie-out -------------------------------------------------------
        # CC-39: the pool is the parent plus every card sub-account; parent + subs must equal what the parent
        # alone showed before any split. With no sub-account in the book this is the old parent-only figure.
        _in_rng = [t for t in view.txns if start <= t.date <= end]
        before = round(sum(SP.pool_amount(t, CCP_PATH) for t in _in_rng), 2)
        parent_before = round(sum(t.amount_on(CCP_PATH) for t in _in_rng), 2)
        subs_before: dict = {}
        for t in _in_rng:
            for pth, v in t.splits:
                if pth != CCP_PATH and SP.in_pool(pth, CCP_PATH):
                    subs_before[pth] = round(subs_before.get(pth, 0.0) + v, 2)
        counted = [j for j in journals if j.status == J.ST_READY or
                   (j.status == J.ST_BOOKED and not (start <= j.date <= end))]
        ptie = SP.pool_tie(counted, CCP_PATH)
        effect = ptie["new_total"]
        _mp = SP.moves_problem(rep.splits)
        if not ptie["ok"]:
            rep.pool_failure = (f"parent + card sub-accounts change by {ptie['new_total']:,.2f} but the parent alone "
                                f"would have changed by {ptie['old_total']:,.2f} (difference {ptie['gap']:,.2f})")
        elif _mp:
            rep.pool_failure = _mp
        unmatched = rep.unmatched_total
        not_booked = round(sum(p.amount for s in settlements if s.status == M.S_PARTLY
                               for p in s.payments if p.in_range), 2)
        not_booked = round(not_booked + sum(p.amount for s, _sub, _why in rep.split_refused
                                            for p in s.payments if p.in_range), 2)
        # CC-33: the CRED difference of a PARTLY statement is booked on its own (Dr CCP / Cr Drawings), while
        # the statement's spends are not: it belongs with the not-booked payments, booked now or earlier
        _partly_ov = [J.overage_journal(pr, min(p.date for p in pr.payments)) for s in settlements
                      if s.status == M.S_PARTLY for pr in s.pairings]
        _partly_ov = {j.num: j.amount for j in _partly_ov if j is not None}
        not_booked = round(not_booked + sum(_partly_ov.values()), 2)
        after = round(before + effect, 2)
        n_settled = sum(1 for s in settlements if s.status == M.S_SETTLED)
        other = round(after - unmatched - not_booked, 2)
        # CC-12: "Other" is listed, entry by entry: the book entries on CCP that are neither an
        # unmatched payment nor a not-booked payment, this run's journals, and a balancing line
        # for whatever is left (so the lines always add up to Other).
        _skip = {p.guid for p, _r in rep.unmatched_payments}
        _skip |= {p.guid for s in settlements if s.status == M.S_PARTLY for p in s.payments if p.in_range}
        _skip |= {t.guid for t in view.txns if t.num in _partly_ov and start <= t.date <= end}
        _csv_guids = {p.guid for pr in rep.csv_pairings for p in pr.payments}
        other_lines = [(t.date, (t.description or "(no description)")
                        + (" [matched to a CSV payment row]" if t.guid in _csv_guids else ""), SP.pool_amount(t, CCP_PATH))
                       for t in sorted(view.txns, key=lambda x: (x.date, x.guid))
                       if start <= t.date <= end and any(SP.in_pool(pth, CCP_PATH) for pth, _v in t.splits)
                       and t.guid not in _skip]
        other_lines.append((None, "This run's journals (CCP effect)", effect))
        rest = round(other - sum(a for _d, _x, a in other_lines), 2)
        if abs(rest) >= 0.005:
            other_lines.append((None, "Not attributable to one entry", rest))
        rep.tie = {"before": before, "effect": effect, "after": after, "unmatched": unmatched,
                   "not_booked": not_booked, "other": other, "other_lines": other_lines,
                   "tol": max(1.0, float(n_settled)),
                   "parent_before": parent_before, "subs_before": subs_before,
                   "parent_effect": ptie["parent_effect"], "subs_effect": ptie["subs_effect"],
                   "old_effect": ptie["old_total"], "pool_gap": ptie["gap"], "pool_ok": not rep.pool_failure}

        # ---- output ------------------------------------------------------------
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        ready = [] if rep.pool_failure else rep.ready
        if ready:
            _write_import_csv(ready, out)
            rep.output_csv = str(out)
        xlsx = out.with_name(out.stem + "_report.xlsx")
        write_workbook(rep, str(xlsx))
        rep.output_xlsx = str(xlsx)

        _mark("write")
    msg = build_message(rep)
    msg += "\n\nTiming: " + ", ".join(_marks)
    return ReplyWithOutputs(msg, [extra_output("workbook", rep.output_xlsx, "Journals, payments, fees, EMI rows, CCP tie-out")],
                            withhold_primary=not ready)
