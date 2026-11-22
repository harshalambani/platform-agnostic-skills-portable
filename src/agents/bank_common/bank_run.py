"""
agents/bank_common/bank_run.py -- the ONE "statements to canonical CSV" run.

The Bank Skills tab calls :func:`run_statements` for every bank, so the six
banks behave the same: the statement files are read by the bank's own
``BankSkill.parse`` (the shared reading rule in ``statements.py`` joins several
statements, one account per run), the rows are written as the standard
canonical CSV with its summary sidecar, and the reply says what happened.

It never writes a bank-specific workbook (HSBC's enriched workbook stays a
scratch file that BNK-12 removes).
"""
from __future__ import annotations

from pathlib import Path

from agents import banks
from agents.bank_common.statements import StatementStop, collect_files
from agents.canonical_io import write_canonical_csv, write_sidecar
from agents.secret_redact import redact_text


def truthy(value) -> bool:
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _fail(title: str, text: str, secrets=()) -> str:
    return f"ERROR - {title}: " + redact_text(str(text), secrets=secrets)


def run_statements(
    bank: str,
    statement_path: str,
    output_path: str,
    password: str | None = None,
    allow_balance_break=False,
) -> str:
    """Read ``statement_path`` (a file, or a folder) for ``bank`` (its
    ``bank_key``) and write the canonical CSV to ``output_path``."""
    secrets = (password or "",)
    info = banks.get(str(bank or "").strip())
    if info is None:
        return _fail("unknown bank", f"'{bank}' is not a bank this app reads.")
    if not str(statement_path or "").strip():
        return _fail("no statements", "Upload the statement file(s) first.")
    override = truthy(allow_balance_break)
    try:
        skill = banks.load_bank_skill(info)
        # The file type is checked here as well as in the browser: a file the
        # bank does not read stops the run and is named.
        src = Path(statement_path)
        if src.is_file() and src.suffix.lower() not in tuple(skill.formats()):
            return _fail(
                "wrong file type",
                f"`{src.name}` is not a {info.display_name} statement type "
                f"({', '.join(skill.formats())}).")
        files, _ = collect_files(skill, statement_path)
        res = skill.parse(statement_path, password=(password or None),
                          allow_balance_break=override)
        write_canonical_csv(res.rows, output_path)
        write_sidecar(output_path, info.display_name, "derived",
                      res.opening_balance, res.closing_balance, res.row_count,
                      account_number=(res.meta.account_number if res.meta else None))
    except StatementStop as e:
        return _fail(e.title, e, secrets)
    except FileNotFoundError as e:
        return _fail("no statements found", e, secrets)
    except Exception as e:  # noqa: BLE001 - a parser error must not leak the password
        return _fail("extraction error", f"{type(e).__name__}: {e}", secrets)

    lines = [
        f"{info.display_name}: read {len(files)} statement file(s), "
        f"{res.row_count} transaction(s) -> canonical CSV "
        f"(opening {res.opening_balance:.2f}, closing {res.closing_balance:.2f}, "
        f"balance check {'ok' if res.balance_check.ok else 'FAILED'}).",
    ]
    if override:
        lines.append("Balance-break override: ON for this run.")
    lines.extend(f"Warning: {w}" for w in res.warnings)
    return "\n\n".join(lines)
