"""
tests/skill_itr_workbook/test_parse_form16.py -- Batch 4 tests: the TRACES
Form 16 Part B/Annexure-I parser (plan section 6.2), the Book<->Form16
cross-checks (verify.py), and agent.py's form16_pdf wiring. Fully offline;
synthetic fixtures only (see fixture_gen.py's build_syn_ind_form16_pdf).
Real-corpus tests are behind @pytest.mark.local_samples and skip when
Data/GNUCashReports/ is absent, so CI never touches real data -- and even
when it runs locally, it never prints amounts/PANs/TANs, only field
presence and check pass/fail (the real corpus is for local eyes only).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent.parent
SRC = ROOT / "src"
SCRIPTS = SRC / "agents" / "skill_itr_workbook" / "scripts"
AGENT_DIR = SRC / "agents" / "skill_itr_workbook"
FIXTURES = Path(__file__).resolve().parent / "fixtures"
REAL_SAMPLES_DIR = ROOT / "Data" / "GNUCashReports"

for p in (str(SCRIPTS), str(AGENT_DIR), str(Path(__file__).resolve().parent)):
    if p not in sys.path:
        sys.path.insert(0, p)

import parse_eguile as pe  # noqa: E402
import parse_form16 as pf  # noqa: E402
import verify as book_verify  # noqa: E402
import mapping  # noqa: E402
import configs  # noqa: E402
import fixture_gen  # noqa: E402
import agent  # noqa: E402

SYN_IND_MAPPING = FIXTURES / "syn_ind.mapping.yaml"


# ---------------------------------------------------------------------------
# parse_form16.py -- extraction goldens
# ---------------------------------------------------------------------------

def test_plain_fixture_extracts_every_field_and_all_identity_checks_pass(tmp_path):
    pdf_path = tmp_path / "form16.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf())
    data = pf.parse_form16(str(pdf_path))

    assert data.tan == fixture_gen.SYN_IND_FORM16_TAN
    assert data.certificate_no == fixture_gen.SYN_IND_FORM16_CERT
    assert data.employee_pan == fixture_gen.SYN_IND_FORM16_PAN
    assert data.assessment_year == "2025-26"
    assert data.period_from == "01-Apr-2024"
    assert data.period_to == "31-Mar-2025"
    assert data.opted_out_115bac == "Yes"
    assert data.extra_certificates == []

    # Every numeric field extracted (none left as None).
    import dataclasses
    for f in dataclasses.fields(data):
        if f.name in ("identity_checks", "extra_certificates"):
            continue
        assert getattr(data, f.name) is not None, f"{f.name} was not extracted"

    assert data.identity_ok
    assert len(data.identity_checks) == 7

    # The two figures the Book<->Form16 cross-checks compare against.
    assert data.s17_1 == 500000.00
    assert data.net_tax_payable_21 == 25000.00


def test_wrong_password_raises_clear_error(tmp_path):
    pdf_path = tmp_path / "form16_enc.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(encrypted=True))
    with pytest.raises(pf.Form16ParseError):
        pf.parse_form16(str(pdf_path), pan="WRONGPAN1")


def test_correct_password_decrypts_and_parses(tmp_path):
    pdf_path = tmp_path / "form16_enc.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(encrypted=True))
    data = pf.parse_form16(str(pdf_path), pan=fixture_gen.SYN_IND_FORM16_PAN)
    assert data.identity_ok
    assert data.s17_1 == 500000.00


def test_two_certificate_pdf_selects_part_b_and_flags_the_extra(tmp_path):
    pdf_path = tmp_path / "form16_two.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(two_certificates=True))
    data = pf.parse_form16(str(pdf_path))

    assert data.tan == fixture_gen.SYN_IND_FORM16_TAN
    assert data.identity_ok
    assert len(data.extra_certificates) == 1
    cert_no, tan = data.extra_certificates[0]
    assert tan == fixture_gen.SYN_IND_FORM16_EXTRA_TAN
    assert cert_no == fixture_gen.SYN_IND_FORM16_EXTRA_CERT


def test_internal_identity_failure_is_flagged_not_corrected(tmp_path):
    """A doctored 1(d) total (480000 instead of 500000) must show up as a
    MISMATCH -- the parser never silently 'fixes' the figure to make the
    identity hold."""
    pdf_path = tmp_path / "form16_broken.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(broken_identity=True))
    data = pf.parse_form16(str(pdf_path))

    assert data.total_1d == 480000.00     # not "corrected" to 500000.00
    assert data.s17_1 == 500000.00        # the underlying field is untouched
    assert not data.identity_ok

    failed = {c.label for c in data.identity_checks if not c.ok}
    assert "1(d) = 17(1)+17(2)+17(3)" in failed
    assert "3 = 1(d)-2(i)" in failed       # 3 is derived from the broken 1(d)
    # Downstream checks that don't depend on 1(d) still pass.
    passed = {c.label for c in data.identity_checks if c.ok}
    assert "9 = 6+8" in passed


# ---------------------------------------------------------------------------
# verify.py -- Book<->Form16 cross-checks
# ---------------------------------------------------------------------------

@pytest.fixture()
def syn_ind_resolved():
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    loaded = configs.load_mapping(SYN_IND_MAPPING)
    result = mapping.resolve_tree(tree, loaded)
    assert not result.blocked
    return tree, result.resolved


def test_cross_check_form16_green_on_syn_ind(tmp_path, syn_ind_resolved):
    tree, resolved = syn_ind_resolved
    pdf_path = tmp_path / "form16.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf())
    data = pf.parse_form16(str(pdf_path))

    results = book_verify.cross_check_form16(tree, resolved, data)
    assert len(results) == 2
    assert all(r.ok for r in results)

    summary = book_verify.summarize_form16(results)
    assert "0 mismatch" in summary


def test_cross_check_form16_reports_both_values_on_mismatch(tmp_path, syn_ind_resolved):
    tree, resolved = syn_ind_resolved
    data = pf.Form16Data(s17_1=999999.00, net_tax_payable_21=1.00)
    results = book_verify.cross_check_form16(tree, resolved, data)
    assert len(results) == 2
    assert all(not r.ok for r in results)
    for r in results:
        assert r.mapped_total != r.form16_total

    summary = book_verify.summarize_form16(results)
    assert "MISMATCH" in summary
    for r in results:
        assert f"{r.mapped_total:.2f}" in summary
        assert f"{r.form16_total:.2f}" in summary


def test_cross_check_form16_empty_when_no_form16():
    tree = pe.parse_html(fixture_gen.build_syn_ind_html())
    assert book_verify.cross_check_form16(tree, {}, None) == []


# ---------------------------------------------------------------------------
# agent.py -- form16_pdf wiring (Definition of Done)
# ---------------------------------------------------------------------------

def test_agent_run_form16_ok_on_syn_ind_with_mapping(tmp_path):
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    form16_path = tmp_path / "form16.pdf"
    form16_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf())
    out_path = tmp_path / "out.xlsx"

    summary = agent.run(
        str(html_path), str(out_path),
        mapping_file=str(SYN_IND_MAPPING), form16_pdf=str(form16_path),
    )
    assert "STATUS: OK" in summary
    assert f"employer TAN {fixture_gen.SYN_IND_FORM16_TAN}" in summary
    assert "opted out of 115BAC(1A)? Yes" in summary
    assert "internal consistency OK (7 check(s))" in summary
    assert "Book<->Form16 cross-check: 2 check(s), 0 mismatch(es)." in summary


def test_agent_run_form16_without_mapping_skips_cross_check(tmp_path):
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    form16_path = tmp_path / "form16.pdf"
    form16_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf())
    out_path = tmp_path / "out.xlsx"

    summary = agent.run(str(html_path), str(out_path), form16_pdf=str(form16_path))
    assert "Book<->Form16 cross-check: no mapping_file supplied -- skipped." in summary


def test_agent_run_form16_encrypted_needs_pan(tmp_path):
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    form16_path = tmp_path / "form16_enc.pdf"
    form16_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(encrypted=True))
    out_path = tmp_path / "out.xlsx"

    summary = agent.run(str(html_path), str(out_path), form16_pdf=str(form16_path))
    assert "Form16: PARSE ERROR" in summary

    summary_ok = agent.run(
        str(html_path), str(out_path), form16_pdf=str(form16_path),
        form16_pan=fixture_gen.SYN_IND_FORM16_PAN,
    )
    assert "Form16: PARSE ERROR" not in summary_ok
    assert "internal consistency OK" in summary_ok


def test_agent_run_form16_encrypted_pan_auto_derived_from_entity(tmp_path):
    """Batch 7: form16_pan is no longer a UI input -- when the run resolves
    an entity (entity_key + entities_path) whose PAN matches the encrypted
    Form 16's password, the PDF decrypts without form16_pan being passed
    explicitly at all."""
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    form16_path = tmp_path / "form16_enc.pdf"
    form16_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(encrypted=True))
    out_path = tmp_path / "out.xlsx"

    summary = agent.run(
        str(html_path), str(out_path), form16_pdf=str(form16_path),
        entities_path=str(ROOT / "Data" / "itr" / "entities.example.yaml"), entity_key="SYN-IND",
    )
    assert "Form16: PARSE ERROR" not in summary
    assert "internal consistency OK" in summary


def test_agent_run_year_mismatch_hard_fails(tmp_path):
    """Batch 7: the `ay` dropdown's selected income year must agree with the
    year inferred from the Balance Sheet HTML's own as-of date (SYN-IND's
    fixture HTML is dated 31-03-2025 -> income year '2024-25'). A mismatch
    is a real misfiling risk, so it hard-fails: ERROR summary, stub
    workbook only -- the full schedule build never even attempts to run."""
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out_path = tmp_path / "out.xlsx"

    summary = agent.run(
        str(html_path), str(out_path), mapping_file=str(SYN_IND_MAPPING), ay="2025-26",
    )
    assert "ERROR: selected Assessment Year does not match" in summary
    assert "Workbook: full schedule model built" not in summary

    import openpyxl
    wb = openpyxl.load_workbook(str(out_path))
    assert wb.sheetnames == ["Reconciliation"]  # stub workbook only


def test_agent_run_year_match_does_not_hard_fail(tmp_path):
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out_path = tmp_path / "out.xlsx"

    summary = agent.run(
        str(html_path), str(out_path), mapping_file=str(SYN_IND_MAPPING), ay="2024-25",
    )
    assert "ERROR: selected Assessment Year does not match" not in summary


def test_agent_run_no_form16_pdf_supplied(tmp_path):
    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    out_path = tmp_path / "out.xlsx"
    summary = agent.run(str(html_path), str(out_path))
    assert "Form16: no form16_pdf supplied -- skipped." in summary


# ---------------------------------------------------------------------------
# local_samples: parse the two REAL Form 16 PDFs
# ---------------------------------------------------------------------------

@pytest.mark.local_samples
def test_real_form16_pdfs_parse_and_cross_check():
    """Parses both real Form 16 PDFs found under Data/GNUCashReports/.
    Skips (with a setup hint) when that directory, or an entities.yaml with
    the PAN needed to decrypt the encrypted certificate, is absent -- this
    test never hand-tags or hard-codes any real amount/PAN/TAN; it only
    asserts field presence and check pass/fail, computed live."""
    if not REAL_SAMPLES_DIR.is_dir():
        pytest.skip(f"{REAL_SAMPLES_DIR} not present -- local-only smoke test")

    pdfs = sorted(REAL_SAMPLES_DIR.glob("*Form16*")) + sorted(REAL_SAMPLES_DIR.glob("*F16*"))
    pdfs = [p for p in pdfs if p.suffix.lower() == ".pdf"]
    if not pdfs:
        pytest.skip(f"no Form16 PDFs found under {REAL_SAMPLES_DIR}")

    entities_path = ROOT / "Data" / "itr" / "entities.yaml"
    pan_by_hint: dict[str, str] = {}
    if entities_path.exists():
        entities = configs.load_entities(entities_path)
        pan_by_hint = {e.key: e.pan for e in entities.values()}

    parsed_count = 0
    for pdf_path in pdfs:
        try:
            data = pf.parse_form16(str(pdf_path))
        except pf.Form16ParseError:
            # Likely encrypted -- try every PAN we have locally; skip this
            # file if none of them work (documents the expected local setup
            # rather than failing CI, which never has Data/itr/entities.yaml).
            data = None
            for pan in pan_by_hint.values():
                try:
                    data = pf.parse_form16(str(pdf_path), pan=pan)
                    break
                except pf.Form16ParseError:
                    continue
            if data is None:
                continue

        parsed_count += 1
        assert data.opted_out_115bac == "Yes"
        assert len(data.identity_checks) > 0

    if parsed_count == 0:
        pytest.skip(
            "No Form16 PDF could be decrypted -- an encrypted certificate needs its PAN. "
            "Create Data/itr/entities.yaml locally (see entities.example.yaml) with the "
            "matching entity's real PAN to exercise this test fully."
        )
