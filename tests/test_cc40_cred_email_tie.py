"""CC-40: tie card-payment debits to CRED payment-confirmation emails.

Synthetic data only: invented names, generic issuers, neutral last-four digits and invented amounts.
The email is EVIDENCE: it never creates, changes or doubles a booking."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from agents import cc_cards as cc
from agents.skill_cc_spend_booking import agent as ccsb
from agents.skill_cc_spend_booking import cred_email as CE
from agents.skill_cc_spend_booking import matcher as M
from tests.test_cc_spend_booking import APR, MAY, april_rows, book_pay, go, may_rows, stmt, tie

sys.path.insert(0, str(Path(__file__).resolve().parent))
from msg_fixtures import build_msg  # noqa: E402

_ORIG_RUN = ccsb.run
ENT = {"SYN"}
SENDER = "CRED <noreply@cred.club>"
BULLETS = "••••"
RUPEE = "₹"

CARDS_TEXT = (
    'cards:\n'
    '  - bank: "HDFC"\n    card: "Regalia"\n    last4: ["1111"]\n    holder: ""\n    active: true\n'
    '  - bank: "ICICI"\n    card: "Sapphiro"\n    last4: ["2222"]\n    holder: ""\n    active: true\n')
CARDS = cc.parse_text(CARDS_TEXT, ENT)
REGALIA, SAPPHIRO = "HDFC-Regalia", "ICICI-Sapphiro"


def body(paid="1,900.00", pay="May 11, 2025", issuer="HDFC Bank", last4="1111", bill="3,456.00"):
    lines = ["Asha,", "here is your payment", "confirmation",
             "Your credit card payment was successful in 2.1 seconds",
             f"{issuer}\t{BULLETS} {last4}", "payment details"]
    if paid is not None:
        lines.append(f"amount paid \t{RUPEE}{paid}")
    lines += [f"payment date \t{pay}", f"credited to card \t{pay}", "transaction summary",
              "Order ID: \tAB12CD", "Payment Method: \tUPI", "UTR No.: \tXY99ZZ", "note",
              "Your bank will consider the date as the", "payment date. Thank you.",
              "CRED PROTECT\tActive", "latest statement", "detected\tbill", "amount",
              f"10th October, 2025\t{bill}",
              f"CRED protect detected over {RUPEE}1cr in extra charges"]
    return "\n".join(lines)


def put(folder, name, text=None, sender=SENDER, **kw):
    folder.mkdir(exist_ok=True)
    p = folder / name
    p.write_bytes(build_msg(subject="Payment", sender_name=sender.split("<")[0].strip() or "x",
                            sender_email=sender.split("<")[-1].rstrip(">").strip(),
                            body=text if text is not None else body(**kw)))
    return p


def pay(guid, amount, d, desc="BANK TRANSFER"):
    return M.BookPayment(guid, d, amount, desc, "Assets:Bank", True)


def run_tie(folder, payments, routing=None, narration=frozenset()):
    res = CE.scan(folder)
    return CE.tie(res, payments, routing or {}, CARDS, narration)


# ----------------------------------------------------------------------------- positives

def test_happy_path_is_confirmed(tmp_path):
    put(tmp_path / "e", "a.msg")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12))], {"p1": {REGALIA}})
    assert [t.status for t in res.ties] == ["confirmed"] and len(res.confirmed) == 1
    assert res.basis["p1"] == "email-confirmed"
    assert not res.unmatched_emails and not res.unmatched_debits and not res.ambiguous and not res.held


@pytest.mark.parametrize("text,want", [("1,23,456.50", 123456.50), ("12,345.00", 12345.0), ("999", 999.0),
                                       ("1,00,00,000.00", 10000000.0)])
def test_indian_grouped_amount_parses(text, want):
    em, why = CE.parse_body(body(paid=text))
    assert em is not None and em.amount == want and em.last4 == "1111" and em.pay_date == date(2025, 5, 11)


def test_a_debit_on_the_payment_date_and_on_day_plus_two_tie(tmp_path):
    put(tmp_path / "e", "a.msg")
    for d in (11, 12, 13):
        res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, d))], {"p1": {REGALIA}})
        assert len(res.confirmed) == 1


def test_issuer_free_text_variants_resolve_to_the_card(tmp_path):
    put(tmp_path / "e", "a.msg", issuer="HDFC BANK INDIA LIMITED")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12))], {"p1": {REGALIA}})
    assert len(res.confirmed) == 1


def test_email_with_no_routing_changes_nothing(tmp_path):
    put(tmp_path / "e", "a.msg")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12))], {})
    assert [t.status for t in res.ties] == ["unrouted"] and not res.confirmed and not res.held


# ----------------------------------------------------------------------------- negatives

def test_neg1_one_email_never_ties_to_two_debits(tmp_path):
    put(tmp_path / "e", "a.msg")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12)), pay("p2", 1900.0, date(2025, 5, 13))],
                  {"p1": {REGALIA}, "p2": {REGALIA}})
    assert not res.ties and not res.confirmed
    assert len(res.ambiguous) == 1 and len(res.ambiguous[0][1]) == 2


def test_neg1b_two_emails_never_tie_to_one_debit(tmp_path):
    put(tmp_path / "e", "a.msg")
    put(tmp_path / "e", "b.msg")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12))], {"p1": {REGALIA}})
    assert not res.ties and len(res.ambiguous) == 1 and len(res.ambiguous[0][0]) == 2


def test_neg2_unmatched_email_and_unmatched_debit_are_listed_not_dropped(tmp_path):
    put(tmp_path / "e", "a.msg", paid="500.00")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12))], {"p1": {REGALIA}})
    assert [e.name for e, _ in res.unmatched_emails] == ["a.msg"]
    assert [p.guid for p in res.unmatched_debits] == ["p1"]
    assert not res.confirmed


def test_neg2b_unreadable_email_is_listed(tmp_path):
    put(tmp_path / "e", "nopaid.msg", paid=None)
    put(tmp_path / "e", "nolast4.msg", text=body().replace(f"{BULLETS} 1111", ""))
    res = CE.scan(tmp_path / "e")
    assert sorted(n for n, _ in res.unreadable) == ["nolast4.msg", "nopaid.msg"] and not res.emails


def test_neg6_day_plus_three_and_day_minus_one_do_not_tie(tmp_path):
    put(tmp_path / "e", "a.msg")
    for d in (10, 14):
        res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, d))], {"p1": {REGALIA}})
        assert not res.ties and [e.name for e, _ in res.unmatched_emails] == ["a.msg"]
        assert [p.guid for p in res.unmatched_debits] == ["p1"]


def test_neg7_same_amount_twice_in_the_window_is_ambiguous(tmp_path):
    put(tmp_path / "e", "a.msg")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 11)), pay("p2", 1900.0, date(2025, 5, 12))],
                  {"p1": {REGALIA}, "p2": {REGALIA}})
    assert not res.ties and len(res.ambiguous) == 1
    assert {p.guid for p in res.ambiguous[0][1]} == {"p1", "p2"}


def test_neg8_the_bill_amount_is_never_taken_as_the_paid_amount(tmp_path):
    put(tmp_path / "e", "a.msg", paid="1,900.00", bill="3,456.00")
    res = run_tie(tmp_path / "e", [pay("p1", 3456.0, date(2025, 5, 12))], {"p1": {REGALIA}})
    assert not res.ties and not res.confirmed
    assert [p.guid for p in res.unmatched_debits] == ["p1"]
    em, _ = CE.parse_body(body(paid="1,900.00", bill="3,456.00"))
    assert em.amount == 1900.0


def test_neg8b_no_amount_paid_label_means_unreadable_never_the_bill():
    em, why = CE.parse_body(body(paid=None, bill="3,456.00"))
    assert em is None and "amount paid" in why


def test_neg9_other_senders_are_ignored_even_with_credit_in_the_body(tmp_path):
    put(tmp_path / "e", "bank.msg", sender="Bank A <statements@banka.example>",
        text=body() + "\nyour credit card statement; cred score")
    put(tmp_path / "e", "look.msg", sender="Cred <hello@notcred.club>")
    put(tmp_path / "e", "sub.msg", sender="Cred <hello@mail.cred.club.example>")
    (tmp_path / "e" / "readme.txt").write_text("x", encoding="utf-8")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12))], {"p1": {REGALIA}})
    assert res.n_emails == 0 and len(res.skipped) == 4 and not res.ties and not res.unmatched_emails


def test_the_domain_not_the_substring_decides():
    assert CE._sender_ok("CRED <noreply@cred.club>") and CE._sender_ok("a@mail.cred.club")
    assert not CE._sender_ok("credit@creditbank.example") and not CE._sender_ok("a@notcred.club")
    assert not CE._sender_ok("CRED cred.club") and not CE._sender_ok(None)


def test_issuer_mismatch_is_not_used_as_evidence(tmp_path):
    put(tmp_path / "e", "a.msg", issuer="Bank Z", last4="1111")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12))], {"p1": {REGALIA}})
    assert not res.ties and not res.confirmed and res.unmatched_emails and res.unmatched_debits


def test_unknown_last4_is_not_guessed(tmp_path):
    put(tmp_path / "e", "a.msg", last4="9999")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12))], {"p1": {REGALIA}})
    assert not res.confirmed and not res.held and res.unmatched_emails


def test_clash_is_detected_in_the_pure_tie(tmp_path):
    put(tmp_path / "e", "a.msg", issuer="ICICI Bank", last4="2222")
    res = run_tie(tmp_path / "e", [pay("p1", 1900.0, date(2025, 5, 12))], {"p1": {REGALIA}})
    assert [t.status for t in res.held] == ["clash"] and not res.confirmed


# ----------------------------------------------------------------------------- end to end

def _run(tmp_path, monkeypatch, folder=None, cards=CARDS_TEXT, desc="HDFC CC PAYMENT"):
    (tmp_path / "cc_cards.yaml").write_text(cards, encoding="utf-8")
    if folder is not None:
        monkeypatch.setattr(ccsb, "run", lambda *a, **k: _ORIG_RUN(*a, cred_email_dir=str(folder), **k))
    s1, s2 = stmt(APR, april_rows()), stmt(MAY, may_rows())
    return go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=1900.0), tie(s2, printed=300.0)],
              [book_pay("p1", 1900, date(2025, 5, 12), desc)])


def _norm(reply, tmp_path):
    import re
    return re.sub(r"[0-9]+\.[0-9]+s", "Ns", str(reply).replace(str(tmp_path), "<T>"))


def test_neg5_blank_folder_runs_exactly_as_today(tmp_path, monkeypatch):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    r0, rows0, _c, out0 = _run(a, monkeypatch)
    monkeypatch.setattr(ccsb, "run", _ORIG_RUN)
    monkeypatch.setattr(ccsb, "run", lambda *a_, **k: _ORIG_RUN(*a_, cred_email_dir="", **k))
    r1, rows1, _c, out1 = _run(b, monkeypatch)
    assert _norm(r0, a) == _norm(r1, b) and rows0 == rows1 and rows0
    assert out0.read_bytes() == out1.read_bytes()
    assert "CRED PAYMENT EMAILS" not in str(r0)


def test_e2e_confirmed_run_books_exactly_what_it_booked_without_emails(tmp_path, monkeypatch):    # NEGATIVE 3
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    r0, rows0, _c, out0 = _run(a, monkeypatch)
    put(b / "mail", "x.msg")
    monkeypatch.setattr(ccsb, "run", _ORIG_RUN)
    r1, rows1, _c, out1 = _run(b, monkeypatch, folder=b / "mail")
    assert rows0 and rows1 == rows0                          # same bookings, none added, none doubled
    assert out0.read_bytes() == out1.read_bytes()
    text = str(r1)
    assert "1 of 1 bank payments confirmed by email" in text and "RED FLAG" not in text


def test_e2e_unmatched_email_adds_nothing_to_the_books(tmp_path, monkeypatch):    # NEGATIVE 2 and 3
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _r, rows0, _c, _o = _run(a, monkeypatch)
    put(b / "mail", "x.msg", paid="777.00")
    monkeypatch.setattr(ccsb, "run", _ORIG_RUN)
    r1, rows1, _c, _o = _run(b, monkeypatch, folder=b / "mail")
    assert rows1 == rows0
    assert "email not matched: x.msg" in str(r1) and "debit with no email" in str(r1)
    assert "0 of 1 bank payments confirmed" in str(r1)


def test_e2e_card_clash_is_a_red_flag_at_the_top_and_the_payment_is_not_booked(tmp_path, monkeypatch):    # NEGATIVE 4
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    _r, rows0, _c, _o = _run(a, monkeypatch)
    put(b / "mail", "x.msg", issuer="ICICI Bank", last4="2222")
    monkeypatch.setattr(ccsb, "run", _ORIG_RUN)
    r1, rows1, _c, _o = _run(b, monkeypatch, folder=b / "mail")
    text = str(r1)
    assert text.startswith("RED FLAG") and "NOT booked (held)" in text and SAPPHIRO in text and REGALIA in text
    # the held payment settles nothing: the statement it would have settled is not booked from it
    assert len(rows1) <= len(rows0)
    assert not any(r.get("Description", "").startswith("Moves card payment") for r in rows1)


def test_e2e_card_clash_pool_tie_out_shows_the_held_payment_never_a_silent_tie(tmp_path, monkeypatch):    # CC-40 follow-up
    """A held payment stays in the pool (the bank paid it) but books nothing, so the tie-out cannot tie to
    zero. It must be reported as an open residual AND the held entry must be named, never a quiet 'ties'."""
    put(tmp_path / "mail", "x.msg", issuer="ICICI Bank", last4="2222")
    r, rows, _c, out = _run(tmp_path, monkeypatch, folder=tmp_path / "mail")
    text = str(r)
    assert text.startswith("RED FLAG") and "NOT booked (held)" in text
    tie_part = text[text.index("tie-out for the range"):]
    # the held 1,900.00 is in the pool, so the figure is open and flagged as a residual
    assert "before (book, as it stands): 1,900.00" in tie_part
    assert "these journals: 0.00" in tie_part
    assert "after: 1,900.00  <- residual" in tie_part
    # and the line that explains it says it was held, not just a bare 'other'
    assert "HDFC CC PAYMENT [HELD: card clash, not booked]: 1,900.00" in tie_part
    # the run is not complete and writes nothing
    assert "NOT complete" in text and not rows
    # negative: it never claims the pool is tied, and nothing is booked against the clashing payment
    assert "tie-out ties" not in text.lower() and "residual: 0.00" not in tie_part
    assert not out.exists() or not out.read_text(encoding="utf-8").strip()


def test_e2e_no_clash_payment_line_is_not_labelled_held(tmp_path, monkeypatch):    # NEGATIVE
    r, _rows, _c, _o = _run(tmp_path, monkeypatch)
    assert "[HELD" not in str(r)


def test_e2e_missing_card_list_is_reported_and_emails_unused(tmp_path, monkeypatch):
    put(tmp_path / "mail", "x.msg")
    (tmp_path / "cc_cards.yaml").unlink(missing_ok=True)
    monkeypatch.setattr(ccsb, "run", lambda *a, **k: _ORIG_RUN(*a, cred_email_dir=str(tmp_path / "mail"), **k))
    s1, s2 = stmt(APR, april_rows()), stmt(MAY, may_rows())
    r, rows, _c, _o = go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=1900.0), tie(s2, printed=300.0)],
                         [book_pay("p1", 1900, date(2025, 5, 12))])
    assert "card list is not available" in str(r) and rows


def test_skill_yaml_declares_the_optional_folder_input():
    import yaml
    root = Path(__file__).resolve().parent.parent / "src" / "agents" / "skill_cc_spend_booking" / "skill.yaml"
    y = yaml.safe_load(root.read_text(encoding="utf-8"))
    inp = {i["name"]: i for i in y["inputs"]}["cred_emails"]
    assert inp["type"] == "directory" and inp["required"] is False
    assert inp["label"] == "Payment confirmation emails (optional)"
    assert y["run_args"]["cred_email_dir"] == "{inputs.cred_emails}"
