"""
IMP-20: KRC demat-charge Nums come from the charge's own data (date + amount),
not from its position in the ledger. Synthetic data only.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_imp17_krc_already_in_book as t17  # noqa: E402

bk = t17.bk
base = t17.base
BROKER = base.BROKER


def _row(day, part, debit=None, credit=None):
    return {"date": day, "particulars": part, "debit": debit, "credit": credit,
            "balance": None, "drcr": None, "category": "Demat Charge"}


def _nums(rows):
    return {(r["date"], r["particulars"]): n
            for r, n in zip(bk.demat_rows(rows), bk.charge_nums(bk.demat_rows(rows)))}


def _other(day="2025-06-02"):
    return {"date": day, "particulars": "unrelated", "debit": 10.0, "credit": None,
            "balance": None, "drcr": None, "category": "Bank Pay-In"}


A = _row("2025-06-03", "DP charges one", 50.0)
B = _row("2025-06-10", "DP charges two", 75.5)


def test_num_comes_from_date_and_paise_and_keeps_the_prefix():
    (n,) = bk.charge_nums(bk.demat_rows([A]))
    assert n == "KRC-D20250603-5000"
    (m,) = bk.charge_nums(bk.demat_rows([B]))
    assert m == "KRC-D20250610-7550"
    assert bk.charge_nums(bk.demat_rows([_row("2025-06-04", "refund", None, 12.34)])) == [
        "KRC-D20250604-R1234"]


def test_inserting_or_removing_an_unrelated_row_never_renames_a_charge():
    before = _nums([A, B])
    after_insert = _nums([_other(), A, _row("2025-06-05", "new earlier charge", 5.0), B])
    assert after_insert[(A["date"], A["particulars"])] == before[(A["date"], A["particulars"])]
    assert after_insert[(B["date"], B["particulars"])] == before[(B["date"], B["particulars"])]
    after_remove = _nums([B])
    assert after_remove[(B["date"], B["particulars"])] == before[(B["date"], B["particulars"])]
    # NEGATIVE: the old per-run sequence (D1, D2 ...) is gone
    assert not any(n.startswith("KRC-D") and n[5:].isdigit() and len(n) < 8
                   for n in bk.charge_nums(bk.demat_rows([A, B])))


def test_same_input_gives_same_nums_across_runs_and_row_order():
    rows = [A, B, _row("2025-06-03", "DP charges one", 50.0)]
    one = bk.charge_nums(bk.demat_rows(rows))
    two = bk.charge_nums(bk.demat_rows(list(rows)))
    assert one == two
    # the identical-pair is numbered by occurrence, and the singleton is untouched
    assert one[1] == "KRC-D20250610-7550"
    assert sorted([one[0], one[2]]) == ["KRC-D20250603-5000", "KRC-D20250603-5000-2"]


def test_two_same_day_same_amount_charges_get_distinct_stable_nums():
    x = _row("2025-06-03", "AAA charge", 50.0)
    y = _row("2025-06-03", "ZZZ charge", 50.0)
    n1 = _nums([x, y])
    n2 = _nums([y, _other(), x])                    # different row order and an unrelated row
    assert n1[(x["date"], x["particulars"])] != n1[(y["date"], y["particulars"])]   # distinct
    assert n1 == n2                                  # stable: ordered by content, not position
    assert n1[(x["date"], x["particulars"])] == "KRC-D20250603-5000"
    assert n1[(y["date"], y["particulars"])] == "KRC-D20250603-5000-2"


def test_a_credit_and_a_charge_of_equal_size_do_not_collide():
    nums = bk.charge_nums(bk.demat_rows([_row("2025-06-03", "c", 50.0),
                                         _row("2025-06-03", "r", None, 50.0)]))
    assert len(set(nums)) == 2


def test_build_charges_uses_the_stable_nums():
    got = bk.build_charges([_other(), A, B], BROKER, "Expenses:Demat Charges")
    assert [c["id"] for c in got] == ["KRC-D20250603-5000", "KRC-D20250610-7550"]


# ---- end to end: the IMP-17 book check still works, and no false RED FLAG ----
def test_charge_in_book_is_found_even_after_the_ledger_changes(tmp_path):
    chg = t17._txn("KRC-D20250603-5000", "2025-06-03",
                   [t17._split("ex1", 50.0), t17._split("br", -50.0)])
    # a new unrelated charge now sits BEFORE the booked one: the old D<n> scheme
    # would have renamed the booked charge and raised a false RED FLAG
    ledger = [("2025-06-01", "earlier charge", 20.0, None, 0.0, "Cr", "Demat Charge"),
              ("2025-06-03", "DP charges synthetic", 50.0, None, 0.0, "Cr", "Demat Charge")]
    r, out = t17._run(tmp_path, [], ledger, [chg],
                      extra=("--demat-account", "Expenses:Demat Charges"))
    assert "RED FLAG - IN BOOK, DIFFERENT AMOUNTS" not in r.stdout          # NEGATIVE
    assert "KRC-D20250603-5000" in r.stdout and "Already in the book" in r.stdout
    ids = t17._ids(out, "Charges.csv")
    assert ids == {"KRC-D20250601-2000"}             # only the unbooked one is offered
    assert "KRC-D20250603-5000" not in ids


def test_changed_amount_under_the_same_num_is_still_a_red_flag(tmp_path):
    # IMP-17 behaviour preserved: the book holds this Num with another amount
    chg = t17._txn("KRC-D20250603-5000", "2025-06-03",
                   [t17._split("ex1", 40.0), t17._split("br", -40.0)])
    ledger = [("2025-06-03", "DP charges synthetic", 50.0, None, 0.0, "Cr", "Demat Charge")]
    r, out = t17._run(tmp_path, [], ledger, [chg],
                      extra=("--demat-account", "Expenses:Demat Charges"))
    assert r.stdout.splitlines()[0].startswith("RED FLAG - IN BOOK, DIFFERENT AMOUNTS")
    assert not (out / "Charges.csv").exists()
