"""
tests/skill_gnucash_xml_extractor/test_bank_split_classification.py

RED FLAG regression tests for skill_gnucash_xml_extractor.agent's split
classification. The bug: any split whose account PATH merely *contained* a
bank's name (ICICI/HDFC/HSBC/BoB) was treated as "the bank" and never
considered as a possible transfer TARGET. That silently:
  (a) skipped bank<->FD sweep transactions whenever the FD account happened
      to be named after the bank (e.g. "...Fixed Deposits:ICICI Bank - FD")
      -- both splits looked like "a bank", so the transaction had no target;
  (b) skipped genuine inter-bank transfers (ICICI<->HDFC, ICICI<->HSBC) the
      same way -- both sides looked like "a bank";
  (c) meant only a transfer to a bank whose name matched NO pattern (e.g.
      "SBM") ever survived as a self-transfer target, so the matcher that
      learns from this history "learned" that self-transfers go to SBM.

Fixed by:
  * threading the caller's exact `gnucash_bank_account` through so, when
    known, that split is unambiguously the source and the target is simply
    the largest OTHER split -- regardless of what that split's name contains;
  * without an explicit account, a split only counts as a bank's own account
    when its GnuCash type is BANK AND its name matches a known bank pattern
    (agent._is_own_bank_account) -- never a bare name-substring match, so an
    FD (type ASSET) or a loan/card (type LIABILITY/CREDIT) named after a bank
    is never mistaken for the bank itself;
  * a transaction between two own bank accounts (e.g. ICICI<->HDFC) now
    yields a pair in EACH bank's own history, not just the first one seen.

All fixtures are synthetic: fake account numbers, fake names, fake IFSC-
shaped codes. No real book data anywhere in this file.
"""
from __future__ import annotations

import gzip
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
AGENTS_ROOT = SRC / "agents"
for _p in (SRC, AGENTS_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from skill_gnucash_xml_extractor.agent import parse_gnucash_file  # noqa: E402


_NS_DECL = (
    'xmlns:gnc="http://www.gnucash.org/XML/gnc" '
    'xmlns:act="http://www.gnucash.org/XML/act" '
    'xmlns:trn="http://www.gnucash.org/XML/trn" '
    'xmlns:split="http://www.gnucash.org/XML/split" '
    'xmlns:ts="http://www.gnucash.org/XML/ts"'
)


def _account_xml(name: str, aid: str, atype: str, parent: str | None) -> str:
    parts = [
        ' <gnc:account version="2.0.0">',
        f'  <act:name>{name}</act:name>',
        f'  <act:id type="guid">{aid}</act:id>',
        f'  <act:type>{atype}</act:type>',
    ]
    if parent is not None:
        parts.append(f'  <act:parent type="guid">{parent}</act:parent>')
    parts.append(' </gnc:account>')
    return "\n".join(parts)


def _split_xml(acc_id: str, value: str) -> str:
    return (
        "   <trn:split>"
        f"<split:value>{value}</split:value>"
        f'<split:account type="guid">{acc_id}</split:account>'
        "</trn:split>"
    )


def _txn_xml(desc: str, date: str, splits: list[str]) -> str:
    return (
        ' <gnc:transaction version="2.0.0">\n'
        f'  <trn:description>{desc}</trn:description>\n'
        f'  <trn:date-posted><ts:date>{date} 00:00:00 +0000</ts:date></trn:date-posted>\n'
        '  <trn:splits>\n'
        + "\n".join(splits) + "\n"
        '  </trn:splits>\n'
        ' </gnc:transaction>'
    )


def _book_xml(accounts: list[str], transactions: list[str]) -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>\n'
        f"<gnc-v2 {_NS_DECL}>\n"
        "<gnc:book version=\"2.0.0\">\n"
        + "\n".join(accounts) + "\n"
        + "\n".join(transactions) + "\n"
        "</gnc:book>\n</gnc-v2>\n"
    )


def _write_book(tmp_path: Path, accounts: list[str], transactions: list[str]) -> Path:
    p = tmp_path / "synthetic.gnucash"
    p.write_bytes(gzip.compress(_book_xml(accounts, transactions).encode("utf-8")))
    return p


