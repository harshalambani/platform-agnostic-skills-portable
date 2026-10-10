"""Reads the one setting the card-spend booking skill takes from Settings.

cc_large_spend_threshold (rupees, default 20000): a card spend of this amount or
more is flagged "Possible asset" in the run result. The flag never changes the
account a spend is mapped to. Stored as a top-level key of the portable
config.yaml (Data\settings\config.yaml).
"""
from __future__ import annotations

from pathlib import Path

THRESHOLD_KEY = "cc_large_spend_threshold"
DEFAULT_LARGE_SPEND_THRESHOLD = 20000.0


def parse_threshold(value) -> float | None:
    """A positive number, or None when the value is not one."""
    try:
        text = str(value).replace(",", "").strip()
        out = float(text)
    except (TypeError, ValueError):
        return None
    if out != out or out <= 0 or out == float("inf"):
        return None
    return out


def read_large_spend_threshold(settings_path: str | Path | None) -> float:
    """The configured threshold; the default when the file, the key or a valid
    value is absent. Never raises."""
    if not settings_path:
        return DEFAULT_LARGE_SPEND_THRESHOLD
    try:
        import yaml
        p = Path(settings_path)
        if not p.is_file():
            return DEFAULT_LARGE_SPEND_THRESHOLD
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        got = parse_threshold(data.get(THRESHOLD_KEY)) if isinstance(data, dict) else None
        return got if got is not None else DEFAULT_LARGE_SPEND_THRESHOLD
    except Exception:
        return DEFAULT_LARGE_SPEND_THRESHOLD
