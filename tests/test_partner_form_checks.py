"""
tests/test_partner_form_checks.py -- UI-17: the partner form's document slots
and its WARN-only pre-run checks. Everything is synthetic.
"""
import copy
import sys
from email.message import EmailMessage
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (ROOT, ROOT / "src"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from agents.registry import discover  # noqa: E402
from agents.skill_partner_comp_recon import agent as agent_module  # noqa: E402
from agents.skill_partner_comp_recon import precheck  # noqa: E402

FY = "2025-26"
YEAR = precheck.fy_months(FY)


def cert(month, name=None):
    return {"month": month, "source_name": name or f"cert-{month}.pdf"}


def stmt(month_name, year, name=None):
    return {"doc_class": "B", "month": month_name, "year": year,
            "source_name": name or f"stmt-{month_name}.pdf"}


def full_year():
    return [cert(m) for m in YEAR]


def _skill():
    return next(s for s in discover(refresh=True)
                if Path(s.manifest_path).parent.name == "skill_partner_comp_recon")


# ---- labels ----------------------------------------------------------------

def test_slot_labels_quote_the_printed_titles():
    labels = {i.name: i.label for i in _skill().inputs}
    assert "payout details for the month of" in labels["advices_dir"]
    assert "Compensation summary : Year ended 31 March" in labels["advisory_path"]
    assert "Statement of Account" in labels["llp_statement"] and "AS ON 31 MARCH" in labels["llp_statement"]
    assert "Payment Schedule FY" in labels["payment_schedule"]
    assert "ONE password" in labels["doc_password"]


def test_advisory_label_does_not_name_target_comp_as_the_wanted_document():
    label = {i.name: i.label for i in _skill().inputs}["advisory_path"]
    assert label.index("Compensation summary") < label.index("Target compensation advice")
    assert "not the" in label


def test_llp_slot_accepts_eml():
    inp = next(i for i in _skill().inputs if i.name == "llp_statement")
    assert ".eml" in inp.file_types and ".pdf" in inp.file_types


# ---- payout set checks -----------------------------------------------------

def test_in_year_single_file_per_month_raises_no_warning():             # NEGATIVE
    assert precheck.check_payout_set(full_year(), FY, None) == []
    assert precheck.check_payout_set(full_year(), FY, set(YEAR)) == []


def test_class_b_statements_in_year_raise_no_warning():                  # NEGATIVE
    recs = [stmt("April", 2025), stmt("May", 2025)] + [cert(m) for m in YEAR[2:]]
    assert precheck.check_payout_set(recs, FY, None) == []


def test_month_outside_the_year_warns():
    recs = full_year() + [cert("2024-12", "old.pdf")]
    notes = precheck.check_payout_set(recs, FY, None)
    assert len(notes) == 1 and "Dec 2024" in notes[0] and "old.pdf" in notes[0]
    assert "outside FY 2025-26" in notes[0]


def test_same_month_given_twice_certificate_and_statement_warns():
    recs = [cert("2025-04", "a.pdf"), stmt("April", 2025, "b.pdf")] + [cert(m) for m in YEAR[1:]]
    notes = precheck.check_payout_set(recs, FY, None)
    assert len(notes) == 1
    assert "Apr 2025" in notes[0] and "a.pdf" in notes[0] and "b.pdf" in notes[0]
    assert "monthly certificate" in notes[0] and "payout statement" in notes[0]


def test_missing_months_say_whether_the_schedule_covers_them():
    recs = [cert(m) for m in YEAR[:10]]          # Feb and Mar missing
    none_sup = precheck.check_payout_set(recs, FY, None)
    assert "Feb 2026, Mar 2026" in none_sup[0] and "no payment schedule was supplied" in none_sup[0]
    full = precheck.check_payout_set(recs, FY, set(YEAR))
    assert "covers all of them" in full[0]
    part = precheck.check_payout_set(recs, FY, {"2026-02"})
    assert "covers only Feb 2026" in part[0]
    none = precheck.check_payout_set(recs, FY, set(YEAR[:10]))
    assert "does not cover them either" in none[0]


def test_warnings_never_drop_or_edit_an_input():                          # NEGATIVE
    recs = [cert("2025-04", "a.pdf"), stmt("April", 2025, "b.pdf"), cert("2024-01", "old.pdf")]
    before = copy.deepcopy(recs)
    notes = precheck.check_payout_set(recs, FY, set())
    assert notes                                  # warnings were raised ...
    assert recs == before                         # ... and nothing was touched


def test_no_records_or_unknown_fy_is_silent():                           # NEGATIVE
    assert precheck.check_payout_set([], FY, None) == []
    assert precheck.check_payout_set(full_year(), None, None) == []


def test_run_surfaces_warnings_and_still_uses_every_document(tmp_path, monkeypatch):
    d = tmp_path / "advices"
    d.mkdir()
    for n in ("a.pdf", "b.pdf"):
        (d / n).write_bytes(b"%PDF-1.4 fake")
    adv = tmp_path / "adv.pdf"
    adv.write_bytes(b"%PDF-1.4 fake")
    seen = []

    def fake_parse(path, password):
        seen.append(Path(path).name)
        return cert("2025-04", Path(path).name)

    monkeypatch.setattr(agent_module._advisory_parser, "parse",
                        lambda p, pw: {"financial_year": FY})
    monkeypatch.setattr(agent_module._payout_advice_parser, "parse", fake_parse)
    monkeypatch.setattr(agent_module, "_resolve_entity_config", lambda e, c: (None, None))
    out = agent_module._run_from_documents(
        entity="X", advices_dir=str(d), doc_password=None, advisory_path=str(adv),
        llp_statement="", payment_schedule="", gnucash_path="", xlsx_26as="",
        output_path=str(tmp_path / "o.xlsx"), config_path=None, model_override=None,
        journal_path="")
    assert "given 2 times" in out
    assert seen == ["a.pdf", "b.pdf"]            # both were handed on, none dropped


# ---- advisory slot ---------------------------------------------------------

def test_target_comp_letter_in_the_advisory_slot_warns():
    notes = precheck.check_advisory_slot("ACME LLP\nTarget Compensation Advice\nFY 2025-26")
    assert len(notes) == 1 and "Target compensation advice" in notes[0]


def test_compensation_summary_is_accepted_silently():                     # NEGATIVE
    assert precheck.check_advisory_slot("Compensation summary : Year ended 31 March 2026") == []


def test_unknown_or_unreadable_advisory_is_silent():                      # NEGATIVE
    assert precheck.check_advisory_slot("some other letter") == []
    assert precheck.check_advisory_slot(None) == []
    assert precheck.read_first_page_text(str(ROOT / "nope.pdf"), None) is None


def test_target_comp_detection_is_marked_unverified_until_a_specimen_exists():
    """GUARD. The two printed titles come from the build brief; no real
    target-compensation letter is in the repo. When a specimen fixture is
    added and the detection verified against it, flip the flag, add the
    specimen test, and delete this guard."""
    src = (ROOT / "src/agents/skill_partner_comp_recon/precheck.py").read_text(encoding="utf-8")
    assert "TODO(UI-17, needs a real specimen)" in src
    assert precheck.ADVISORY_TITLE_UNVERIFIED is True
    specimens = list((ROOT / "tests").rglob("*target_comp*"))
    assert not specimens, (
        "A target-compensation specimen now exists: verify classify_advisory_text "
        f"against it, set ADVISORY_TITLE_UNVERIFIED = False, drop this guard: {specimens}")


# ---- .eml ------------------------------------------------------------------

def _eml(tmp_path, with_pdf=True, name="stmt.pdf"):
    m = EmailMessage()
    m["Subject"] = "Statement"
    m["From"] = "a@example.invalid"
    m["To"] = "b@example.invalid"
    m.set_content("see attached")
    if with_pdf:
        m.add_attachment(b"%PDF-1.4 synthetic", maintype="application",
                         subtype="pdf", filename=name)
    p = tmp_path / "mail.eml"
    p.write_bytes(bytes(m))
    return p


def test_eml_pdf_attachment_is_extracted(tmp_path):
    out = precheck.extract_pdf_from_eml(str(_eml(tmp_path)))
    assert Path(out).read_bytes() == b"%PDF-1.4 synthetic"
    assert Path(out).name == "stmt.pdf"


def test_eml_without_a_pdf_is_a_clear_error(tmp_path):                    # NEGATIVE
    with pytest.raises(ValueError, match="no PDF attachment"):
        precheck.extract_pdf_from_eml(str(_eml(tmp_path, with_pdf=False)))


def test_llp_leg_reads_the_eml_attachment(tmp_path, monkeypatch):
    got = {}

    def fake_parse(path, password):
        got["bytes"] = Path(path).read_bytes()
        return {"financial_year": FY}

    monkeypatch.setattr(agent_module._llp_statement_parser, "parse", fake_parse)
    note, rec = agent_module._resolve_llp_leg(str(_eml(tmp_path)), None)
    assert got["bytes"] == b"%PDF-1.4 synthetic" and rec == {"financial_year": FY}
    assert "parsed from" in note


def test_llp_leg_eml_with_no_pdf_degrades_not_crashes(tmp_path):          # NEGATIVE
    note, rec = agent_module._resolve_llp_leg(str(_eml(tmp_path, with_pdf=False)), None)
    assert rec is None and "not available" in note


def test_a_plain_pdf_is_passed_straight_through(tmp_path, monkeypatch):    # NEGATIVE
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4 x")
    seen = []
    monkeypatch.setattr(agent_module._llp_statement_parser, "parse",
                        lambda p, pw: seen.append(p) or {})
    agent_module._resolve_llp_leg(str(pdf), None)
    assert seen == [str(pdf)]


# ---- default journal paths -------------------------------------------------

def test_default_journal_paths_are_named_for_entity_and_fy(tmp_path):
    j, a = precheck.default_journal_paths("TEST-IND", "2025-26", str(tmp_path / "o" / "r.xlsx"))
    assert Path(j).parent == tmp_path / "o" == Path(a).parent
    assert Path(j).name == "TEST-IND-FY2025-26-partner-journal.csv"
    assert Path(a).name == "TEST-IND-FY2025-26-partner-accrual-journal.csv"


def test_default_paths_differ_by_entity_and_year(tmp_path):
    o = str(tmp_path / "r.xlsx")
    assert precheck.default_journal_paths("A", "2025-26", o) != precheck.default_journal_paths("B", "2025-26", o)
    assert precheck.default_journal_paths("A", "2024-25", o) != precheck.default_journal_paths("A", "2025-26", o)


def test_form_opens_with_auto_for_both_journals():
    d = {i.name: i.default for i in _skill().inputs}
    assert d["journal_path"] == "auto" and d["accrual_journal_path"] == "auto"
    assert all(not i.default for i in _skill().inputs
               if i.name not in ("journal_path", "accrual_journal_path"))


def test_auto_journal_is_skipped_not_failed_without_a_book(tmp_path, monkeypatch):   # NEGATIVE
    d = tmp_path / "advices"
    d.mkdir()
    (d / "a.pdf").write_bytes(b"%PDF-1.4 fake")
    adv = tmp_path / "adv.pdf"
    adv.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(agent_module._advisory_parser, "parse",
                        lambda p, pw: {"financial_year": FY})
    monkeypatch.setattr(agent_module._payout_advice_parser, "parse",
                        lambda p, pw: cert("2025-04"))
    monkeypatch.setattr(agent_module, "_resolve_entity_config", lambda e, c: (None, None))
    out = agent_module._run_from_documents(
        entity="X", advices_dir=str(d), doc_password=None, advisory_path=str(adv),
        llp_statement="", payment_schedule="", gnucash_path="", xlsx_26as="",
        output_path=str(tmp_path / "o.xlsx"), config_path=None, model_override=None,
        journal_path="auto", accrual_journal_path="auto")
    assert not list(tmp_path.glob("*.csv"))
    assert "journal_path was supplied" not in out
    assert "Journal CSV: skipped" in out
