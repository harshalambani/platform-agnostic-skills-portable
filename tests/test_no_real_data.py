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


# ---------------------------------------------------------------------------
# SEC-22 -- listed-company examples; the public price lists are the one place
# these tokens may appear.
# ---------------------------------------------------------------------------

def _sec22(monkeypatch, token):
    monkeypatch.setattr(g, "LISTED_NAME_SHA256", frozenset({hashlib.sha256(token.lower().encode()).hexdigest()}))


def test_sec22_token_is_flagged_in_tests_and_src(monkeypatch, tmp_path):
    token = "Zqxvorchard"
    _sec22(monkeypatch, token)
    assert g.scan_text(f"name = '{token.upper()} LTD'\n")
    for sub in ("tests", "src/agents/skill_x"):
        d = tmp_path / sub
        d.mkdir(parents=True)
        f = d / "a.py"
        f.write_text(f"v = '{token}'\n", encoding="utf-8")
        problems = g.scan_repo(tmp_path, files=[f])
        assert problems and token not in "\n".join(problems)          # the value is never echoed


def test_sec22_token_is_whole_word_only(monkeypatch):
    _sec22(monkeypatch, "Zqxvorchard")
    assert g.scan_text("zqxvorchards here\n") == []                    # NEGATIVE: inside a longer word
    assert g.scan_text("azqxvorchard here\n") == []                    # NEGATIVE
    assert g.scan_text("nothing here\n") == []                         # NEGATIVE


def test_sec22_same_token_passes_in_the_public_data_directory(monkeypatch, tmp_path):
    token = "Zqxvorchard"
    _sec22(monkeypatch, token)
    d = tmp_path / "src" / "agents" / "skill_itr_workbook" / "data"
    d.mkdir(parents=True)
    f = d / "prices.csv"
    f.write_text(f"{token},100\n", encoding="utf-8")
    assert g.scan_repo(tmp_path, files=[f]) == []                      # NEGATIVE: exempt there
    sibling = tmp_path / "src" / "agents" / "skill_itr_workbook" / "other.py"
    sibling.write_text(f"v = '{token}'\n", encoding="utf-8")
    assert g.scan_repo(tmp_path, files=[sibling])                      # exemption is the data dir only


def test_sec22_older_lists_still_apply_in_the_data_directory(monkeypatch, tmp_path):
    token = "Zq9Throwaway77"
    monkeypatch.setattr(g, "DENYLIST_SHA256", frozenset({hashlib.sha256(token.lower().encode()).hexdigest()}))
    d = tmp_path / "src" / "agents" / "skill_itr_workbook" / "data"
    d.mkdir(parents=True)
    f = d / "prices.csv"
    f.write_text(f"{token},100\n", encoding="utf-8")
    assert g.scan_repo(tmp_path, files=[f])                            # old list is NOT exempted
    word = "Zqxv"
    monkeypatch.setattr(g, "EMPLOYER_NAME_SHA256", frozenset({hashlib.sha256(word.lower().encode()).hexdigest()}))
    f.write_text(f"{word},100\n", encoding="utf-8")
    assert g.scan_repo(tmp_path, files=[f])


def test_sec22_real_hash_list_is_populated_and_nonempty():
    assert len(g.LISTED_NAME_SHA256) >= 5
    assert all(len(h) == 64 for h in g.LISTED_NAME_SHA256)


# ---------------------------------------------------------------------------
# SEC-22 -- the deductor alias table must not carry a hashed listed-company token.
# ---------------------------------------------------------------------------

def _load_journal_module():
    src = ROOT / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))
    path = src / "agents" / "skill_26as_journal" / "scripts" / "build_tds_journals.py"
    spec = importlib.util.spec_from_file_location("build_tds_journals_nrd", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def _hashed_alias_tokens(aliases, hashes):
    """[(key, token)] for every alias key or value whose whole-token hash is in `hashes`."""
    found = []
    for key, values in aliases.items():
        for tok in [key, *values]:
            if hashlib.sha256(tok.lower().encode("utf-8")).hexdigest() in hashes:
                found.append((key, tok))
    return found


def test_sec22_no_alias_key_or_value_is_a_hashed_listed_company_token():
    assert _hashed_alias_tokens(_load_journal_module().ALIASES, g.LISTED_NAME_SHA256) == []


def test_sec22_alias_check_fails_on_a_hashed_token_and_not_otherwise():
    throwaway = "Zqxvorchard"
    local = frozenset({hashlib.sha256(throwaway.lower().encode()).hexdigest()})
    assert _hashed_alias_tokens({"ZQK": {"OTHER", throwaway.upper()}}, local) == [("ZQK", throwaway.upper())]
    assert _hashed_alias_tokens({throwaway.upper(): {"OTHER"}}, local)          # a key is checked too
    assert _hashed_alias_tokens({"ZQK": {"OTHER"}}, local) == []                # NEGATIVE
    assert _hashed_alias_tokens({"ZQK": {throwaway.upper()}}, frozenset()) == []  # NEGATIVE: empty list


# ---------------------------------------------------------------------------
# SEC-27 -- one more listed-company hash.
# ---------------------------------------------------------------------------

def test_sec27_hash_is_in_the_listed_name_list():
    assert "5cafe3c306a6a95bb03cc1afba6d768c20cd1cf78ee8d5ad578b51bfaa42895f" in g.LISTED_NAME_SHA256


def test_sec27_exact_word_is_flagged_but_a_shared_prefix_is_not(monkeypatch):
    _sec22(monkeypatch, "Zqxvmeadow")
    assert g.scan_text("see Zqxvmeadow here\n")                        # the exact word
    assert g.scan_text("see ZQXVMEADOW LTD\n")                         # case-insensitive
    assert g.scan_text("see Zqxvmead here\n") == []                    # NEGATIVE: shares only a prefix
    assert g.scan_text("see Zqxvmeadows here\n") == []                 # NEGATIVE: longer word
    assert g.scan_text("see Zqxvmeadlark here\n") == []                # NEGATIVE: same prefix, other word
