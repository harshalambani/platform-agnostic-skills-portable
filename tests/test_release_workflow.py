"""The release workflow's release step: assets, name, pre-release flag, body.

The workflow is never run here; these tests read it as data.
"""
from pathlib import Path

import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "release.yml"


def _release_step():
    doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps = doc["jobs"]["build"]["steps"] if "build" in doc["jobs"] else next(
        iter(doc["jobs"].values()))["steps"]
    hits = [s for s in steps if "action-gh-release" in str(s.get("uses", ""))]
    assert len(hits) == 1, "expected exactly one release-creating step"
    return hits[0]["with"]


def test_release_is_created_with_both_assets():
    files = _release_step()["files"]
    assert "dist/PASkillsPortable_*.paf.exe" in files
    assert "dist/PASkillsPortable_*.zip" in files


def test_release_name_carries_the_version():
    name = _release_step()["name"]
    assert name == "PA Skills Portable ${{ env.VERSION }}"


def test_tag_with_a_dash_is_a_prerelease():
    pre = _release_step()["prerelease"]
    assert "contains(github.ref_name, '-')" in pre
    assert _release_step()["draft"] is False


def test_body_names_the_commit_and_the_generated_list_stays():
    w = _release_step()
    assert w["body"].strip() == "Built from commit ${{ github.sha }}."
    assert w["generate_release_notes"] is True


def test_body_has_no_install_steps_or_smartscreen_text():
    body = _release_step()["body"]
    for stale in ("SmartScreen", "Smart App Control", "Recommended", "Unblock-File",
                  "download", "extract"):
        assert stale not in body, stale
