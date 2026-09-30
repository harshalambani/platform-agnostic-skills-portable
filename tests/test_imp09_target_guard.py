"""
IMP-09 -- a hidden / placeholder account is NEVER a posting target.

One shared guard (agents.gnucash_accounts.TargetGuard) is the LAST step of
every target-emitting pass. Only Hidden and Placeholder block (Hidden is
inherited from any ancestor; a Placeholder parent does NOT block children).
Tax-related / opening-balance accounts are NOT blocked.

Every fixture is synthetic (see tests/gnc_book_fixture.py).
"""
from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (SRC, ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import gnucash_accounts as ga  # noqa: E402
from agents.skill_gnucash_account_mapper import agent as mapper  # noqa: E402
from agents.skill_gnucash_account_mapper import persistent_rules as pr  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _book(tmp_path, txns=(), extra=(), stem="synbook"):
    return fx.write_book(tmp_path / f"{stem}.gnucash",
                         fx.standard_accounts(extra), txns)


def _run(tmp_path, book, rows, bank_account=fx.P_HDFC1, config_path=None,
         monkeypatch=None):
    if monkeypatch is not None:
        monkeypatch.chdir(tmp_path)          # never let the rules resolver scan a real Data/
    csv_in = fx.canonical_csv(tmp_path / "in.csv", rows)
    out = tmp_path / "out.csv"
    mapper.run(book, csv_in, str(out), config_path=config_path,
               bank_name="HDFC", gnucash_bank_account=bank_account)
    with open(out, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _hist(desc, target_id, n, date="2025-06-10", bank=fx.HDFC1):
    """n identical withdrawals from `bank` to `target_id`."""
    return [fx.txn_xml(desc, date, [(bank, -10000), (target_id, 10000)])
            for _ in range(n)]


# ---------------------------------------------------------------------------
# the guard itself
# ---------------------------------------------------------------------------

@pytest.fixture()
def guard(tmp_path):
    return ga.TargetGuard.from_book(_book(tmp_path))


def test_hidden_account_is_blocked(guard):
    assert guard.blocked_target_reason("Root Account:Expenses:Dining")
    assert guard.blocked_target_reason(fx.P_HDFC_OLD) == "target is hidden in the book"


def test_placeholder_account_is_blocked(guard):
    assert "placeholder" in guard.blocked_target_reason("Expenses:Household")


def test_child_of_hidden_parent_is_blocked(guard):
    why = guard.blocked_target_reason("Expenses:Old:Dining")
    assert why and "parent 'Old' is hidden" in why


def test_child_of_placeholder_parent_is_still_offered(guard):
    """NEGATIVE: a placeholder parent must not block its children."""
    assert guard.blocked_target_reason("Expenses:Household:Fuel") is None


def test_postable_account_with_same_leaf_as_hidden_is_offered(guard):
    """NEGATIVE: the guard keys on the full path, never the leaf name."""
    assert guard.blocked_target_reason("Expenses:Food:Dining") is None
    assert guard.blocked_target_reason("Expenses:Dining") is not None  # the hidden twin


def test_tax_related_account_is_not_blocked(guard):
    """NEGATIVE: is_special covers tax-related, the hard block does not."""
    assert guard.blocked_target_reason("Expenses:GST Paid") is None


def test_opening_balance_equity_is_not_blocked_by_the_guard(guard):
    assert guard.blocked_target_reason("Equity:Opening Balances") is None


def test_unknown_path_is_not_invented_as_blocked(guard):
    assert guard.blocked_target_reason("Expenses:Not In The Book") is None


def test_is_special_keeps_its_meaning(tmp_path):
    accs = {a.path: a for a in ga.load_accounts(_book(tmp_path))}
    assert accs["Expenses:GST Paid"].is_special
    assert accs["Equity:Opening Balances"].is_special
    assert not accs["Expenses:Food:Dining"].is_special


def test_postable_accounts_excludes_child_of_hidden_parent(tmp_path):
    paths = ga.read_postable_paths(_book(tmp_path))
    assert "Expenses:Old:Dining" not in paths
    assert "Expenses:Household:Fuel" in paths
    assert "Expenses:Food:Dining" in paths


# -- dormant (advisory) ------------------------------------------------------

def test_looks_dormant_when_no_split_in_fy_or_prior_fy_and_zero_balance(tmp_path):
    book = _book(tmp_path, _hist("OLD SHOP", "groc", 1, date="2019-05-01"))
    # groc has a split in FY2019 -> balance 100.00 != 0 so NOT dormant; use a
    # reversal to make the balance zero.
    txns = _hist("OLD SHOP", "groc", 1, date="2019-05-01") + [
        fx.txn_xml("REVERSAL", "2019-06-01", [(fx.HDFC1, 10000), ("groc", -10000)])]
    g = ga.TargetGuard.from_book(_book(tmp_path, txns, stem="dorm"))
    assert g.dormant_reason("Expenses:Groceries", 2025)
    assert g.blocked_target_reason("Expenses:Groceries") is None


def test_account_active_in_current_or_prior_fy_is_not_dormant(tmp_path):
    txns = [fx.txn_xml("A", "2025-06-01", [(fx.HDFC1, 10000), ("groc", -10000)]),
            fx.txn_xml("B", "2025-06-02", [(fx.HDFC1, -10000), ("groc", 10000)])]
    g = ga.TargetGuard.from_book(_book(tmp_path, txns, stem="act"))
    assert g.dormant_reason("Expenses:Groceries", 2025) is None      # current FY
    assert g.dormant_reason("Expenses:Groceries", 2026) is None      # prior FY
    assert g.dormant_reason("Expenses:Groceries", 2027)              # two FYs on


def test_nonzero_balance_is_not_dormant(tmp_path):
    txns = _hist("OLD", "groc", 1, date="2019-05-01")
    g = ga.TargetGuard.from_book(_book(tmp_path, txns, stem="bal"))
    assert g.dormant_reason("Expenses:Groceries", 2025) is None


# ---------------------------------------------------------------------------
# mapper end-to-end
# ---------------------------------------------------------------------------

ROW = ("2025-08-01", "ZQVRT MONTHLY PAYMENT", "", "100.00")


def test_history_whose_only_target_is_hidden_lands_in_suspense(tmp_path, monkeypatch):
    book = _book(tmp_path, _hist("ZQVRT MONTHLY PAYMENT", "hid_dining", 3),
                 stem="hist_hidden")
    rows = _run(tmp_path, book, [ROW], monkeypatch=monkeypatch)
    assert rows[0]["Account"].endswith("Suspense")
    assert rows[0]["Confidence"] == "suspense"
    assert rows[0]["MatchReason"].startswith("blocked: target is hidden in the book")
    assert "Expenses:Dining" != rows[0]["Account"]


def test_same_leaf_postable_twin_is_still_offered_and_chosen(tmp_path, monkeypatch):
    book = _book(tmp_path, _hist("ZQVRT MONTHLY PAYMENT", "dining", 3),
                 stem="twin")
    rows = _run(tmp_path, book, [ROW], monkeypatch=monkeypatch)
    assert rows[0]["Account"] == "Expenses:Food:Dining"
    assert rows[0]["Confidence"] != "suspense"


def test_child_of_hidden_parent_history_target_goes_to_suspense(tmp_path, monkeypatch):
    book = _book(tmp_path, _hist("ZQVRT MONTHLY PAYMENT", "old_dining", 3),
                 stem="hidparent")
    rows = _run(tmp_path, book, [ROW], monkeypatch=monkeypatch)
    assert rows[0]["Confidence"] == "suspense"
    assert rows[0]["MatchReason"].startswith("blocked: ")
    assert "parent 'Old' is hidden" in rows[0]["MatchReason"]


def test_child_of_placeholder_parent_is_mapped(tmp_path, monkeypatch):
    book = _book(tmp_path, _hist("ZQVRT MONTHLY PAYMENT", "fuel", 3),
                 stem="phparent")
    rows = _run(tmp_path, book, [ROW], monkeypatch=monkeypatch)
    assert rows[0]["Account"] == "Expenses:Household:Fuel"
    assert rows[0]["Confidence"] != "suspense"


def test_tax_related_account_is_offered_and_chosen(tmp_path, monkeypatch):
    """NEGATIVE: the old is_special filter would have dropped it."""
    book = _book(tmp_path, _hist("ZQVRT MONTHLY PAYMENT", "tax", 3), stem="taxrel")
    rows = _run(tmp_path, book, [ROW], monkeypatch=monkeypatch)
    assert rows[0]["Account"] == "Expenses:GST Paid"
    assert rows[0]["Confidence"] != "suspense"


def test_opening_balance_equity_is_not_newly_offered_without_history(tmp_path, monkeypatch):
    book = _book(tmp_path, _hist("ZQVRT MONTHLY PAYMENT", "groc", 3), stem="obe")
    rows = _run(tmp_path, book, [("2025-08-01", "SOMETHING UNSEEN", "", "5.00")],
                monkeypatch=monkeypatch)
    assert rows[0]["Account"] != "Equity:Opening Balances"


def test_saved_rule_pointing_at_placeholder_is_skipped_but_kept(tmp_path, monkeypatch):
    book = _book(tmp_path, [], stem="savedrule")
    monkeypatch.chdir(tmp_path)
    pr.save_overrides_batch(book, [{"patterns": ["ZQVRT"], "account": "Expenses:Household"}])
    rp = pr.rules_path(book)
    before = yaml.safe_load(rp.read_text(encoding="utf-8"))
    rows = _run(tmp_path, book, [ROW], monkeypatch=monkeypatch)
    assert rows[0]["Account"].endswith("Suspense")
    assert rows[0]["MatchReason"].startswith("blocked: target is a placeholder")
    after = yaml.safe_load(rp.read_text(encoding="utf-8"))
    ov = after.get("_overrides") or []
    assert any(o.get("account") == "Expenses:Household" for o in ov), \
        "the saved rule must NOT be deleted from the rules file"
    assert before["_overrides"] == after["_overrides"]


def test_hidden_source_splits_still_train_other_targets(tmp_path, monkeypatch):
    """NEGATIVE: history read FROM a hidden account is not thrown away."""
    txns = _hist("ZQVRT MONTHLY PAYMENT", "groc", 3, bank=fx.HDFC_OLD)
    book = _book(tmp_path, txns, stem="hidsrc")
    rows = _run(tmp_path, book, [ROW], bank_account=None, monkeypatch=monkeypatch)
    assert rows[0]["Account"] == "Expenses:Groceries"
    assert rows[0]["Confidence"] != "suspense"


def test_looks_dormant_account_is_still_mapped_and_highlighted(tmp_path, monkeypatch):
    # groc used (and zeroed) only in FY2019; statement is FY2025.
    txns = (_hist("ZQVRT MONTHLY PAYMENT", "groc", 2, date="2019-05-01")
            + [fx.txn_xml("REV", "2019-06-01", [(fx.HDFC1, 20000), ("groc", -20000)])])
    book = _book(tmp_path, txns, stem="dormant")
    rows = _run(tmp_path, book, [ROW], monkeypatch=monkeypatch)
    assert rows[0]["Account"] == "Expenses:Groceries"
    assert rows[0]["Confidence"] != "suspense"
    assert mapper._DORMANT_MARKER in rows[0]["MatchReason"]


def test_account_active_in_current_fy_is_never_highlighted(tmp_path, monkeypatch):
    txns = _hist("ZQVRT MONTHLY PAYMENT", "groc", 3, date="2025-05-01")
    book = _book(tmp_path, txns, stem="active")
    rows = _run(tmp_path, book, [ROW], monkeypatch=monkeypatch)
    assert rows[0]["Account"] == "Expenses:Groceries"
    assert mapper._DORMANT_MARKER not in rows[0]["MatchReason"]


# -- one parametrised test per pass -----------------------------------------

def _scn_rules(tmp_path):
    return _book(tmp_path, _hist("ZQVRT MONTHLY PAYMENT", "hid_dining", 3), stem="p_rules"), \
        ROW, {}


def _scn_history(tmp_path):
    txns = (_hist("QQFAM UTILITY ALPHA", "hid_dining", 2)
            + _hist("QQFAM UTILITY BRAVO", "hid_dining", 2))
    return _book(tmp_path, txns, stem="p_hist"), \
        ("2025-08-01", "QQFAM UTILITY CHARLIE", "", "100.00"), {}


def _scn_saved(tmp_path):
    book = _book(tmp_path, [], stem="p_saved")
    pr.save_overrides_batch(book, [{"patterns": ["ZQVRT"], "account": "Expenses:Old:Dining"}])
    return book, ROW, {}


def _scn_own_transfer(tmp_path):
    ob = [fx.account_xml("zb", "ZBNK Bank - 094XXXX0001 (old)", "BANK", "cab", ["hidden"])]
    txns = (_hist("XFER TO SELF ALPHA", fx.HDFC2, 1) + _hist("XFER TO SELF BETA", fx.HDFC4, 1)
            + _hist("XFER TO SELF GAMMA", fx.HSBC1, 1))
    return _book(tmp_path, txns, extra=ob, stem="p_own"), \
        ("2025-08-01", "XFER TO SELF ZBNK0000123", "", "100.00"), {}


def _scn_llm(tmp_path):
    return _book(tmp_path, _hist("SOMETHING ELSE", "groc", 1), stem="p_llm"), \
        ("2025-08-01", "UNSEEN MERCHANT XYZ", "", "100.00"), {"llm": "Expenses:Dining"}


def _scn_keyword(tmp_path):
    txns = _hist("BANK INTEREST CREDIT", "int", 2)
    book = _book(tmp_path, txns, stem="p_kw")
    return book, ("2025-08-01", "SB INTEREST CREDITED", "50.00", ""), {}


@pytest.mark.parametrize("scenario", [
    "rules", "history", "saved_rule", "own_transfer", "llm",
])
def test_no_pass_can_emit_a_hidden_target(tmp_path, monkeypatch, scenario):
    build = {"rules": _scn_rules, "history": _scn_history, "saved_rule": _scn_saved,
             "own_transfer": _scn_own_transfer, "llm": _scn_llm}[scenario]
    book, row, opts = build(tmp_path)
    cfg = None
    if "llm" in opts:
        cfg = str(tmp_path / "settings" / "config.yaml")
        (tmp_path / "settings").mkdir()

        def fake_llm(unmatched_rows, **kw):
            return {r["row"]: {"account": opts["llm"], "reason": "LLM: fake"}
                    for r in unmatched_rows}
        monkeypatch.setattr(mapper, "llm_fallback_mapping", fake_llm)
    rows = _run(tmp_path, book, [row], config_path=cfg, monkeypatch=monkeypatch)
    g = ga.TargetGuard.from_book(book)
    for r in rows:
        assert g.blocked_target_reason(r["Account"]) is None, \
            f"{scenario}: emitted a blocked target {r['Account']!r}"
    assert rows[0]["Confidence"] == "suspense"
    if scenario != "own_transfer":  # hidden bank never becomes a candidate, so no history reason exists
        assert rows[0]["MatchReason"].startswith("blocked: "), rows[0]["MatchReason"]


def test_keyword_pass_scenario_never_emits_a_hidden_target(tmp_path, monkeypatch):
    """Keyword / smart-pattern pass: the smart matcher is fed only candidate
    accounts that survived the guard, and the final guard backs it."""
    book, row, _ = _scn_keyword(tmp_path)
    hid = ga.TargetGuard.from_book(book)
    rows = _run(tmp_path, book, [row], monkeypatch=monkeypatch)
    assert hid.blocked_target_reason(rows[0]["Account"]) is None


def test_guard_neutralised_control_proves_scenarios_really_hit_hidden(tmp_path, monkeypatch):
    """CONTROL: with the guard switched off the same rules scenario DOES land
    on the hidden account -- so the tests above are not passing vacuously."""
    book, row, _ = _scn_rules(tmp_path)
    monkeypatch.setattr(ga.TargetGuard, "blocked_target_reason", lambda self, p: None)
    rows = _run(tmp_path, book, [row], monkeypatch=monkeypatch)
    assert rows[0]["Account"] == "Expenses:Dining"


# ---------------------------------------------------------------------------
# 26AS mirror + drift
# ---------------------------------------------------------------------------

def _load_builder():
    script = SRC / "agents" / "skill_26as_journal" / "scripts" / "build_tds_journals.py"
    spec = importlib.util.spec_from_file_location("build_tds_journals_imp09", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_26as_mirror_flag_keys_match_shared_constant():
    b = _load_builder()
    assert tuple(b.BLOCKING_FLAGS) == tuple(ga.BLOCKING_FLAGS)


def test_26as_mirror_and_shared_guard_agree_on_every_account(tmp_path):
    """DRIFT: the stand-alone 26AS loader and TargetGuard must block exactly
    the same accounts (hidden, hidden-ancestor, placeholder)."""
    b = _load_builder()
    book = _book(tmp_path)
    g = ga.TargetGuard.from_book(book)
    mirror = {a.path: a.blocked for a in b.load_accounts(Path(book))}
    shared = {a.path: g.is_blocked(a.path) for a in ga.load_accounts(book) if a.path}
    assert mirror == shared
    # sanity: the interesting cases are really present in both
    assert mirror["Expenses:Old:Dining"] is True
    assert mirror["Expenses:Household:Fuel"] is False
    assert mirror["Expenses:GST Paid"] is False


def test_26as_candidates_offer_tax_related_but_not_hidden(tmp_path):
    b = _load_builder()
    extra = [fx.account_xml("ti", "Tax Interest", "INCOME", "int", ["tax-related"]),
             fx.account_xml("hi", "Interest on Old Bank", "INCOME", "int", ["hidden"]),
             fx.account_xml("ci", "Interest on Live Bank", "INCOME", "int")]
    accs = b.load_accounts(Path(_book(tmp_path, extra=extra, stem="s26")))
    got = {a.path for a in b._candidates_for("A", accs)}
    assert "Income:Interest:Tax Interest" in got
    assert "Income:Interest:Interest on Live Bank" in got
    assert "Income:Interest:Interest on Old Bank" not in got


# ---------------------------------------------------------------------------
# partner skill + pipeline bank pick
# ---------------------------------------------------------------------------

def test_partner_validation_refuses_hidden_and_accepts_tax_related(tmp_path):
    from agents.skill_partner_comp_recon import agent as partner
    book = _book(tmp_path, stem="partner")
    errs = partner._validate_accounts_against_book(
        {"ok": "Expenses:Food:Dining", "tax": "Expenses:GST Paid",
         "hid": "Expenses:Dining", "kid": "Expenses:Old:Dining",
         "ph": "Expenses:Household"}, book)
    joined = "\n".join(errs)
    assert "['ok']" not in joined
    assert "['tax']" not in joined            # tax-related IS a valid target now
    for k in ("hid", "kid", "ph"):
        assert f"['{k}']" in joined


def test_pipeline_bank_pick_never_returns_hidden_account_even_when_first(tmp_path):
    from agents.skill_gnucash_pipeline.agent import _get_gnucash_account_balance
    # only a hidden HDFC account and one postable, hidden FIRST in book order
    accs = [fx.account_xml("root", "Root Account", "ROOT", None),
            fx.account_xml("assets", "Assets", "ASSET", "root", ["placeholder"]),
            fx.account_xml("h1", "HDFC Bank - 094XXXX9012 (old)", "BANK", "assets", ["hidden"]),
            fx.account_xml("h2", "HDFC Bank - 094XXXX1234", "BANK", "assets")]
    book = fx.write_book(tmp_path / "pick.gnucash", accs, [])
    info = _get_gnucash_account_balance(book, "HDFC")
    assert info["found"]
    assert "(old)" not in info["account_name"]
    assert info["account_name"].endswith("HDFC Bank - 094XXXX1234")


def test_pipeline_bank_pick_with_only_hidden_account_is_not_found(tmp_path):
    from agents.skill_gnucash_pipeline.agent import _get_gnucash_account_balance
    accs = [fx.account_xml("root", "Root Account", "ROOT", None),
            fx.account_xml("assets", "Assets", "ASSET", "root", ["placeholder"]),
            fx.account_xml("h1", "HDFC Bank - 094XXXX9012 (old)", "BANK", "assets", ["hidden"])]
    book = fx.write_book(tmp_path / "pick2.gnucash", accs, [])
    assert _get_gnucash_account_balance(book, "HDFC")["found"] is False


# ---------------------------------------------------------------------------
# Review presentation
# ---------------------------------------------------------------------------

def test_review_shows_dormant_highlight_only_on_mapped_rows():
    from ui.tabs import gnucash_review as rv
    dormant = {"Confidence": "history", "Account": "Expenses:Groceries",
               "MatchReason": "history [looks dormant: no activity]"}
    rv._row_presentation(dormant, None)
    assert dormant["_badges"]["Account"]["cls"] == "violet"
    assert dormant["_tags"] == ["history"]                 # NOT moved to suspense
    fresh = {"Confidence": "history", "Account": "Expenses:Groceries", "MatchReason": "history"}
    rv._row_presentation(fresh, None)
    assert "_badges" not in fresh                          # never highlighted


def test_review_blocked_suspense_row_shows_reason():
    from ui.tabs import gnucash_review as rv
    row = {"Confidence": "suspense", "Account": "Assets:Suspense",
           "MatchReason": "blocked: target is hidden in the book (was: X)"}
    rv._row_presentation(row, None)
    b = row["_badges"]["Account"]
    assert b["text"] == "SUSPENSE" and "hidden in the book" in b["title"]
