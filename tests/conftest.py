"""
Shared pytest fixtures.

SEC-19: the review tabs refuse a file outside the known folders. Most review
tests write their synthetic CSVs under pytest's tmp_path, so by default the
pytest base temp directory counts as a known folder. tests/test_sec19_path_guard.py
opts out (it tests the guard itself).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
for _p in (str(_ROOT), str(_ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)


@pytest.fixture(autouse=True)
def _review_tabs_known_folders(request, tmp_path_factory, monkeypatch):
    if Path(str(request.node.fspath)).name == "test_sec19_path_guard.py":
        return
    try:
        from ui import _safe_paths
    except Exception:  # noqa: BLE001 -- UI package not importable in this test
        return
    base = tmp_path_factory.getbasetemp().resolve()
    real = _safe_paths.known_folders
    monkeypatch.setattr(_safe_paths, "known_folders", lambda: list(real()) + [base])
