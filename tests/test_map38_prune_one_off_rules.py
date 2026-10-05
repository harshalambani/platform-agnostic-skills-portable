"""
MAP-38 -- stored auto-rules learned under the old min_freq=1 are dropped once
their recorded frequency is below the bank threshold (BANK_RULE_MIN_FREQ).

Synthetic fixtures only. Every behaviour carries NEGATIVE tests.
"""
from __future__ import annotations

import copy
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "src" / "agents", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
from agents.skill_gnucash_account_mapper import persistent_rules as pr  # noqa: E402
import test_map37_bank_rule_min_freq as t37  # noqa: E402

MIN = m.BANK_RULE_MIN_FREQ
ONCE = "Expenses:Once Supplier"
TWICE = "Expenses:Twice Supplier"
USERACCT = "Expenses:User Pick"
OMIT = "__omit__"


def _auto(pat, acct, freq=OMIT):
    r = {"patterns": [pat], "account": acct, "confidence": "high",
         "source": "auto", "last_date": "2026-01-10"}
    if freq != OMIT:
        r["frequency"] = freq
    return r


def _pats(rules):
    return [p for r in rules for p in r["patterns"]]


def _fixture():
    return {
        "_overrides": [{"patterns": ["zzovr"], "account": USERACCT, "source": "user"}],
        "_global": [_auto("ZZGLOB/.*", ONCE, 1)],
        "HDFC": [
            _auto("ZZONE/.*", ONCE, 1),
            _auto("ZZTWO/.*", TWICE, 2),
            _auto("ZZTHREE/.*", TWICE, 9),
            _auto("ZZNOFREQ/.*", ONCE),
            _auto("ZZBADFREQ/.*", ONCE, "many"),
            {**_auto("ZZUSER/.*", USERACCT, 1), "source": "user"},
        ],
        "ICICI": [_auto("ZZONEB/.*", ONCE, 1), _auto("ZZKEEPB/.*", TWICE, 2)],
    }


# ------------------------------------------------------------ the helper

def test_prune_drops_only_below_threshold_auto_rules():
    rules = _fixture()
    report = pr.prune_one_off_rules(rules, MIN)
    h = _pats(rules["HDFC"])
    assert "ZZONE/.*" not in h and "ZZONEB/.*" not in _pats(rules["ICICI"])
    assert {"ZZTWO/.*", "ZZTHREE/.*"} <= set(h)                 # >= threshold stays
    assert report["HDFC"] == (1, 5) and report["ICICI"] == (1, 1)


def test_prune_never_touches_overrides_global_user_or_unknown_frequency():
    rules = _fixture()
    before = copy.deepcopy(rules)
    pr.prune_one_off_rules(rules, MIN)
    assert rules["_overrides"] == before["_overrides"]          # identical content
    assert rules["_global"] == before["_global"]                # freq 1 survives
    h = _pats(rules["HDFC"])
    assert "ZZUSER/.*" in h                                     # source user, freq 1
    assert "ZZNOFREQ/.*" in h                                   # missing frequency
    assert "ZZBADFREQ/.*" in h                                  # non-integer frequency


def test_prune_is_idempotent_and_reports_nothing_second_time():
    rules = _fixture()
    pr.prune_one_off_rules(rules, MIN)
    snap = copy.deepcopy(rules)
    assert pr.prune_one_off_rules(rules, MIN) == {}
    assert rules == snap


def test_prune_uses_the_passed_threshold_not_a_hardcoded_one():
    rules = _fixture()
    pr.prune_one_off_rules(rules, 1)                            # nothing is below 1
    assert "ZZONE/.*" in _pats(rules["HDFC"])


# ------------------------------------------------------------ merge + backup

def _setup(tmp_path, monkeypatch, content):
    rf = tmp_path / "book_rules.yaml"
    rf.write_text(yaml.safe_dump(content), encoding="utf-8")
    monkeypatch.setattr(pr, "rules_path", lambda gf, cp=None: rf)
    return rf, tmp_path / "book_rules.pre-map38.bak.yaml"


def test_merge_writes_one_backup_with_the_original_then_prunes(tmp_path, monkeypatch):
    original = _fixture()
    rf, bak = _setup(tmp_path, monkeypatch, original)
    merged = pr.merge_auto_rules("book.gnucash", {}, None, min_bank_freq=MIN)
    assert bak.exists()
    assert yaml.safe_load(bak.read_text(encoding="utf-8")) == original   # pre-prune copy
    assert "ZZONE/.*" not in _pats(merged["HDFC"])                       # same-run result
    assert "ZZONE/.*" not in _pats(yaml.safe_load(rf.read_text(encoding="utf-8"))["HDFC"])
    assert merged["_overrides"] == original["_overrides"]


