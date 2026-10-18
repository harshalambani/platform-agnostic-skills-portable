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


def resolve_large_spend_threshold(entity, settings_path) -> tuple:
    """CC-14: (value, source). The entity's own value when it has one, else the
    global setting, else the default. ``source`` names which one applied."""
    own = parse_threshold(getattr(entity, "cc_large_spend_threshold", None))
    if own is not None:
        return own, "this entity's own setting"
    value = read_large_spend_threshold(settings_path)
    try:
        import yaml
        p = Path(settings_path) if settings_path else None
        data = yaml.safe_load(p.read_text(encoding="utf-8")) if p and p.is_file() else None
        if isinstance(data, dict) and parse_threshold(data.get(THRESHOLD_KEY)) is not None:
            return value, "the global setting"
    except Exception:
        pass
    return value, "the default"


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


CRED_KEY = "cc_cred_max_overage"


def resolve_cred_max_overage(entity) -> tuple:
    """CC-31: (rupees, source) for the entity being run. A missing key is the code default (50); a
    value that is not a number from 0 to 500 raises ValueError naming the key - never a silent fallback.
    One entity's value is never read for another: only ``entity`` is looked at."""
    from .matcher import CRED_MAX_OVERAGE
    raw = getattr(entity, CRED_KEY, None)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return CRED_MAX_OVERAGE, "default"
    import configs
    try:
        return configs.parse_cred_max_overage(raw), "from entities.yaml"
    except ValueError as e:
        raise ValueError(f"entity {getattr(entity, 'key', '?')!r} {CRED_KEY}: {e} "
                         f"(a number from 0 to {configs.CRED_MAX_OVERAGE_LIMIT:g} rupees)") from None
