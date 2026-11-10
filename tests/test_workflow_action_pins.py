"""Every third-party action in every workflow is pinned to a full commit SHA."""
import re
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
_USES = re.compile(r"^\s*-?\s*uses:\s*(\S+)", re.MULTILINE)
_PINNED = re.compile(r"^[\w.-]+/[\w./-]+@[0-9a-f]{40}$")


def unpinned(text: str) -> list[str]:
    """The `uses:` targets that are not owner/repo@<40-hex>; local ./ actions
    are exempt."""
    return [u for u in _USES.findall(text)
            if not u.startswith("./") and not _PINNED.match(u)]


def test_every_workflow_action_is_pinned_to_a_full_sha():
    files = sorted(WORKFLOWS.glob("*.y*ml"))
    assert files, "no workflow files found"
    bad = {f.name: unpinned(f.read_text(encoding="utf-8")) for f in files}
    bad = {k: v for k, v in bad.items() if v}
    assert not bad, f"unpinned actions: {bad}"


def test_the_pin_check_rejects_floating_tags_and_short_shas():
    assert unpinned("      - uses: actions/checkout@v7\n") == ["actions/checkout@v7"]
    assert unpinned("        uses: actions/setup-python@main\n")
    assert unpinned("      - uses: actions/checkout@3d3c42e\n")
    assert unpinned("      - uses: actions/checkout@" + "z" * 40 + "\n")


def test_the_pin_check_accepts_a_full_sha_and_a_local_action():
    ok = "      - uses: actions/checkout@" + "a" * 40 + " # v7.0.1\n"
    assert unpinned(ok) == []
    assert unpinned("      - uses: ./.github/actions/thing\n") == []
