"""BNK-01 commit 6 -- the one Bank Skills tab.

Registry: six hidden per-bank skills named skill_<bank_key> with their old
names kept as aliases, one visible "Bank Skills" skill. UI: checkbox input,
visible_when, per-bank file-type hint, balance-break checkbox reset. Run:
one shared statements-to-CSV run. Synthetic data only.
"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent

from agents import banks, registry  # noqa: E402
from agents.bank_common import bank_run  # noqa: E402
from tests.test_bnk04_sbm_parser import ACCT, PW, _rows, _write  # noqa: E402

OLD_TO_NEW = {
    "BoB": "skill_bob", "HSBC": "skill_hsbc", "ICICI": "skill_icici",
    "SBM": "skill_sbm", "Kotak": "skill_kotak", "skill_hdfc": "skill_hdfc",
}
ONE_ACCOUNT = ("One account per run: upload that account's statement file(s); "
               "a statement of a different account stops the run.")


# -- registry -----------------------------------------------------------------

@pytest.mark.parametrize("old,new", sorted(OLD_TO_NEW.items()))
def test_old_name_and_new_name_are_the_same_skill(old, new):
    a, b = registry.get(old), registry.get(new)
    assert a is not None and b is not None
    assert a.name == b.name == new
    assert a.hidden


def test_lowercase_old_names_still_resolve():
    assert registry.get("bob").name == "skill_bob"
    assert registry.get("BOB").name == "skill_bob"


def test_every_bank_has_a_skill_named_for_its_key():
    for info in banks.discover():
        assert info.skill_name == f"skill_{info.bank_key}"


def test_an_unknown_name_does_not_resolve():
    assert registry.get("skill_nosuchbank") is None
    assert registry.get("Bank Skillz") is None


def test_exactly_one_visible_banks_skill_and_six_hidden():
    banks_skills = [s for s in registry.discover() if s.category == "banks"]
    visible = [s for s in banks_skills if not s.hidden]
    assert [s.display_name for s in visible] == ["Bank Skills"]
    assert len([s for s in banks_skills if s.hidden]) == 6


def test_bank_skills_is_not_itself_a_bank():
    assert "skill_bank_skills" not in [b.skill_name for b in banks.discover()]
    assert len(banks.discover()) == 6


# -- hints --------------------------------------------------------------------

def test_every_bank_says_one_account_per_run_in_its_statement_hint():
    for info in banks.discover():
        hint = registry.get(info.skill_name).help.inputs[0].accepts
        assert ONE_ACCOUNT in hint, info.bank_key


def test_the_hint_follows_the_bank_and_differs_between_banks():
    from ui.tabs import _generic
    kotak, icici = _generic.bank_statement_hint("kotak"), _generic.bank_statement_hint("icici")
    assert ".pdf" in kotak and ".xls" in icici and kotak != icici
    assert _generic.bank_statement_hint("") == ""
    assert _generic.bank_statement_hint("nosuchbank") == ""


# -- visible_when -------------------------------------------------------------

def test_visible_when_follows_the_controlling_value():
    from ui.tabs._generic import input_visible
    skill = registry.get("skill_bank_skills")
    folder = next(i for i in skill.inputs if i.name == "hsbc_folder")
    assert input_visible(folder, {"bank": "hsbc"})
    assert not input_visible(folder, {"bank": "bob"})
    assert not input_visible(folder, {"bank": None})
    assert input_visible(skill.inputs[0], {})        # no condition: always


def test_a_visible_when_naming_no_input_is_refused(tmp_path):
    bad = tmp_path / "skill.yaml"
    bad.write_text(
        'name: x\ndisplay_name: X\ndescription: d\nmode: direct\nentry_point: "a:run"\n'
        'inputs:\n  - {name: a, type: text, label: A, visible_when: "zzz=1"}\n',
        encoding="utf-8")
    with pytest.raises(ValueError, match="visible_when"):
        registry._parse_manifest(bad)


# -- the tab renders; the checkbox resets -------------------------------------

def test_the_tab_renders_with_a_checkbox_that_resets_after_the_run():
    import gradio as gr
    from ui.tabs import _generic
    skill = registry.get("skill_bank_skills")
    with gr.Blocks() as demo:
        _generic.render(skill)
    boxes = [b for b in demo.blocks.values() if isinstance(b, gr.Checkbox)]
    assert len(boxes) == 1 and boxes[0].value is False
    ids = {boxes[0]._id}
    chained = [d for d in demo.fns.values()
               if getattr(d, "trigger_after", None) is not None
               and ids & {o._id for o in d.outputs}]
    assert chained, "the checkbox must be an output of a step that follows the run"


# -- input handling -----------------------------------------------------------

def test_checkbox_and_hidden_input_reach_the_run_as_expected():
    from ui.tabs._generic import build_run_kwargs
    skill = registry.get("skill_bank_skills")
    kw = build_run_kwargs(skill, {"bank": "hsbc", "statement_files": "", "hsbc_folder": "",
                                  "password": "", "allow_balance_break": "true"},
                          Path("o.csv"), "cfg", "", "wd")
    assert kw["allow_balance_break"] == "true" and kw["bank"] == "hsbc"


# -- the run ------------------------------------------------------------------

def _csv_rows(p):
    with open(p, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def test_run_writes_the_canonical_csv_and_sidecar(tmp_path):
    f = _write(tmp_path / "s.xlsx", _rows())
    out = tmp_path / "out.csv"
    msg = bank_run.run_statements("sbm", f, str(out), password=PW)
    assert len(_csv_rows(out)) == 6
    assert out.with_suffix(".csv_summary.json").is_file()
    assert "SBM: read 1 statement file(s), 6 transaction(s)" in msg
    assert "balance check ok" in msg
    assert "override" not in msg.lower()


def test_run_through_the_skills_entry_point(tmp_path):
    from agents.skill_bank_skills.agent import run
    f = _write(tmp_path / "s.xlsx", _rows())
    out = tmp_path / "out.csv"
    msg = run("sbm", f, "", PW, "false", str(out))
    assert out.is_file() and "6 transaction(s)" in msg


def test_a_wrong_file_type_stops_the_run_and_names_the_file(tmp_path):
    f = tmp_path / "x.pdf"
    f.write_bytes(b"%PDF-1.4 not a statement")
    out = tmp_path / "out.csv"
    msg = bank_run.run_statements("sbm", str(f), str(out))
    assert msg.startswith("ERROR") and "x.pdf" in msg and not out.exists()


def test_an_unknown_bank_and_no_statement_stop(tmp_path):
    out = tmp_path / "o.csv"
    assert bank_run.run_statements("nobank", str(tmp_path), str(out)).startswith("ERROR")
    assert bank_run.run_statements("sbm", "", str(out)).startswith("ERROR")
    assert not out.exists()


def test_a_wrong_password_stops_and_never_prints_the_password(tmp_path):
    f = _write(tmp_path / "s.xlsx", _rows())
    out = tmp_path / "o.csv"
    msg = bank_run.run_statements("sbm", f, str(out), password="wrong-secret-xyz")
    assert msg.startswith("ERROR") and "wrong-secret-xyz" not in msg and not out.exists()


def test_uploads_and_folder_together_are_refused(tmp_path):
    from agents.skill_bank_skills.agent import run
    msg = run("hsbc", str(tmp_path), str(tmp_path), "", "false", str(tmp_path / "o.csv"))
    assert msg.startswith("ERROR - two sources")


def _two_statements(tmp_path):
    a = _write(tmp_path / "a.xlsx", _rows())
    b = _write(tmp_path / "b.xlsx", [
        ["2025-06-10", "UPI/P2P/500000000002/MASKED", "500000000002", None, 100, 5100],
        ["2025-06-30", ACCT + ":Int.Pd:01-06-2025 to 30-06-2025", None, None, 50, 5150],
    ])
    d = tmp_path / "up"
    d.mkdir()
    shutil.copy(a, d / "a.xlsx")
    shutil.copy(b, d / "b.xlsx")
    return str(d)


def test_a_balance_break_stops_unless_the_override_is_on_and_then_says_so(tmp_path):
    d = _two_statements(tmp_path)
    out1, out2 = tmp_path / "o1.csv", tmp_path / "o2.csv"
    stopped = bank_run.run_statements("sbm", d, str(out1), password=PW)
    assert stopped.startswith("ERROR") and not out1.exists()
    ok = bank_run.run_statements("sbm", d, str(out2), password=PW, allow_balance_break="true")
    assert not ok.startswith("ERROR") and out2.exists()
    assert "Balance-break override: ON for this run." in ok


def test_the_override_text_is_absent_when_the_box_is_off(tmp_path):
    f = _write(tmp_path / "s.xlsx", _rows())
    for off in ("false", "", False, None):
        assert "override" not in bank_run.run_statements(
            "sbm", f, str(tmp_path / "o.csv"), password=PW, allow_balance_break=off).lower()


# -- History ------------------------------------------------------------------

def test_history_labels_the_new_output_suffix():
    from ui.tabs.history import _infer_skill
    assert _infer_skill("data-Bank-Skills.csv") == "Bank Skills"
    assert _infer_skill("data-HSBC.xlsx") == "HSBC"          # old names unchanged


# -- the run handler honours visible_when -------------------------------------

def _first_texts(gen, n=3):
    out = []
    try:
        for _ in range(n):
            out.append(str(next(gen)[0]))
    except StopIteration:
        pass
    finally:
        gen.close()
    return " ".join(out)


def test_a_hidden_folder_is_never_checked_but_a_shown_one_is(tmp_path):
    from ui.tabs._generic import _make_run_handler
    skill = registry.get("skill_bank_skills")
    bogus = str(tmp_path / "no-such-folder")
    # bank = bob: the HSBC folder box is hidden, so its stale text is ignored.
    hidden = _first_texts(_make_run_handler(skill)("bob", None, bogus, "", False, ""))
    assert "does not exist" not in hidden
    # bank = hsbc: the same text is checked and stops the run.
    shown = _first_texts(_make_run_handler(skill)("hsbc", None, bogus, "", False, ""))
    assert "does not exist" in shown


# -- helpers that look a skill up by name resolve old names too ---------------

@pytest.mark.parametrize("old,new", sorted(OLD_TO_NEW.items()))
def test_name_helpers_give_the_same_answer_for_old_and_new_names(old, new):
    from ui.tabs._generic import reply_label_for, skill_uses_llm
    assert skill_uses_llm(old) == skill_uses_llm(new) is False
    assert reply_label_for(old) == reply_label_for(new) == "Result"


def test_name_helpers_still_default_for_an_unknown_name():
    from ui.tabs._generic import reply_label_for, skill_uses_llm
    assert skill_uses_llm("no-such-skill") is True
    assert reply_label_for("no-such-skill") == "Agent reply"