ROOT_ACC = _account_xml("Root Account", "root", "ROOT", None)
ASSETS_ACC = _account_xml("Assets", "assets", "ASSET", "root")
EXPENSES_ACC = _account_xml("Expenses", "expenses", "EXPENSE", "root")
ICICI_CUR = _account_xml("ICICI Bank - 001", "icici_cur", "BANK", "assets")
HDFC_CUR = _account_xml("HDFC Bank - 002", "hdfc_cur", "BANK", "assets")
HSBC_CUR = _account_xml("HSBC Bank - 003", "hsbc_cur", "BANK", "assets")
FD_ACCT = _account_xml("ICICI Bank - FD", "fd_acct", "ASSET", "assets")
RENT_ACCT = _account_xml("Rent Expense", "rent_acct", "EXPENSE", "expenses")

ICICI_CUR_PATH = "Assets:ICICI Bank - 001"
HDFC_CUR_PATH = "Assets:HDFC Bank - 002"
HSBC_CUR_PATH = "Assets:HSBC Bank - 003"
FD_ACCT_PATH = "Assets:ICICI Bank - FD"
RENT_ACCT_PATH = "Expenses:Rent Expense"


# ---------------------------------------------------------------------------
# 1. A bank<->FD sweep whose FD account name contains the bank name IS
#    extracted, with the FD as the target -- not skipped as "bank-to-bank".
# ---------------------------------------------------------------------------

def test_bank_to_fd_sweep_with_bank_named_fd_account_is_extracted(tmp_path):
    accounts = [ROOT_ACC, ASSETS_ACC, ICICI_CUR, FD_ACCT]
    txns = [
        _txn_xml(
            "AUTOSWEEP TO 111111111111 SAMPLE PARTY", "2024-01-05",
            [_split_xml("icici_cur", "-100000/100"), _split_xml("fd_acct", "100000/100")],
        )
    ]
    book = _write_book(tmp_path, accounts, txns)
    result = parse_gnucash_file(str(book))

    assert result["metadata"]["total_transactions_skipped"] == 0
    icici_mappings = result["mappings"].get("ICICI", [])
    assert len(icici_mappings) == 1
    entry = icici_mappings[0]
    assert entry["account"] == f"Root Account:{FD_ACCT_PATH}"
    # Negative: the FD account is never mistaken for "the bank" -- the ICICI
    # current account itself must never appear as its own target.
    assert entry["account"] != f"Root Account:{ICICI_CUR_PATH}"


# ---------------------------------------------------------------------------
# 2. ICICI<->HDFC and ICICI<->HSBC transfers appear in BOTH banks' histories.
# ---------------------------------------------------------------------------

def test_inter_bank_transfer_appears_in_both_banks_histories(tmp_path):
    accounts = [ROOT_ACC, ASSETS_ACC, ICICI_CUR, HDFC_CUR, HSBC_CUR]
    txns = [
        _txn_xml(
            "Xfer to self/SAMPLE PARTY/HDFC0000002", "2024-01-05",
            [_split_xml("icici_cur", "-50000/100"), _split_xml("hdfc_cur", "50000/100")],
        ),
        _txn_xml(
            "Xfer to self/SAMPLE PARTY/HSBC0000003", "2024-01-06",
            [_split_xml("icici_cur", "-60000/100"), _split_xml("hsbc_cur", "60000/100")],
        ),
    ]
    book = _write_book(tmp_path, accounts, txns)
    result = parse_gnucash_file(str(book))

    assert result["metadata"]["total_transactions_skipped"] == 0
    icici_accounts = {m["account"] for m in result["mappings"].get("ICICI", [])}
    hdfc_accounts = {m["account"] for m in result["mappings"].get("HDFC", [])}
    hsbc_accounts = {m["account"] for m in result["mappings"].get("HSBC", [])}

    assert f"Root Account:{HDFC_CUR_PATH}" in icici_accounts
    assert f"Root Account:{HSBC_CUR_PATH}" in icici_accounts
    # The reverse pairing must ALSO exist -- each bank's own history records
    # the OTHER bank as the target, not just whichever side was seen first.
    assert f"Root Account:{ICICI_CUR_PATH}" in hdfc_accounts
    assert f"Root Account:{ICICI_CUR_PATH}" in hsbc_accounts
    # Negative: neither bank's history ever names itself as its own target.
    assert f"Root Account:{HDFC_CUR_PATH}" not in hdfc_accounts
    assert f"Root Account:{HSBC_CUR_PATH}" not in hsbc_accounts


# ---------------------------------------------------------------------------
# 3. An explicitly named bank account (gnucash_bank_account) is never emitted
#    as its own target.
# ---------------------------------------------------------------------------

