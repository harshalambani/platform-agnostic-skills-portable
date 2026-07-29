#!/usr/bin/env python3
"""
Unified persistent mapping rules for a GnuCash book.

One YAML file per GnuCash book (year-less), stored alongside it:
    MyBook2526.gnucash → MyBook_mapping_rules.yaml
    MyBook2627.gnucash → MyBook_mapping_rules.yaml   (same file — survives the year roll)

The sidecar filename is derived from the book's stem with any trailing run
of digits (the year/FY suffix) stripped, so learned rules are not orphaned
when a book is rolled to a new financial year. If a legacy year-stamped
sidecar (e.g. MyBook2526_mapping_rules.yaml) is found on load and the
year-less file does not yet exist, it is self-healed: merged into the
year-less file and renamed to `*.migrated`. See `_yearless_stem` and
`_migrate_legacy_year_stamped`.

Contains TWO sections:
    _overrides:   User corrections from the Review UI (highest priority)
    <BankKey>:    Auto-generated rules from GnuCash transaction history

Auto-rules are merged (new patterns added, existing updated) on each run.
User overrides are never overwritten by auto-rules.

Format matches what map_accounts() already consumes:
    _overrides:
      - patterns: ["IMPS.*WORLDLINE"]
        account: "Expenses:Online Payments"
        confidence: override
        source: user
        added: "2026-06-17"
    BoB:
      - patterns: ["SELF"]
        account: "Root Account:Assets:Cash"
        confidence: high
        reason: "31 occurrences, last 2022-04-18"
        source: auto
"""

import logging
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

log = logging.getLogger(__name__)

OVERRIDES_KEY = "_overrides"


# ---------------------------------------------------------------------------
# Path resolution
# ---------------------------------------------------------------------------

# Temp-directory markers (Gradio uploads, Python tempfile, Windows %TEMP%)
_TEMP_MARKERS = ("gradio", "tmp", "temp", "appdata" + os.sep + "local" + os.sep + "temp")

# Module-level cache: resolved original directory per gnucash stem
_original_dir_cache: Dict[str, Optional[Path]] = {}

# Trailing run of digits on a book stem == year/FY suffix (e.g. "2526").
_TRAILING_YEAR_DIGITS_RE = re.compile(r"\d+$")


def _yearless_stem(stem: str) -> str:
    """Strip a trailing run of digits (year/FY suffix) from a book stem.

    Pure/side-effect-free. Digits not at the very end (mid-name) are left
    alone; a stem with no trailing digits is returned unchanged. Case is
    preserved.

    Examples:
        "CarolDoe2526"      -> "CarolDoe"
        "BobDoeHUF2526" -> "BobDoeHUF"
        "NoDigitsHere"          -> "NoDigitsHere"
    """
    return _TRAILING_YEAR_DIGITS_RE.sub("", stem)


def _is_temp_dir(d: Path) -> bool:
    """Heuristic: does this path look like a temp/upload staging directory?"""
    low = str(d).lower().replace("\\", "/")
    return any(marker in low for marker in _TEMP_MARKERS)


def _find_original_gnucash_dir(stem: str, config_path: str = None) -> Optional[Path]:
    """Scan Data/ for a .gnucash file matching *stem* and return its directory."""
    if stem in _original_dir_cache:
        return _original_dir_cache[stem]

    search_roots: list[Path] = []
    if config_path:
        # config_path is typically Data/settings/config.yaml → Data/
        search_roots.append(Path(config_path).resolve().parent.parent)
    # Also try CWD-relative Data/
    cwd_data = Path.cwd() / "Data"
    if cwd_data.is_dir() and cwd_data not in search_roots:
        search_roots.append(cwd_data)

    target = f"{stem}.gnucash"
    for root in search_roots:
        if not root.is_dir():
            continue
        try:
            for gf in root.rglob(target):
                if gf.is_file() and not _is_temp_dir(gf.parent):
                    _original_dir_cache[stem] = gf.parent
                    return gf.parent
        except PermissionError:
            continue

    _original_dir_cache[stem] = None
    return None


