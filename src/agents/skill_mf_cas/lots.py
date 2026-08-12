"""
lots.py -- FIFO capital-gains lot derivation for the MF CAS skill.

Mirrors the discipline of skill_itr_workbook/scripts/lots.py (read that
module first -- this is the same philosophy applied to a different, simpler
input): FIFO per folio+scheme, one output row per CONSUMED buy lot (never
merged), never guess an attribution that isn't certain, and a reconciliation
invariant that is checked and reported rather than silently trusted.

Unlike the GnuCash lot reconstruction, the CAS statement gives an explicit,
already-ordered sequence of acquisition/disposal events -- there is no
"match a sale split against historical buy splits" ambiguity problem here.
The one genuine source of uncertainty is a nonzero OPENING unit balance: it
represents units acquired before the statement's own window, with no buy
date/NAV/cost visible anywhere in this statement. Modelling it as a lot with
an unknown buy date, and flagging any disposal that has to dip into it as
"unattributed -- review", is this module's answer to "never guess" for that
case -- no cost is invented, no date is invented.

Rate/slab/exemption computation, indexation, and LT/ST classification are
explicitly OUT of scope here (see AGENT.md's v1 non-goals) -- this module
emits raw facts only: units, dates, NAVs, cost, proceeds, holding_days, gain,
and flags. The consumer (the ITR workbook and its tax_rules config) applies
thresholds and rates.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .parser import ACQUISITION, DISPOSAL, SchemeBlock

UNATTRIBUTED = "unattributed -- review"
GRANDFATHER_FLAG = "REVIEW - FMV 31-01-2018 needed"
RECONCILIATION_BREACH = "reconciliation breach -- review"

_GRANDFATHER_CUTOFF = date(2018, 1, 31)  # s.112A grandfathering cutoff

# Reconciliation tolerance in UNITS: opening + acquisitions - disposals must
# equal closing (and sum of matched lot units must equal disposed units)
# within this many units, or the scheme is reported as a breach. Named per
# repo convention (magic thresholds get a module-level constant + comment).
UNITS_RECONCILIATION_TOLERANCE = 0.001


@dataclass
class _AcqLot:
    date: date | None  # None => the synthetic OPENING-balance lot
    units: float
    cost: float | None  # None when date is None (cost is unknowable)
    nav: float | None
    remaining_units: float = 0.0
    remaining_cost: float | None = None

    def __post_init__(self):
        self.remaining_units = self.units
        self.remaining_cost = self.cost


@dataclass
class DisposalLot:
    folio: str
    scheme: str
    isin: str
    scheme_type: str
    units: float
    buy_date: date | None
    buy_nav: float | None
    buy_cost: float | None
    sell_date: date
    sell_nav: float
    sale_proceeds: float
    holding_days: int | None
    gain: float | None
    flags: list[str] = field(default_factory=list)


@dataclass
class SchemeReconciliation:
    folio: str
    scheme: str
    isin: str
    units_ok: bool
    units_expected_closing: float | None
    units_actual_closing: float | None
    units_diff: float | None
    matched_vs_disposed_ok: bool
    disposal_lots: list[DisposalLot] = field(default_factory=list)
    rta_gain_reported: float | None = None
    rta_gain_derived: float | None = None
    rta_gain_variance: float | None = None


def _term_days(buy_date: date | None, sell_date: date) -> int | None:
    if buy_date is None:
        return None
    return (sell_date - buy_date).days


def derive_scheme(scheme: SchemeBlock) -> SchemeReconciliation:
    """FIFO-derive realised-gain lots for one folio+scheme.

    Replays the scheme's own transactions in date order (the CAS statement
    is assumed already date-ordered; this module does not re-sort within a
    date, preserving the statement's own tie-break for same-day rows).
    """
    lots: list[_AcqLot] = []
    if scheme.opening_units and scheme.opening_units > 0:
        lots.append(_AcqLot(date=None, units=scheme.opening_units, cost=None, nav=None))

    disposal_lots: list[DisposalLot] = []
    total_disposed_units = 0.0
    total_matched_units = 0.0

    for txn in scheme.transactions:
        if txn.kind == ACQUISITION:
            lots.append(_AcqLot(date=txn.date, units=txn.units, cost=txn.amount, nav=txn.nav))
        elif txn.kind == DISPOSAL:
            qty_needed = -txn.units  # units is negative on a disposal row
            total_disposed_units += qty_needed
            remaining_to_consume = qty_needed

            for lot in lots:
                if remaining_to_consume <= 0:
                    break
                if lot.remaining_units <= 0:
                    continue
                qty_taken = min(lot.remaining_units, remaining_to_consume)
                proceeds_share = txn.amount * (qty_taken / qty_needed) if qty_needed else 0.0
                proceeds_share = abs(proceeds_share)

                flags: list[str] = []
                buy_cost_taken = None
                gain = None
                if lot.date is None:
                    flags.append(UNATTRIBUTED)
                else:
                    buy_cost_taken = (lot.remaining_cost or 0.0) * (qty_taken / lot.remaining_units)
                    gain = proceeds_share - buy_cost_taken
                    if lot.date <= _GRANDFATHER_CUTOFF:
                        flags.append(GRANDFATHER_FLAG)

                disposal_lots.append(DisposalLot(
                    folio=scheme.folio, scheme=scheme.scheme_name, isin=scheme.isin,
                    scheme_type=scheme.scheme_type, units=qty_taken,
                    buy_date=lot.date, buy_nav=lot.nav, buy_cost=buy_cost_taken,
                    sell_date=txn.date, sell_nav=txn.nav, sale_proceeds=proceeds_share,
                    holding_days=_term_days(lot.date, txn.date), gain=gain, flags=flags,
                ))
                total_matched_units += qty_taken

                if lot.date is not None and lot.remaining_cost is not None:
                    lot.remaining_cost -= buy_cost_taken
                lot.remaining_units -= qty_taken
                remaining_to_consume -= qty_taken

            if remaining_to_consume > UNITS_RECONCILIATION_TOLERANCE:
                # Disposal exceeds every available lot (including the
                # OPENING lot) -- never guess a phantom lot to cover it.
                disposal_lots.append(DisposalLot(
                    folio=scheme.folio, scheme=scheme.scheme_name, isin=scheme.isin,
                    scheme_type=scheme.scheme_type, units=remaining_to_consume,
                    buy_date=None, buy_nav=None, buy_cost=None,
                    sell_date=txn.date, sell_nav=txn.nav,
                    sale_proceeds=abs(txn.amount) * (remaining_to_consume / qty_needed) if qty_needed else 0.0,
                    holding_days=None, gain=None, flags=[UNATTRIBUTED],
                ))
                total_matched_units += remaining_to_consume
        # NEITHER rows never touch unit lots.

    expected_closing = None
    units_diff = None
    units_ok = True
    if scheme.opening_units is not None:
        net_acquired = sum(t.units for t in scheme.transactions if t.kind == ACQUISITION)
        net_disposed = sum(-t.units for t in scheme.transactions if t.kind == DISPOSAL)
        expected_closing = scheme.opening_units + net_acquired - net_disposed
        if scheme.closing_units is not None:
            units_diff = expected_closing - scheme.closing_units
            units_ok = abs(units_diff) <= UNITS_RECONCILIATION_TOLERANCE

    matched_vs_disposed_ok = abs(total_matched_units - total_disposed_units) <= UNITS_RECONCILIATION_TOLERANCE

    rta_gain_derived = sum(dl.gain for dl in disposal_lots if dl.gain is not None) or 0.0
    rta_gain_variance = None
    if scheme.rta_realised_gain is not None:
        rta_gain_variance = rta_gain_derived - scheme.rta_realised_gain

    return SchemeReconciliation(
        folio=scheme.folio, scheme=scheme.scheme_name, isin=scheme.isin,
        units_ok=units_ok, units_expected_closing=expected_closing,
        units_actual_closing=scheme.closing_units, units_diff=units_diff,
        matched_vs_disposed_ok=matched_vs_disposed_ok, disposal_lots=disposal_lots,
        rta_gain_reported=scheme.rta_realised_gain, rta_gain_derived=rta_gain_derived if disposal_lots else None,
        rta_gain_variance=rta_gain_variance,
    )


def derive_all(schemes: list[SchemeBlock]) -> list[SchemeReconciliation]:
    return [derive_scheme(s) for s in schemes]
