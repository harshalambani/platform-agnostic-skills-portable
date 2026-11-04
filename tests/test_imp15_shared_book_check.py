"""IMP-15 step 4a: the shared already-in-book rule (S.1 = B), unit level."""
from datetime import date

from agents import book_check as bc

D1, D2, D3 = date(2026, 4, 1), date(2026, 4, 2), date(2026, 4, 5)
BANK, EXP, INC = "Assets:Bank", "Expenses:Fees", "Income:Misc"


def bt(i, num, d, legs, **kw):
    return bc.BookTxn(id=i, num=num, date=d, legs=tuple(legs), **kw)


def en(num, d, legs, **kw):
    return bc.Entry(num=num, date=d, legs=tuple(legs), **kw)


L = [(BANK, -100), (EXP, 100)]


def kinds(entries, txns, **kw):
    return [v.kind for v in bc.check_all(entries, txns, **kw)]


def test_definite_same_num_same_amounts():
    v = bc.check_all([en("N1", D1, L)], [bt("b1", "N1", D1, L)])[0]
    assert v.kind == bc.DEFINITE and v.book.id == "b1"


def test_mismatch_same_num_different_amounts():
    v = bc.check_all([en("N1", D1, L)], [bt("b1", "N1", D1, [(BANK, -90), (EXP, 90)])])[0]
    assert v.kind == bc.MISMATCH


def test_probable_no_num_same_date_amounts_accounts():
    v = bc.check_all([en("N1", D1, L)], [bt("b1", "", D1, L)])[0]
    assert v.kind == bc.PROBABLE and v.book.id == "b1"


def test_possible_needs_opt_in_and_other_accounts():
    other = [(BANK, -100), (INC, 100)]
    txns = [bt("b1", "", D1, other)]
    assert kinds([en("N1", D1, L)], txns) == [bc.NOT_POSTED]
    opts = bc.Options(possible_amount_only=True)
    assert kinds([en("N1", D1, L)], txns, options=opts) == [bc.POSSIBLE]


def test_date_window():
    txns = [bt("b1", "", D2, L)]
    assert kinds([en("N1", D1, L)], txns) == [bc.NOT_POSTED]
    assert kinds([en("N1", D1, L)], txns, options=bc.Options(window_days=1)) == [bc.PROBABLE]


def test_tie_is_never_auto_picked_and_claims_nothing():
    txns = [bt("b1", "", D1, L), bt("b2", "", D1, L)]
    v = bc.check_all([en("N1", D1, L)], txns)[0]
    assert v.kind == bc.TIE and {t.id for t in v.candidates} == {"b1", "b2"} and v.book is None


def test_nearest_date_wins_inside_window():
    txns = [bt("far", "", D3, L), bt("near", "", D2, L)]
    v = bc.check_all([en("N1", D1, L)], txns, options=bc.Options(window_days=7))[0]
    assert v.kind == bc.PROBABLE and v.book.id == "near"


def test_one_to_one_second_look_alike_is_not_cleared():
    txns = [bt("b1", "", D1, L)]
    out = kinds([en("N1", D1, L), en("N2", D1, L)], txns)
    assert out == [bc.PROBABLE, bc.NOT_POSTED]


def test_num_claims_first_across_all_entries():
    # entry A (no Num hit) is listed first and looks like b1, but b1 belongs
    # to entry B by Num: A must not steal it.
    txns = [bt("b1", "NB", D1, L)]
    a, b = en("NA", D1, L), en("NB", D1, L)
    out = kinds([a, b], txns)
    assert out == [bc.NOT_POSTED, bc.DEFINITE]


def test_reserved_num_not_taken_by_fallback_even_if_unclaimed():
    txns = [bt("b1", "NB", D1, L), bt("b2", "NB", D1, L)]
    out = kinds([en("NA", D1, L), en("NB", D1, L)], txns)
    assert out[0] == bc.NOT_POSTED and out[1] == bc.DEFINITE
    out2 = kinds([en("NA", D1, L), en("NB", D1, L)], txns, reserve=False)
    assert out2[0] == bc.PROBABLE


def test_ignore_accounts_on_both_sides():
    e = en("N1", D1, [(BANK, -100), (EXP, 100)], ignore=frozenset({BANK}))
    t = bt("b1", "N1", D1, [(BANK, -555), (EXP, 100)])
    assert kinds([e], [t]) == [bc.DEFINITE]


def test_net_per_account_and_subset():
    t = bt("b1", "N1", D1, [(BANK, -60), (BANK, -40), (EXP, 100), (INC, 0)])
    e = en("N1", D1, L)
    assert kinds([e], [t]) == [bc.MISMATCH]
    assert kinds([e], [t], options=bc.Options(net_per_account=True)) == [bc.DEFINITE]
    extra = bt("b1", "N1", D1, L + [(INC, 7)])
    assert kinds([e], [extra], options=bc.Options(net_per_account=True)) == [bc.MISMATCH]
    sub = en("N1", D1, L, subset=True)
    assert kinds([sub], [extra], options=bc.Options(net_per_account=True)) == [bc.DEFINITE]


def test_quantity_must_match_when_given():
    t = bt("b1", "N1", D1, L, qty=((EXP, 5),))
    assert kinds([en("N1", D1, L, qty=((EXP, 5),))], [t]) == [bc.DEFINITE]
    assert kinds([en("N1", D1, L, qty=((EXP, 6),))], [t]) == [bc.MISMATCH]


def test_unreadable_book_is_unchecked_and_everything_is_written():
    es = [en("N1", D1, L), en("N2", D2, L)]
    vs = bc.unchecked_verdicts(es, "could not read")
    part = bc.partition(es, vs)
    assert [v.kind for v in vs] == [bc.UNCHECKED] * 2
    assert part["write"] == es and len(part[bc.UNCHECKED]) == 2
    flags, info = bc.report_lines(part, "could not read")
    assert flags and flags[0].startswith("RED FLAG: NOT CHECKED") and not info


def test_partition_and_report_wording():
    txns = [bt("b1", "N1", D1, L), bt("b2", "N2", D1, [(BANK, -1), (EXP, 1)]),
            bt("b3", "", D1, [(BANK, -7), (EXP, 7)])]
    es = [en("N1", D1, L), en("N2", D1, L), en("N3", D1, [(BANK, -7), (EXP, 7)]),
          en("N4", D3, L)]
    part = bc.partition(es, bc.check_all(es, txns))
    assert [e.num for e in part["write"]] == ["N3", "N4"]
    flags, info = bc.report_lines(part)
    assert len(flags) == 1 and "N2" in flags[0] and flags[0].startswith("RED FLAG")
    assert any("N1" in i and "left out" in i for i in info)
    assert any("N3" in i and "check before import" in i for i in info)
    assert "100" not in " ".join(flags + info)   # Num and date only, never amounts


# --- standing negatives -------------------------------------------------

def test_negative_entry_already_in_book_is_never_offered():
    es = [en("N1", D1, L)]
    part = bc.partition(es, bc.check_all(es, [bt("b1", "N1", D1, L)]))
    assert part["write"] == []


def test_negative_same_amount_on_another_date_is_still_offered():
    es = [en("N1", D1, L)]
    part = bc.partition(es, bc.check_all(es, [bt("b1", "", D3, L)]))
    assert part["write"] == es and not part[bc.PROBABLE]


def test_negative_mismatch_is_never_written():
    es = [en("N1", D1, L)]
    part = bc.partition(es, bc.check_all(es, [bt("b1", "N1", D1, [(BANK, -1), (EXP, 1)])]))
    assert part["write"] == [] and len(part[bc.MISMATCH]) == 1
