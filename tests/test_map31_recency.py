"""
MAP-31 -- recent transactions outweigh old ones (saved rules AND history).

Synthetic fixtures only. Every behaviour carries NEGATIVE tests: the old,
count-only behaviour must NOT come back.
"""
from __future__ import annotations

import csv
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT / "src", ROOT / "tests", ROOT / "tests" / "skill_gnucash_xml_extractor"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import recency  # noqa: E402
from agents.skill_gnucash_account_mapper import agent as m  # noqa: E402
from agents.skill_gnucash_mapping_generator import agent as gen  # noqa: E402

ACCT_A = "Expenses:Old Supplier"
ACCT_B = "Expenses:New Supplier"
STMT = date(2026, 1, 15)


def _dates(first: date, n: int, step_days: int = 30):
    return [(first + timedelta(days=i * step_days)).isoformat() for i in range(n)]


def _hist(desc, acct, dates):
    return {"description": desc, "account": acct, "frequency": len(dates),
            "last_date": max(dates), "dates": list(dates)}


def _undated(desc, acct, freq):
    return {"description": desc, "account": acct, "frequency": freq}


def _rules_run(tmp_path, rules, stmt_date, desc="ZZCAFE BILL"):
    ypath = tmp_path / "rules.yaml"
    ypath.write_text(yaml.safe_dump(rules), encoding="utf-8")
    cpath = tmp_path / "in.csv"
    with open(cpath, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "Description", "Deposit", "Withdrawal"])
        w.writerow([stmt_date, desc, "", "100"])
    out = tmp_path / "mapped.csv"
    m.map_accounts(str(cpath), str(ypath), str(out), str(tmp_path / "rep.txt"))
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))[0]


def _rule(conf="high", last="2025-12-01", freq=5, account="Expenses:Dining", score=None):
    r = {"patterns": ["zzcafe"], "account": account, "confidence": conf,
         "frequency": freq, "last_date": last, "reason": f"{freq} occurrences, last {last}"}
    if score is not None:
        r["score"] = score
    return r


# ------------------------------------------------------------ the decay itself

def test_decay_is_exponential_and_clamped():
    assert recency.decay_weight(0) == 1.0
    assert abs(recency.decay_weight(recency.HALF_LIFE_DAYS) - 0.5) < 1e-9
    assert abs(recency.decay_weight(2 * recency.HALF_LIFE_DAYS) - 0.25) < 1e-9
    assert recency.decay_weight(-400) == 1.0            # NEGATIVE: the future never weighs MORE than 1
    assert recency.weight_at("2030-01-01", STMT) == 1.0
    assert recency.weight_at("not a date", STMT) == 1.0  # no date -> no decay (legacy)


# ------------------------------------------------------------ saved rules (d)

def test_rule_with_50_hits_last_seen_10_years_ago_is_never_high_or_medium(tmp_path):
    for stored in ("high", "medium"):
        row = _rules_run(tmp_path, {"HSBC": [_rule(conf=stored, last="2013-05-01", freq=50)]}, "2025-06-01")
        assert row["Confidence"] == "low", (stored, row)           # NEGATIVE
        assert row["Confidence"] not in ("high", "medium")
        assert "old rule, last seen 2013" in row["MatchReason"]


def test_rule_band_by_age_high_within_2y_medium_within_5y_low_beyond():
    ref = date(2026, 1, 15)

    def lvl(last):
        return recency.rule_confidence(50, last, ref)[0]

    assert lvl("2025-06-01") == "high"
    assert lvl("2023-06-01") == "medium"      # ~2.6y: a big count cannot buy 'high'
    assert lvl("2019-06-01") == "low"         # ~6.6y
    assert lvl("2012-01-01") == "low"
    assert recency.rule_confidence(50, "garbage", ref) == (None, None, None)


def test_recent_frequent_rule_keeps_high(tmp_path):
    row = _rules_run(tmp_path, {"HSBC": [_rule(conf="high", last="2025-05-01", freq=12)]}, "2025-06-01")
    assert row["Confidence"] == "high"
    assert "old rule" not in row["MatchReason"]


def test_recomputing_never_promotes_a_stored_label(tmp_path):
    row = _rules_run(tmp_path, {"HSBC": [_rule(conf="low", last="2025-05-01", freq=50)]}, "2025-06-01")
    assert row["Confidence"] == "low"                                  # NEGATIVE: not raised to high