def test_explicit_bank_account_never_emitted_as_its_own_target(tmp_path):
    accounts = [ROOT_ACC, ASSETS_ACC, ICICI_CUR, HDFC_CUR]
    txns = [
        _txn_xml(
            "Xfer to self/SAMPLE PARTY/HDFC0000002", "2024-01-05",
            [_split_xml("icici_cur", "-70000/100"), _split_xml("hdfc_cur", "70000/100")],
        )
    ]
    book = _write_book(tmp_path, accounts, txns)
    result = parse_gnucash_file(str(book), gnucash_bank_account=ICICI_CUR_PATH)

    assert result["metadata"]["total_transactions_skipped"] == 0
    all_targets = [
        m["account"] for bank_maps in result["mappings"].values() for m in bank_maps
    ]
    assert all_targets == [f"Root Account:{HDFC_CUR_PATH}"]
    assert f"Root Account:{ICICI_CUR_PATH}" not in all_targets


# ---------------------------------------------------------------------------
# 4. A three-split transaction still picks the LARGEST other split -- not the
#    first non-forced split found, and not biased by whether a split's name
#    contains a bank pattern.
# ---------------------------------------------------------------------------

def test_three_split_transaction_picks_largest_other_split(tmp_path):
    accounts = [ROOT_ACC, ASSETS_ACC, EXPENSES_ACC, ICICI_CUR, HDFC_CUR, RENT_ACCT]
    txns = [
        _txn_xml(
            "SPLIT SETTLEMENT SAMPLE", "2024-01-05",
            [
                _split_xml("icici_cur", "-100000/100"),  # forced source
                _split_xml("hdfc_cur", "20000/100"),      # smaller other split
                _split_xml("rent_acct", "80000/100"),     # largest other split
            ],
        )
    ]
    book = _write_book(tmp_path, accounts, txns)
    result = parse_gnucash_file(str(book), gnucash_bank_account=ICICI_CUR_PATH)

    all_targets = [
        m["account"] for bank_maps in result["mappings"].values() for m in bank_maps
    ]
    assert all_targets == [f"Root Account:{RENT_ACCT_PATH}"]
    # Negative: the smaller (but bank-named) split is never chosen just
    # because its name matches a bank pattern.
    assert f"Root Account:{HDFC_CUR_PATH}" not in all_targets


# ---------------------------------------------------------------------------
# 5. End-to-end: agent.run() (the account mapper) on a synthetic book where
#    sweeps must land on the FD account and an HSBC-IFSC self-transfer must
#    land on HSBC -- never SBM or HDFC -- with a stale Medium "rent" rule
#    present that history must displace.
# ---------------------------------------------------------------------------

SBM_CUR = _account_xml("SBM Bank - 004", "sbm_cur", "BANK", "assets")
SBM_CUR_PATH = "Assets:SBM Bank - 004"


