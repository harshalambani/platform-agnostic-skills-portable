"""UI-38: an explicit per-tag 'reduces income' kind, no prefix fallback."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
for p in (str(ROOT), str(SRC), str(SRC / "agents" / "skill_itr_workbook" / "scripts"),
          str(SRC / "agents" / "skill_itr_workbook")):
    if p not in sys.path:
        sys.path.insert(0, p)

import tag_suggest  # noqa: E402
import tags  # noqa: E402
from ui import _review_tree as rt  # noqa: E402

EXPENSE_TAGS = {"BUS_EXPENSE", "HP_MUNICIPAL_TAX", "HP_INTEREST"}


def test_expense_tags_reduce_income():
    for t in EXPENSE_TAGS:
        assert tag_suggest.kind(t) == "exp"
        assert tag_suggest.affects_tax(t)


def test_income_tags_still_add_income_and_are_not_exp():
    for t in ("SALARY_GROSS", "BUS_REMUNERATION", "HP_RENT", "OS_DIVIDEND", "OS_INTEREST_SB", "CG_LT_CONTROL"):
        assert tag_suggest.kind(t) == "inc"
    assert {t for t, k in tag_suggest.TAG_KIND.items() if k == "exp"} == EXPENSE_TAGS


def test_every_tag_is_listed_explicitly_and_the_table_has_no_strays():
    assert set(tag_suggest.TAG_KIND) == set(tags.TAGS)


def test_unlisted_tag_is_not_guessed_from_its_prefix():
    # a made-up BUS_/HP_/OS_ tag must not become income or expense by prefix
    for t in ("BUS_NEW_THING", "HP_NEW_THING", "OS_NEW_THING", "DED_NEW", "TAXPAID_NEW"):
        assert tag_suggest.kind(t) == "none"
        assert not tag_suggest.affects_tax(t)


def test_no_tag_or_hold_has_no_kind():
    assert tag_suggest.kind(None) is None and tag_suggest.kind("") is None
    assert tag_suggest.kind(tags.HOLD) is None


def test_other_kinds_unchanged():
    assert tag_suggest.kind("DED_80C_CANDIDATE") == "ded"
    assert tag_suggest.kind("TAXPAID_TDS_INTEREST") == "paid"
    assert tag_suggest.kind("EXEMPT_PPF_INTEREST") == "rep"
    assert tag_suggest.kind("AL_VEHICLE") == "rep"
    assert tag_suggest.kind("PERSONAL") == "none"
    assert tag_suggest.kind("NONTAX_REFUND_PRINCIPAL") == "none"


def test_exp_has_its_own_pill_colour_and_legend_entry():
    css = rt.CSS
    assert ".tp.k-exp" in css and "--exp:" in css and "sw.exp" in css
    assert "kr-exp" in css and "lr-exp" in css

    blob = rt.CSS + "".join(v for v in vars(rt).values() if isinstance(v, str))
    assert "reduces income" in blob
    # distinct from the income colour
    dark = css.split("--exp:")[1].split(";")[0].strip()
    assert dark != css.split("--inc:")[1].split(";")[0].strip()