def test_rule_age_is_measured_from_the_statement_not_today(tmp_path):
    rules = {"HSBC": [_rule(conf="high", last="2014-11-01", freq=6)]}
    old_stmt = _rules_run(tmp_path, rules, "2015-03-01")
    new_stmt = _rules_run(tmp_path, rules, "2025-03-01")
    assert old_stmt["Confidence"] == "high"                            # 2014 rule, 2015 statement
    assert new_stmt["Confidence"] == "low"                             # same rule, 10 years on


def test_rules_rank_by_recomputed_confidence_then_decayed_score(tmp_path):
    rules = {"HSBC": [
        _rule(conf="high", last="2012-01-01", freq=500, account="Expenses:Ancient"),   # big but dead
        _rule(conf="high", last="2025-05-01", freq=2, account="Expenses:Fresh"),
    ]}
    row = _rules_run(tmp_path, rules, "2025-06-01")
    assert row["Account"] == "Expenses:Fresh"                          # NEGATIVE: raw count must not win


def test_rule_without_frequency_only_gets_the_age_cap(tmp_path):
    r = _rule(conf="high", last="2025-05-01")
    r.pop("frequency")
    assert _rules_run(tmp_path, {"HSBC": [r]}, "2025-06-01")["Confidence"] == "high"
    r2 = _rule(conf="high", last="2012-05-01")
    r2.pop("frequency")
    assert _rules_run(tmp_path, {"HSBC": [r2]}, "2025-06-01")["Confidence"] == "low"


def test_generator_uses_the_reference_date_not_now():
    ext = {"mappings": {"HSBC": [{
        "description": "UPI/ZZCAFE/zz@bank 1", "account": "Expenses:Dining",
        "frequency": 8, "last_date": "2014-11-01"}]}}
    at_2015 = gen.generate_rules(ext, min_freq=1, reference_date="2015-03-01")["HSBC"][0]
    at_2026 = gen.generate_rules(ext, min_freq=1, reference_date="2026-03-01")["HSBC"][0]
    assert at_2015["confidence"] == "high"
    assert at_2026["confidence"] == "low"                              # NEGATIVE: not frozen at 'high'
    assert "old rule, last seen 2014" in at_2026["reason"]
    assert at_2015["score"] > at_2026["score"]


def test_generator_label_never_exceeds_the_age_cap():
    ext = {"mappings": {"HSBC": [{
        "description": "UPI/ZZCAFE/zz@bank 1", "account": "Expenses:Dining",
        "frequency": 50, "last_date": "2012-04-04"}]}}
    rule = gen.generate_rules(ext, min_freq=1, reference_date="2025-06-01")["HSBC"][0]
    assert rule["confidence"] == "low"


# ------------------------------------------------------------ history (c)

DESC = "zzvendor consulting invoice"


def _match(history, ref, desc=DESC):
    model = m._build_history_token_model(history, ref)
    return m._history_token_match(desc, model), model


def test_recent_rebooking_beats_a_large_old_history():
    old = _hist(DESC, ACCT_A, _dates(date(2019, 1, 1), 30))            # 30 txns, 6+ years before
    new = _hist(DESC, ACCT_B, _dates(date(2025, 3, 1), 4))             # 4 txns, last year
    match, _ = _match([old, new], STMT)
    assert match is not None and match["account"] == ACCT_B
    assert match["account"] != ACCT_A                                  # NEGATIVE
    assert match["confidence"] == "history"
    assert "recent bookings outweigh older ones" in match["reason"]


def test_without_dates_the_same_history_still_picks_the_big_old_account():
    old = _undated(DESC, ACCT_A, 30)
    new = _undated(DESC, ACCT_B, 4)
    match, _ = _match([old, new], STMT)
    assert match is not None and match["account"] == ACCT_A            # legacy: counts only


def test_importing_an_old_statement_weighs_its_own_era_fully():
    hist = [_hist(DESC, ACCT_A, _dates(date(2014, 1, 1), 12))]         # 2014-2015 history
    at_2015, model15 = _match(hist, date(2015, 3, 1))
    at_2026, model26 = _match(hist, date(2026, 1, 15))
    assert at_2015 is not None and at_2015["confidence"] == "history"
    assert "only old history" not in at_2015["reason"]
    assert at_2026 is not None and at_2026["confidence"] == "low"      # NEGATIVE: the same history, 11y on
    tok = next(iter(model15))
    assert model15[tok][ACCT_A] > 10 * model26[tok][ACCT_A]            # age comes from the STATEMENT


def test_history_after_the_statement_date_is_clamped_to_full_weight():
    future = _hist(DESC, ACCT_A, _dates(date(2030, 1, 1), 3))
    model = m._build_history_token_model([future], STMT)
    tok = next(iter(model))
    assert model[tok][ACCT_A] == 3.0                                   # NEGATIVE: never more than 1 each


