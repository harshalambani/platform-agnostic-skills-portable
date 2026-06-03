"""
agent.py — Document Summarizer (direct mode).

Reads a PDF or text file, sends the content to the LLM with a
summarisation prompt, and writes the Markdown summary to disk.
"""
from __future__ import annotations

from pathlib import Path

from agents.base_agent import run_direct

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")

# Rough word limit to stay within most model context windows.
# ~12 000 words ≈ ~16 000 tokens for English text — safe for 32k-context
# models even with system prompt overhead.
_MAX_WORDS = 12_000


# ---------------------------------------------------------------------------
# File readers
# ---------------------------------------------------------------------------

def _read_pdf(path: Path) -> str:
    """Extract text from a PDF using pdfplumber."""
    import pdfplumber

    pages: list[str] = []
    with pdfplumber.open(path) as pdf:
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
    return "\n\n".join(pages)


def _read_text(path: Path) -> str:
    """Read a plain-text file (any extension except .pdf)."""
    return path.read_text(encoding="utf-8", errors="replace")


def _read_file(path: Path) -> str:
    """Dispatch to the right reader based on file extension."""
    if path.suffix.lower() == ".pdf":
        return _read_pdf(path)
    return _read_text(path)


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------

def _truncate(text: str, max_words: int = _MAX_WORDS) -> tuple[str, bool]:
    """
    Truncate *text* to at most *max_words* words.

    Returns (possibly_truncated_text, was_truncated).
    """
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
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    Summarise a document and save the result as Markdown.

    Args:
        file_path:      Path to the input document (PDF or text).
        output_path:    Where to write the output .md file.
        config_path:    Path to config.yaml (LLM settings).
        model_override: Optional model name override.

    Returns:
        The LLM's summary text (also written to *output_path*).
    """
    src = Path(file_path)
    if not src.is_file():
        raise FileNotFoundError(f"Input file not found: {file_path}")

    # 1. Read file content.
    print(f"[summarize] Reading {src.name} ({src.suffix}) …")
    content = _read_file(src)

    if not content.strip():
        raise ValueError(
            f"No extractable text found in {src.name}. "
            "If this is a scanned PDF, OCR support is not available in this skill."
        )

    # 2. Truncate if necessary.
    content, was_truncated = _truncate(content)
    if was_truncated:
        print(
            f"[summarize] Content truncated to {_MAX_WORDS} words "
            f"(original was longer). The LLM will note this."
        )

    # 3. Build user message.
    user_message = (
        f"Please summarise the following document.\n\n"
        f"**File name:** {src.name}\n\n"
        f"---\n\n"
        f"{content}"
    )
    if was_truncated:
        user_message += (
            "\n\n---\n\n"
            "*[The document was truncated to fit within the model's context "
            "window. Summarise only the portion provided and note the "
            "truncation.]*"
        )

    # 4. Call the LLM.
    print("[summarize] Sending to LLM …")
    summary = run_direct(
        user_message=user_message,
        system_prompt=SYSTEM_PROMPT,
        config_path=config_path,
        model_override=model_override,
    )

    # 5. Write output.
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(summary, encoding="utf-8")
    print(f"[summarize] Summary written to {out}")

    return summary
