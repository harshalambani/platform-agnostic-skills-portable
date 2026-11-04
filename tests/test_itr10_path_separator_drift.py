"""ITR-10: a separator-only difference (":" vs "/") is never "path drifted"."""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import configs                                   # noqa: E402
from apply_mapping_corrections import refresh_drifted_paths   # noqa: E402


def _map(tmp_path, path):
    f = tmp_path / "m.mapping.yaml"
    f.write_text(yaml.safe_dump([{"guid": "g1", "path": path, "tag": "AL_CASH_BANK"}]), encoding="utf-8")
    return f


def test_separator_only_difference_is_not_drift(tmp_path):
    for stored in ("Assets:Bank:Main", "Assets/Bank/Main", "Assets : Bank / Main", "Root Account:Assets:Bank:Main"):
        r = configs.load_mapping(_map(tmp_path, stored), {"g1": "Assets/Bank/Main"})
        assert not r.warnings, (stored, r.warnings)
        r = configs.load_mapping(_map(tmp_path, stored), {"g1": "Assets:Bank:Main"})
        assert not r.warnings, (stored, r.warnings)


def test_a_real_rename_still_warns(tmp_path):
    r = configs.load_mapping(_map(tmp_path, "Assets:Bank:Main"), {"g1": "Assets/Bank/Primary"})
    assert len(r.warnings) == 1 and "drifted" in r.warnings[0]


def test_not_found_still_warns(tmp_path):
    r = configs.load_mapping(_map(tmp_path, "Assets:Bank:Main"), {"other": "Assets/Bank/Main"})
    assert len(r.warnings) == 1 and "NOT FOUND" in r.warnings[0]


def test_refresh_does_not_rewrite_separator_only_differences():
    e = configs.MappingEntry(guid="g1", path="Assets:Bank:Main", tag="AL_CASH_BANK", flags=[], note=None,
                             suggested_by_llm=None)
    out, n = refresh_drifted_paths({"g1": e}, {"g1": "Assets/Bank/Main"})
    assert n == 0 and out["g1"].path == "Assets:Bank:Main"
    out, n = refresh_drifted_paths({"g1": e}, {"g1": "Assets/Bank/Primary"})
    assert n == 1 and out["g1"].path == "Assets/Bank/Primary"


def test_canonical_form():
    assert configs.canonical_account_path("Root Account:A : B") == "A/B"
    assert not configs.same_account_path("A/B", "A/C")
