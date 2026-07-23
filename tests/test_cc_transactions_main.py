"""
tests/test_cc_transactions_main.py — Tests for
skill_cc_transactions/scripts/create_cc_transaction_list.py's top-level
error handling.

Focus: a WrongPdftextFlavourError/FileNotFoundError escaping from
extract_all_transactions() (via extract_text_from_pdf()'s unconditional
resolve_pdftotext() call) must be caught at main() and printed as a clean
'FATAL: <message>' line to stderr with a distinct non-zero exit code --
never a raw Python traceback. Mirrors the equivalent test in test_26as.py.

All synthetic -- no real PDFs, no real PII.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_cc_transactions" / "scripts" / "create_cc_transaction_list.py"


def _load_module():
    if str(SRC) not in sys.path:
        sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("create_cc_transaction_list", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


m = _load_module()


def test_script_exists():
    assert SCRIPT.exists(), f"Script not found: {SCRIPT}"


def test_wrong_pdftotext_flavour_prints_clean_fatal_and_exit_4(monkeypatch, capsys):
    message = (
        "Wrong pdftotext found at '/fake/vendor/poppler/bin/pdftotext': "
        "this is Xpdf (Glyph & Cog), not Poppler."
    )

    def fake_extract_all_transactions(pdf_dir):
        raise m.WrongPdftextFlavourError(message)

    monkeypatch.setattr(m, "extract_all_transactions", fake_extract_all_transactions)
    rc = m.main(["create_cc_transaction_list.py", "some_pdf_dir"])
    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err.startswith("FATAL:")
    assert message in captured.err
    assert "Traceback" not in captured.err
    assert "Traceback" not in captured.out


def test_file_not_found_pdftotext_prints_clean_fatal_and_exit_4(monkeypatch, capsys):
    """The bare vendored-binary-missing case (FileNotFoundError, not a
    flavour mismatch) must be caught the same way."""
    message = "pdftotext not found: no vendored copy at /fake/vendor/poppler/bin and none on PATH."

    def fake_extract_all_transactions(pdf_dir):
        raise FileNotFoundError(message)

    monkeypatch.setattr(m, "extract_all_transactions", fake_extract_all_transactions)
    rc = m.main(["create_cc_transaction_list.py", "some_pdf_dir"])
    captured = capsys.readouterr()
    assert rc == 4
    assert captured.err.startswith("FATAL:")
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_usage_message_when_no_pdf_dir_given(capsys):
    rc = m.main(["create_cc_transaction_list.py"])
    assert rc == 1
    captured = capsys.readouterr()
    assert "Usage" in captured.out
