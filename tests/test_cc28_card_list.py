"""tests/test_cc28_card_list.py -- CC-28: one card list (cc_cards.yaml) that every CC skill reads.

Synthetic only: made-up card digits (0001..0009), made-up entity keys, made-up merchants.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import cc_cards as cc  # noqa: E402
from agents.cc_cards import Card, CardListError, CardListMissing  # noqa: E402
from agents.skill_cc_sort import icici_csv as ic  # noqa: E402
from agents.skill_cc_spend_booking import matcher as M  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "extract_sort_cc_pdfs_cc28", ROOT / "src" / "agents" / "skill_cc_sort" / "scripts" / "extract_sort_cc_pdfs.py")
sort = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = sort
_spec.loader.exec_module(sort)

ENT = {"entity_a", "entity_b"}
HDR = "# top comment line one\n# top comment line two\n\n"


def _yaml(*cards: str) -> str:
    return "cards:\n" + "".join(cards)


def _q(s):
    return '"' + s + '"'


def _card(bank='"ICICI"', card='"Sapphiro"', last4='["0001"]', holder='"entity_a"', active="true", extra=""):
    return ("  - bank: " + bank + "\n    card: " + card + "\n    last4: " + last4 + "\n    holder: " + holder
            + "\n    active: " + active + "\n" + extra)


def _load(text, ents=ENT):
    return cc.parse_text(text, ents)


# ---------------------------------------------------------------- loader: the good path

def test_a_valid_list_loads():
    cards = _load(_yaml(_card(_q("ICICI"), _q("Sapphiro"), '["0001", "0002"]'),
                        _card(_q("ICICI"), _q("Amazon"), '["0003"]', '""', "false")))
    assert [c.folder for c in cards] == [("ICICI", "Sapphiro"), ("ICICI", "Amazon")]
    assert cards[0].last4 == ("0001", "0002")
    assert cards[1].holder == "" and cards[1].active is False


def test_the_shipped_example_loads():
    ex = ROOT / "bundling" / "canonical" / "settings" / "cc_cards.example.yaml"
    text = ex.read_text(encoding="utf-8")
    assert text.lstrip().startswith("#")
    assert len(cc.parse_text(text, {"entity_a"})) == 2


def test_an_alias_reaches_its_folder():
    c = _load(_yaml(_card(_q("SBM"), _q("Niyo Global"), '["0004"]')))
    assert c[0].folder == ("SBM", "Global")


# ---------------------------------------------------------------- loader: one negative per refusal

def _refused(text, ents=ENT, digits=()):
    with pytest.raises(CardListError) as ei:
        _load(text, ents)
    for d in digits:
        assert d not in str(ei.value)          # an error never echoes a digit
    return str(ei.value)


def test_refuses_an_unquoted_yes_bank():                                    # NEGATIVE
    _refused(_yaml(_card("YES", _q("Reserv"))))


def test_refuses_a_non_string_card():                                       # NEGATIVE
    _refused(_yaml(_card(_q("ICICI"), "123")))


def test_refuses_last4_that_is_not_a_list():                                # NEGATIVE
    _refused(_yaml(_card(last4='"0001"')), digits=("0001",))


def test_refuses_last4_with_an_unquoted_number():                           # NEGATIVE
    _refused(_yaml(_card(last4="[1234]")), digits=("1234",))


def test_refuses_last4_of_the_wrong_length():                               # NEGATIVE
    _refused(_yaml(_card(last4='["123"]')), digits=("123",))
    _refused(_yaml(_card(last4='["12345"]')), digits=("12345",))


def test_refuses_a_full_card_number_without_echoing_it():                   # NEGATIVE
    full = "5555000011112222"
    _refused(_yaml(_card(last4='["' + full + '"]')), digits=(full, full[-4:]))


def test_refuses_an_empty_last4_list():                                     # NEGATIVE
    _refused(_yaml(_card(last4="[]")))


def test_refuses_a_holder_that_is_not_an_entity_key():                      # NEGATIVE
    _refused(_yaml(_card(holder='"nobody"')))


def test_refuses_a_non_string_holder():                                     # NEGATIVE
    _refused(_yaml(_card(holder="5")))


def test_refuses_an_unknown_key():                                          # NEGATIVE
    _refused(_yaml(_card(extra='    number: "0009"\n')), digits=("0009",))


def test_refuses_a_missing_key():                                           # NEGATIVE
    _refused('cards:\n  - bank: "ICICI"\n    card: "Sapphiro"\n    last4: ["0001"]\n    active: true\n')


def test_refuses_a_top_level_unknown_key():                                 # NEGATIVE
    _refused(_yaml(_card()) + "extra: 1\n")


def test_refuses_a_bank_card_pair_with_no_folder():                         # NEGATIVE
    _refused(_yaml(_card(_q("ICICI"), _q("Coral"))))


def test_refuses_active_that_is_not_a_bool():                               # NEGATIVE
    _refused(_yaml(_card(active='"yes"')))


def test_refuses_the_same_last4_on_two_cards():                             # NEGATIVE
    msg = _refused(_yaml(_card(last4='["0001"]'), _card(_q("ICICI"), _q("Amazon"), '["0001"]')), digits=("0001",))
    assert "share" in msg


def test_refuses_the_same_last4_twice_on_one_card():                        # NEGATIVE
    _refused(_yaml(_card(last4='["0001", "0001"]')), digits=("0001",))


def test_refuses_broken_yaml_without_echoing_it():                          # NEGATIVE
    _refused('cards: [\n  - bank: "ICICI" 0007\n', digits=("0007",))


def test_a_missing_file_is_loud_never_no_cards(tmp_path):                   # NEGATIVE
    with pytest.raises(CardListMissing):
        cc.load_cards(tmp_path / "cc_cards.yaml", ENT)
    with pytest.raises(CardListMissing):
        cc.load_for_settings(tmp_path / "config.yaml")


def test_the_file_is_found_beside_the_settings_file(tmp_path):
    sd = tmp_path / "settings"
    sd.mkdir()
    (sd / "cc_cards.yaml").write_text(_yaml(_card(holder='""')), encoding="utf-8")
    assert cc.cards_path_for(sd / "config.yaml") == (sd / "cc_cards.yaml").resolve()
    assert len(cc.load_for_settings(sd / "config.yaml")) == 1


# ---------------------------------------------------------------- digit tokens

CARDS = [Card("ICICI", "Sapphiro", ("0001", "0002"), "", True), Card("ICICI", "Amazon", ("0003",), "", True)]


def test_a_filename_token_is_an_exact_four_digits():
    assert [c.label for c in cc.cards_named_in("stmt_0001_apr.pdf", CARDS)] == ["ICICI-Sapphiro"]
    assert [c.label for c in cc.cards_named_in("XXXX0003.pdf", CARDS)] == ["ICICI-Amazon"]


def test_digits_inside_a_longer_number_never_match():                       # NEGATIVE
    assert cc.cards_named_in("ref_100012.pdf", CARDS) == []
    assert cc.cards_named_in("ref_000100.pdf", CARDS) == []
    assert cc.cards_named_in("12340001.pdf", CARDS) == []


def test_a_year_is_never_a_card_even_if_listed():                           # NEGATIVE
    yr = [Card("ICICI", "Sapphiro", ("2025",), "", True)]
    assert cc.cards_named_in("stmt 01-04-2025.pdf", yr) == []
    assert cc.cards_named_in("stmt_Apr_2025.pdf", yr) == []
    assert cc.cards_named_in("stmt 2025/26.pdf", yr) == []


def test_an_amount_is_never_a_card():                                       # NEGATIVE
    assert cc.cards_named_in("pay 5,0001.pdf", CARDS) == []
    assert cc.cards_named_in("pay 12.0001.pdf", CARDS) == []


def test_printed_masks_are_read_only_when_masked():
    assert cc.printed_last4s("Card No: 5100 XXXX XXXX 0001") == ["0001"]
    assert cc.printed_last4s("Total 5100 0000 0000 0001 due") == []          # no mask characters: not taken
    assert cc.printed_last4s("Amount 1234.50 on 0001") == []


# ---------------------------------------------------------------- CC Sort: ICICI CSV

def test_an_unlisted_csv_number_is_refused_never_defaulted_to_amazon():     # NEGATIVE
    who, why = ic.identify_card(["5100 XXXX XXXX 0009"], CARDS)
    assert who is None and why
    assert "0009" not in why


def test_no_card_list_means_refused_not_guessed():                          # NEGATIVE
    who, why = ic.identify_card(["5100 XXXX XXXX 0003"], None)
    assert who is None and why


def test_a_listed_csv_number_is_matched():
    assert ic.identify_card(["5100 XXXX XXXX 0003"], CARDS)[0] == "Amazon"
    assert ic.identify_card(["5100 XXXX XXXX 0002"], CARDS)[0] == "Sapphiro"


def test_numbers_on_two_cards_in_one_csv_are_refused():                     # NEGATIVE
    assert ic.identify_card(["5100 XXXX XXXX 0001", "5100 XXXX XXXX 0003"], CARDS)[0] is None


# ---------------------------------------------------------------- CC Sort: PDFs

def _printed(monkeypatch, text):
    from agents.skill_cc_sort import completeness
    monkeypatch.setattr(completeness, "read_page1_text", lambda p: text)


def test_a_printed_listed_number_files_the_card(monkeypatch):
    _printed(monkeypatch, "Card No 5100 XXXX XXXX 0003")
    assert sort.choose_folder_why("statement.pdf", "x.pdf", CARDS) == ("ICICI", "Amazon", None)


def test_a_printed_number_that_conflicts_with_the_filename_is_refused(monkeypatch):     # NEGATIVE
    _printed(monkeypatch, "Card No 5100 XXXX XXXX 0003")
    b, c, why = sort.choose_folder_why("icici sapphiro 0001.pdf", "x.pdf", CARDS)
    assert (b, c) == ("Unknown", "Unknown") and why
    assert "0003" not in why and "0001" not in why


def test_a_printed_unlisted_number_is_refused(monkeypatch):                 # NEGATIVE
    _printed(monkeypatch, "Card No 5100 XXXX XXXX 0009")
    b, c, why = sort.choose_folder_why("icici sapphiro.pdf", "x.pdf", CARDS)
    assert (b, c) == ("Unknown", "Unknown") and why and "0009" not in why


def test_nothing_printed_falls_back_to_the_filename(monkeypatch):
    _printed(monkeypatch, "no number here")
    assert sort.choose_folder_why("stmt_0002.pdf", "x.pdf", CARDS) == ("ICICI", "Sapphiro", None)


def test_a_filename_naming_two_cards_is_refused(monkeypatch):               # NEGATIVE
    _printed(monkeypatch, "")
    assert sort.choose_folder_why("0001_0003.pdf", "x.pdf", CARDS)[2]


def test_a_filename_digit_unknown_to_the_list_is_not_placed_by_a_built_in_pattern(monkeypatch):   # NEGATIVE
    _printed(monkeypatch, "")
    b, c, _ = sort.choose_folder_why("stmt_0009.pdf", "x.pdf", CARDS)
    assert (b, c) == ("Unknown", "Unknown")


def test_the_old_hard_coded_digits_are_gone():                              # NEGATIVE
    src = (ROOT / "src" / "agents" / "skill_cc_sort" / "scripts" / "extract_sort_cc_pdfs.py").read_text(encoding="utf-8")
    assert "SAPPHIRO_DIGITS" not in (ROOT / "src" / "agents" / "skill_cc_sort" / "icici_csv.py").read_text(encoding="utf-8")
    pat = src[src.index("patterns = {"):src.index("for (bank, card_type), pattern")]
    assert not re.search(r"[0-9]{3,}", pat)


def test_an_unlisted_csv_goes_to_unknown_with_a_reason(tmp_path, monkeypatch):    # NEGATIVE
    settings = tmp_path / "settings"
    settings.mkdir()
    (settings / "cc_cards.yaml").write_text(_yaml(_card(_q("ICICI"), _q("Sapphiro"), '["0001"]', '""')), encoding="utf-8")
    monkeypatch.setenv("PASK_CC_SETTINGS_PATH", str(settings / "config.yaml"))
    inp = tmp_path / "in"
    inp.mkdir()
    (inp / "stmt.csv").write_text("not a card csv", encoding="utf-8")
    out = tmp_path / "out"
    res = sort.organize_pdfs(str(inp), str(out), password=None)
    assert not (out / "Decrypted_PDFs_Correct" / "ICICI-Sapphiro" / "stmt.csv").exists()
    assert res is not None


# ---------------------------------------------------------------- Book Spends

def _line(label, day, amt):
    return M.PayLine(SimpleNamespace(card=label), SimpleNamespace(description="PAYMENT RECEIVED"), day, amt)


def _pay(guid, day, amt, text):
    return M.BookPayment(guid, day, amt, text, "Assets:Bank")


D1 = date(2025, 4, 10)


def test_a_narration_with_a_listed_last4_settles_only_that_card():
    lines = [_line("ICICI-Amazon", D1, 500.0), _line("ICICI-Sapphiro", D1, 500.0)]
    out = M.match_payments(lines, [_pay("p1", D1, 500.0, "CC PAYMENT 0001")], cards=CARDS)
    assert len(out.pairings) == 1
    assert out.pairings[0].line.stmt.card == "ICICI-Sapphiro"


def test_a_narration_naming_a_different_card_is_a_conflict_and_not_booked():    # NEGATIVE
    lines = [_line("ICICI-Amazon", D1, 500.0)]
    out = M.match_payments(lines, [_pay("p1", D1, 500.0, "CC PAYMENT 0001")], cards=CARDS)
    assert out.pairings == []
    assert out.conflicts and out.conflicts[0]["payment"].guid == "p1"
    assert "p1" in out.unmatched_reason
    assert "0001" not in out.unmatched_reason["p1"]


def test_a_narration_naming_two_cards_is_barred():                          # NEGATIVE
    lines = [_line("ICICI-Amazon", D1, 500.0)]
    out = M.match_payments(lines, [_pay("p1", D1, 500.0, "PAY 0001 0003")], cards=CARDS)
    assert out.pairings == []


def test_no_card_is_named_from_a_payment_without_digits():                  # NEGATIVE
    p = _pay("p1", D1, 500.0, "ICICI SAPPHIRO CARD PAYMENT")
    assert M.narration_cards([p], CARDS) == {}
    out = M.match_payments([_line("ICICI-Amazon", D1, 500.0)], [p], cards=CARDS)
    assert out.conflicts == []                  # no digits: no card is claimed and no conflict is raised


def test_without_a_card_list_matching_is_unchanged():
    out = M.match_payments([_line("ICICI-Amazon", D1, 500.0)], [_pay("p1", D1, 500.0, "CC PAYMENT 0001")])
    assert len(out.pairings) == 1 and out.conflicts == []


# ---------------------------------------------------------------- the Cards screen

from ui.tabs import _entity_cards as ec  # noqa: E402


def _file(tmp_path, body=None):
    p = tmp_path / "settings" / "cc_cards.yaml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(HDR + (body or _yaml(_card(_q("ICICI"), _q("Sapphiro"), '["0001"]', _q("entity_a")))), encoding="utf-8")
    return p


def test_a_full_number_is_refused_and_not_echoed(tmp_path):                 # NEGATIVE
    p = _file(tmp_path)
    before = p.read_text(encoding="utf-8")
    full = "5555000011112222"
    msg = ec.save_card(p, ENT, None, "ICICI-Amazon", full, "", True)
    assert "Not saved" in msg and full not in msg and full[-4:] not in msg
    assert p.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("bad", ["", "123", "12345", "abcd", "0001 55"])
def test_a_bad_last4_entry_is_refused(tmp_path, bad):                       # NEGATIVE
    p = _file(tmp_path)
    before = p.read_text(encoding="utf-8")
    assert "Not saved" in ec.save_card(p, ENT, None, "ICICI-Amazon", bad, "", True)
    assert p.read_text(encoding="utf-8") == before


def test_save_adds_a_card_keeps_the_top_comments_and_writes_a_backup(tmp_path):
    p = _file(tmp_path)
    msg = ec.save_card(p, ENT, None, "ICICI-Amazon", "0003, 0004", "entity_b", True)
    assert msg.startswith("**Saved")
    text = p.read_text(encoding="utf-8")
    assert text.startswith(HDR)
    cards = cc.parse_text(text, ENT)
    assert [c.label for c in cards] == ["ICICI-Sapphiro", "ICICI-Amazon"]
    assert cards[1].last4 == ("0003", "0004") and cards[1].holder == "entity_b"
    assert '"ICICI"' in text and '"entity_b"' in text            # every text value double-quoted
    baks = list(p.parent.glob("cc_cards.yaml.bak-*"))
    assert len(baks) == 1 and baks[0].read_text(encoding="utf-8").startswith(HDR)


def test_save_changes_a_card_in_place_and_keeps_the_others(tmp_path):
    body = _yaml(_card(_q("ICICI"), _q("Sapphiro"), '["0001"]', _q("entity_a")),
                 _card(_q("ICICI"), _q("Amazon"), '["0003"]', '""'))
    p = _file(tmp_path, body)
    ec.save_card(p, ENT, 0, "ICICI-Sapphiro", "0001, 0002", "entity_b", False)
    cards = cc.parse_text(p.read_text(encoding="utf-8"), ENT)
    assert cards[0].last4 == ("0001", "0002") and cards[0].active is False
    assert cards[1].last4 == ("0003",)


def test_a_save_that_the_loader_refuses_writes_nothing(tmp_path):           # NEGATIVE
    p = _file(tmp_path)
    before = p.read_text(encoding="utf-8")
    assert "Not saved" in ec.save_card(p, ENT, None, "ICICI-Amazon", "0001", "", True)       # duplicate last4
    assert "Not saved" in ec.save_card(p, ENT, None, "ICICI-Amazon", "0005", "nobody", True)  # holder
    assert p.read_text(encoding="utf-8") == before
    assert list(p.parent.glob("*.bak-*")) == []


def test_a_file_with_an_unknown_key_is_not_overwritten(tmp_path):           # NEGATIVE
    body = _yaml(_card(_q("ICICI"), _q("Sapphiro"), '["0001"]', _q("entity_a"), extra='    note: "keep"\n'))
    p = _file(tmp_path, body)
    before = p.read_text(encoding="utf-8")
    assert "Not saved" in ec.save_card(p, ENT, None, "ICICI-Amazon", "0003", "", True)
    assert p.read_text(encoding="utf-8") == before


def test_save_never_touches_entities_yaml(tmp_path):                        # NEGATIVE
    ents = tmp_path / "itr" / "entities.yaml"
    ents.parent.mkdir()
    ents.write_text("entity_a: {}\nentity_b: {}\n", encoding="utf-8")
    snap = (ents.read_bytes(), ents.stat().st_mtime_ns)
    p = _file(tmp_path)
    ec.save_card(p, ENT, None, "ICICI-Amazon", "0003", "entity_a", True)
    ec.delete_card(p, ENT, 0, True)
    assert (ents.read_bytes(), ents.stat().st_mtime_ns) == snap
    names = sorted(x.name for x in tmp_path.rglob("*") if x.is_file() and ".bak-" not in x.name)
    assert names == ["cc_cards.yaml", "entities.yaml"]


def test_delete_needs_the_confirmation_and_keeps_a_backup(tmp_path):
    p = _file(tmp_path)
    assert "Not deleted" in ec.delete_card(p, ENT, 0, False)
    assert len(cc.parse_text(p.read_text(encoding="utf-8"), ENT)) == 1
    assert ec.delete_card(p, ENT, 0, True).startswith("**Deleted")
    assert cc.parse_text(p.read_text(encoding="utf-8"), ENT) == []
    assert p.read_text(encoding="utf-8").startswith(HDR)
    assert len(list(p.parent.glob("cc_cards.yaml.bak-*"))) == 1


def test_the_first_save_creates_the_file(tmp_path):
    p = tmp_path / "settings" / "cc_cards.yaml"
    assert ec.save_card(p, ENT, None, "ICICI-Amazon", "0003", "", True).startswith("**Saved")
    assert p.read_text(encoding="utf-8").lstrip().startswith("#")


def test_the_list_is_shown_grouped_by_holder():
    rows = [{"bank": "ICICI", "card": "Amazon", "last4": ["0003"], "holder": "", "active": True},
            {"bank": "ICICI", "card": "Sapphiro", "last4": ["0001"], "holder": "entity_a", "active": False}]
    md = ec.grouped_markdown(rows)
    assert md.index("entity_a") < md.index("(holder not set)")
    assert "(not active)" in md


def test_the_screen_builds():
    import gradio as gr
    with gr.Blocks():
        ec.render()
