"""
tests/skill_itr_workbook/test_configs_entities.py -- configs.py entities.yaml
writer (dump_entities) + field validator (validate_entity_fields), added
alongside the ITR Entities CRUD tab (2026-07-25 handover).

Covers:
  - round-trip: load_entities(dump_entities(entities)) reproduces the same
    EntityProfile values, including audit_case / audit_case_by_ay (PR #117).
  - determinism/idempotency: dump_entities is a pure function of the dict
    (sorted entity keys + sorted field/nested-dict keys), so re-dumping
    after touching only one entity leaves the others' block unchanged.
  - validate_entity_fields: PAN regex, status/regime enums, ISO dates,
    regime_by_ay / audit_case_by_ay value types.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SCRIPTS = ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"
for p in (str(SCRIPTS),):
    if p not in sys.path:
        sys.path.insert(0, p)

import configs  # noqa: E402


def _make_entities() -> dict:
    return {
        "SYN-IND": configs.EntityProfile(
            key="SYN-IND", name="Synthetic Individual", pan="AAAAA0000A",
            status="Individual", residency="Resident", dob="1980-01-01",
            address="123 Example Street", default_regime="new",
            regime_by_ay={"2026-27": "new", "2025-26": "old"},
            audit_case=False, audit_case_by_ay={},
            extra_items={"bf_losses": [], "clubbing_notes": []},
        ),
        "SYN-HUF": configs.EntityProfile(
            key="SYN-HUF", name="Synthetic HUF", pan="BBBBB1111B",
            status="HUF", residency="Resident", doi="1975-06-15",
            default_regime="new",
        ),
        "SYN-IND-BIZ": configs.EntityProfile(
            key="SYN-IND-BIZ", name="Synthetic Business Individual", pan="DDDDD3333D",
            status="Individual", residency="Resident", dob="1978-01-01",
            business_subtree="Income/xBusiness Income",
            workbook_match="SynIndBiz", default_regime="new",
            audit_case=True, audit_case_by_ay={"2025-26": False},
        ),
    }


# ---------------------------------------------------------------------------
# Round-trip.
# ---------------------------------------------------------------------------

def test_dump_then_load_round_trips_all_fields(tmp_path):
    entities = _make_entities()
    text = configs.dump_entities(entities)
    out = tmp_path / "entities.yaml"
    out.write_text(text, encoding="utf-8")

    loaded = configs.load_entities(out)
    assert set(loaded.keys()) == set(entities.keys())
    for key, orig in entities.items():
        got = loaded[key]
        assert got.name == orig.name
        assert got.pan == orig.pan
        assert got.status == orig.status
        assert got.residency == orig.residency
        assert got.dob == orig.dob
        assert got.doi == orig.doi
        assert got.address == orig.address
        assert got.business_subtree == orig.business_subtree
        assert got.workbook_match == orig.workbook_match
        assert got.default_regime == orig.default_regime
        assert got.regime_by_ay == orig.regime_by_ay
        assert got.audit_case == orig.audit_case
        assert got.audit_case_by_ay == orig.audit_case_by_ay


def test_dump_includes_stable_header():
    text = configs.dump_entities(_make_entities())
    assert text.startswith(configs.ENTITIES_YAML_HEADER)
    assert "entities.yaml" in text
    assert "ITR Entities" in text


def test_dump_is_idempotent():
    entities = _make_entities()
    text1 = configs.dump_entities(entities)
    text2 = configs.dump_entities(entities)
    assert text1 == text2


def test_dump_omits_default_and_empty_optional_fields():
    """A minimal entity (only required fields + the always-emitted
    residency/default_regime) must not carry stray nulls/empty collections
    for fields nobody touched."""
    e = configs.EntityProfile(key="SYN-MIN", name="Min", pan="CCCCC2222C", status="Individual")
    text = configs.dump_entities({"SYN-MIN": e})
    assert "dob" not in text
    assert "doi" not in text
    assert "address" not in text
    assert "aadhaar" not in text
    assert "business_subtree" not in text
    assert "workbook_match" not in text  # not set -> no stray key in the dump
    assert "audit_case" not in text  # False is default -> omitted entirely
    assert "regime_by_ay" not in text
    assert "extra_items" not in text


def test_dump_emits_workbook_match_when_set():
    e = configs.EntityProfile(
        key="SYN-WBM", name="Workbook Match", pan="FFFFF5555F", status="Individual",
        workbook_match="SynWbm",
    )
    text = configs.dump_entities({"SYN-WBM": e})
    assert "workbook_match: SynWbm" in text


def test_dump_emits_audit_case_true_explicitly():
    e = configs.EntityProfile(
        key="SYN-AUDIT", name="Audit Case", pan="EEEEE4444E", status="Individual",
        audit_case=True,
    )
    text = configs.dump_entities({"SYN-AUDIT": e})
    assert "audit_case: true" in text


# ---------------------------------------------------------------------------
# Determinism: editing one entity leaves the other entity's block untouched
# byte-for-byte (the "unrelated entities byte-stable" acceptance gate).
# ---------------------------------------------------------------------------

def test_editing_one_entity_leaves_others_byte_stable():
    entities = _make_entities()
    text_before = configs.dump_entities(entities)

    # Extract SYN-HUF's block from the "before" dump.
    def _block(text: str, key: str) -> str:
        lines = text.splitlines()
        start = next(i for i, ln in enumerate(lines) if ln == f"{key}:")
        end = start + 1
        while end < len(lines) and (lines[end].startswith("  ") or not lines[end].strip()):
            end += 1
        return "\n".join(lines[start:end])

    huf_before = _block(text_before, "SYN-HUF")

    # Edit only SYN-IND.
    entities["SYN-IND"].name = "Renamed Individual"
    text_after = configs.dump_entities(entities)
    huf_after = _block(text_after, "SYN-HUF")

    assert huf_before == huf_after
    assert "Renamed Individual" in text_after


# ---------------------------------------------------------------------------
# validate_entity_fields.
# ---------------------------------------------------------------------------

def test_validate_accepts_well_formed_entity():
    errors = configs.validate_entity_fields(
        key="SYN-IND", name="Synthetic Individual", pan="AAAAA0000A",
        status="Individual", dob="1980-01-01", default_regime="new",
        regime_by_ay={"2025-26": "old"}, audit_case_by_ay={"2025-26": True},
    )
    assert errors == []


def test_validate_rejects_bad_pan():
    for bad_pan in ("AAAA0000A", "AAAAA0000", "12345ABCDE", ""):
        errors = configs.validate_entity_fields(
            key="X", name="N", pan=bad_pan, status="Individual",
        )
        assert any("PAN" in e for e in errors), f"expected PAN error for {bad_pan!r}"


def test_validate_accepts_lowercase_pan_after_uppercasing():
    # The tab uppercases PAN before storing; validate_entity_fields itself
    # also accepts lowercase input (uppercases internally) so a raw call
    # from a test/UI path isn't accidentally stricter than the stored form.
    errors = configs.validate_entity_fields(key="X", name="N", pan="aaaaa0000a", status="Individual")
    assert errors == []


def test_validate_rejects_bad_status():
    errors = configs.validate_entity_fields(key="X", name="N", pan="AAAAA0000A", status="Trust")
    assert any("Status" in e for e in errors)


def test_validate_rejects_bad_regime():
    errors = configs.validate_entity_fields(
        key="X", name="N", pan="AAAAA0000A", status="Individual", default_regime="middle",
    )
    assert any("default_regime" in e for e in errors)


def test_validate_rejects_bad_dates():
    errors = configs.validate_entity_fields(
        key="X", name="N", pan="AAAAA0000A", status="Individual",
        dob="not-a-date",
    )
    assert any("dob" in e for e in errors)

    errors2 = configs.validate_entity_fields(
        key="X", name="N", pan="AAAAA0000A", status="Individual",
        doi="31-01-2018",
    )
    assert any("doi" in e for e in errors2)


def test_validate_rejects_bad_regime_by_ay_value():
    errors = configs.validate_entity_fields(
        key="X", name="N", pan="AAAAA0000A", status="Individual",
        regime_by_ay={"2025-26": "medium"},
    )
    assert any("regime_by_ay" in e for e in errors)


def test_validate_rejects_bad_audit_case_by_ay_value():
    errors = configs.validate_entity_fields(
        key="X", name="N", pan="AAAAA0000A", status="Individual",
        audit_case_by_ay={"2025-26": "yes"},
    )
    assert any("audit_case_by_ay" in e for e in errors)


def test_validate_rejects_blank_name_and_key():
    errors = configs.validate_entity_fields(key="", name="", pan="AAAAA0000A", status="Individual")
    assert any("key" in e.lower() for e in errors)
    assert any("Name" in e for e in errors)


def test_validate_rejects_key_with_path_separator():
    errors = configs.validate_entity_fields(key="a/b", name="N", pan="AAAAA0000A", status="Individual")
    assert any("key" in e.lower() for e in errors)


# ---------------------------------------------------------------------------
# find_filed_returns -- entity-key-prefix matching over Data/ITRFiled/.
# ---------------------------------------------------------------------------

def test_find_filed_returns_matches_json_and_pdf(tmp_path):
    d = tmp_path / "ITRFiled"
    d.mkdir()
    (d / "SynEnt2425.json").write_text("{}", encoding="utf-8")
    (d / "SynEnt2425.pdf").write_text("x", encoding="utf-8")
    (d / "SynEnt2324.json").write_text("{}", encoding="utf-8")

    found = configs.find_filed_returns("SynEnt", d)
    assert [p.name for p in found] == ["SynEnt2324.json", "SynEnt2425.json", "SynEnt2425.pdf"]


def test_find_filed_returns_is_sorted_and_deterministic(tmp_path):
    d = tmp_path / "ITRFiled"
    d.mkdir()
    for name in ("SynEnt2425.json", "SynEnt2223.json", "SynEnt2324.pdf"):
        (d / name).write_text("{}", encoding="utf-8")

    found1 = configs.find_filed_returns("SynEnt", d)
    found2 = configs.find_filed_returns("SynEnt", d)
    assert [p.name for p in found1] == [p.name for p in found2]
    assert [p.name for p in found1] == sorted(p.name for p in found1)


def test_find_filed_returns_missing_dir_is_noop(tmp_path):
    missing = tmp_path / "does-not-exist"
    assert configs.find_filed_returns("SynEnt", missing) == []


def test_find_filed_returns_ignores_unrelated_files(tmp_path):
    d = tmp_path / "ITRFiled"
    d.mkdir()
    (d / "SynEnt2425.json").write_text("{}", encoding="utf-8")
    (d / "OtherEnt2425.json").write_text("{}", encoding="utf-8")
    (d / "SynEnt2425.txt").write_text("x", encoding="utf-8")  # not json/pdf -- excluded
    (d / "readme.md").write_text("x", encoding="utf-8")

    found = configs.find_filed_returns("SynEnt", d)
    assert [p.name for p in found] == ["SynEnt2425.json"]


def test_find_filed_returns_prefix_collision_boundary(tmp_path):
    """CRITICAL: entity keys can be prefixes of each other (e.g.
    "SynEnt" vs "SynEntHUF"). A file belongs to `key` iff `key` is
    immediately followed by a non-letter (digit) or end-of-stem."""
    d = tmp_path / "ITRFiled"
    d.mkdir()
    (d / "SynEnt2425.json").write_text("{}", encoding="utf-8")
    (d / "SynEntHUF2425.json").write_text("{}", encoding="utf-8")

    short_matches = configs.find_filed_returns("SynEnt", d)
    assert [p.name for p in short_matches] == ["SynEnt2425.json"]
    assert "SynEntHUF2425.json" not in [p.name for p in short_matches]

    huf_matches = configs.find_filed_returns("SynEntHUF", d)
    assert [p.name for p in huf_matches] == ["SynEntHUF2425.json"]
    assert "SynEnt2425.json" not in [p.name for p in huf_matches]


def test_find_filed_returns_digit_extended_key_collision_needs_all_keys(tmp_path):
    """CRITICAL (review fix): the boundary rule alone does not separate
    digit-extended key collisions -- "SynP1" and "SynP12" both match a
    "SynP12425.json" stem, since the character right after each key is a
    digit either way. Passing all_entity_keys disambiguates by longest
    match; without it, the old (documented) limitation still applies."""
    d = tmp_path / "ITRFiled"
    d.mkdir()
    (d / "SynP12425.json").write_text("{}", encoding="utf-8")

    all_keys = {"SynP1", "SynP12"}

    # Without all_entity_keys: both keys match under the plain boundary rule.
    short_alone = configs.find_filed_returns("SynP1", d)
    assert [p.name for p in short_alone] == ["SynP12425.json"]

    # With all_entity_keys: attributed only to the longest matching key.
    short_disambiguated = configs.find_filed_returns("SynP1", d, all_entity_keys=all_keys)
    assert short_disambiguated == []

    long_disambiguated = configs.find_filed_returns("SynP12", d, all_entity_keys=all_keys)
    assert [p.name for p in long_disambiguated] == ["SynP12425.json"]


def test_find_filed_returns_case_insensitive_stem(tmp_path):
    """Windows filesystems are case-preserving but not case-sensitive; a
    differently-cased filename must still resolve to the entity key so the
    rename/archive cascade doesn't silently skip it."""
    d = tmp_path / "ITRFiled"
    d.mkdir()
    (d / "synent2425.json").write_text("{}", encoding="utf-8")

    found = configs.find_filed_returns("SynEnt", d)
    assert [p.name for p in found] == ["synent2425.json"]
