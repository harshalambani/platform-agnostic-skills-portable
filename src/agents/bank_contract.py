"""
bank_contract.py — The uniform contract every bank statement parser implements.

This module defines the *shape* that all bank skills converge on, so that adding
a new bank becomes "implement one interface" rather than "write a skill plus a
separate adapter". Nothing here performs IO or parsing — it is pure data types
plus a structural protocol. The shared IO tail lives in ``canonical_io.py`` and
the row-level balance checks in ``balance_utils.py``.

Three pieces:
  * ``BalanceCheck`` — the result of a running-balance verification.
  * ``BankResult``   — what every parser returns: canonical rows + metadata.
  * ``BankSkill``    — the structural protocol a bank parser satisfies.

Canonical row schema (8 columns), as produced across the codebase:
    Date, Transaction ID, Description, Account, Deposit, Withdrawal, Balance, Currency
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable


@dataclass(frozen=True)
class BalanceCheck:
    """Outcome of a running-balance verification over canonical rows.

    Mirrors the dict returned by ``balance_utils.verify_running_balance`` in a
    typed, immutable form. Build one with :meth:`from_running` to avoid drift.

    Attributes:
        ok:               True when every row's running balance reconciles.
        mismatches:       Count of rows whose balance did not reconcile.
        first_mismatch:   Human-readable description of the first failing row,
                          or None when ``ok`` is True.
        details:          Up to ``max_warnings`` human-readable mismatch lines.
        opening_balance:  Balance before the first row (derived from row 0).
        closing_balance:  Balance after the last row.
    """
    ok: bool
    mismatches: int = 0
    first_mismatch: str | None = None
    details: tuple[str, ...] = ()
    opening_balance: float = 0.0
    closing_balance: float = 0.0

    @classmethod
    def from_running(cls, running: dict[str, Any]) -> "BalanceCheck":
        """Build a BalanceCheck from a ``verify_running_balance`` result dict."""
        details = tuple(running.get("details", ()) or ())
        return cls(
            ok=bool(running.get("ok", False)),
            mismatches=int(running.get("mismatches", 0)),
            first_mismatch=details[0] if details else None,
            details=details,
            opening_balance=float(running.get("opening_balance", 0.0)),
            closing_balance=float(running.get("closing_balance", 0.0)),
        )


@dataclass(frozen=True)
class RowProvenance:
    """Trace-back reference for one canonical row, to its source line.

    Optional and best-effort: a parser that can't cheaply track this (e.g. a
    tabular reader that already loses the original line on read) may simply
    not emit entries for those rows. ``row_index`` is the index into
    :attr:`BankResult.rows` this entry describes.

    Attributes:
        row_index:    Index into the canonical rows list this entry describes.
        page:         1-based source page number, for paginated formats (PDF).
                      None for formats without pages (XLS/XLSX/CSV).
        source_line:  Raw source line/row text the canonical row was built
                      from, for drill-back during review. None when unknown.
    """
    row_index: int
    page: int | None = None
    source_line: str | None = None


@dataclass(frozen=True)
class BankStatementMeta:
    """Metadata about the statement itself, independent of its transaction rows.

    Attributes:
        bank_key:        Stable identifier for the bank (e.g. "bob", "hdfc").
        account_number:  Normalized digits-only account number, or None when
                          the statement didn't carry one / it couldn't be read.
        period_from:      Statement period start (ISO ``YYYY-MM-DD``), or None.
        period_to:        Statement period end (ISO ``YYYY-MM-DD``), or None.
        source_format:    Shape the statement was read from, e.g. "pw-pdf",
                          "pdf", "xls", "xlsx", "csv".
        fidelity:         "exact" for a direct digital read, "ocr-approx" when
                          the rows came via OCR (lower-confidence amounts/text).
        password_used:    Whether a password was supplied to open the file.
                          NEVER the password itself — that must never be
                          logged or stored here.
    """
    bank_key: str
    account_number: str | None = None
    period_from: str | None = None
    period_to: str | None = None
    source_format: str = ""
    fidelity: str = "exact"
    password_used: bool = False


@dataclass(frozen=True)
class BankResult:
    """The uniform return value of every bank parser.

    Attributes:
        rows:             Canonical 8-column records (list of dicts).
        bank_key:         Stable identifier for the bank (e.g. "bob", "hsbc").
        account_label:    Human-facing account label, if known.
        currency:         ISO currency code for the statement (e.g. "INR").
        opening_balance:  Opening balance for the statement period.
        closing_balance:  Closing balance for the statement period.
        balance_check:    Running-balance verification outcome.
        sidecar_path:     Path to the emitted ``*.csv_summary.json`` sidecar,
                          or None when no sidecar was written.
        warnings:         Non-fatal issues encountered while parsing.
        meta:             Statement-level metadata (:class:`BankStatementMeta`),
                          or None when the parser doesn't yet populate it.
        provenance:       Optional per-row source trace-back entries.
    """
    rows: list[dict]
    bank_key: str
    account_label: str = ""
    currency: str = "INR"
    opening_balance: float = 0.0
    closing_balance: float = 0.0
    balance_check: BalanceCheck = field(default_factory=lambda: BalanceCheck(ok=True))
    sidecar_path: Path | None = None
    warnings: list[str] = field(default_factory=list)
    meta: BankStatementMeta | None = None
    provenance: tuple[RowProvenance, ...] = ()

    @property
    def row_count(self) -> int:
        return len(self.rows)


@runtime_checkable
class BankSkill(Protocol):
    """Structural protocol every bank parser satisfies.

    A bank skill owns ONLY format-specific work: sniffing whether a file is its
    own, and parsing it into canonical rows. The shared tail (CSV writing,
    sidecar emission, balance derivation) belongs to ``canonical_io`` so that no
    two banks duplicate it.

    Because this is ``@runtime_checkable``, ``isinstance(obj, BankSkill)`` checks
    only that ``detect``, ``parse`` and ``formats`` exist as attributes — not
    their signatures.
    """

    def detect(self, path: str | Path) -> float:
        """Confidence in [0.0, 1.0] that this parser owns ``path``.

        Implementations sniff headers/keywords cheaply and conservatively;
        return 0.0 when the file is clearly not theirs so the registry can fall
        back to a generic handler.
        """
        ...

    def parse(self, path: str | Path, password: str | None = None) -> BankResult:
        """Parse ``path`` into a :class:`BankResult` of canonical rows.

        ``password`` is only meaningful for formats that can be encrypted
        (PDF); parsers that never see encrypted input may ignore it.
        """
        ...

    def formats(self) -> tuple[str, ...]:
        """Accepted file suffixes/shapes, e.g. ``(".pdf", ".xls", ".csv")``."""
        ...
