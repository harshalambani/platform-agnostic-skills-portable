"""BNK-01 commit 1: the one reading rule every bank follows.

Part A drives bank_common.statements with a stub bank (pure rules).
Part B runs real bank readers (BoB, Kotak) on synthetic PDFs.
Part C runs Convert to GnuCash.

Synthetic data only.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

from agents.bank_common.statements import StatementStop, read_statements  # noqa: E402
from agents.bank_contract import BankResult, BankStatementMeta  # noqa: E402


# ---------------------------------------------------------------------------
# Part A: the rules, with a stub bank
# ---------------------------------------------------------------------------

def row(date, desc, dep, wd, bal):
    return {"Date": date, "Transaction ID": "", "Description": desc, "Account": "",
            "Deposit": str(dep), "Withdrawal": str(wd), "Balance": str(bal),
            "Currency": "INR"}


def result(rows, number="11112222", warnings=()):
    return BankResult(
        rows=list(rows), bank_key="stub", warnings=list(warnings),
        meta=BankStatementMeta(bank_key="stub", account_number=number))


class Stub:
    bank_key = "stub"

    def __init__(self, by_name):
        self.by_name = by_name

    def formats(self):
        return (".csv",)

    def parse_one(self, path, password=None):
        r = self.by_name[Path(path).name]
        if isinstance(r, Exception):
            raise r
        return r


def folder(tmp_path, by_name, extra=()):
    d = tmp_path / "up"
    d.mkdir()
    for n in list(by_name) + list(extra):
        (d / n).write_text("x")
    return d


JAN = [row("2026-01-05", "SALARY", 1000, 0, 1500), row("2026-01-20", "RENT", 0, 400, 1100)]
FEB = [row("2026-02-03", "SHOP", 0, 100, 1000), row("2026-02-10", "REFUND", 50, 0, 1050)]


def test_a_single_statement_comes_back_exactly_as_the_bank_read_it(tmp_path):
    only = result(JAN, warnings=["its own warning"])
    d = folder(tmp_path, {"a.csv": only})
    got = read_statements(Stub({"a.csv": only}), d)
    assert got.rows == only.rows and got.warnings == only.warnings
    assert got.meta == only.meta


def test_a_file_and_a_folder_of_that_one_file_give_the_same_rows(tmp_path):
    only = result(JAN)
    d = folder(tmp_path, {"a.csv": only})
    stub = Stub({"a.csv": only})
    assert read_statements(stub, d / "a.csv").rows == read_statements(stub, d).rows


def test_statements_are_joined_in_date_order_not_name_order(tmp_path):
    by = {"z_jan.csv": result(JAN), "a_feb.csv": result(FEB)}
    got = read_statements(Stub(by), folder(tmp_path, by))
    assert [r["Date"] for r in got.rows] == [r["Date"] for r in JAN + FEB]
    assert got.balance_check.ok


def test_a_break_in_the_running_balance_stops_naming_both_files_and_balances(tmp_path):
    mar = [row("2026-03-03", "SHOP", 0, 100, 900)]          # opens at 1,000
    by = {"jan.csv": result(JAN), "mar.csv": result(mar)}
    with pytest.raises(StatementStop) as exc:
        read_statements(Stub(by), folder(tmp_path, by))
    msg = str(exc.value)
    assert "jan.csv" in msg and "mar.csv" in msg
    assert "1,100.00" in msg and "1,000.00" in msg
    assert exc.value.title == "break in the running balance"


def test_the_override_joins_across_the_break_and_names_it(tmp_path):
    mar = [row("2026-03-03", "SHOP", 0, 100, 900)]
    by = {"jan.csv": result(JAN), "mar.csv": result(mar)}
    got = read_statements(Stub(by), folder(tmp_path, by), allow_balance_break=True)
    assert len(got.rows) == 3
    assert any(w.startswith("BALANCE BREAK (continued by your choice)")
               and "jan.csv" in w and "mar.csv" in w for w in got.warnings)


def test_the_override_is_off_by_default():
    import inspect
    sig = inspect.signature(read_statements)
    assert sig.parameters["allow_balance_break"].default is False


def test_overlapping_statements_keep_a_shared_row_once_and_warn(tmp_path):
    a = JAN + FEB[:1]                       # ... 1,000 on 02-03
    b = FEB                                 # starts with the same 02-03 row
    by = {"a.csv": result(a), "b.csv": result(b)}
    got = read_statements(Stub(by), folder(tmp_path, by))
    assert [r["Description"] for r in got.rows] == ["SALARY", "RENT", "SHOP", "REFUND"]
    assert any("Overlapping statements: 1 identical row(s) of `b.csv`" in w
               and "`a.csv`" in w for w in got.warnings)
    assert got.balance_check.ok


def test_two_identical_rows_inside_one_statement_are_both_kept(tmp_path):
    """NEGATIVE: only a cross-file overlap is dropped, never a repeat inside
    a single statement (two genuine identical payments on one day)."""
    twice = [row("2026-01-05", "SALARY", 1000, 0, 1500),
             row("2026-01-20", "COFFEE", 0, 10, 1490),
             row("2026-01-20", "COFFEE", 0, 10, 1490)]
    nxt = [row("2026-02-03", "SHOP", 0, 100, 1390)]
    by = {"a.csv": result(twice), "b.csv": result(nxt)}
    got = read_statements(Stub(by), folder(tmp_path, by), allow_balance_break=True)
    assert [r["Description"] for r in got.rows].count("COFFEE") == 2
    assert not any("Overlapping" in w for w in got.warnings)


def test_a_statement_alone_keeps_its_identical_rows(tmp_path):
    twice = [row("2026-01-20", "COFFEE", 0, 10, 1490)] * 2
    p = tmp_path / "a.csv"
    p.write_text("x")
    got = read_statements(Stub({"a.csv": result(twice)}), p)
    assert len(got.rows) == 2


def test_statements_of_another_account_are_skipped_and_listed_by_last_4(tmp_path):
    by = {"mine_jan.csv": result(JAN, "11112222"),
          "mine_feb.csv": result(FEB, "11112222"),
          "other.csv": result([row("2026-01-09", "X", 5, 0, 5)], "99998888")}
    got = read_statements(Stub(by), folder(tmp_path, by), selected_account="11112222")
    assert len(got.rows) == 4
    assert any("another account (...8888)" in w and "other.csv" in w for w in got.warnings)


def test_two_accounts_and_no_choice_stops_listing_both(tmp_path):
    by = {"a.csv": result(JAN, "11112222"), "b.csv": result(FEB, "99998888")}
    with pytest.raises(StatementStop) as exc:
        read_statements(Stub(by), folder(tmp_path, by))
    assert "...2222" in str(exc.value) and "...8888" in str(exc.value)
    assert "11112222" not in str(exc.value)          # never the full number


def test_the_resolver_can_pick_the_account(tmp_path):
    by = {"a.csv": result(JAN, "11112222"), "b.csv": result(FEB, "99998888")}
    got = read_statements(Stub(by), folder(tmp_path, by),
                          account_resolver=lambda numbers: "99998888")
    assert [r["Description"] for r in got.rows] == ["SHOP", "REFUND"]


def test_a_selected_account_that_matches_no_statement_stops(tmp_path):
    by = {"a.csv": result(JAN, "11112222"), "b.csv": result(FEB, "11112222")}
    with pytest.raises(StatementStop):
        read_statements(Stub(by), folder(tmp_path, by), selected_account="55556666")


def test_an_unreadable_account_number_stops_naming_the_file(tmp_path):
    by = {"a.csv": result(JAN, "11112222"), "b.csv": result(FEB, None)}
    with pytest.raises(StatementStop) as exc:
        read_statements(Stub(by), folder(tmp_path, by))
    assert "b.csv" in str(exc.value) and "a.csv" not in str(exc.value)


def test_an_unreadable_file_stops_naming_it_and_joins_nothing(tmp_path):
    by = {"a.csv": result(JAN), "b.csv": ValueError("not a statement")}
    with pytest.raises(StatementStop) as exc:
        read_statements(Stub(by), folder(tmp_path, by))
    assert "`b.csv`" in str(exc.value) and "not a statement" in str(exc.value)


def test_a_wrong_password_stops_naming_the_file(tmp_path):
    by = {"a.csv": result(JAN), "b.csv": ValueError("Wrong password for this file")}
    with pytest.raises(StatementStop) as exc:
        read_statements(Stub(by), folder(tmp_path, by), password="secret-pw")
    assert "`b.csv`" in str(exc.value) and "secret-pw" not in str(exc.value)


def test_files_of_other_types_in_a_folder_are_named_in_a_warning(tmp_path):
    by = {"a.csv": result(JAN), "b.csv": result(FEB)}
    d = folder(tmp_path, by, extra=("notes.txt", "scan.png"))
    got = read_statements(Stub(by), d)
    assert any("notes.txt" in w and "scan.png" in w and "not read" in w for w in got.warnings)
    assert len(got.rows) == 4


def test_a_later_statements_carry_forward_row_is_dropped(tmp_path):
    class HStub(Stub):
        @staticmethod
        def is_carry_forward_row(r):
            return r["Description"] == "BALANCE BROUGHT FORWARD"

    bf = row("2026-02-01", "BALANCE BROUGHT FORWARD", 0, 0, 1100)
    by = {"a.csv": result(JAN), "b.csv": result([bf] + FEB)}
    got = read_statements(HStub(by), folder(tmp_path, by))
    assert [r["Description"] for r in got.rows].count("BALANCE BROUGHT FORWARD") == 0
    assert got.balance_check.ok


def test_a_missing_path_is_a_file_not_found(tmp_path):
    with pytest.raises(FileNotFoundError):
        read_statements(Stub({}), tmp_path / "nothing.csv")
    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(FileNotFoundError):
        read_statements(Stub({}), empty)


# ---------------------------------------------------------------------------
# Part B: real readers on synthetic PDFs
# ---------------------------------------------------------------------------

import bob_fixture_gen  # noqa: E402
import kotak_fixture_gen  # noqa: E402

_B_JAN = [("15-01-2025", "NEFT SALARY CREDIT-SYNCO", "", "", "50,000.00", "1,50,000.00Cr")]
_B_FEB = [("10-02-2025", "UPI-GROCERY STORE-SYN", "", "2,000.00", "", "1,48,000.00Cr")]
_B_MAR = [("05-03-2025", "ACH D-BD-SYNTH MF-SIP0001", "", "5,000.00", "", "1,43,000.00Cr")]
_JAN_ARGS = (_B_JAN, "01-01-2025", "31-01-2025")
_FEB_ARGS = (_B_FEB, "01-02-2025", "28-02-2025")
_MAR_ARGS = (_B_MAR, "01-03-2025", "31-03-2025")


def _bob(tmp_path, files):
    d = tmp_path / "bob"
    d.mkdir()
    for name, (args, acct) in files.items():
        (d / name).write_bytes(bob_fixture_gen.build_pdf_for(*args, account_number=acct))
    return d


def test_bob_joins_two_consecutive_statements(tmp_path):
    from agents.skill_bob.agent import BoBSkill
    d = _bob(tmp_path, {"jan.pdf": (_JAN_ARGS, "1234567890"),
                        "feb.pdf": (_FEB_ARGS, "1234567890")})
    got = BoBSkill().parse(d)
    assert [r["Date"] for r in got.rows] == ["2025-01-15", "2025-02-10"]
    assert got.balance_check.ok


def test_bob_stops_on_a_missing_month_and_the_switch_joins_it(tmp_path):
    from agents.skill_bob.agent import BoBSkill
    d = _bob(tmp_path, {"jan.pdf": (_JAN_ARGS, "1234567890"),
                        "mar.pdf": (_MAR_ARGS, "1234567890")})
    with pytest.raises(StatementStop) as exc:
        BoBSkill().parse(d)
    assert "jan.pdf" in str(exc.value) and "mar.pdf" in str(exc.value)
    got = BoBSkill().parse(d, allow_balance_break=True)
    assert len(got.rows) == 2


def test_bob_file_and_one_file_folder_give_identical_rows(tmp_path):
    from agents.skill_bob.agent import BoBSkill
    d = _bob(tmp_path, {"jan.pdf": (_JAN_ARGS, "1234567890")})
    assert BoBSkill().parse(d / "jan.pdf").rows == BoBSkill().parse(d).rows


def test_bob_skips_the_statement_of_another_account(tmp_path):
    from agents.skill_bob.agent import BoBSkill
    d = _bob(tmp_path, {"jan.pdf": (_JAN_ARGS, "1234567890"),
                        "feb.pdf": (_FEB_ARGS, "1234567890"),
                        "other.pdf": (_JAN_ARGS, "5555566666")})
    got = BoBSkill().parse(d, selected_account="1234567890")
    assert len(got.rows) == 2
    assert any("another account (...6666)" in w and "other.pdf" in w for w in got.warnings)
    with pytest.raises(StatementStop):
        BoBSkill().parse(d)                           # two accounts, none chosen


def test_kotak_reads_a_folder_and_a_list_through_the_shared_rule(tmp_path):
    from agents.skill_kotak.agent import KotakSkill
    d = tmp_path / "kotak"
    d.mkdir()
    (d / "jun.pdf").write_bytes(kotak_fixture_gen.build_pdf())
    one = KotakSkill().parse(d / "jun.pdf")
    assert KotakSkill().parse(d).rows == one.rows
    assert KotakSkill().parse([str(d / "jun.pdf")]).rows == one.rows
    assert len(one.rows) > 0


def test_kotak_a_duplicated_statement_is_kept_once(tmp_path):
    from agents.skill_kotak.agent import KotakSkill
    d = tmp_path / "kotak"
    d.mkdir()
    (d / "jun.pdf").write_bytes(kotak_fixture_gen.build_pdf(layout="real"))
    (d / "jun_again.pdf").write_bytes(kotak_fixture_gen.build_pdf(layout="real"))
    one = KotakSkill().parse(d / "jun.pdf")
    got = KotakSkill().parse(d)
    assert got.rows == one.rows
    assert any("Overlapping statements" in w for w in got.warnings)


# ---------------------------------------------------------------------------
# Part C: Convert to GnuCash
# ---------------------------------------------------------------------------

from agents.skill_gnucash_pipeline.agent import run as pipeline_run  # noqa: E402
import test_pipeline_bank_warnings_surfaced as _pw  # noqa: E402


def _pipe(tmp_path, bank, files, **kw):
    out = tmp_path / "out.csv"
    res = pipeline_run(bank=bank, statement_files=files,
                       gnucash_file=_pw._empty_book(tmp_path),
                       output_path=str(out), config_path=None, **kw)
    return res, out


def test_pipeline_stops_on_a_balance_break_and_writes_nothing(tmp_path):
    d = _bob(tmp_path, {"jan.pdf": (_JAN_ARGS, "1234567890"),
                        "mar.pdf": (_MAR_ARGS, "1234567890")})
    res, out = _pipe(tmp_path, "Bank of Baroda", str(d))
    assert "break in the running balance" in res
    assert "jan.pdf" in res and "mar.pdf" in res
    assert not out.exists()


def test_pipeline_override_continues_and_names_the_break(tmp_path):
    d = _bob(tmp_path, {"jan.pdf": (_JAN_ARGS, "1234567890"),
                        "mar.pdf": (_MAR_ARGS, "1234567890")})
    res, out = _pipe(tmp_path, "Bank of Baroda", str(d), allow_balance_break=True)
    assert not res.startswith("## Bank of Baroda → break in the running balance")
    assert "BALANCE BREAK (continued by your choice)" in res


def test_pipeline_two_accounts_and_an_empty_box_asks_for_the_box(tmp_path):
    d = _bob(tmp_path, {"a.pdf": (_JAN_ARGS, "1234567890"),
                        "b.pdf": (_FEB_ARGS, "5555566666")})
    res, out = _pipe(tmp_path, "Bank of Baroda", str(d))
    assert "more than one account" in res
    assert "...7890" in res and "...6666" in res and "Bank account box" in res
    assert not out.exists()


def test_pipeline_passes_the_bank_account_box_to_the_reader_as_digits(tmp_path, monkeypatch):
    """The box choice reaches the reader as the account to keep; the reader
    (tested above) then skips the other account's file."""
    from agents.skill_bob.agent import BoBSkill
    import agents.skill_gnucash_pipeline.agent as pipe
    seen = {}
    real = BoBSkill()

    class Spy:
        def parse(self, path, password=None, **kw):
            seen.update(kw)
            return real.parse(path, password=password, **kw)

    monkeypatch.setattr(pipe, "load_bank_skill", lambda info: Spy())
    d = _bob(tmp_path, {"a.pdf": (_JAN_ARGS, "1234567890"),
                        "b.pdf": (_FEB_ARGS, "5555566666")})
    res, out = _pipe(tmp_path, "Bank of Baroda", str(d),
                     bank_account="Assets:Bank of Baroda 1234567890")
    assert seen["selected_account"] == "1234567890"
    assert seen["allow_balance_break"] is False
    assert "more than one account" not in res


def test_pipeline_reads_kotak_end_to_end(tmp_path):
    from agents import banks
    name = banks.get("kotak").display_name
    d = tmp_path / "kotak"
    d.mkdir()
    (d / "jun.pdf").write_bytes(kotak_fixture_gen.build_pdf(layout="real"))
    res, out = _pipe(tmp_path, name, str(d))
    assert "Unknown bank" not in res and "extraction error" not in res
    assert "Step 1" in res


def test_pipeline_no_longer_branches_on_bank_display_names():
    """Guard: the per-bank reshaping branches are gone from run()."""
    import inspect
    from agents.skill_gnucash_pipeline import agent as pipe
    src = inspect.getsource(pipe.run)
    # (the ICICI progress-step number further down is not input shaping)
    for needle in ('bank == "HDFC"', 'bank == "SBM"',
                   'bank == "HSBC"', 'bank == "Bank of Baroda"', "_resolve_hsbc_input"):
        assert needle not in src, needle
    assert not hasattr(pipe, "_resolve_hsbc_input")
