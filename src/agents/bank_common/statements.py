"""
agents/bank_common/statements.py -- the ONE reading rule for every bank.

Every ``BankSkill.parse()`` accepts a file, a list of files or a folder and
hands the work to :func:`read_statements`, which applies the same rules to all
banks (BNK-01):

* A folder takes every file type in the bank's ``formats()`` (plus any extra
  type the bank reads, ``read_suffixes``); other files are named in a warning.
* One password for the whole upload. An unreadable file, or a wrong password,
  stops the run and names the file.
* Statements are ordered by their first transaction date.
* Statements of other accounts are skipped (listed by their last 4 digits).
  When the upload holds several accounts and none can be chosen, it stops.
* Rows that appear in two overlapping statements (same date, amounts, balance
  and description) are kept once, with a warning. Identical rows inside ONE
  statement are never touched.
* The running balance must carry on from one statement to the next. A break
  stops the run naming both files and both balances, unless
  ``allow_balance_break`` is set, in which case the statements are joined and
  the warning names the break.

A single statement is returned exactly as the bank's ``parse_one`` made it.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Sequence

from agents.bank_common.consolidate import _natural_key, check_continuity, StatementGroup
from agents.bank_contract import BankResult, BankStatementMeta

_ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TOLERANCE = 0.015


class StatementStop(ValueError):
    """The statements cannot be read or joined; the run must stop.

    ``title`` is the short heading shown by the pipeline; the message names
    the files and figures involved.
    """

    def __init__(self, message: str, title: str = "statements cannot be joined"):
        super().__init__(message)
        self.title = title


@dataclass
class _Part:
    name: str
    path: Path
    result: BankResult
    number: str | None
    first: str | None
    last: str | None


def _digits(text: Any) -> str:
    return "".join(c for c in str(text or "") if c.isdigit())


def _fits(a: str, b: str) -> bool:
    """Two account numbers are the same account when one digit string holds
    the other (masked and last-digits forms fit), as in IMP-08/09."""
    return bool(a) and bool(b) and (a in b or b in a)


def _last4(number: str) -> str:
    return "..." + number[-4:] if number else "unknown"


def _num(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def _row_key(row: dict) -> tuple:
    def amount(v: Any) -> float | None:
        n = _num(v)
        return None if n is None else round(n, 2)

    return (
        str(row.get("Date", "")).strip(),
        amount(row.get("Deposit")),
        amount(row.get("Withdrawal")),
        amount(row.get("Balance")),
        " ".join(str(row.get("Description", "")).split()),
    )


def _iso_dates(rows: Sequence[dict]) -> list[str]:
    out = []
    for r in rows:
        d = str(r.get("Date", "")).strip()
        if _ISO_RE.match(d):
            out.append(d)
    return out


def _suffixes(skill: Any) -> tuple[str, ...]:
    extra = getattr(skill, "read_suffixes", None)
    base = tuple(skill.formats())
    return tuple(s.lower() for s in (extra if extra else base))


def _entries(path: Any) -> list[Path]:
    if isinstance(path, (list, tuple)):
        items = [str(p).strip() for p in path if str(p).strip()]
    else:
        items = [str(path).strip()] if str(path or "").strip() else []
    return [Path(i) for i in items]


def collect_files(skill: Any, path: Any) -> tuple[list[Path], list[str]]:
    """Expand ``path`` (file, list of files, folder) into statement files.

    Returns ``(files, warnings)``. Raises ``FileNotFoundError`` when a given
    path does not exist or nothing readable is found.
    """
    suffixes = _suffixes(skill)
    files: list[Path] = []
    warnings: list[str] = []
    seen: set[str] = set()
    entries = _entries(path)
    if not entries:
        raise FileNotFoundError("No statement file was given.")
    for entry in entries:
        if entry.is_dir():
            kids = sorted((c for c in entry.iterdir() if c.is_file()),
                          key=lambda c: _natural_key(c.name))
            keep = [c for c in kids if c.suffix.lower() in suffixes]
            other = [c.name for c in kids if c.suffix.lower() not in suffixes]
            if other:
                warnings.append(
                    f"{len(other)} file(s) in the folder were not read because "
                    f"they are not {', '.join(suffixes)} files: " + ", ".join(other)
                )
            if not keep:
                raise FileNotFoundError(
                    f"No {', '.join(suffixes)} statement file found in: {entry}")
            found = keep
        elif entry.is_file():
            found = [entry]
        else:
            raise FileNotFoundError(f"Statement not found: {entry}")
        for f in found:
            key = str(f.resolve())
            if key not in seen:
                seen.add(key)
                files.append(f)
    return files, warnings


def _stop_accounts(numbers: list[str], head: str, tail: str) -> StatementStop:
    listed = ", ".join(sorted({_last4(n) for n in numbers}))
    return StatementStop(f"{head} Accounts found: {listed}. {tail}",
                         title="more than one account")


def read_statements(
    skill: Any,
    path: Any,
    password: str | None = None,
    *,
    allow_balance_break: bool = False,
    selected_account: str | None = None,
    account_resolver: Callable[[list[str]], str | None] | None = None,
) -> BankResult:
    """Read ``path`` with ``skill.parse_one`` and join the statements.

    ``selected_account`` is the digits of the account to keep (statements of
    other accounts are skipped). When it is not given and the upload holds
    several accounts, ``account_resolver(numbers)`` may return the digits to
    keep or raise :class:`StatementStop`; with no resolver the run stops.
    """
    files, warnings = collect_files(skill, path)

    if len(files) == 1:
        res = skill.parse_one(files[0], password)
        if warnings:
            res = replace(res, warnings=list(res.warnings) + warnings)
        return res

    parts: list[_Part] = []
    for f in files:
        try:
            res = skill.parse_one(f, password)
        except StatementStop:
            raise
        except Exception as e:  # noqa: BLE001 - name the file, keep the cause
            raise StatementStop(
                f"`{f.name}` could not be read: {e}",
                title="a statement could not be read") from e
        dates = _iso_dates(res.rows)
        number = _digits(res.meta.account_number) if res.meta and res.meta.account_number else ""
        parts.append(_Part(f.name, f, res, number or None,
                           min(dates) if dates else None,
                           max(dates) if dates else None))

    # -- which account -------------------------------------------------------
    unreadable = [p.name for p in parts if not p.number]
    no_number_shown = len(unreadable) == len(parts)
    if unreadable and not no_number_shown:
        raise StatementStop(
            "Some statements show an account number and some do not. The "
            "account number could not be read from: "
            + ", ".join(f"`{n}`" for n in unreadable)
            + ". With several statements the account must be known for each, "
            "or for none, so nothing was joined.",
            title="account number not readable")

    if no_number_shown:
        # None of the statements shows a number (e.g. header-less exports):
        # they are all taken as the ONE account picked in the pipeline (the
        # Bank account box, else the book account), nothing is skipped.
        warnings.append(
            "No statement shows an account number; all "
            f"{len(parts)} statements were taken as the one account selected.")
        kept, skipped = list(parts), []
    else:
        wanted = _digits(selected_account)
        if not wanted:
            clusters: list[str] = []
            for p in parts:
                if not any(_fits(p.number, c) for c in clusters):
                    clusters.append(p.number)
            if len(clusters) > 1:
                if account_resolver is None:
                    raise _stop_accounts(
                        clusters, "These statements belong to more than one account.",
                        "Upload the statements of one account at a time.")
                wanted = _digits(account_resolver(clusters))
            else:
                wanted = clusters[0]
        kept, skipped = [], []
        for p in parts:
            (kept if _fits(wanted, p.number) else skipped).append(p)
        if not kept:
            raise _stop_accounts(
                [p.number for p in parts],
                f"None of the statements is for the selected account ({_last4(wanted)}).",
                "Nothing was joined.")
        for n in sorted({_last4(p.number) for p in skipped}):
            names = ", ".join(f"`{p.name}`" for p in skipped if _last4(p.number) == n)
            warnings.append(f"Skipped, statement of another account ({n}): {names}")
    parts = kept

    # -- order, dedupe, running balance -------------------------------------
    parts.sort(key=lambda p: (p.first is None, p.first or "", _natural_key(p.name)))
    carry = getattr(skill, "is_carry_forward_row", None)
    rows: list[dict] = []
    dropped_by_pair: list[tuple[str, str, int]] = []
    breaks: list[str] = []
    owner: list[str] = []                       # file name per accumulated row
    seen_keys: dict[tuple, int] = {}
    for i, p in enumerate(parts):
        incoming = list(p.result.rows)
        if i and carry is not None:
            incoming = [r for r in incoming if not carry(r)]
        fresh: list[dict] = []
        drops: dict[str, int] = {}
        budget = dict(seen_keys)
        for r in incoming:
            k = _row_key(r)
            if budget.get(k, 0) > 0:
                budget[k] -= 1
                # who held it first
                src = next(o for o, rr in zip(owner, rows) if _row_key(rr) == k)
                drops[src] = drops.get(src, 0) + 1
            else:
                fresh.append(r)
        for src, n in drops.items():
            dropped_by_pair.append((src, p.name, n))
        if rows and fresh:
            prev = _num(rows[-1].get("Balance"))
            first = fresh[0]
            fb = _num(first.get("Balance"))
            dep = _num(first.get("Deposit")) or 0.0
            wd = _num(first.get("Withdrawal")) or 0.0
            if prev is not None and fb is not None:
                opening = round(fb - dep + wd, 2)
                if abs(opening - prev) > _TOLERANCE:
                    breaks.append(
                        f"`{owner[-1]}` ends with a balance of {prev:,.2f}, but "
                        f"`{p.name}` starts from {opening:,.2f}")
        for r in fresh:
            rows.append(r)
            owner.append(p.name)
            k = _row_key(r)
            seen_keys[k] = seen_keys.get(k, 0) + 1

    if breaks and not allow_balance_break:
        raise StatementStop(
            "The running balance does not carry on between statements: "
            + "; ".join(breaks)
            + ". A statement may be missing. Nothing was joined. If the missing "
            "period is already in your book, run again with the option to "
            "continue past a break in the running balance.",
            title="break in the running balance")
    for b in breaks:
        warnings.append(f"BALANCE BREAK (continued by your choice): {b}")
    for src, name, n in dropped_by_pair:
        warnings.append(
            f"Overlapping statements: {n} identical row(s) of `{name}` were "
            f"already in `{src}` and were kept once.")
    for p in parts:
        warnings.extend(f"{p.name}: {w}" for w in p.result.warnings)
    groups = [StatementGroup(p.name, p.result.rows, p.first, p.last) for p in parts]
    warnings.extend(w for w in check_continuity(groups)
                    if not w.startswith("OVERLAPPING") or not dropped_by_pair)

    from agents.canonical_io import run_balance_check  # noqa: PLC0415

    check = run_balance_check(rows)
    if not check.ok:
        warnings.extend(f"Balance: {d}" for d in check.details)

    base = parts[0].result
    all_dates = [d for p in parts for d in (p.first, p.last) if d]
    meta = base.meta
    if meta is not None:
        meta = replace(
            meta,
            account_number=parts[0].number,
            period_from=min(all_dates) if all_dates else meta.period_from,
            period_to=max(all_dates) if all_dates else meta.period_to,
            password_used=any(bool(p.result.meta and p.result.meta.password_used) for p in parts),
        )
    return replace(
        base,
        rows=rows,
        opening_balance=check.opening_balance,
        closing_balance=check.closing_balance,
        balance_check=check,
        sidecar_path=None,
        warnings=warnings,
        meta=meta,
        provenance=(),
    )
