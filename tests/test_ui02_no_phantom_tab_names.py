"""
UI-02 -- user-facing text must name only tabs that really render.

The UI renders a skill under its `display_name` (ui/webui.py), never under its
registry `name`. A help string that tells the user to run "26AS Journal" or
"Partner Comp Recon" points at a tab that does not exist. This checks every
skill.yaml (help text, description) plus the strings the journal builder shows,
against the set of rendered labels.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

# Labels that were once used in prose but are not rendered anywhere.
KNOWN_PHANTOMS = ["Partner Comp Recon", "26AS Journal", "ITR Mapping tab"]


def _manifests():
    for p in sorted((SRC / "agents").glob("skill_*/skill.yaml")):
        yield p, p.read_text(encoding="utf-8")


def _rendered_labels():
    labels = set()
    for _p, text in _manifests():
        d = yaml.safe_load(text) or {}
        if d.get("display_name"):
            labels.add(d["display_name"])
    return labels


def _phantoms():
    """Registry names that differ from the rendered label of every skill,
    mapped to the manifest that owns them (a skill may use its own name)."""
    labels = _rendered_labels()
    out = {ph: None for ph in KNOWN_PHANTOMS}
    for p, text in _manifests():
        d = yaml.safe_load(text) or {}
        n, dn = d.get("name"), d.get("display_name")
        if n and dn and n != dn and n not in labels and " " in n:
            out[n] = p
    return out


def test_no_skill_yaml_names_a_tab_that_does_not_render():
    bad = []
    for p, text in _manifests():
        for ph, owner in _phantoms().items():
            if owner == p:
                continue    # a skill's own registry name in its own file
            for i, line in enumerate(text.splitlines(), 1):
                if line.lstrip().startswith("name:"):
                    continue    # the manifest's own registry name is not prose
                if ph in line:
                    bad.append(f"{p.parent.name}/skill.yaml:{i}: {ph!r}")
    assert not bad, "help text names non-rendered tabs:\n" + "\n".join(bad)


def test_journal_builder_messages_name_only_rendered_tabs():
    text = (SRC / "agents" / "skill_26as_journal" / "scripts"
            / "build_tds_journals.py").read_text(encoding="utf-8")
    assert "Partner Comp Recon" not in text


def test_review_mapping_tab_heading_matches_its_label():
    text = (ROOT / "ui" / "tabs" / "itr_mapping_review.py").read_text(encoding="utf-8")
    assert '"## ITR Mapping' not in text
    assert "## Review Mapping" in text


def test_the_detector_is_not_vacuous():
    """NEGATIVE control: the phantom set really contains the old names, and a
    line naming one is caught."""
    ph = _phantoms()
    assert "26AS Journal" in ph and "Partner Comp Recon" in ph
    assert any(p in "run \"26AS Journal\" next" for p in ph)


def test_the_real_paths_are_written():
    yj = (SRC / "agents" / "skill_26as_journal" / "skill.yaml").read_text(encoding="utf-8")
    assert "Partner Compensation Reconciliation" in yj
    y26 = (SRC / "agents" / "skill_26as" / "skill.yaml").read_text(encoding="utf-8")
    assert "GnuCash > 26AS > Convert to GnuCash" in y26
    labels = _rendered_labels()
    assert "Convert to GnuCash" in labels
    assert "Partner Compensation Reconciliation" in labels
