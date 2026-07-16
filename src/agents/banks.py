"""
agents/banks.py — bank-parser discovery registry.

Discovers banks the way agents/registry.py discovers UI skills: scans
agents/*/skill.yaml at call time for a `bank: true` manifest key and records
a lightweight BankInfo. No dynamic imports happen during discover() itself —
a bank's module is only imported when a caller resolves it via
load_bank_skill(), the same frozen-safe (PyInstaller-survives) pattern
agents.registry.load_run_function already uses.

By convention, a bank module exposes its BankSkill instance as a module-level
`bank_skill` attribute (see agents/skill_hdfc/agent.py for the reference).

Public surface:
    BankInfo            — frozen dataclass describing one discovered bank.
    discover()           — returns a list of BankInfo sorted by display_name.
    get(bank_key)         — returns a single BankInfo by key, or None.
    load_bank_skill(info)  — lazily imports and returns the bank's BankSkill instance.
"""
from __future__ import annotations

import importlib
import logging
from dataclasses import dataclass
from pathlib import Path

import yaml

from agents.bank_contract import BankSkill
from agents.registry import _AGENTS_ROOT

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class BankInfo:
    """One bank parser discovered via its skill.yaml `bank: true` flag."""
    bank_key: str
    display_name: str
    skill_name: str
    package: str


def _parse_bank_manifest(path: Path) -> BankInfo | None:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as e:
        log.warning("Skipping %s: YAML parse error: %s", path, e)
        return None

    if not isinstance(raw, dict) or not raw.get("bank"):
        return None

    missing = [k for k in ("name", "display_name") if k not in raw]
    if missing:
        log.debug("Skipping bank manifest %s: missing required fields: %s", path, missing)
        return None

    skill_dir = path.parent
    return BankInfo(
        bank_key=str(raw.get("bank_key", raw["name"])).lower(),
        display_name=raw["display_name"],
        skill_name=raw["name"],
        package=f"agents.{skill_dir.name}",
    )


_cache: list[BankInfo] | None = None


def discover(*, refresh: bool = False) -> list[BankInfo]:
    """Scan agents/*/skill.yaml for `bank: true` manifests; cached, sorted by
    display_name."""
    global _cache
    if _cache is not None and not refresh:
        return list(_cache)

    banks: list[BankInfo] = []
    if not _AGENTS_ROOT.is_dir():
        log.warning("Agents root not found: %s", _AGENTS_ROOT)
        _cache = banks
        return list(_cache)

    for skill_dir in sorted(_AGENTS_ROOT.iterdir()):
        manifest = skill_dir / "skill.yaml"
        if not manifest.is_file():
            continue
        info = _parse_bank_manifest(manifest)
        if info is not None:
            banks.append(info)

    banks.sort(key=lambda b: b.display_name)
    _cache = banks
    log.info("Discovered %d bank(s): %s", len(banks), [b.bank_key for b in banks])
    return list(_cache)


def get(bank_key: str) -> BankInfo | None:
    """Look up a bank by its key (case-insensitive)."""
    key_lower = bank_key.lower()
    for bank in discover():
        if bank.bank_key == key_lower:
            return bank
    return None


def load_bank_skill(info: BankInfo) -> BankSkill:
    """Import the bank's module and return its `bank_skill` instance.

    Raises AttributeError if the module has no `bank_skill` attribute, or
    TypeError if that attribute doesn't satisfy the BankSkill protocol —
    both indicate a manifest/module mismatch that should fail loud rather
    than silently falling back.
    """
    mod = importlib.import_module(f"{info.package}.agent")
    skill = getattr(mod, "bank_skill")
    if not isinstance(skill, BankSkill):
        raise TypeError(f"{info.package}.agent.bank_skill does not implement BankSkill")
    return skill