def rules_path(gnucash_file: str, config_path: str = None) -> Path:
    """Resolve the persistent rules YAML for a GnuCash book.

    The sidecar filename uses the YEAR-LESS stem (trailing digit run
    stripped) so it survives a book being rolled to a new financial year.
    The original .gnucash file is still located (step 2) using the RAW
    stem, since that filename genuinely carries the year.

    Resolution order:
        1. If the gnucash file is in a real (non-temp) directory, co-locate.
        2. Scan Data/ for a .gnucash with the same name → co-locate with original.
        3. Fallback: Data/settings/{yearless_stem}_mapping_rules.yaml
        4. Last resort: next to the (temp) gnucash file.
    """
    p = Path(gnucash_file)
    yaml_name = f"{_yearless_stem(p.stem)}_mapping_rules.yaml"

    # 1. Non-temp directory → co-locate directly
    if not _is_temp_dir(p.parent):
        return p.parent / yaml_name

    # 2. Find original .gnucash in Data/ tree (raw stem — that filename
    #    really does carry the year)
    orig_dir = _find_original_gnucash_dir(p.stem, config_path)
    if orig_dir:
        return orig_dir / yaml_name

    # 3. Fallback to settings directory
    if config_path:
        settings_dir = Path(config_path).resolve().parent
        return settings_dir / yaml_name

    # 4. Last resort — temp directory (will be lost)
    return p.parent / yaml_name


# ---------------------------------------------------------------------------
# Legacy year-stamped sidecar self-heal
# ---------------------------------------------------------------------------

