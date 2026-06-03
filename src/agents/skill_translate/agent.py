"""
agent.py — Text Translator (direct mode).

Takes user-provided text, source language, and target language,
sends a translation prompt to the LLM, and writes the result to disk.
"""
from __future__ import annotations

from pathlib import Path

from agents.base_agent import run_direct

SYSTEM_PROMPT = (Path(__file__).parent / "AGENT.md").read_text(encoding="utf-8")


def run(
    text: str,
    source_lang: str,
    target_lang: str,
    output_path: str,
    config_path: str = "config.yaml",
    model_override: str | None = None,
) -> str:
    """
    Translate text and save the result.

    Args:
        text:           The text to translate.
        source_lang:    Source language name (e.g. "English", "auto", or "").
        target_lang:    Target language name (e.g. "Hindi", "Spanish").
        output_path:    Where to write the output .txt file.
        config_path:    Path to config.yaml (LLM settings).
        model_override: Optional model name override.

    Returns:
        The translated text (also written to *output_path*).
    """
    if not text.strip():
        raise ValueError("No text provided for translation.")
    if not target_lang.strip():
        raise ValueError("Target language is required.")

    # Resolve source language label.
    src = source_lang.strip() if source_lang.strip() else "auto-detect"

    print(f"[translate] {src} → {target_lang.strip()}, {len(text.split())} words")

    # Build user message.
    user_message = (
        f"Translate the following text from **{src}** to "
        f"**{target_lang.strip()}**.\n\n"
        f"---\n\n"
        f"{text}"
    )

    # Call the LLM.
    print("[translate] Sending to LLM …")
    translation = run_direct(
        user_message=user_message,
        system_prompt=SYSTEM_PROMPT,
        config_path=config_path,
        model_override=model_override,
    )

    # Write output.
    out = Path(output_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(translation, encoding="utf-8")
    print(f"[translate] Translation written to {out}")

    return translation
