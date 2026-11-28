"""BNK-01 standing rule: nothing the app has already learned may stop working.

Learned rules are user corrections (the book's ``_overrides``: a regex on the
statement description, no bank field) and per-bank auto rules filtered by the
mapper's bank key. So three things must not move:

1. The canonical rows the mapper sees (Description exactly as before, same
   dates, amounts and order) -- pinned against a golden file produced by the
   code BEFORE BNK-01 (origin/main e522f95) on synthetic statements.
2. The bank key the mapper resolves for each bank name.
3. The rules-file path and the ``_overrides`` matching.

Synthetic data only.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

import bnk01_fixtures as fx  # noqa: E402

GOLDEN = json.loads((ROOT / "tests" / "golden_bnk01_old_rows.json").read_text(encoding="utf-8"))


# ---- (1) the rows the mapper sees -----------------------------------------

@pytest.fixture(scope="module")
def rows_now():
    return fx.dump_rows()


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_every_banks_rows_are_exactly_what_the_old_code_gave(rows_now, key):
    """Description, Date, amounts, balance, order: identical to the old code."""
    assert rows_now[key] == GOLDEN[key]


def test_the_golden_file_covers_all_six_banks():
    from agents import banks
    assert sorted(GOLDEN) == sorted(b.bank_key for b in banks.discover())
    assert all(GOLDEN[k] for k in GOLDEN)


def test_joining_never_rewrites_the_narration(tmp_path):
    """Two BoB statements joined: every row is the very row each file gave,
    narration included (spacing, case, joins)."""
    import bob_fixture_gen
    from agents.skill_bob.agent import BoBSkill

    jan = [("15-01-2025", "NEFT  SALARY  credit-SYNCO/Ref 77", "", "", "50,000.00", "1,50,000.00Cr")]
    feb = [("10-02-2025", "upi-Grocery   STORE-syn", "", "2,000.00", "", "1,48,000.00Cr")]
    (tmp_path / "jan.pdf").write_bytes(bob_fixture_gen.build_pdf_for(jan, "01-01-2025", "31-01-2025", "1234567890"))
    (tmp_path / "feb.pdf").write_bytes(bob_fixture_gen.build_pdf_for(feb, "01-02-2025", "28-02-2025", "1234567890"))
    skill = BoBSkill()
    each = skill.parse_one(tmp_path / "jan.pdf").rows + skill.parse_one(tmp_path / "feb.pdf").rows
    joined = skill.parse(tmp_path).rows
    assert joined == each
    assert [r["Description"] for r in joined] == [r["Description"] for r in each]


def test_joined_narration_keeps_its_spacing_case_and_tabs(tmp_path):
    """A stub bank with awkward narration: the join passes it through as is."""
    from agents.bank_common.statements import read_statements
    from agents.bank_contract import BankResult, BankStatementMeta

    def res(rows):
        return BankResult(rows=rows, bank_key="s",
                          meta=BankStatementMeta(bank_key="s", account_number="12345678"))

    def r(d, desc, dep, wd, bal):
        return {"Date": d, "Transaction ID": "", "Description": desc, "Account": "",
                "Deposit": dep, "Withdrawal": wd, "Balance": bal, "Currency": "INR"}

    odd = ["NEFT  CR-  Rohan\tMehta   /ref 9", "upi/DR/ 123 /Mixed Case", " leading and trailing "]

    class S:
        formats = staticmethod(lambda: (".csv",))
        data = {"a.csv": res([r("2026-01-02", odd[0], "100", "0", "100"), r("2026-01-03", odd[1], "0", "10", "90")]),
                "b.csv": res([r("2026-02-02", odd[2], "5", "0", "95")])}

        def parse_one(self, path, password=None):
            return self.data[Path(path).name]

    for n in S.data:
        (tmp_path / n).write_text("x")
    got = read_statements(S(), tmp_path)
    assert [x["Description"] for x in got.rows] == odd


def test_the_join_code_only_reads_the_description():
    """Guard: every mention of Description in statements.py is a read."""
    import re
    src = (ROOT / "src" / "agents" / "bank_common" / "statements.py").read_text(encoding="utf-8")
    mentions = re.findall(r"Description", src)
    reads = re.findall(r"""\.get\(["']Description["']""", src)
    assert len(mentions) == len(reads), "statements.py must only read Description, never write it"


# ---- (2) bank_key ---------------------------------------------------------

OLD_BANK_KEYS = {"Bank of Baroda": "BoB", "HDFC": "HDFC", "HSBC": "HSBC", "ICICI": "ICICI"}
NEW_BANK_KEYS = {"SBM": "SBM", "Kotak": "Kotak"}


def test_the_mapper_bank_key_map_is_the_old_one_plus_sbm_and_kotak():
    from agents.skill_gnucash_account_mapper.agent import _BANK_KEY_MAP
    assert _BANK_KEY_MAP == {**OLD_BANK_KEYS, **NEW_BANK_KEYS}


@pytest.mark.parametrize("name,key", sorted(OLD_BANK_KEYS.items()))
def test_bank_key_resolves_to_the_old_string(name, key):
    from agents.skill_gnucash_account_mapper.agent import _BANK_KEY_MAP
    assert _BANK_KEY_MAP.get(name) == key


@pytest.mark.parametrize("name", ["Other Bank (CSV)"])
def test_other_bank_has_no_key(name):
    """Other Bank (CSV) is not a bank skill: no key."""
    from agents.skill_gnucash_account_mapper.agent import _BANK_KEY_MAP
    assert _BANK_KEY_MAP.get(name) is None


def test_the_extractor_still_files_history_under_the_old_names():
    from agents.skill_gnucash_xml_extractor import agent as ex
    assert {k for k in ex.BANK_PATTERNS} == {"ICICI", "HDFC", "HSBC", "BoB", "SBM", "Kotak"}


# ---- (3) rules path and overrides ----------------------------------------

def test_the_rules_file_path_is_unchanged(tmp_path):
    from agents.skill_gnucash_account_mapper import persistent_rules as pr
    assert pr._yearless_stem("Family Books FY2025") == "Family Books FY"
    book = tmp_path / "Family Books FY2025.gnucash"
    book.write_bytes(b"x")
    assert pr.rules_path(str(book)).name == "Family Books FY_mapping_rules.yaml"


def test_the_overrides_section_name_is_unchanged():
    from agents.skill_gnucash_account_mapper import persistent_rules as pr
    assert pr.OVERRIDES_KEY == "_overrides"


OVERRIDES = [
    {"pattern": r"grocery\s+store", "account": "Expenses:Food", "added": "2025-01-01"},
    {"patterns": [r"^NEFT\b.*salary", r"payroll"], "account": "Income:Salary", "added": "2025-01-02"},
]


@pytest.mark.parametrize("key", sorted(GOLDEN))
def test_an_override_that_matched_a_row_on_the_old_code_still_matches(rows_now, key):
    """The same overrides give the same answer for every fixture row, old
    rows versus new rows."""
    from agents.skill_gnucash_account_mapper.persistent_rules import match_overrides
    import re
    # one override per row, built from that row's own text, so every bank's
    # rows are really hit (a user correction is a regex on the description)
    own = [{"pattern": re.escape(str(r["Description"])[:12]), "account": f"Expenses:Row{i}",
            "added": "2025-01-03"} for i, r in enumerate(GOLDEN[key]) if str(r["Description"])[:12].strip()]
    overrides = OVERRIDES + own
    old = [match_overrides(r["Description"], overrides) for r in GOLDEN[key]]
    new = [match_overrides(r["Description"], overrides) for r in rows_now[key]]
    assert new == old
    assert any(a for a, _ in old), "the overrides must really hit this bank's rows"


def test_an_override_really_matches_a_fixture_row():
    """Control: the check above is not vacuous (at least one hit)."""
    from agents.skill_gnucash_account_mapper.persistent_rules import match_overrides
    hits = [match_overrides(r["Description"], [{"pattern": r"\w+", "account": "X:Y"}])
            for rows in GOLDEN.values() for r in rows]
    assert any(h[0] == "X:Y" for h in hits)
