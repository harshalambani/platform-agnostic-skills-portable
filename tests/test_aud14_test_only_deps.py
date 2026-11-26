"""aiohttp is a TEST-ONLY dependency (the UI-09 / UI-11 browser-render tests).

It must stay out of everything the packaged app installs, and CI must still
install it so those tests run instead of skipping.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PACKAGED = ("requirements-lock.txt", "requirements.txt")


def _pinned_names(text: str) -> set[str]:
    names = set()
    for line in text.splitlines():
        m = re.match(r"^([A-Za-z0-9][A-Za-z0-9._-]*)\s*(==|>=|<=|~=|<|>|$)", line.strip())
        if m:
            names.add(re.sub(r"[-_.]+", "-", m.group(1)).lower())
    return names


def test_aiohttp_is_not_in_what_the_packaged_build_installs():
    for name in PACKAGED:
        assert "aiohttp" not in _pinned_names((ROOT / name).read_text(encoding="utf-8")), name
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert not re.search(r"""["']aiohttp\b""", pyproject)


def test_the_detector_sees_aiohttp_when_it_is_there():
    """Negative control: the check above is not green over nothing."""
    assert "aiohttp" in _pinned_names("aiohttp==3.14.5 \\n    --hash=sha256:00\n")
    assert "aiohttp" in _pinned_names("aiohttp>=3\n")
    assert "aiohttp" not in _pinned_names("aiohttp-extra==1\n")


def test_ci_installs_aiohttp_for_the_browser_tests_and_fails_on_a_skip():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(r"pip install pytest reportlab xlwt aiohttp", ci)
    assert "test_ui09_hover_and_header_bleed.py" in ci
    assert "test_ui11_resizable_columns.py" in ci
    assert "browser-render tests were skipped" in ci
    assert "test_real_drag_widens_only_that_column" in ci
