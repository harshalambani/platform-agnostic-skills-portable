"""
configs.py -- Data/itr/ config schemas + loaders (plan section 4.2).

Three user-editable config files, none committed (real files are user data
under Data/, which is gitignored; only rules/*.yaml and *.example.yaml are
tracked -- see .gitignore and the fixtures under tests/skill_itr_workbook/
for the committed synthetic examples):

    Data/itr/entities.yaml               # per-entity profile (name/PAN/regime/...)
    Data/itr/mappings/<entity>.mapping.yaml   # GUID -> {path, tag, flags, note}
    Data/itr/scrips.yaml                 # commodity symbol -> ISIN/FMV-31-01-2018

Loader validation (mapping.py's resolution engine builds on top of this):
  - unknown tags are rejected (MappingValidationError)
  - duplicate GUIDs within one mapping file are rejected (MappingValidationError)
  - a GUID whose `path` no longer matches the currently-parsed tree is a
    warning, not a failure (rename detection, plan section 3.1) -- pass
    `known_paths` (guid -> current path) to surface these.
"""
from __future__ import annotations

import yaml
from dataclasses import dataclass, field
from pathlib import Path

import tags as tag_vocab


class ConfigValidationError(Exception):
    pass


class MappingValidationError(Exception):
    pass


# ---------------------------------------------------------------------------
# entities.yaml
# ---------------------------------------------------------------------------

@dataclass
class EntityProfile:
    key: str
    name: str
    pan: str
    status: str                      # "Individual" | "HUF"
    residency: str = "Resident"      # legacy free-text display field, NOT one of the
                                       # statutory tokens the tax engine now consumes -- see
                                       # rules.resolve_residency(), which treats anything
                                       # other than exactly "R/OR" / "RNOR" / "NR" as
                                       # undeclared and falls back to R/OR (2026-07-19
                                       # residency prompt, section 2)
    dob: str | None = None
    doi: str | None = None           # date of incorporation (HUF/non-individual password
                                       # material, e.g. encrypted 26AS PDFs) -- CF6; NEVER
                                       # used for age-class resolution (see rules.resolve_age_class)
    address: str | None = None
    father_name: str | None = None
    aadhaar: str | None = None       # store raw digits only (no spaces); presentation.py
                                       # formats it CA-file style (space-grouped) for display.
                                       # No at-rest protection exists for identity fields in
                                       # this project (PAN/DOB/address are all plaintext) --
                                       # stored the same way, not a new decision (2026-07-19
                                       # residency prompt, section 1)
    business_subtree: str | None = None   # GnuCash account path prefix (e.g.
                                       # "Income/xBusiness Income") that `PL for Business`
                                       # walks as a subtree -- never a hardcoded literal in
                                       # presentation.py, so a book rename can't silently
                                       # render a zero sheet (2026-07-19 PL for Business prompt)
    default_regime: str = "new"
    regime_by_ay: dict = field(default_factory=dict)   # {"2026-27": "old", ...}
    extra_items: dict = field(default_factory=dict)     # b/f losses, clubbing notes


_REQUIRED_ENTITY_FIELDS = ("name", "pan", "status")


