"""UI-34: the Banks pipeline tab is "Bank - Convert to GnuCash", first under Banks."""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for p in (str(SRC), str(ROOT)):
    if p not in sys.path:
        sys.path.insert(0, p)

NEW = "Bank - Convert to GnuCash"


def _dn(skill_dir):
    return yaml.safe_load((SRC / "agents" / skill_dir / "skill.yaml").read_text(encoding="utf-8"))["display_name"]


def test_banks_pipeline_is_renamed_and_other_tabs_are_not():
    assert _dn("skill_gnucash_pipeline") == NEW
    assert _dn("skill_26as_journal") == "Convert to GnuCash"
    assert _dn("skill_krc_gnucash") == "Convert to GnuCash"


def test_krchoksey_order_is_unchanged():
    text = (ROOT / "ui" / "webui.py").read_text(encoding="utf-8")
    assert re.search(r'_krc_order = \{"Convert": 0,\s*"Reconcile": 1,\s*"Convert to GnuCash": 2\}', text)


def test_bank_convert_renders_first_even_when_discovered_late():
    from ui import webui
    before, after = webui._split_banks_tabs(["Some Future Skill", "Coverage Gap Detector", NEW])
    assert before[0] == NEW
    assert after == ["Coverage Gap Detector"]
    before, after = webui._split_banks_tabs([NEW, "Coverage Gap Detector"])
    assert before == [NEW] and after == ["Coverage Gap Detector"]


def test_saved_inputs_are_keyed_on_the_skill_name_not_the_tab_label():
    """Remembered folders and fallbacks are keyed '<skill.name>.<input>', so the
    rename loses no saved value; no code keys anything on the old label."""
    gen = (ROOT / "ui" / "tabs" / "_generic.py").read_text(encoding="utf-8")
    assert 'f"{skill.name}.{_inp.name}"' in gen
    assert 'f"{skill.name}.{_finp.name}"' in gen or 'f"{skill.name}.{_finp.name}' in gen
    for f in list((ROOT / "ui").rglob("*.py")) + list(SRC.rglob("*.py")):
        t = f.read_text(encoding="utf-8", errors="ignore")
        assert '"Convert to GnuCash.' not in t, f


def test_no_pointer_still_says_banks_convert_to_gnucash():
    stale = [r'Banks\s*>\s*Convert to GnuCash', r'Banks "Convert to GnuCash"',
             r'Banks\s*(?:→|->)\s*Convert to GnuCash']
    bad = []
    files = (list(SRC.rglob("*.py")) + list(SRC.rglob("skill.yaml")) + list(SRC.rglob("*.md"))
             + list((ROOT / "ui").rglob("*.py")) + [ROOT / "README.md"])
    for f in files:
        if f.name.startswith("test_"):
            continue
        t = f.read_text(encoding="utf-8", errors="ignore")
        for rx in stale:
            if re.search(rx, t):
                bad.append(f"{f.relative_to(ROOT)}: {rx}")
    assert not bad, "\n".join(bad)