def test_second_run_removes_nothing_and_never_overwrites_the_backup(tmp_path, monkeypatch):
    rf, bak = _setup(tmp_path, monkeypatch, _fixture())
    pr.merge_auto_rules("book.gnucash", {}, None, min_bank_freq=MIN)
    first = bak.read_bytes()
    after1 = yaml.safe_load(rf.read_text(encoding="utf-8"))
    pr.merge_auto_rules("book.gnucash", {}, None, min_bank_freq=MIN)
    assert bak.read_bytes() == first
    assert yaml.safe_load(rf.read_text(encoding="utf-8")) == after1
    # an existing backup is not replaced even if a later prune has work to do
    d = yaml.safe_load(rf.read_text(encoding="utf-8"))
    d["HDFC"].append(_auto("ZZLATE/.*", ONCE, 1))
    rf.write_text(yaml.safe_dump(d), encoding="utf-8")
    pr.merge_auto_rules("book.gnucash", {}, None, min_bank_freq=MIN)
    assert bak.read_bytes() == first
    assert "ZZLATE/.*" not in _pats(yaml.safe_load(rf.read_text(encoding="utf-8"))["HDFC"])


def test_no_backup_when_nothing_to_remove(tmp_path, monkeypatch):
    clean = {"_overrides": [], "HDFC": [_auto("ZZTWO/.*", TWICE, 2)]}
    _, bak = _setup(tmp_path, monkeypatch, clean)
    pr.merge_auto_rules("book.gnucash", {}, None, min_bank_freq=MIN)
    assert not bak.exists()


def test_without_min_bank_freq_nothing_is_pruned(tmp_path, monkeypatch):
    _, bak = _setup(tmp_path, monkeypatch, _fixture())
    merged = pr.merge_auto_rules("book.gnucash", {}, None)
    assert "ZZONE/.*" in _pats(merged["HDFC"])
    assert not bak.exists()


def test_log_line_has_counts_only(tmp_path, monkeypatch, caplog):
    _setup(tmp_path, monkeypatch, _fixture())
    with caplog.at_level("INFO"):
        pr.merge_auto_rules("book.gnucash", {}, None, min_bank_freq=MIN)
    text = " ".join(r.getMessage() for r in caplog.records if "MAP-38" in r.getMessage())
    assert "removed 1" in text
    assert "ZZONE" not in text and ONCE not in text and "HDFC" not in text


# ------------------------------------------------------------ end to end

def test_run_prunes_stored_one_off_rule_and_it_no_longer_matches(tmp_path, monkeypatch):
    existing = {"HDFC": [_auto("UPI/ZZONCE/.*", ONCE, 1)], "_overrides": []}
    _, mapped, saved = t37._run_pipeline(
        tmp_path, monkeypatch, [(t37.DESC_ONCE, ONCE, 1)], [t37.DESC_ONCE],
        existing_yaml=existing)
    assert "UPI/ZZONCE/.*" not in [p for r in saved.get("HDFC", []) for p in r["patterns"]]
    # NEGATIVE: same run, so the removed rule must not have matched
    assert mapped[0]["Confidence"] not in t37.RULE_LEVELS
    assert (tmp_path / "book_rules.pre-map38.bak.yaml").exists()


def test_run_keeps_a_stored_twice_seen_rule(tmp_path, monkeypatch):
    existing = {"HDFC": [_auto("UPI/ZZTWICE/.*", TWICE, 2)]}
    _, mapped, saved = t37._run_pipeline(
        tmp_path, monkeypatch, [(t37.DESC_TWICE, TWICE, 2)], [t37.DESC_TWICE],
        existing_yaml=existing)
    assert "UPI/ZZTWICE/.*" in [p for r in saved["HDFC"] for p in r["patterns"]]
    assert not (tmp_path / "book_rules.pre-map38.bak.yaml").exists()


def test_stored_one_off_now_seen_twice_is_updated_and_kept(tmp_path, monkeypatch):
    """Proves the merge runs BEFORE the prune: a stored frequency-1 rule whose
    description is now booked twice is refreshed to frequency 2 and kept."""
    stored = {"HDFC": [_auto("ZZGROW/.*", TWICE, 1), _auto("ZZSTILL1/.*", ONCE, 1)]}
    rf, _ = _setup(tmp_path, monkeypatch, stored)
    fresh = {"HDFC": [_auto("ZZGROW/.*", TWICE, 2)]}
    merged = pr.merge_auto_rules("book.gnucash", fresh, None, min_bank_freq=MIN)
    by = {r["patterns"][0]: r for r in merged["HDFC"]}
    assert by["ZZGROW/.*"]["frequency"] == 2          # updated and kept
    assert "ZZSTILL1/.*" not in by                    # NEGATIVE: untouched one-off still goes
    saved = yaml.safe_load(rf.read_text(encoding="utf-8"))
    assert "ZZGROW/.*" in _pats(saved["HDFC"])