def load_entities(path: str | Path) -> dict[str, EntityProfile]:
    """Load Data/itr/entities.yaml -> {entity_key: EntityProfile}."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigValidationError(f"{p}: expected a mapping of entity_key -> profile")

    entities: dict[str, EntityProfile] = {}
    for key, fields_ in raw.items():
        missing = [f for f in _REQUIRED_ENTITY_FIELDS if f not in fields_]
        if missing:
            raise ConfigValidationError(f"{p}: entity {key!r} missing required field(s) {missing}")
        entities[key] = EntityProfile(
            key=key,
            name=fields_["name"],
            pan=fields_["pan"],
            status=fields_["status"],
            residency=fields_.get("residency", "Resident"),
            dob=fields_.get("dob"),
            doi=fields_.get("doi"),
            address=fields_.get("address"),
            father_name=fields_.get("father_name"),
            aadhaar=fields_.get("aadhaar"),
            business_subtree=fields_.get("business_subtree"),
            default_regime=fields_.get("default_regime", "new"),
            regime_by_ay=fields_.get("regime_by_ay") or {},
            extra_items=fields_.get("extra_items") or {},
        )
    return entities


# ---------------------------------------------------------------------------
# <entity>.mapping.yaml
# ---------------------------------------------------------------------------

@dataclass
class MappingEntry:
    guid: str
    path: str
    tag: str
    flags: list = field(default_factory=list)
    note: str | None = None
    suggested_by_llm: str | None = None   # date string, or None once user-approved


@dataclass
class MappingLoadResult:
    entries: dict           # guid -> MappingEntry
    warnings: list          # human-readable strings (path drift, etc.)


def load_mapping(path: str | Path, known_paths: dict | None = None) -> MappingLoadResult:
    """
    Load an <entity>.mapping.yaml file: a YAML list of entries (a list, not
    a dict, so duplicate GUIDs are detectable rather than silently
    overwriting one another).

    Raises MappingValidationError on an unknown tag or a duplicate GUID.
    If `known_paths` (guid -> current account path from the parsed tree) is
    supplied, every entry is checked against it (rename detection, plan
    section 3.1) -- never a failure, but the two outcomes are deliberately
    worded differently (2026-07-23 path-drift fix):

      * GUID found in `known_paths` but the stored `path` differs -- a
        benign rename. The GUID is identity; `path` is descriptive metadata
        that has simply gone stale, and it self-heals the moment
        apply_mapping_corrections.py next writes the mapping file (or is
        run with `--refresh-paths`) -- so the warning points at that fix
        rather than reading like a defect.
      * GUID absent from `known_paths` entirely -- the account this entry
        names is not in the currently-parsed tree at all (deleted, or the
        wrong book was loaded). This is a real problem, not a rename, and
        is NEVER auto-healed -- it must stay loud on every run until a
        human resolves it.
    """
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or []
    if not isinstance(raw, list):
        raise MappingValidationError(f"{p}: expected a YAML list of mapping entries")

    entries: dict[str, MappingEntry] = {}
    warnings: list[str] = []

    for i, item in enumerate(raw):
        guid = item.get("guid")
        tag = item.get("tag")
        if not guid:
            raise MappingValidationError(f"{p}: entry {i} missing 'guid'")
        if guid in entries:
            raise MappingValidationError(f"{p}: duplicate GUID {guid!r} (entry {i})")
        if not tag_vocab.is_valid_tag(tag):
            raise MappingValidationError(f"{p}: entry {i} (guid {guid}) has unknown tag {tag!r}")
        for flag in item.get("flags") or []:
            if not tag_vocab.is_valid_flag(flag):
                raise MappingValidationError(f"{p}: entry {i} (guid {guid}) has unknown flag {flag!r}")

        entries[guid] = MappingEntry(
            guid=guid,
            path=item.get("path", ""),
            tag=tag,
            flags=item.get("flags") or [],
            note=item.get("note"),
            suggested_by_llm=item.get("suggested_by_llm"),
        )

        if known_paths is not None:
            if guid in known_paths:
                if known_paths[guid] != entries[guid].path:
                    warnings.append(
                        f"GUID {guid} path drifted (benign rename, auto-fixable): mapping has "
                        f"{entries[guid].path!r}, tree now has {known_paths[guid]!r} -- the "
                        "mapping still applies by GUID; refresh the stored path via "
                        "apply_mapping_corrections.py --refresh-paths (or any correction run, "
                        "which refreshes drifted paths for free)."
                    )
            else:
                warnings.append(
                    f"GUID {guid} (mapping path {entries[guid].path!r}) NOT FOUND in the "
                    "parsed tree -- account deleted, or the wrong book was loaded. This is "
                    "NEVER auto-healed; verify manually before proceeding."
                )

    return MappingLoadResult(entries=entries, warnings=warnings)


def dump_mapping_entries(entries: list) -> str:
    """Serialize a list of MappingEntry (or plain dicts) back to the
    <entity>.mapping.yaml list format, for the learning-loop snippet."""
    out = []
    for e in entries:
        d = e.__dict__.copy() if hasattr(e, "__dict__") else dict(e)
        d = {k: v for k, v in d.items() if v not in (None, [], {})}
        out.append(d)
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# scrips.yaml
# ---------------------------------------------------------------------------

@dataclass
class ScripRef:
    symbol: str
    isin: str | None = None
    fmv_31jan2018: float | None = None
    table_ref: str | None = None   # bundled FMV table row alias (populated in B6)


def load_scrips(path: str | Path) -> dict[str, ScripRef]:
    """Load Data/itr/scrips.yaml -> {symbol: ScripRef}."""
    p = Path(path)
    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ConfigValidationError(f"{p}: expected a mapping of symbol -> {{isin, fmv_31jan2018 | table_ref}}")

    scrips: dict[str, ScripRef] = {}
    for symbol, fields_ in raw.items():
        scrips[symbol] = ScripRef(
            symbol=symbol,
            isin=fields_.get("isin"),
            fmv_31jan2018=fields_.get("fmv_31jan2018"),
            table_ref=fields_.get("table_ref"),
        )
    return scrips