def test_end_to_end_run_sweeps_to_fd_and_self_transfer_to_hsbc_never_sbm_or_hdfc(tmp_path, monkeypatch):
    import csv

    import skill_gnucash_mapping_generator.agent as mapgen_mod
    import skill_gnucash_account_mapper.persistent_rules as persistent_rules_mod
    from skill_gnucash_account_mapper import agent as mapper_agent

    accounts = [
        ROOT_ACC, ASSETS_ACC, EXPENSES_ACC,
        ICICI_CUR, HDFC_CUR, HSBC_CUR, SBM_CUR, FD_ACCT, RENT_ACCT,
    ]

    def _sweep(date):
        return _txn_xml(
            "AUTOSWEEP TO 555555555555 SAMPLE PARTY", date,
            [_split_xml("icici_cur", "-50000/100"), _split_xml("fd_acct", "50000/100")],
        )

    def _self_xfer_hsbc(date):
        return _txn_xml(
            "Xfer to self/SAMPLE PARTY/HSBC0000003", date,
            [_split_xml("icici_cur", "-20000/100"), _split_xml("hsbc_cur", "20000/100")],
        )

    def _self_xfer_sbm_decoy(date):
        # A genuine historical self-transfer to SBM -- must stay a decoy and
        # never be offered for the HSBC-addressed live row below.
        return _txn_xml(
            "Xfer to self/SAMPLE PARTY/SBMX0000004", date,
            [_split_xml("icici_cur", "-30000/100"), _split_xml("sbm_cur", "30000/100")],
        )

    def _self_xfer_hdfc_decoy(date):
        return _txn_xml(
            "Xfer to self/SAMPLE PARTY/HDFC0000002", date,
            [_split_xml("icici_cur", "-40000/100"), _split_xml("hdfc_cur", "40000/100")],
        )

    txns = []
    for d in ("2024-01-05", "2024-02-05", "2024-03-05"):
        txns.append(_sweep(d))
    for d in ("2024-01-06", "2024-02-06", "2024-03-06"):
        txns.append(_self_xfer_hsbc(d))
    for d in ("2024-01-07", "2024-02-07"):
        txns.append(_self_xfer_sbm_decoy(d))
    for d in ("2024-01-08", "2024-02-08"):
        txns.append(_self_xfer_hdfc_decoy(d))

    gnucash_file = _write_book(tmp_path, accounts, txns)

    # A stale Medium rule that used to (wrongly) claim every AUTOSWEEP row for
    # Rent -- history must be able to displace it once it clears thresholds.
    def fake_generate_rules(extractor_output, min_freq=1):
        return {
            "ICICI": [{
                "patterns": ["AUTOSWEEP"],
                "account": RENT_ACCT_PATH,
                "confidence": "medium",
                "frequency": 5,
                "last_date": "2023-01-01",
                "score": 0.6,
                "reason": "stale rule: AUTOSWEEP -> Rent",
                "bank": "ICICI",
            }]
        }

    def fake_merge_auto_rules(gnucash_file, rules_by_bank, config_path=None):
        return rules_by_bank

    def fake_load_overrides(gnucash_file, config_path=None):
        return []

    def fake_migrate_legacy_overrides(gnucash_file, config_path=None):
        return 0

    def fake_rules_path(gnucash_file, config_path=None):
        return tmp_path / "fake_persistent_rules.yaml"

    monkeypatch.setattr(mapgen_mod, "generate_rules", fake_generate_rules)
    monkeypatch.setattr(persistent_rules_mod, "merge_auto_rules", fake_merge_auto_rules)
    monkeypatch.setattr(persistent_rules_mod, "load_overrides", fake_load_overrides)
    monkeypatch.setattr(persistent_rules_mod, "migrate_legacy_overrides", fake_migrate_legacy_overrides)
    monkeypatch.setattr(persistent_rules_mod, "rules_path", fake_rules_path)
    monkeypatch.setattr(mapper_agent, "_emit_mapper_progress", lambda msg: None)

    canonical_rows = [
        {"Date": "05-04-2024", "Description": "AUTOSWEEP TO 999999999999 SAMPLE PARTY",
         "Withdrawal": "50000.00", "Deposit": ""},
        {"Date": "06-04-2024", "Description": "Xfer to self/SAMPLE PARTY/HSBC0000003",
         "Withdrawal": "20000.00", "Deposit": ""},
    ]
    canonical_csv = tmp_path / "canonical.csv"
    with open(canonical_csv, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=["Date", "Description", "Withdrawal", "Deposit"])
        writer.writeheader()
        writer.writerows(canonical_rows)

    output_path = tmp_path / "mapped.csv"

    mapper_agent.run(
        gnucash_file=str(gnucash_file),
        canonical_csv=str(canonical_csv),
        output_path=str(output_path),
        config_path=None,  # no LLM config -- rows must resolve without it
        model_override=None,
        bank_name="ICICI",
        gnucash_bank_account=ICICI_CUR_PATH,
    )

    with open(output_path, newline="", encoding="utf-8") as f:
        mapped_rows = list(csv.DictReader(f))

    sweep_row = next(r for r in mapped_rows if "AUTOSWEEP" in r["Description"])
    xfer_row = next(r for r in mapped_rows if "Xfer to self" in r["Description"])

    # The stale Medium rent rule is DISPLACED by real history evidence.
    assert sweep_row["Confidence"] == "history", sweep_row
    assert sweep_row["Account"] == FD_ACCT_PATH
    assert sweep_row["Account"] != RENT_ACCT_PATH
    assert "Rent" not in sweep_row["Account"]

    # The HSBC-addressed self-transfer resolves to HSBC's own account --
    # never SBM (the old bug's learned target) and never HDFC (a different
    # own account that also appears in history).
    assert xfer_row["Confidence"] == "history", xfer_row
    assert xfer_row["Account"] == HSBC_CUR_PATH
    assert xfer_row["Account"] != SBM_CUR_PATH
    assert xfer_row["Account"] != HDFC_CUR_PATH
