"""
lots.py -- Capital-gains lot reconstruction from the .gnucash book.

Per the plan (2026-07-12-itr-workbook-skill-plan.md, section 6.1), proven on
the real corpus:
  - Sale-transaction anatomy: one split per disposed lot on the stock account
    (negative qty, negative cost basis); one broker/bank proceeds split; one
    LTCG/STCG income split carrying the aggregate booked gain/loss for the
    whole transaction.
  - Buy-date attribution: match each sale-split's (qty, cost) against the
    account's historical buy splits. The real corpus showed three shapes,
    all handled here:
      1. exact single whole-lot match (qty and cost both match one buy lot),
      2. a single old lot **partially** disposed of at a proportional cost
         (e.g. 200 of a 70,000-share holding at 200/70000 of its cost --
         "LTCG on a multi-decade-old holding at proportional average cost"),
      3. two or more whole buy lots consumed together in one sell-split
         (qty and cost both equal the FIFO-ordered running sum).
    Ambiguous or missing match => lot flagged "unattributed -- review",
    never guessed.
  - FIFO validation: the matched buy lot(s) must be the earliest still-
    available (unconsumed) lot(s) for that account as of the sale date.
    Violations are flagged, never auto-corrected. Every historical sale
    (not only ones inside the target FY) must be replayed in date order to
    track consumption correctly -- otherwise a lot disposed of years before
    the target FY still looks "available" and a later, legitimate disposal
    is misflagged. This is exactly the false-positive the plan describes
    dissolving on inspection of the real books.
  - Reconciliation invariant: Sigma(lot proceeds - lot cost) == booked gain
    split for the transaction (+/- 0.01).
  - Tier-3 straddle patch (Batch 3 carry-forward from B2 review): a Tier-3
    match consumes two or more whole buy lots in one sell-split. When those
    lots sit on opposite sides of the LT/ST holding-period boundary (12
    months) or the 31-01-2018 grandfathering cutoff, merging them into one
    row would hide a classification difference that matters for tax
    computation -- so the match is split back into one row per consumed
    lot (qty/cost are already known exactly per lot; gain is allocated
    pro-rata by each lot's share of the split's proceeds). Non-straddling
    Tier-3 matches keep the existing single merged row (same tax
    treatment either way, so splitting would be noise). A match where a
    straddle is detected but a clean per-lot split isn't possible is
    flagged STRADDLE_UNRESOLVED rather than silently merged.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta
from fractions import Fraction

from parse_gnucash import Book, fy_window

GAIN_ACTIONS = {"LTCG", "STCG"}
_STOCK_TYPES = {"STOCK", "MUTUAL"}
UNATTRIBUTED = "unattributed — review"
FIFO_VIOLATION = "FIFO violation — review"
STRADDLE_UNRESOLVED = "straddle — review"

_LT_HOLDING_PERIOD = timedelta(days=365)  # long-term: held > 12 months
_GRANDFATHER_CUTOFF = date(2018, 1, 31)   # s.112A grandfathering cutoff


@dataclass
class _BuyLot:
    txn_guid: str
    date: date
    original_qty: Fraction
    original_cost: Fraction
    remaining_qty: Fraction
    remaining_cost: Fraction

    @property
    def consumed(self) -> bool:
        return self.remaining_qty <= 0


@dataclass
class Lot:
    scrip: str
    account_guid: str
    sale_txn_guid: str
    sale_date: date
    qty: float
    cost: float
    proceeds: float
    gain: float
    buy_date: date | None
    buy_txn_guid: str | None
    attribution: str  # "matched", UNATTRIBUTED, or STRADDLE_UNRESOLVED
    fifo_flag: str | None  # None (OK) or FIFO_VIOLATION
    straddle_split: bool = False  # True if this row came from splitting a
                                  # Tier-3 multi-lot match across an LT/ST or
                                  # grandfathering boundary


@dataclass
class SaleReconciliation:
    sale_txn_guid: str
    sale_date: date
    booked_gain: float
    lot_gain_sum: float
    lots: list[Lot] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return abs(self.booked_gain - self.lot_gain_sum) <= 0.01


def _buy_lots(book: Book, account_guid: str) -> list[_BuyLot]:
    lots = []
    for txn in book.transactions:
        for sp in txn.splits:
            if sp.account_guid == account_guid and sp.quantity > 0:
                lots.append(_BuyLot(
                    txn_guid=txn.guid, date=txn.date_posted,
                    original_qty=sp.quantity, original_cost=sp.value,
                    remaining_qty=sp.quantity, remaining_cost=sp.value,
                ))
    lots.sort(key=lambda bl: bl.date)
    return lots


def _sale_transactions(book: Book):
    """Yield (txn, stock_splits, gain_splits, proceeds_splits) for every
    transaction that disposes of at least one STOCK/MUTUAL lot."""
    for txn in book.transactions:
        stock_splits, gain_splits, other_splits = [], [], []
        for sp in txn.splits:
            acct = book.accounts.get(sp.account_guid)
            if acct is None:
                continue
            if acct.type in _STOCK_TYPES and sp.quantity < 0:
                stock_splits.append(sp)
            elif acct.type == "INCOME" and sp.action in GAIN_ACTIONS:
                gain_splits.append(sp)
            else:
                other_splits.append(sp)
        if stock_splits:
            yield txn, stock_splits, gain_splits, other_splits


_COST_TOLERANCE = 0.01  # paisa-level rounding observed in the real corpus


def _cost_close(a: Fraction, b: Fraction) -> bool:
    return abs(float(a) - float(b)) <= _COST_TOLERANCE


def _find_match(qty: Fraction, cost: Fraction, available: list[_BuyLot], as_of: date):
    """Try, in order: (1) exact single whole-lot match, (2) a single lot
    partially disposed of at proportional cost, (3) a FIFO-ordered run of
    two-or-more whole lots summing exactly to (qty, cost). Returns a list of
    (lot, qty_taken, cost_taken) tuples, or None if no unique match exists.
    Quantities must match exactly; costs are compared with a paisa-level
    tolerance since the real corpus's proportional-cost lots are rounded to
    2 decimals rather than carrying the exact mathematical fraction."""
    candidates = [bl for bl in available if not bl.consumed and bl.date <= as_of]

    # Tier 1: exact single whole-lot match.
    whole_matches = [bl for bl in candidates if bl.remaining_qty == qty and _cost_close(bl.remaining_cost, cost)]
    if len(whole_matches) == 1:
        return [(whole_matches[0], qty, whole_matches[0].remaining_cost)]
    if len(whole_matches) > 1:
        return None  # ambiguous -- never guess

    # Tier 2: single lot, partial (proportional-cost) disposal.
    partial_matches = [
        bl for bl in candidates
        if bl.remaining_qty > qty and _cost_close(bl.remaining_cost * qty / bl.remaining_qty, cost)
    ]
    if len(partial_matches) == 1:
        bl = partial_matches[0]
        proportional_cost = bl.remaining_cost * qty / bl.remaining_qty
        return [(bl, qty, proportional_cost)]
    if len(partial_matches) > 1:
        return None

    # Tier 3: FIFO-ordered run of whole lots consumed together.
    ordered = sorted(candidates, key=lambda bl: bl.date)
    running_qty = Fraction(0)
    running_cost = Fraction(0)
    run: list[_BuyLot] = []
    for bl in ordered:
        run.append(bl)
        running_qty += bl.remaining_qty
        running_cost += bl.remaining_cost
        if running_qty == qty:
            if _cost_close(running_cost, cost):
                return [(bl, bl.remaining_qty, bl.remaining_cost) for bl in run]
            return None  # qty lines up but cost doesn't -- not this run, don't guess further
        if running_qty > qty:
            break  # overshot without an exact qty match -- no combination fits

    return None


def _term(buy_date: date, sale_date: date) -> str:
    return "LT" if (sale_date - buy_date) >= _LT_HOLDING_PERIOD else "ST"


def _straddles_boundary(match, sale_date: date) -> bool:
    """True if the matched lots don't all agree on LT/ST term AND on which
    side of the 31-01-2018 grandfathering cutoff they fall -- i.e. merging
    them into one row would hide a classification difference."""
    terms = {_term(bl.date, sale_date) for bl, _, _ in match}
    pre_cutoff = {bl.date <= _GRANDFATHER_CUTOFF for bl, _, _ in match}
    return len(terms) > 1 or len(pre_cutoff) > 1


def reconstruct_lots(book: Book, year_key: str) -> list[SaleReconciliation]:
    """Reconstruct per-lot CG rows for every disposal transaction posted
    within the FY window for `year_key`."""
    start, end = fy_window(year_key)
    buy_lots_cache: dict[str, list[_BuyLot]] = {}
    results: list[SaleReconciliation] = []

    for txn, stock_splits, gain_splits, proceeds_splits in _sale_transactions(book):
        in_window = start <= txn.date_posted <= end

        total_proceeds = sum((sp.value for sp in proceeds_splits), Fraction(0))
        total_gain_raw = sum((sp.value for sp in gain_splits), Fraction(0))
        booked_gain = -float(total_gain_raw)  # INCOME flip (plan section 3.2)
        total_qty = sum((-sp.quantity for sp in stock_splits), Fraction(0))

        # Pass 1: resolve matches for every disposed split in this transaction
        # WITHOUT mutating lot state yet -- lots disposed together in the same
        # sale must not count each other as "an earlier lot left available"
        # during the FIFO check, and a multi-lot Tier-3 match must reserve its
        # lots before a sibling split's Tier-1 lookup runs.
        pending = []
        reserved_ids: set[int] = set()
        for sp in stock_splits:
            acct = book.accounts[sp.account_guid]
            qty = -sp.quantity
            cost = -sp.value
            if acct.guid not in buy_lots_cache:
                buy_lots_cache[acct.guid] = _buy_lots(book, acct.guid)
            available = [bl for bl in buy_lots_cache[acct.guid] if id(bl) not in reserved_ids]
            match = _find_match(qty, cost, available, txn.date_posted)
            pending.append((sp, acct, qty, cost, match))
            if match is not None:
                reserved_ids.update(id(bl) for bl, _, _ in match)

        lots: list[Lot] = []
        for sp, acct, qty, cost, match in pending:
            proceeds_share = total_proceeds * (qty / total_qty) if total_qty else Fraction(0)

            if match is None:
                lots.append(Lot(
                    scrip=acct.name, account_guid=acct.guid, sale_txn_guid=txn.guid,
                    sale_date=txn.date_posted, qty=float(qty), cost=float(cost),
                    proceeds=float(proceeds_share), gain=float(proceeds_share) - float(cost),
                    buy_date=None, buy_txn_guid=None,
                    attribution=UNATTRIBUTED, fifo_flag=None,
                ))
                continue

            buy_date = min(bl.date for bl, _, _ in match)
            matched_lot_ids = {id(bl) for bl, _, _ in match}
            earlier_available = [
                bl for bl in buy_lots_cache[acct.guid]
                if id(bl) not in matched_lot_ids and not bl.consumed
                and id(bl) not in reserved_ids
                and bl.date < buy_date
            ]
            fifo_flag = FIFO_VIOLATION if earlier_available else None

            if len(match) > 1 and _straddles_boundary(match, txn.date_posted):
                # Tier-3 straddle patch: split back into one row per
                # consumed lot instead of merging across an LT/ST or
                # grandfathering boundary. qty/cost per lot are already
                # known exactly; gain is allocated pro-rata by each lot's
                # share of this split's proceeds.
                for bl, qty_taken, cost_taken in match:
                    lot_proceeds = proceeds_share * (qty_taken / qty) if qty else Fraction(0)
                    lots.append(Lot(
                        scrip=acct.name, account_guid=acct.guid, sale_txn_guid=txn.guid,
                        sale_date=txn.date_posted, qty=float(qty_taken), cost=float(cost_taken),
                        proceeds=float(lot_proceeds), gain=float(lot_proceeds) - float(cost_taken),
                        buy_date=bl.date, buy_txn_guid=bl.txn_guid,
                        attribution="matched", fifo_flag=fifo_flag, straddle_split=True,
                    ))
            else:
                buy_txn_guid = min(match, key=lambda m: m[0].date)[0].txn_guid
                lots.append(Lot(
                    scrip=acct.name, account_guid=acct.guid, sale_txn_guid=txn.guid,
                    sale_date=txn.date_posted, qty=float(qty), cost=float(cost),
                    proceeds=float(proceeds_share), gain=float(proceeds_share) - float(cost),
                    buy_date=buy_date, buy_txn_guid=buy_txn_guid,
                    attribution="matched", fifo_flag=fifo_flag,
                ))

        # Pass 2: now that FIFO checks are done, apply the consumption.
        for sp, acct, qty, cost, match in pending:
            if match is None:
                continue
            for bl, qty_taken, cost_taken in match:
                bl.remaining_qty -= qty_taken
                bl.remaining_cost -= cost_taken

        if in_window:
            results.append(SaleReconciliation(
                sale_txn_guid=txn.guid, sale_date=txn.date_posted,
                booked_gain=booked_gain, lot_gain_sum=sum(lot.gain for lot in lots),
                lots=lots,
            ))

    results.sort(key=lambda r: r.sale_date)
    return results


def all_lots(reconciliations: list[SaleReconciliation]) -> list[Lot]:
    return [lot for r in reconciliations for lot in r.lots]
