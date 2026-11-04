"""
tests/test_cc_sort_no_password_logging.py — Regression guard for the CC-sort
password-logging fix (CodeQL py/clear-text-logging-sensitive-data), plus the
follow-on fix that stopped passing the password via argv to qpdf.

extract_sort_cc_pdfs.py and agent.py used to print the live statement
password to stdout in four places: the full supplied password list before
decryption, the working password per decrypted file, the password-bearing
argv line in the agent wrapper, and the single-password filename fallback
(where the .txt stem IS the password). This test:

  1. Exercises _pw_label() -- the helper that now reports WHICH password
     matched by position, never by value -- and asserts the password value
     never appears in its output.
  2. Source-greps both files for the literal expressions that used to leak
     the password, so a copy-paste re-introduction (how this class of bug
     tends to come back) is caught even if the fixed helpers are never
     touched again.
  3. Exercises decrypt_pdf() with subprocess.run monkeypatched, asserting the
     password never appears in the constructed argv and is only ever passed
     via the stdin `input=` kwarg (the 2026-08-07 audit finding: argv is
     readable by any other local process via the Windows process table).

No qpdf, no real pipeline run -- this only imports the module, monkeypatches
subprocess.run, and inspects source text.
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

    # These literal expressions are exactly what leaked the password before
    # the fixes. They are simple enough that a future contributor could
    # copy-paste them back in (e.g. "for debugging") without realizing the
    # security implication -- this guard exists specifically for that case.
    assert "', '.join(passwords)" not in script_src
    assert "(pw: {working_pw})" not in script_src
    assert "' '.join(args)" not in agent_src
    # 2026-08-07 audit finding: the password used to be built into the qpdf
    # argv via '--password=' + password, which is readable by any other
    # local process through the Windows process table. Guard against this
    # specific copy-paste re-introduction even if decrypt_pdf() itself is
    # never touched again.
    assert "'--password=' +" not in script_src


# ---------------------------------------------------------------------------
# decrypt_pdf() -- password must travel via stdin, never via argv
# ---------------------------------------------------------------------------

def test_decrypt_pdf_never_puts_password_in_argv(monkeypatch, tmp_path):
    """
    decrypt_pdf() must invoke qpdf with the password fed over stdin
    (--password-file=- plus input=<password>\\n), never as a '--password=...'
    argv element. argv is visible to any other local, unprivileged process
    via the Windows process table for as long as qpdf is running; stdin is
    not.
    """
    calls = []

    def fake_run(cmd, **kwargs):
        stdin_input = kwargs.get("input")
        calls.append((list(cmd), stdin_input))

        class FakeResult:
            # Simulate a real qpdf: the empty-password attempt tried first
            # by decrypt_pdf() must fail (this PDF is "encrypted"), only the
            # attempt carrying TESTPW1234 on stdin succeeds.
            returncode = 0 if stdin_input and "TESTPW1234" in stdin_input else 2
            stdout = ""
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m, "resolve_qpdf", lambda: "C:/fake/qpdf.exe")

    pdf_path = tmp_path / "in.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    output_path = tmp_path / "out.pdf"

    result = m.decrypt_pdf(pdf_path, ["TESTPW1234"], output_path)

    assert result == "TESTPW1234"
    assert calls, "subprocess.run was never invoked"

    # Every call whose cmd list carries a non-empty password argument must
    # not exist -- the password must appear only in the `input=` kwarg.
    saw_password_via_stdin = False
    for cmd, stdin_input in calls:
        for arg in cmd:
            assert "TESTPW1234" not in arg, f"password leaked into argv: {cmd}"
        if stdin_input and "TESTPW1234" in stdin_input:
            saw_password_via_stdin = True

    assert saw_password_via_stdin, "password never observed on stdin either"


def test_decrypt_pdf_empty_password_passes_no_password_option(monkeypatch, tmp_path):
    """The empty-password (unencrypted PDF) path must not send any stdin
    input or --password-file option -- qpdf's default is already 'no
    password'."""
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs.get("input")))

        class FakeResult:
            returncode = 0
            stdout = ""
            stderr = ""

        return FakeResult()

    monkeypatch.setattr(m.subprocess, "run", fake_run)
    monkeypatch.setattr(m, "resolve_qpdf", lambda: "C:/fake/qpdf.exe")

    pdf_path = tmp_path / "in.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    output_path = tmp_path / "out.pdf"

    result = m.decrypt_pdf(pdf_path, [], output_path)

    assert result == "(empty)"
    cmd, stdin_input = calls[0]
    assert stdin_input is None
    assert not any(arg.startswith("--password") for arg in cmd)
