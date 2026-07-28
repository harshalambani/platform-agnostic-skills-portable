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

import re
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
    workbook_match: str | None = None     # ITR Mapping Review Layer A attribution token --
                                       # the name portion of the workbook filename WITHOUT
                                       # the year (e.g. "KiranAmbani") -- consumed by
                                       # _workbook_match_map() in ui/tabs/itr_mapping_review.py
    default_regime: str = "new"
    regime_by_ay: dict = field(default_factory=dict)   # {"2026-27": "old", ...}
    audit_case: bool = False             # liable to audit -> 31 Oct s.139(1) due date
                                         # (else 31 July); drives 234A/B/C interest.
    audit_case_by_ay: dict = field(default_factory=dict)  # per-AY override, {"2026-27": true}
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
            workbook_match=fields_.get("workbook_match"),
            default_regime=fields_.get("default_regime", "new"),
            regime_by_ay=fields_.get("regime_by_ay") or {},
            audit_case=bool(fields_.get("audit_case", False)),
            audit_case_by_ay=fields_.get("audit_case_by_ay") or {},
            extra_items=fields_.get("extra_items") or {},
        )
    return entities


# ---------------------------------------------------------------------------
# entities.yaml -- writer (ITR Entities CRUD tab, 2026-07-25).
#
# DECISION (Harshal, 2026-07-23, see the itr-entities-crud handover prompt's
# "Decisions -- RESOLVED" section -- do not re-litigate): no ruamel.yaml.
# `entities.yaml` carries only 3 comment lines out of 77 in the live file;
# re-emitting a stable header + `yaml.safe_dump(..., sort_keys=True)` is a
# better trade than adding a comment-preserving YAML dependency to a
# PyInstaller-frozen build. Output is a pure function of the entities dict
# (sorted entity keys, sorted field names, sorted nested dict keys) --
# saving one entity never perturbs another entity's emitted block.
# ---------------------------------------------------------------------------

ENTITIES_YAML_HEADER = (
    "# =============================================================================\n"
    "# entities.yaml -- Data/itr/entities.yaml (per-entity ITR profile).\n"
    "# Managed via the \"ITR Entities\" tab in the app (Add/Modify/Delete) -- hand\n"
    "# edits survive individual field values but NOT comments or key ordering: this\n"
    "# header is re-emitted verbatim on every save, and entity blocks + fields are\n"
    "# written in a deterministic (sorted-key) order via configs.dump_entities().\n"
    "# Unknown/extra top-level fields on an entity are also silently dropped on\n"
    "# save (EntityProfile is a closed dataclass) -- only fields it declares survive.\n"
    "# See bundling/canonical/itr/entities.example.yaml for field documentation.\n"
    "# =============================================================================\n"
)


def _entity_to_dict(e: EntityProfile) -> dict:
    """EntityProfile -> plain dict for serialization. Required fields plus
    `residency`/`default_regime` (both carry non-obvious defaults, so the
    live convention -- see entities.example.yaml -- always spells them out)
    are always emitted; everything else is emitted only when it carries a
    real (non-empty/non-default) value, so a field nobody has ever touched
    never appears as a stray `null` or empty collection in the file."""
    d: dict = {
        "name": e.name,
        "pan": e.pan,
        "status": e.status,
        "residency": e.residency,
        "default_regime": e.default_regime,
    }
    if e.dob:
        d["dob"] = e.dob
    if e.doi:
        d["doi"] = e.doi
    if e.address:
        d["address"] = e.address
    if e.father_name:
        d["father_name"] = e.father_name
    if e.aadhaar:
        d["aadhaar"] = e.aadhaar
    if e.business_subtree:
        d["business_subtree"] = e.business_subtree
    if e.workbook_match:
        d["workbook_match"] = e.workbook_match
    if e.regime_by_ay:
        d["regime_by_ay"] = dict(e.regime_by_ay)
    if e.audit_case:
        d["audit_case"] = True
    if e.audit_case_by_ay:
        d["audit_case_by_ay"] = dict(e.audit_case_by_ay)
    if e.extra_items:
        d["extra_items"] = dict(e.extra_items)
    return d


def _filed_return_boundary_pattern(entity_key: str) -> re.Pattern:
    """The boundary-rule regex used by find_filed_returns(): `entity_key`
    must be immediately followed by a non-letter (digit) or end-of-stem.
    Case-insensitive (Windows filesystems are case-preserving but not
    case-sensitive, and files are only ever moved/archived, never deleted,
    so matching loosely here is safe)."""
    return re.compile(rf"^{re.escape(entity_key)}(?![A-Za-z])", re.IGNORECASE)


