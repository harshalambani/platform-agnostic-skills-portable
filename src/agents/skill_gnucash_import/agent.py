"""
agent.py — GnuCash Bank Statement Import (direct mode, Phase 1 scaffold).

Reads a CSV/XLSX/TXT bank statement, sends its content to the LLM with
the AGENT.md system prompt, and writes the resulting GnuCash-format CSV
to disk.

Phase 1 scope (2026-06-09):
  * Direct mode — the LLM does the schema mapping under the AGENT.md
    contract. No deterministic parser here yet.
  * Input formats: .csv, .xlsx/.xls, .txt. .pdf and .ofx come later.
  * Output: a single .csv file in the canonical
    Date,Transaction ID,Description,Account,Deposit,Withdrawal,Balance
    schema.

Not yet implemented in this scaffold (intentional — see
2026-06-09-GNUCASH-AUTOMATION-PROJECT-PLAN.md):
  * QIF output (Phase 2).
  * PDF input (Phase 2).
  * Account-mapping rules file (Phase 3).
  * Chunking for very long statements (Phase 2).
  * Reconciliation against an existing GnuCash file (Phase 4).
"""
from __future__ import annotations

from pathlib import Path

from agents.base_agent import run_direct

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")

# Word ceiling for the LLM payload. Matches skill_summarize's tuning —
# ~12 000 words ≈ ~16 000 tokens, leaving headroom for the system prompt
# and the CSV output on a 32k-context model. Statements that overflow
# get a single-pass truncation in Phase 1; chunked transform lands in
# Phase 2.
_MAX_WORDS = 12_000


# ---------------------------------------------------------------------------
# Input readers
# ---------------------------------------------------------------------------

def _read_csv_or_text(path: Path) -> str:
    """Read a .csv or .txt file as raw UTF-8 text (let the LLM parse it)."""
    return path.read_text(encoding="utf-8", errors="replace")


def _read_xlsx(path: Path) -> str:
    """
    Render an Excel sheet as CSV-formatted text and hand that to the LLM.

    Phase 1: read only the first sheet. Phase 2 can add sheet selection.
    """
    import pandas as pd

    df = pd.read_excel(path, sheet_name=0, dtype=str)
    # Treat NaN as empty so the LLM sees blank cells, not 'nan'.
    df = df.fillna("")
    return df.to_csv(index=False)


def _read_input(path: Path) -> str:
    ext = path.suffix.lower()
    if ext in (".csv", ".txt"):
        return _read_csv_or_text(path)
    if ext in (".xlsx", ".xls"):
        return _read_xlsx(path)
    raise ValueError(
        f"Unsupported input file type {ext!r}. "
        f"Phase 1 accepts .csv, .xlsx, .xls, .txt. "
        f"PDF support arrives in Phase 2."
    )


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def _truncate(text: str, max_words: int = _MAX_WORDS) -> tuple[str, bool]:
    """Truncate *text* to at most *max_words* words. Returns (text, was_truncated)."""
    words = text.split()
    if len(words) <= max_words:
        return text, False
    return " ".join(words[:max_words]), True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(
    file_path: str,
    output_path: str,
    account_name: str = "",
    bank_hint: str = "Auto-detect",
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    Convert a bank-statement file into a GnuCash-importable CSV.

    Args:
        file_path:      Path to the input statement (.csv, .xlsx, .xls, .txt).
        output_path:    Where to write the output .csv file.
        account_name:   Optional GnuCash account name to populate the Account
                        column (e.g. "Assets:Bank:HDFC Savings"). Empty by
                        default — leaves Account blank for the user to map at
                        GnuCash import time.
        bank_hint:      Source-bank dropdown value ("Auto-detect", "HDFC",
                        "SBI", ...). Passed to the LLM as context.
        config_path:    Path to config.yaml (LLM settings).
        model_override: Optional model name override.

    Returns:
        The LLM's CSV output (also written to *output_path*).
    """
    src = Path(file_path)
    if not src.is_file():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    # 1. Read raw content.
    print(f"[gnucash_import] Reading {src.name} ({src.suffix}) …")
    content = _read_input(src)

    if not content.strip():
        raise ValueError(
            f"No content extracted from {src.name}. "
            f"Check the file is not empty and is one of the supported formats."
        )

    # 2. Truncate for context-window safety.
    content, was_truncated = _truncate(content)
    if was_truncated:
        print(
            f"[gnucash_import] Content truncated to {_MAX_WORDS} words "
            f"(original was longer). Output will note this; consider splitting "
            f"the statement and re-running for full coverage (Phase 2 will "
            f"auto-chunk)."
        )

    # 3. Build the user message. Pass through the dropdown values explicitly
    # so the LLM does not have to guess and the AGENT.md "non-interactive
    # batch mode" rules apply.
    instructions = [
        f"**File name:** {src.name}",
        f"**Source bank hint:** {bank_hint or 'Auto-detect'}",
    ]
    if account_name.strip():
        instructions.append(
            f"**Account name to use for the Account column:** "
            f"`{account_name.strip()}`"
        )
    else:
        instructions.append(
            "**Account name:** not provided — leave the Account column blank."
        )

    if was_truncated:
        instructions.append(
            "**Note:** the input below was truncated to fit the context "
            "window. Process only what's provided and add a `# Note:` "
            "comment line at the top of your CSV output flagging the "
            "truncation."
        )

    header = "\n".join(instructions)

    user_message = (
        f"Convert the bank statement below into a GnuCash-importable CSV "
        f"per the AGENT.md spec.\n\n"
        f"{header}\n\n"
        f"---\n\n"
        f"{content}"
    )

    # 4. Call the LLM.
    print("[gnucash_import] Sending to LLM …")
    output_csv = run_direct(
        user_message=user_message,
        system_prompt=SYSTEM_PROMPT,
        config_path=config_path,
        model_override=model_override,
    )

    # 5. Write output.
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(output_csv, encoding="utf-8")
    print(f"[gnucash_import] GnuCash CSV written to {out}")

    return output_csv
