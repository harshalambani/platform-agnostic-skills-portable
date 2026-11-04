"""BNK-07 guard: every skill's declared python_packages must be declared in
bundling/paskills.spec.

agents/ is excluded from PyInstaller's Analysis, so a package imported only
lazily from a skill agent (e.g. msoffcrypto in skill_sbm) is invisible to it
and the installed app fails with "No module named ...". The spec must list
each such package as a hidden import or a collect_* entry.
"""
from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SPEC = ROOT / "bundling" / "paskills.spec"
SKILLS = ROOT / "src" / "agents"

# distribution name -> import name, where they differ.
_DIST_TO_IMPORT = {
    "msoffcrypto-tool": "msoffcrypto",
    "pyyaml": "yaml",
    "pillow": "PIL",
    "python-dotenv": "dotenv",
    "extract-msg": "extract_msg",
}


def import_name(dist: str) -> str:
    key = dist.strip().lower()
    return _DIST_TO_IMPORT.get(key, key.replace("-", "_"))


def declared_packages() -> dict[str, list[str]]:
    """{distribution: [skill.yaml paths that declare it]}"""
    out: dict[str, list[str]] = {}
    for y in sorted(SKILLS.glob("*/skill.yaml")):
        data = yaml.safe_load(y.read_text(encoding="utf-8")) or {}
        pk = []

        def walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "python_packages" and isinstance(v, list):
                        pk.extend(str(x) for x in v)
                    else:
                        walk(v)
            elif isinstance(node, list):
                for v in node:
                    walk(v)

        walk(data)
        for d in pk:
            out.setdefault(d, []).append(y.parent.name)
    return out


def spec_declares(spec_text: str, mod: str) -> bool:
    """True if the spec lists `mod` (or a submodule) as a quoted hidden import
    or passes it to a collect_* helper."""
    pat = r'["\']' + re.escape(mod) + r'(?:\.[\w.]+)?["\']'
    # ignore commented lines
    live = "\n".join(l for l in spec_text.splitlines() if not l.lstrip().startswith("#"))
    return re.search(pat, live) is not None


def missing_from_spec(spec_text: str) -> list[str]:
    return sorted(
        f"{dist} (import {import_name(dist)}; declared by {', '.join(skills)})"
        for dist, skills in declared_packages().items()
        if not spec_declares(spec_text, import_name(dist))
    )


def test_every_skill_package_is_declared_in_the_spec():
    missing = missing_from_spec(SPEC.read_text(encoding="utf-8"))
    assert not missing, "paskills.spec is missing: " + "; ".join(missing)


def test_msoffcrypto_collects_all_submodules():
    text = SPEC.read_text(encoding="utf-8")
    assert 'collect_submodules("msoffcrypto")' in text


def test_guard_fails_naming_the_package_when_spec_lacks_it():
    synthetic = 'hiddenimports = ["yaml", "openpyxl", "xlrd", "pypdfium2", "pytesseract"]\n'
    missing = missing_from_spec(synthetic)
    assert any(m.startswith("msoffcrypto-tool") for m in missing)
    assert not any(m.startswith("openpyxl") or m.startswith("pyyaml") for m in missing)


def test_guard_ignores_commented_mentions():
    synthetic = '# "msoffcrypto"\nhiddenimports = ["yaml"]\n'
    assert any(m.startswith("msoffcrypto-tool") for m in missing_from_spec(synthetic))


def test_dist_to_import_mapping():
    assert import_name("msoffcrypto-tool") == "msoffcrypto"
    assert import_name("pyyaml") == "yaml"
    assert import_name("pillow") == "PIL"
    assert import_name("openpyxl") == "openpyxl"
