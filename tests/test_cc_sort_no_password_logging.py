"""
tests/test_cc_sort_no_password_logging.py — Regression guard for the CC-sort
password-logging fix (CodeQL py/clear-text-logging-sensitive-data).

extract_sort_cc_pdfs.py and agent.py used to print the live statement
password to stdout in four places: the full supplied password list before
decryption, the working password per decrypted file, the password-bearing
argv line in the agent wrapper, and the single-password filename fallback
(where the .txt stem IS the password). This test:

  1. Exercises _pw_label() -- the helper that now reports WHICH password
     matched by position, never by value -- and asserts the password value
     never appears in its output.
  2. Source-greps both files for the three literal expressions that used to
     leak the password, so a copy-paste re-introduction (how this class of
     bug tends to come back) is caught even if _pw_label() itself is never
     touched again.

No qpdf, no real pipeline run -- this only imports the module and inspects
source text.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_cc_sort" / "scripts" / "extract_sort_cc_pdfs.py"
AGENT = SRC / "agents" / "skill_cc_sort" / "agent.py"


def _load_module():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("extract_sort_cc_pdfs", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_module()


# ---------------------------------------------------------------------------
# _pw_label()
# ---------------------------------------------------------------------------

def test_pw_label_reports_position_not_value():
    passwords = ["OTHER", "TESTPW1234"]
    label = m._pw_label("TESTPW1234", passwords)
    assert label == "password #2"
    assert "TESTPW1234" not in label


def test_pw_label_empty_password_variants():
    passwords = ["TESTPW1234"]
    assert m._pw_label("", passwords) == "no password needed"
    assert m._pw_label("(empty)", passwords) == "no password needed"


def test_pw_label_not_in_list():
    label = m._pw_label("NOTINLIST", ["A"])
    assert "NOTINLIST" not in label


# ---------------------------------------------------------------------------
# Source-level guard: catches copy-paste re-introduction of the leaks, even
# if a future edit bypasses _pw_label() entirely.
# ---------------------------------------------------------------------------

def test_source_no_longer_contains_password_leaking_expressions():
    script_src = SCRIPT.read_text(encoding="utf-8")
    agent_src = AGENT.read_text(encoding="utf-8")

    # These three literal expressions are exactly what leaked the password
    # before the fix. They are simple enough that a future contributor could
    # copy-paste them back in (e.g. "for debugging") without realizing the
    # security implication -- this guard exists specifically for that case.
    assert "', '.join(passwords)" not in script_src
    assert "(pw: {working_pw})" not in script_src
    assert "' '.join(args)" not in agent_src
