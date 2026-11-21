"""
DOC-08 -- where the help quotes a message the user sees, the quoted text must be
in the message the code actually produces (not only somewhere in the source).

Each case builds the real message (synthetic books / files only) and checks, in
both directions, that (a) the help row names the phrase and (b) the message
contains it. A deliberately wrong phrase must be reported missing.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (
    SRC, SRC / "agents", ROOT / "tests", ROOT / "tests" / "skill_kotak",
    SRC / "agents" / "skill_gnucash_intercompany_matrix" / "scripts",
    SRC / "agents" / "skill_gnucash_intercompany" / "scripts",
):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents import registry  # noqa: E402


def _norm(s: str) -> str:
    return " ".join(
        s.replace("—", "-").replace("–", "-").replace("→", "->").split()
    ).casefold()


def _missing(phrases, text):
    """Phrases that are NOT in `text` (empty list == all present)."""
    t = _norm(text)
    return [p for p in phrases if _norm(p) not in t]


def _help_text(skill_name: str) -> str:
    registry.discover(refresh=True)
    s = registry.get(skill_name)          # resolves old names via aliases
    if s is not None:
        h = s.help
        parts = [h.overview, h.tips, h.when_to_use]
        parts += [i.gotchas for i in h.inputs]
        parts += [f"{t.problem} {t.fix}" for t in h.troubleshooting]
        return " ".join(p for p in parts if p)
    raise AssertionError(f"skill {skill_name} not found")


def _check(skill_name: str, phrases, message: str):
    helptext = _help_text(skill_name)
    assert _missing(phrases, helptext) == [], "help does not quote the phrase"
    assert _missing(phrases, message) == [], "the code's message does not contain the quoted phrase"


# --- the checker itself: a wrong quote must fail ---------------------------

def test_a_wrong_quote_is_reported_missing():
    msg = "This upload holds 2 statements and ICICI can only convert one statement per run."
    assert _missing(["can only convert one statement per run"], msg) == []
    assert _missing(["can only convert two statements per run"], msg) == [
        "can only convert two statements per run"]


# --- item 2: Convert to GnuCash ---------------------------------------------

def test_multi_statement_stop_message_matches_the_help():
    from agents.skill_gnucash_pipeline import agent as pipe
    msg = pipe._multi_file_stop("Other Bank (CSV)", ["a.csv", "b.csv"])
    assert msg
    _check("gnucash_pipeline",
           ["more than one statement", "can only convert one statement per run"], msg)


def test_one_statement_does_not_trigger_the_multi_statement_stop():
    from agents.skill_gnucash_pipeline import agent as pipe
    assert pipe._multi_file_stop("Other Bank (CSV)", ["a.csv"]) is None


def test_wrong_book_message_matches_the_help():
    import test_bnk10_account_number_guard as b10
    from agents.skill_gnucash_pipeline import agent as pipe

    tmp = Path(__import__("tempfile").mkdtemp(prefix="doc08-"))
    a_dir = tmp / "AlphaFolder"
    a_dir.mkdir()
    alpha = b10._book(a_dir / "alpha.gnucash",
                      [("a1", f"TESTBANK - {b10.NUM_A}", "BANK", [])])
    ent = b10._entities(tmp / "entities.yaml",
                        {"Alpha": ("Alpha Test", b10.PAN_A, {"2025-26": alpha})})
    res = pipe._get_gnucash_account_balance(alpha, "TESTBANK", b10.NUM_B, entities_path=ent)
    assert res["wrong_book"] is True
    # The live reason, then the wrapper the run adds (a literal in run()).
    assert _missing(["carries a different account number", "Nothing was imported"],
                    res["match_warning"]) == []
    src = Path(pipe.__file__).read_text(encoding="utf-8")
    wrapper = ["does not belong in this book",
               "Nothing was written. Open the right book and run again."]
    assert _missing(wrapper, src) == []
    helptext = _help_text("gnucash_pipeline")
    assert _missing(wrapper + ["carries a different account number"], helptext) == []


# --- item 3: Intercompany matrix ---------------------------------------------

def _bad(tmp_path, stem):
    p = tmp_path / f"{stem}.gnucash"
    p.write_bytes(b"not a gnucash file")
    return str(p)


def test_matrix_unreadable_and_nothing_compared_messages_match_the_help(tmp_path):
    from agents.skill_gnucash_intercompany_matrix import agent as mx
    books = "\n".join([_bad(tmp_path, "AliceRao2526"), _bad(tmp_path, "BobRao2526")])
    msg = mx.run(books, str(tmp_path / "o.xlsx"))
    _check("gnucash_intercompany_matrix",
           ["UNREADABLE PAIR",
            "ERROR: no pair could be compared -- this is NOT a clean run."], msg)


def test_matrix_clean_run_carries_neither_message(tmp_path):
    import test_intercompany_reco_real_functions as t
    from agents.skill_gnucash_intercompany_matrix import agent as mx
    a = t.alice(tmp_path, [t.A_LEND])
    b = t.bob(tmp_path, [t.B_LEND])
    msg = mx.run("\n".join([a, b]), str(tmp_path / "o.xlsx"))
    assert _missing(["UNREADABLE PAIR", "no pair could be compared"], msg) == [
        "UNREADABLE PAIR", "no pair could be compared"]


# --- item 5: Kotak -------------------------------------------------------------

def test_kotak_password_message_matches_the_help(tmp_path):
    import kotak_fixture_gen as fx
    from agents.skill_kotak import agent as ka
    pdf = tmp_path / "enc.pdf"
    pdf.write_bytes(fx.build_pdf(password="SYNPWD1", layout="real"))
    msg = ka.run(str(pdf), str(tmp_path / "o.csv"))
    assert msg.startswith("ERROR:")
    assert "SYNPWD1" not in msg
    _check("Kotak", ["is password-protected - supply the statement password"], msg)


def test_kotak_unprotected_statement_gives_no_password_message(tmp_path):
    import kotak_fixture_gen as fx
    from agents.skill_kotak import agent as ka
    pdf = tmp_path / "plain.pdf"
    pdf.write_bytes(fx.build_pdf(layout="real"))
    msg = ka.run(str(pdf), str(tmp_path / "o.csv"))
    assert _missing(["is password-protected"], msg) == ["is password-protected"]