def find_filed_returns(
    entity_key: str,
    itrfiled_dir: str | Path,
    all_entity_keys: set[str] | None = None,
) -> list[Path]:
    """Find filed-return files (Data/ITRFiled/<entity_key><token>.{json,pdf})
    belonging to `entity_key`.

    The `<token>` suffix (e.g. "2425") is a human filing-batch label, not a
    reliable assessment year -- this function does not parse or trust any AY
    out of it, and the caller does not need one either; it keys on the
    entity-key prefix only.

    CRITICAL collision rule: entity keys can be prefixes of one another
    (e.g. "Vaikunth" vs "VaikunthHUF"). A file belongs to `entity_key` iff
    its stem matches ``^{re.escape(entity_key)}(?![A-Za-z])`` (case
    -insensitive) -- i.e. `entity_key` must be immediately followed by a
    non-letter (digit) or the end of the stem. This makes "Vaikunth2425"
    match entity_key="Vaikunth" but NOT "VaikunthHUF", and "VaikunthHUF2425"
    match only "VaikunthHUF".

    That boundary rule alone does NOT separate digit-extended key collisions
    -- keys like "Prop1" and "Prop12" both match a "Prop12425.json" stem
    (the character after "Prop1" is a digit, same as after "Prop12"). Pass
    `all_entity_keys` (the full set of currently-defined entity keys) to
    disambiguate: when supplied, a file is attributed to `entity_key` only
    if `entity_key` is the LONGEST key in `all_entity_keys` whose boundary
    rule matches the file's stem -- so "Prop12425.json" resolves to "Prop12"
    only, never "Prop1", once both keys are known. Without `all_entity_keys`
    (None, the default), the single-key boundary rule above is used as-is --
    existing callers that don't have the full key set keep their prior
    behaviour.

    Matches both .json and .pdf extensions (case-insensitive on extension).
    Returns [] (not an error) if `itrfiled_dir` doesn't exist. Deterministic:
    results are sorted. Pure aside from the one directory listing -- no other
    I/O.
    """
    d = Path(itrfiled_dir)
    if not d.is_dir():
        return []
    pattern = _filed_return_boundary_pattern(entity_key)
    key_patterns = (
        [(k, _filed_return_boundary_pattern(k)) for k in all_entity_keys]
        if all_entity_keys
        else None
    )
    matches: list[Path] = []
    for p in d.iterdir():
        if not p.is_file():
            continue
        if p.suffix.lower() not in (".json", ".pdf"):
            continue
        if not pattern.match(p.stem):
            continue
        if key_patterns:
            candidates = [k for k, pat in key_patterns if pat.match(p.stem)]
            if candidates:
                longest = max(candidates, key=len)
                if longest.lower() != entity_key.lower():
                    continue
        matches.append(p)
    return sorted(matches)


def dump_entities(entities: dict[str, EntityProfile]) -> str:
    """Serialize {entity_key: EntityProfile} -> entities.yaml text.

    Deterministic: entity keys and all mapping keys (including nested dicts
    like regime_by_ay/extra_items) are sorted, so re-dumping after touching
    only one entity leaves every other entity's block byte-identical. See
    the module-level DECISION note above for why this doesn't use
    ruamel.yaml despite dropping hand-written comments.
    """
    body = {key: _entity_to_dict(entities[key]) for key in sorted(entities.keys())}
    payload = yaml.safe_dump(body, sort_keys=True, allow_unicode=True, default_flow_style=False)
    return ENTITIES_YAML_HEADER + "\n" + payload


_PAN_RE_SRC = r"[A-Z]{5}\d{4}[A-Z]"
_ENTITY_STATUS_CHOICES = ("Individual", "HUF")
_ENTITY_REGIME_CHOICES = ("old", "new")


def validate_entity_fields(
    key: str,
    name: str,
    pan: str,
    status: str,
    dob: str | None = None,
    doi: str | None = None,
    default_regime: str = "new",
    regime_by_ay: dict | None = None,
    audit_case_by_ay: dict | None = None,
) -> list[str]:
    """Validate the fields of one entity before a write. Returns a list of
    human-readable error strings (empty list == valid). Pure/side-effect-free
    so the UI layer and tests can both call it directly."""
    import re as _re
    import datetime as _dt

    errors: list[str] = []

    if not key or not key.strip():
        errors.append("Entity key is required.")
    elif _re.search(r"[\\/]", key):
        errors.append(f"Entity key {key!r} must not contain '/' or '\\\\' (it becomes a filename).")

    if not name or not name.strip():
        errors.append("Name is required.")

    if not pan or not _re.fullmatch(_PAN_RE_SRC, pan.strip().upper()):
        errors.append(f"PAN {pan!r} is invalid -- expected format AAAAA0000A ({_PAN_RE_SRC}).")

    if status not in _ENTITY_STATUS_CHOICES:
        errors.append(f"Status {status!r} must be one of {_ENTITY_STATUS_CHOICES}.")

    if default_regime not in _ENTITY_REGIME_CHOICES:
        errors.append(f"default_regime {default_regime!r} must be one of {_ENTITY_REGIME_CHOICES}.")

    def _check_date(label: str, value: str | None) -> None:
        if not value:
            return
        try:
            _dt.date.fromisoformat(value.strip())
        except ValueError:
            errors.append(f"{label} {value!r} is not a valid ISO date (YYYY-MM-DD).")

    _check_date("dob", dob)
    _check_date("doi", doi)

    for ay, regime in (regime_by_ay or {}).items():
        if regime not in _ENTITY_REGIME_CHOICES:
            errors.append(f"regime_by_ay[{ay!r}] = {regime!r} must be one of {_ENTITY_REGIME_CHOICES}.")

    for ay, flag in (audit_case_by_ay or {}).items():
        if not isinstance(flag, bool):
            errors.append(f"audit_case_by_ay[{ay!r}] = {flag!r} must be true/false.")

    return errors


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
