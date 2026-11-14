"""
BNK-10 -- Convert to GnuCash must not import a statement into a book whose
account it does not belong to.

(a) One same-bank candidate whose name carries a DIFFERENT account number:
    stop, write nothing. A candidate with no number keeps the old fallback
    (plus a hint).
(b) On a stop (or when no account of the bank exists here), name the other
    entity's folder when that entity's book for the same year holds the number.
(c) When the number matches this book AND another entity's book holds it too,
    report it (RED FLAG: one account in two books is a double booking).

Everything here is synthetic: tiny books, obviously fake numbers, a throwaway
entities.yaml. The lookup is read-only and never raises.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (SRC, SRC / "agents", ROOT / "tests"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.skill_gnucash_pipeline import agent as pipe  # noqa: E402
import gnc_book_fixture as fx  # noqa: E402

PAN_A = "ABCDE1234X"
PAN_B = "ABCDE5678Y"
NUM_A = "100000000001"   # held by entity Alpha's book
NUM_B = "200000000002"   # held by entity Beta's book


def _book(path: Path, bank_accounts, extra=()) -> str:
    accs = [
        fx.account_xml("root", "Root Account", "ROOT", None),
        fx.account_xml("assets", "Assets", "ASSET", "root", ["placeholder"]),
    ]
    accs += [fx.account_xml(aid, name, typ, "assets", flags)
             for aid, name, typ, flags in bank_accounts]
    accs += list(extra)
    return fx.write_book(path, accs)


def _entities(path: Path, books: dict) -> Path:
    """books: {key: (name, pan, {fy: bookpath})}"""
    lines = []
    for key, (name, pan, bk) in books.items():
        lines += [f"{key}:", f"  name: {name}", f"  pan: {pan}", "  status: Individual"]
        if bk:
            lines.append("  books:")
            lines += [f"    {fy}: {Path(p).as_posix()}" for fy, p in bk.items()]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def _digest(p: Path) -> str:
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


@pytest.fixture()
def world(tmp_path):
    """Alpha's book holds NUM_A; Beta's book holds NUM_B; same FY."""
    a_dir = tmp_path / "AlphaFolder"
    b_dir = tmp_path / "BetaFolder"
    a_dir.mkdir()
    b_dir.mkdir()
    alpha = _book(a_dir / "alpha.gnucash",
                  [("a1", f"TESTBANK - {NUM_A}", "BANK", [])])
    beta = _book(b_dir / "beta.gnucash",
                 [("b1", f"TESTBANK - {NUM_B}", "BANK", [])])
    ent = _entities(tmp_path / "entities.yaml", {
        "Alpha": ("Alpha Test", PAN_A, {"2025-26": alpha}),
        "Beta": ("Beta Test", PAN_B, {"2025-26": beta}),
    })
    return {"alpha": alpha, "beta": beta, "ent": ent, "tmp": tmp_path,
            "a_dir": a_dir, "b_dir": b_dir}


def _bal(book, number, ent, **kw):
    return pipe._get_gnucash_account_balance(
        book, "TESTBANK", number, entities_path=ent, **kw)


# ---------------------------------------------------------------- (a)

def test_matching_number_imports_as_today(world):
    # NEGATIVE: a matching number is not stopped and carries no warning
    res = _bal(world["alpha"], NUM_A, world["ent"])
    assert res["found"] is True and res["wrong_book"] is False
    assert res["match_warning"] is None
    assert res["double_booking"] == []


def test_masked_number_still_fits_like_before(world):
    # NEGATIVE: the existing last-digits rule still matches (no second rule)
    res = _bal(world["alpha"], "0001", world["ent"])
    assert res["found"] is True and res["wrong_book"] is False


def test_candidate_without_number_keeps_fallback_and_adds_hint(tmp_path):
    # NEGATIVE: the no-number candidate is NOT stopped
    book = _book(tmp_path / "b.gnucash", [("x", "TESTBANK Savings", "BANK", [])])
    ent = _entities(tmp_path / "e.yaml", {})
    res = _bal(book, NUM_A, ent)
    assert res["found"] is True and res["wrong_book"] is False
    assert "Verify this is the correct account" in res["match_warning"]
    assert "Put the account number in the GnuCash account name" in res["match_warning"]


def test_candidate_with_different_number_stops(world):
    res = _bal(world["alpha"], NUM_B, world["ent"])
    assert res["found"] is False and res["wrong_book"] is True
    assert "different account number" in res["match_warning"]
    assert "Nothing was imported" in res["match_warning"]


def test_different_number_never_produces_an_import_file(world):
    # NEGATIVE: the reconcile step stops, so no output is built from it
    rows = [{"Date": "2025-06-01", "Description": "NEFT", "Deposit": "500.00",
             "Withdrawal": "", "Balance": "1500.00", "Currency": "INR"}]
    rec = pipe._reconcile_opening_balance(
        rows, world["alpha"], "TESTBANK", NUM_B, entities_path=world["ent"])
    assert rec.get("stop") is True and rec.get("wrong_book") is True
    assert rec["ok"] is False and rec["rows_skipped"] == 0
    assert list(world["tmp"].glob("**/*.csv")) == []


def test_explicit_pick_is_not_second_guessed(world):
    # NEGATIVE: the user's own bank_account choice bypasses the number guard
    res = _bal(world["alpha"], NUM_B, world["ent"],
               chosen_account=f"Assets:TESTBANK - {NUM_A}")
    assert res["found"] is True and res["wrong_book"] is False


# ---------------------------------------------------------------- (b)

def test_stop_names_the_other_entitys_folder(world):
    res = _bal(world["alpha"], NUM_B, world["ent"])
    msg = res["match_warning"]
    assert "Beta Test" in msg and "BetaFolder" in msg
    assert "AlphaFolder" not in msg


def test_number_nobody_holds_names_no_folder(world):
    # NEGATIVE: no false attribution when no other book holds the number
    res = _bal(world["alpha"], "300000000003", world["ent"])
    assert res["wrong_book"] is True
    assert "BetaFolder" not in res["match_warning"]


def test_number_held_elsewhere_never_imports_here_when_bank_absent(world):
    # NEGATIVE: this book has no TESTBANK account at all; Beta holds the number
    other = _book(world["tmp"] / "AlphaFolder" / "alpha2.gnucash",
                  [("z", "OTHERBANK - 777", "BANK", [])])
    ent = _entities(world["tmp"] / "e2.yaml", {
        "Alpha": ("Alpha Test", PAN_A, {"2025-26": other}),
        "Beta": ("Beta Test", PAN_B, {"2025-26": world["beta"]}),
    })
    res = _bal(other, NUM_B, ent)
    assert res["found"] is False and res["wrong_book"] is True
    assert "BetaFolder" in res["match_warning"]


def test_bank_absent_and_number_not_held_elsewhere_is_unchanged(world):
    # NEGATIVE: today's behaviour (not found, no stop) is kept
    other = _book(world["tmp"] / "AlphaFolder" / "alpha3.gnucash",
                  [("z", "OTHERBANK - 777", "BANK", [])])
    ent = _entities(world["tmp"] / "e3.yaml", {
        "Alpha": ("Alpha Test", PAN_A, {"2025-26": other}),
        "Beta": ("Beta Test", PAN_B, {"2025-26": world["beta"]}),
    })
    res = _bal(other, "300000000003", ent)
    assert res["found"] is False and res["wrong_book"] is False


def test_several_candidates_none_matching_and_nobody_holds_it_is_unchanged(tmp_path):
    # NEGATIVE: with no other holder, today's not-found result (no stop) is kept
    book = _book(tmp_path / "m.gnucash", [
        ("m1", "TESTBANK - 111", "BANK", []), ("m2", "TESTBANK - 222", "BANK", [])])
    ent = _entities(tmp_path / "e.yaml", {"Alpha": ("Alpha Test", PAN_A, {"2025-26": book})})
    res = _bal(book, NUM_B, ent)
    assert res["found"] is False and res["wrong_book"] is False
    assert res["match_warning"] is not None


def test_several_candidates_none_matching_stops_and_names_folder(tmp_path):
    book = _book(tmp_path / "m.gnucash", [
        ("m1", "TESTBANK - 111", "BANK", []), ("m2", "TESTBANK - 222", "BANK", [])])
    bdir = tmp_path / "BetaFolder"
    bdir.mkdir()
    beta = _book(bdir / "b.gnucash", [("b1", f"TESTBANK - {NUM_B}", "BANK", [])])
    ent = _entities(tmp_path / "e.yaml", {
        "Alpha": ("Alpha Test", PAN_A, {"2025-26": book}),
        "Beta": ("Beta Test", PAN_B, {"2025-26": beta}),
    })
    res = _bal(book, NUM_B, ent)
    assert res["wrong_book"] is True and "BetaFolder" in res["match_warning"]


def test_other_year_book_is_not_consulted(world):
    # NEGATIVE: Beta only has a different year registered -> not a holder
    ent = _entities(world["tmp"] / "e4.yaml", {
        "Alpha": ("Alpha Test", PAN_A, {"2025-26": world["alpha"]}),
        "Beta": ("Beta Test", PAN_B, {"2024-25": world["beta"]}),
    })
    res = _bal(world["alpha"], NUM_B, ent)
    assert res["wrong_book"] is True
    assert "BetaFolder" not in res["match_warning"]


def test_lookup_leaves_every_book_untouched_and_makes_no_lock(world):
    # NEGATIVE: nothing written, no .LCK, no backup or log files appear
    before = {p: _digest(p) for p in (Path(world["alpha"]), Path(world["beta"]))}
    names = sorted(p.name for p in world["tmp"].rglob("*"))
    _bal(world["alpha"], NUM_B, world["ent"])
    _bal(world["alpha"], NUM_A, world["ent"])
    assert {p: _digest(p) for p in before} == before
    assert sorted(p.name for p in world["tmp"].rglob("*")) == names
    assert not list(world["tmp"].rglob("*.LCK"))


def test_missing_other_book_is_skipped_and_does_not_crash(world):
    # NEGATIVE: a missing registered book never turns into a crash
    Path(world["beta"]).unlink()  # a fixture file this test created
    res = _bal(world["alpha"], NUM_B, world["ent"])
    assert res["wrong_book"] is True
    assert "missing; skipped" in res["match_warning"]


def test_unreadable_other_book_is_skipped_and_does_not_crash(world):
    # NEGATIVE: garbage bytes do not crash, and a correct import still goes through
    Path(world["beta"]).write_bytes(b"not a book")
    res = _bal(world["alpha"], NUM_A, world["ent"])
    assert res["found"] is True and res["wrong_book"] is False
    assert res["double_booking"] == []
    assert any("could not be read" in n for n in res["other_book_notes"])


def test_malformed_entities_file_does_not_crash(world):
    # NEGATIVE
    bad = world["tmp"] / "bad.yaml"
    bad.write_text(":::\n  - [", encoding="utf-8")
    res = _bal(world["alpha"], NUM_A, bad)
    assert res["found"] is True and res["double_booking"] == []


def _hidden_world(tmp_path, flags, parent_flags=()):
    adir, bdir = tmp_path / "AlphaFolder", tmp_path / "BetaFolder"
    adir.mkdir()
    bdir.mkdir()
    alpha = _book(adir / "a.gnucash", [("a1", f"TESTBANK - {NUM_A}", "BANK", [])])
    beta_accs = [
        fx.account_xml("root", "Root Account", "ROOT", None),
        fx.account_xml("assets", "Assets", "ASSET", "root", list(parent_flags)),
        fx.account_xml("b1", f"TESTBANK - {NUM_A}", "BANK", "assets", list(flags)),
    ]
    beta = fx.write_book(bdir / "b.gnucash", beta_accs)
    ent = _entities(tmp_path / "e.yaml", {
        "Alpha": ("Alpha Test", PAN_A, {"2025-26": alpha}),
        "Beta": ("Beta Test", PAN_B, {"2025-26": beta}),
    })
    return alpha, ent


@pytest.mark.parametrize("flags,parent_flags", [
    (["hidden"], []),
    (["placeholder"], []),
    ([], ["hidden"]),
])
def test_hidden_or_placeholder_in_other_book_is_never_a_holder(tmp_path, flags, parent_flags):
    # NEGATIVE: hidden, placeholder and hidden-by-parent accounts do not count
    alpha, ent = _hidden_world(tmp_path, flags, parent_flags)
    res = _bal(alpha, NUM_A, ent)
    assert res["found"] is True and res["double_booking"] == []


def test_non_bank_account_in_other_book_is_never_a_holder(tmp_path):
    # NEGATIVE: an ASSET-type (fixed deposit, fund) carrying the number is not a bank account
    adir, bdir = tmp_path / "AlphaFolder", tmp_path / "BetaFolder"
    adir.mkdir()
    bdir.mkdir()
    alpha = _book(adir / "a.gnucash", [("a1", f"TESTBANK - {NUM_A}", "BANK", [])])
    beta = _book(bdir / "b.gnucash", [("b1", f"TESTBANK FD {NUM_A}", "ASSET", [])])
    ent = _entities(tmp_path / "e.yaml", {
        "Alpha": ("Alpha Test", PAN_A, {"2025-26": alpha}),
        "Beta": ("Beta Test", PAN_B, {"2025-26": beta}),
    })
    assert _bal(alpha, NUM_A, ent)["double_booking"] == []


# ---------------------------------------------------------------- (c)

def test_same_number_in_two_books_is_flagged_without_changing_the_import(tmp_path):
    adir, bdir = tmp_path / "AlphaFolder", tmp_path / "BetaFolder"
    adir.mkdir()
    bdir.mkdir()
    alpha = _book(adir / "a.gnucash", [("a1", f"TESTBANK - {NUM_A}", "BANK", [])])
    beta = _book(bdir / "b.gnucash", [("b1", f"TESTBANK - {NUM_A}", "BANK", [])])
    ent = _entities(tmp_path / "e.yaml", {
        "Alpha": ("Alpha Test", PAN_A, {"2025-26": alpha}),
        "Beta": ("Beta Test", PAN_B, {"2025-26": beta}),
    })
    res = _bal(alpha, NUM_A, ent)
    assert res["found"] is True and res["wrong_book"] is False
    assert res["account_name"].endswith(f"TESTBANK - {NUM_A}")
    assert len(res["double_booking"]) == 1 and "BetaFolder" in res["double_booking"][0]


def test_unique_number_has_no_red_flag(world):
    # NEGATIVE
    assert _bal(world["alpha"], NUM_A, world["ent"])["double_booking"] == []


def test_unregistered_book_does_no_lookup(world):
    # NEGATIVE: a book not in the registry has no year to compare, so no flag, no stop
    loose = _book(world["tmp"] / "loose.gnucash",
                  [("l1", f"TESTBANK - {NUM_A}", "BANK", [])])
    res = _bal(loose, NUM_A, world["ent"])
    assert res["found"] is True and res["double_booking"] == []
