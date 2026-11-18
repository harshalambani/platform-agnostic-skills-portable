"""PIPE-19: one rule for every test that needs a real qpdf.

Locally, with no qpdf that runs, such a test is skipped (as before). On CI the
workflow sets PASK_REQUIRE_QPDF=1, and then a missing qpdf FAILS the test, so a
runner without qpdf can never turn the real-decryption tests into quiet skips.
"""
from __future__ import annotations

import os
import subprocess

import pytest

REQUIRE_ENV = "PASK_REQUIRE_QPDF"


def working_qpdf():
    """Path of a qpdf that runs, or None. The vendored copy can be an unfetched
    LFS pointer, so the path is proven by running --version."""
    try:
        from agents._native_resolve import resolve_qpdf
        path = resolve_qpdf()
        subprocess.run([path, "--version"], capture_output=True, timeout=30, check=True)
        return path
    except Exception:
        return None


def qpdf_or_skip_or_fail():
    """Return the qpdf path; else fail (PASK_REQUIRE_QPDF=1) or skip."""
    path = working_qpdf()
    if path is not None:
        return path
    if os.environ.get(REQUIRE_ENV) == "1":
        pytest.fail(f"qpdf is required here ({REQUIRE_ENV}=1) but no working qpdf was found. "
                    "Install it (CI: apt-get install -y qpdf).", pytrace=False)
    pytest.skip("qpdf is not available here")


# A test marked with this runs the gate (fixture require_qpdf, in conftest.py).
needs_qpdf = pytest.mark.usefixtures("require_qpdf")