def test_only_old_history_still_matches_but_at_low():
    hist = [_hist(DESC, ACCT_A, _dates(date(2012, 1, 1), 3))]
    match, _ = _match(hist, STMT)
    assert match is not None                                           # NEGATIVE: not dropped to nothing
    assert match["account"] == ACCT_A
    assert match["confidence"] == "low"
    assert "only old history" in match["reason"] and "last 2012" in match["reason"]
    assert "-> low" in match["reason"]


def test_twelve_txns_all_before_2020_read_as_old_in_2026():
    hist = [_hist(DESC, ACCT_A, _dates(date(2018, 1, 1), 12))]
    match, _ = _match(hist, STMT)
    assert match["confidence"] == "low"
    assert "12 txns" in match["reason"]


def test_two_recent_bookings_are_not_called_old():
    hist = [_hist(DESC, ACCT_A, ["2025-08-01", "2025-10-01"])]
    match, _ = _match(hist, STMT)
    assert match is not None and match["confidence"] == "history"
    assert "only old history" not in match["reason"]


def test_support_floor_stays_on_the_raw_count():
    one_recent = [_hist(DESC, ACCT_A, ["2025-12-01"])]
    match, _ = _match(one_recent, STMT)
    assert match is None                                               # 1 txn is under HISTORY_MIN_SUPPORT_TXNS
    many_old = [_hist(DESC, ACCT_A, _dates(date(2010, 1, 1), 40))]
    match, model = _match(many_old, STMT)
    tok = next(iter(model))
    assert model.raw[tok][ACCT_A] == 40                                # raw intact
    assert model[tok][ACCT_A] < 1                                      # weighted almost nothing
    assert match is not None and match["confidence"] == "low"          # raw support still clears the floor


def test_legacy_undated_history_behaves_exactly_as_before():
    legacy = [_undated(DESC, ACCT_A, 5), _undated("other thing here", ACCT_B, 3)]
    with_ref = m._build_history_token_model(legacy, STMT)
    no_ref = m._build_history_token_model(legacy)
    assert dict(with_ref) == dict(no_ref)
    for tok, accts in with_ref.items():
        for acct, w in accts.items():
            assert w == with_ref.raw[tok][acct]                        # weighted == raw
    assert not with_ref.newest
    match = m._history_token_match(DESC, with_ref)
    assert match["confidence"] == "history" and match["account"] == ACCT_A
    assert "only old history" not in match["reason"] and "recent bookings" not in match["reason"]


def test_a_hand_built_plain_dict_model_still_works():
    plain = {"zzvendor": {ACCT_A: 5}, "consulting": {ACCT_A: 5}, "invoice": {ACCT_A: 5}}
    match = m._history_token_match(DESC, plain)
    assert match is not None and match["account"] == ACCT_A and match["confidence"] == "history"


def test_dates_without_a_reference_date_are_ignored():
    hist = [_hist(DESC, ACCT_A, _dates(date(2010, 1, 1), 10))]
    model = m._build_history_token_model(hist)                         # no statement date given
    tok = next(iter(model))
    assert model[tok][ACCT_A] == 10                                    # NEGATIVE: not decayed against today


def test_exact_ties_are_not_broken_by_a_one_day_age_difference():
    a = _hist(DESC, ACCT_A, _dates(date(2025, 1, 5), 5))
    b = _hist(DESC, ACCT_B, _dates(date(2025, 1, 6), 5))
    match, _ = _match([a, b], STMT)
    assert match is None                                               # near-equal evidence stays unmatched


# ------------------------------------------------------------ extractor (b)

def test_extractor_adds_dates_and_keeps_frequency_and_last_date():
    import test_bank_split_classification as t
    from agents.skill_gnucash_xml_extractor.agent import parse_gnucash_file
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        accounts = [t.ROOT_ACC, t.ASSETS_ACC, t.ICICI_CUR, t.FD_ACCT]
        txns = [
            t._txn_xml("SWEEP TO 111111111111 SAMPLE PARTY", d,
                       [t._split_xml("icici_cur", "-100000/100"), t._split_xml("fd_acct", "100000/100")])
            for d in ("2024-01-05", "2024-03-05", "2023-11-05")
        ]
        result = parse_gnucash_file(str(t._write_book(tmp, accounts, txns)))
    entry = result["mappings"]["ICICI"][0]
    assert entry["frequency"] == 3 and entry["last_date"] == "2024-03-05"
    assert entry["dates"] == ["2023-11-05", "2024-01-05", "2024-03-05"]
    assert len(entry["dates"]) == entry["frequency"]
    assert set(entry) == {"description", "account", "frequency", "last_date", "dates"}   # additive only
