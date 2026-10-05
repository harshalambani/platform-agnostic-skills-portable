"""SEC-13/16/17 -- the guard that keeps real credentials and account data out.

The guard stores only hashes, so these tests build their own throwaway values.
"""
from __future__ import annotations

import hashlib
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("check_no_real_data", ROOT / "scripts" / "check_no_real_data.py")
g = importlib.util.module_from_spec(_spec)
sys.modules["check_no_real_data"] = g
_spec.loader.exec_module(g)


def test_the_repository_is_clean():
    assert g.scan_repo(ROOT) == []


def test_a_real_password_constant_with_a_literal_is_flagged():
    hits = g.scan_text('REAL_PDF_PASSWORD = "anything"\n')
    assert hits and hits[0][0] == 1


def test_a_real_password_constant_read_from_the_environment_is_not_flagged():
    assert g.scan_text('REAL_PDF_PASSWORD = os.environ.get("X", "")\n') == []      # NEGATIVE
    assert g.scan_text('REAL_PDF_PASSWORD = ""\n') == []                          # NEGATIVE


def test_an_ordinary_synthetic_password_is_not_flagged():
    assert g.scan_text('pdf_password="not-a-real-password"\n') == []              # NEGATIVE


def test_a_denylisted_token_is_flagged_and_its_leading_zero_form_too(monkeypatch):
    token = "Zq9Throwaway77"
    monkeypatch.setattr(g, "DENYLIST_SHA256", frozenset({hashlib.sha256(token.lower().encode()).hexdigest()}))
    assert g.scan_text(f"x = '{token}'\n") == [(1, "a value that was removed under SEC-13/16/17")]
    assert g.scan_text(f"x = '0000{token}'\n")                                      # zero-padded form
    assert g.scan_text("x = 'a different token entirely'\n") == []                 # NEGATIVE


def test_the_report_never_contains_the_value(monkeypatch, tmp_path):
    token = "Zq9Throwaway77"
    monkeypatch.setattr(g, "DENYLIST_SHA256", frozenset({hashlib.sha256(token.lower().encode()).hexdigest()}))
    f = tmp_path / "a.py"
    f.write_text(f"v = '{token}'\n", encoding="utf-8")
    problems = g.scan_repo(tmp_path, files=[f])
    assert problems and token not in "\n".join(problems)                           # NEGATIVE


def test_the_guard_is_wired_into_ci():
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "scripts/check_no_real_data.py" in ci


def test_sec20_word_is_flagged_whole_word_case_insensitive(monkeypatch):
    word = "Zqxv"
    monkeypatch.setattr(g, "EMPLOYER_NAME_SHA256", frozenset({hashlib.sha256(word.lower().encode()).hexdigest()}))
    assert g.scan_text("see ZQXV here\n")
    assert g.scan_text("see zqxv.\n")
    assert g.scan_text("see zqxvs here\n") == []          # NEGATIVE: inside a longer word
    assert g.scan_text("see azqxv here\n") == []          # NEGATIVE
    assert g.scan_text("nothing here\n") == []            # NEGATIVE


def test_sec20_guard_file_does_not_match_itself():
    assert g.scan_text((ROOT / "scripts" / "check_no_real_data.py").read_text(encoding="utf-8")) == []