def _parse_added_date(rule: Dict):
    """Parse a rule's `added: YYYY-MM-DD` field. Returns a date or None."""
    added = rule.get("added") if isinstance(rule, dict) else None
    if not added:
        return None
    try:
        return datetime.strptime(str(added), "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def _merge_rules_dicts(dicts: List[Dict]) -> Dict[str, List[Dict]]:
    """Merge multiple already-loaded rules dicts into one.

    Union of ALL top-level sections (``_overrides`` and every bank-key
    section). Within a section, rules are deduplicated by their first
    pattern (consistent with `merge_auto_rules`). On a pattern collision,
    the entry with the newest `added:` date wins; an entry with a
    missing/unparseable date loses to any entry with a valid date; if all
    colliding entries lack a valid date, the first encountered is kept.

    `dicts` should be provided in a deterministic order (e.g. legacy files
    sorted by filename) so the result is reproducible.
    """
    merged: Dict[str, List[Dict]] = {}
    section_pattern_index: Dict[str, Dict[str, int]] = {}

    for d in dicts:
        if not isinstance(d, dict):
            continue
        for section, rules in d.items():
            if not isinstance(rules, list):
                continue
            out = merged.setdefault(section, [])
            idx_map = section_pattern_index.setdefault(section, {})
            for rule in rules:
                if not isinstance(rule, dict):
                    continue
                first_pat = (rule.get("patterns") or [""])[0]
                if not first_pat:
                    continue
                if first_pat in idx_map:
                    existing_idx = idx_map[first_pat]
                    existing_rule = out[existing_idx]
                    existing_date = _parse_added_date(existing_rule)
                    new_date = _parse_added_date(rule)
                    if new_date is not None and (existing_date is None or new_date > existing_date):
                        out[existing_idx] = rule
                    # else: keep existing (first-encountered / already-newest wins)
                else:
                    out.append(rule)
                    idx_map[first_pat] = len(out) - 1

    return merged


def _write_rules_yaml(rp: Path, rules: Dict[str, List[Dict]]) -> None:
    """Write a rules dict to *rp* with the same section ordering as `save_rules`."""
    rp.parent.mkdir(parents=True, exist_ok=True)
    ordered: Dict[str, list] = {}
    if OVERRIDES_KEY in rules:
        ordered[OVERRIDES_KEY] = rules[OVERRIDES_KEY]
    for k in sorted(rules):
        if k != OVERRIDES_KEY:
            ordered[k] = rules[k]
    with open(rp, "w", encoding="utf-8") as f:
        yaml.dump(
            ordered, f,
            default_flow_style=False,
            sort_keys=False,
            allow_unicode=True,
        )


def _migrate_legacy_year_stamped(rp: Path, yearless_stem: str) -> bool:
    """Self-heal: if year-stamped sidecar(s) exist next to *rp* but the
    year-less *rp* does not, merge them into *rp* and rename each consumed
    legacy file to `*.migrated`.

    Returns True if *rp* now exists as a result of migration.
    """
    if rp.exists():
        return False
    parent = rp.parent
    if not parent.is_dir():
        return False

    suffix_len = len("_mapping_rules.yaml")
    candidates = []
    for f in sorted(parent.glob(f"{yearless_stem}*_mapping_rules.yaml"), key=lambda x: x.name):
        name_stem = f.name[:-suffix_len]
        year_part = name_stem[len(yearless_stem):]
        if year_part.isdigit():  # must be yearless_stem + digits, nothing else
            candidates.append(f)

    if not candidates:
        return False

    loaded: List[Dict] = []
    for f in candidates:
        try:
            with open(f, "r", encoding="utf-8") as fh:
                data = yaml.safe_load(fh)
        except Exception as e:
            log.warning("Skipping unreadable legacy rules file %s: %s", f, e)
            continue
        if isinstance(data, dict):
            loaded.append(data)
        else:
            log.warning("Skipping invalid legacy rules file %s (not a mapping)", f)

    if not loaded:
        return False

    merged = _merge_rules_dicts(loaded)
    try:
        _write_rules_yaml(rp, merged)
    except Exception as e:
        log.error("Failed to write migrated rules to %s: %s", rp, e)
        return False

    for f in candidates:
        try:
            f.rename(f.with_name(f.name + ".migrated"))
        except Exception as e:
            log.warning("Failed to rename legacy rules file %s: %s", f, e)

    total = sum(len(v) for v in merged.values())
    log.info("Self-healed %d legacy year-stamped rule file(s) (%d rules) -> %s",
              len(candidates), total, rp)
    return True


# ---------------------------------------------------------------------------
# Load / save full rules file
# ---------------------------------------------------------------------------

def load_rules(gnucash_file: str, config_path: str = None) -> Dict[str, List[Dict]]:
    """Load the unified rules YAML. Returns {} if missing.

    Before the existence check, self-heals legacy year-stamped sidecars
    (see `_migrate_legacy_year_stamped`) if the year-less file is absent.
    """
    rp = rules_path(gnucash_file, config_path)
    if not rp.exists():
        yearless_stem = _yearless_stem(Path(gnucash_file).stem)
        _migrate_legacy_year_stamped(rp, yearless_stem)
    if not rp.exists():
        return {}
    try:
        with open(rp, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        return data if isinstance(data, dict) else {}
    except Exception as e:
        log.error("Failed to load rules from %s: %s", rp, e)
        return {}


def save_rules(gnucash_file: str, rules: Dict[str, List[Dict]],
               config_path: str = None) -> None:
    """Write the unified rules YAML."""
    rp = rules_path(gnucash_file, config_path)
    rp.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(rp, "w", encoding="utf-8") as f:
            # Write _overrides first, then bank keys alphabetically
            ordered: Dict[str, list] = {}
            if OVERRIDES_KEY in rules:
                ordered[OVERRIDES_KEY] = rules[OVERRIDES_KEY]
            for k in sorted(rules):
                if k != OVERRIDES_KEY:
                    ordered[k] = rules[k]
            yaml.dump(
                ordered, f,
                default_flow_style=False,
                sort_keys=False,
                allow_unicode=True,
            )
        total = sum(len(v) for v in rules.values())
        log.info("Saved %d rules to %s", total, rp)
    except Exception as e:
        log.error("Failed to save rules to %s: %s", rp, e)
        raise


# ---------------------------------------------------------------------------
# Merge auto-generated rules (additive, never overwrites overrides)
# ---------------------------------------------------------------------------

def merge_auto_rules(
    gnucash_file: str,
    new_rules_by_bank: Dict[str, List[Dict]],
    config_path: str = None,
) -> Dict[str, List[Dict]]:
    """Merge freshly generated auto-rules into the persistent file.

    - New patterns are added.
    - Existing patterns get their confidence/reason/frequency updated.
    - User overrides (_overrides section) are never touched.
    - Returns the merged dict (also saved to disk).
    """
    existing = load_rules(gnucash_file, config_path)

    for bank, new_rules in new_rules_by_bank.items():
        if bank == OVERRIDES_KEY:
            continue  # never overwrite user overrides via auto merge

        old_rules = existing.get(bank, [])
        # Index existing auto-rules by their first pattern for dedup
        pattern_index: Dict[str, int] = {}
        for i, rule in enumerate(old_rules):
            if rule.get("source") == "user":
                continue  # skip user overrides that somehow ended up here
            first_pat = (rule.get("patterns") or [""])[0]
            if first_pat:
                pattern_index[first_pat] = i

        for nr in new_rules:
            nr["source"] = "auto"
            first_pat = (nr.get("patterns") or [""])[0]
            if not first_pat:
                continue
            if first_pat in pattern_index:
                # Update existing rule (confidence/reason may have changed)
                idx = pattern_index[first_pat]
                old_rules[idx] = nr
            else:
                old_rules.append(nr)
                pattern_index[first_pat] = len(old_rules) - 1

        existing[bank] = old_rules

    save_rules(gnucash_file, existing, config_path)
    return existing


# ---------------------------------------------------------------------------
# User overrides — add/load/match (used by Review UI)
# ---------------------------------------------------------------------------

def load_overrides(gnucash_file: str, config_path: str = None) -> List[Dict]:
    """Load just the _overrides section. Backward-compat wrapper."""
    rules = load_rules(gnucash_file, config_path)
    overrides = rules.get(OVERRIDES_KEY, [])
    # Validate
    valid = []
    for rule in overrides:
        pat = rule.get("patterns", [rule.get("pattern", "")])[0] if rule.get("patterns") else rule.get("pattern", "")
        acct = rule.get("account", "")
        if not pat or not acct:
            continue
        try:
            re.compile(pat)
        except re.error:
            continue
        valid.append(rule)
    return valid


def save_overrides_batch(gnucash_file: str, overrides: List[Dict],
                        config_path: str = None) -> None:
    """Replace the _overrides section. Preserves auto-rules."""
    rules = load_rules(gnucash_file, config_path)
    today = datetime.now().strftime("%Y-%m-%d")

    clean = []
    for ov in overrides:
        # Normalize to patterns list format
        patterns = ov.get("patterns", [])
        if not patterns and ov.get("pattern"):
            patterns = [ov["pattern"]]
        if not patterns:
            continue
        clean.append({
            "patterns": patterns,
            "account": ov.get("account", ""),
            "confidence": "override",
            "source": "user",
            "added": ov.get("added", today),
        })

    rules[OVERRIDES_KEY] = clean
    save_rules(gnucash_file, rules, config_path)


def match_overrides(description: str, overrides: List[Dict]) -> Tuple[Optional[str], str]:
    """Match description against override rules. Returns (account, reason) or (None, '')."""
    if not description:
        return None, ""

    for ov in overrides:
        patterns = ov.get("patterns", [])
        if not patterns and ov.get("pattern"):
            patterns = [ov["pattern"]]
        account = ov.get("account", "")
        added = ov.get("added", "unknown")

        for pat in patterns:
            if not pat or not account:
                continue
            try:
                if re.search(pat, description, re.IGNORECASE):
                    return account, f"Override (added {added})"
            except re.error:
                continue

    return None, ""


# ---------------------------------------------------------------------------
# Migration helper — import old _account_overrides.yaml into unified file
# ---------------------------------------------------------------------------

def migrate_legacy_overrides(gnucash_file: str, config_path: str = None) -> int:
    """If a legacy _account_overrides.yaml exists, merge it and rename the old file.

    Returns the number of overrides migrated (0 if nothing to do).
    """
    p = Path(gnucash_file)
    legacy = p.parent / f"{p.stem}_account_overrides.yaml"
    if not legacy.exists():
        return 0

    try:
        with open(legacy, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
    except Exception:
        return 0

    if not isinstance(data, list) or not data:
        return 0

    # Convert legacy format (flat list with 'pattern' key) to unified format
    converted = []
    for item in data:
        pat = item.get("pattern", "")
        acct = item.get("account", "")
        if pat and acct:
            converted.append({
                "patterns": [pat],
                "account": acct,
                "confidence": "override",
                "source": "user",
                "added": item.get("added", "unknown"),
            })

    if not converted:
        return 0

    # Merge into unified file
    rules = load_rules(gnucash_file, config_path)
    existing_overrides = rules.get(OVERRIDES_KEY, [])
    existing_pats = set()
    for ov in existing_overrides:
        for p2 in ov.get("patterns", []):
            existing_pats.add(p2)

    added = 0
    for c in converted:
        cpat = c["patterns"][0]
        if cpat not in existing_pats:
            existing_overrides.append(c)
            existing_pats.add(cpat)
            added += 1

    if added:
        rules[OVERRIDES_KEY] = existing_overrides
        save_rules(gnucash_file, rules, config_path)

    # Rename legacy file so it's not re-migrated
    backup = legacy.with_suffix(".yaml.migrated")
    legacy.rename(backup)
    log.info("Migrated %d overrides from %s → %s", added, legacy.name, rules_path(gnucash_file, config_path).name)
    return added


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as tmpdir:
        test_gc = str(Path(tmpdir) / "test.gnucash")
        Path(test_gc).touch()

        # Test save/load overrides
        save_overrides_batch(test_gc, [
            {"pattern": "IMPS.*WORLDLINE", "account": "Expenses:Online"},
            {"pattern": "UPI/AMAZON", "account": "Expenses:Shopping"},
        ])
        ovs = load_overrides(test_gc)
        print(f"Overrides: {len(ovs)}")
        for o in ovs:
            print(f"  {o['patterns']} → {o['account']}")

        # Test merge auto-rules
        merge_auto_rules(test_gc, {
            "BoB": [
                {"patterns": ["SELF"], "account": "Assets:Cash", "confidence": "high",
                 "reason": "31 occ", "frequency": 31},
            ],
        })

        rules = load_rules(test_gc)
        print(f"\nFull rules file has {len(rules)} sections:")
        for k, v in rules.items():
            print(f"  {k}: {len(v)} rules")

        # Test match
        acct, reason = match_overrides("IMPS to WORLDLINE PAYMENT", ovs)
        print(f"\nMatch: {acct} — {reason}")

        print("\n✓ All tests passed!")
