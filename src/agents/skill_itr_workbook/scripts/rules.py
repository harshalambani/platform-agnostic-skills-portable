"""
rules.py -- load tax_rules_<year>.yaml (plan section 5, 4.2) by the
canonical income-year key (plan section 5.1, D19), resolve the applicable
regime block + age class, and load user_rules.yaml (Harshal's numbered
RULES, plan section 4.2) for the Rules-sheet dump.

Nothing in this module (or anywhere downstream) may hardcode a rate, cap,
slab, or section number -- every figure comes from the loaded YAML.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import yaml


class RulesError(Exception):
    pass


@dataclass
class RulesConfig:
    year_key: str            # canonical income-year key, e.g. "2025-26"
    act: str                 # "1961" | "2025" (plan D19)
    year_label: str           # display label, e.g. "AY 2026-27 (FY 2025-26)"
    version: str
    raw: dict = field(default_factory=dict)

    @property
    def common(self) -> dict:
        return self.raw.get("common", {})

    def regime(self, regime: str) -> dict:
        """regime: 'new' | 'old'."""
        try:
            return self.raw["regimes"][regime]
        except KeyError:
            raise RulesError(f"unknown regime {regime!r} in {self.year_key} rules config") from None


def _year_key_from_filename(path: Path) -> str | None:
    m = re.search(r"AY(\d{4})-(\d{2})", path.stem)
    if m:
        start = int(m.group(1)) - 1
        return f"{start}-{str(start + 1)[-2:]}"
    m = re.search(r"TY(\d{4})-(\d{2})", path.stem)
    if m:
        return f"{m.group(1)}-{m.group(2)}"
    return None


def load_rules(rules_dir: str | Path, year_key: str) -> RulesConfig:
    """Load Data/itr/rules/tax_rules_<AY|TY>.yaml whose canonical income-year
    key (derived from meta.ay/meta.fy, D19) matches `year_key` (e.g.
    '2025-26'). Raises RulesError if no file matches."""
    rules_dir = Path(rules_dir)
    for p in sorted(rules_dir.glob("tax_rules_*.yaml")):
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        meta = raw.get("meta", {})
        fy = meta.get("fy")
        if fy == year_key:
            return RulesConfig(
                year_key=year_key,
                act=meta.get("act", "1961"),
                year_label=meta.get("year_label", f"AY {year_key}"),
                version=meta.get("version", "unknown"),
                raw=raw,
            )
    raise RulesError(f"no tax_rules_*.yaml under {rules_dir} matches income year {year_key!r}")


@dataclass
class UserRule:
    id: str
    stated: str
    statement: str
    enforcement: str
    test: str
    status: str = "active"


def load_user_rules(path: str | Path) -> list[UserRule]:
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    return [
        UserRule(
            id=r["id"], stated=r.get("stated", ""), statement=r.get("statement", ""),
            enforcement=r.get("enforcement", ""), test=r.get("test", ""),
            status=r.get("status", "active"),
        )
        for r in raw
    ]


def age_class(dob: str | None, fy_end: date) -> str:
    """'general' | 'senior' (60<=age<80) | 'super_senior' (age>=80) at FY end.
    A missing DOB is treated as 'general' (age unknown -- never assume
    senior-citizen status)."""
    if not dob:
        return "general"
    d = date.fromisoformat(dob)
    age = fy_end.year - d.year - ((fy_end.month, fy_end.day) < (d.month, d.day))
    if age >= 80:
        return "super_senior"
    if age >= 60:
        return "senior"
    return "general"


#: The only tokens that count as a DECLARED residency (2026-07-19 residency
#: prompt, section 2). EntityProfile.residency is a pre-existing field that
#: historically held free text like "Resident" -- never consumed by any rule.
#: Anything other than exactly one of these three is treated as undeclared.
RESIDENCY_VALUES = ("R/OR", "RNOR", "NR")
DEFAULT_RESIDENCY = "R/OR"


def resolve_residency(residency: str | None) -> tuple[str, bool]:
    """Resolve `EntityProfile.residency` to (value, declared).

    Only the three statutory tokens (R/OR, RNOR, NR) count as a declaration.
    Anything else -- unset, or legacy free text such as "Resident" (which
    predates this resolver and doesn't distinguish R/OR from RNOR) -- is
    undeclared and defaults to R/OR. This keeps every pre-existing
    entities.yaml (real and synthetic) resolving exactly as before: still
    R/OR, still with the Assumptions footnote, until the entity explicitly
    declares one of the three tokens."""
    if residency in RESIDENCY_VALUES:
        return residency, True
    return DEFAULT_RESIDENCY, False


def resolve_age_class(status: str, dob: str | None, fy_end: date, residency: str | None = None) -> str:
    """Age-class resolution (CF6) applies only to resident Individuals with
    a known DOB; HUF/other non-individual statuses, individuals with no DOB
    on file, and NON-RESIDENT individuals always resolve to 'general' -- the
    higher senior/super-senior basic exemption is a resident-only benefit
    (2026-07-19 residency prompt, section 3). RNOR is a resident sub-status
    under s.6 and still gets the benefit; only NR is excluded. `doi` (date of
    incorporation -- HUF/non-individual password material, e.g. for
    encrypted 26AS PDFs) is NEVER used here -- it carries no age semantics."""
    if status != "Individual" or not dob:
        return "general"
    residency_value, _ = resolve_residency(residency)
    if residency_value == "NR":
        return "general"
    return age_class(dob, fy_end)


def resolve_slabs(rules: RulesConfig, regime: str, status: str, dob: str | None, fy_end: date,
                   residency: str | None = None) -> list[dict]:
    """Resolve the applicable slab table for `regime` ('new'/'old') given
    entity `status` ('Individual'/'HUF'), DOB and residency (age class only
    matters for the old regime, individual, resident -- plan section 3.4;
    2026-07-19 residency prompt section 3)."""
    block = rules.regime(regime)
    if regime == "new":
        return block["slabs"]
    if status == "HUF":
        return block["huf_slabs"]
    cls = resolve_age_class(status, dob, fy_end, residency=residency)
    return block["slabs_by_age"][cls]
