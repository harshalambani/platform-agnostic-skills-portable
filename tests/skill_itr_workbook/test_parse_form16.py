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
from datetime import date
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

import as26 as as26_engine  # noqa: E402
import parse_eguile as pe  # noqa: E402
import parse_form16 as pf  # noqa: E402
import verify as book_verify  # noqa: E402
import mapping  # noqa: E402
import configs  # noqa: E402
import fixture_gen  # noqa: E402
import agent  # noqa: E402

# Minimal tds_sections map, same shape rules.common["tds_sections"] provides
# (see test_schedules.py's _TDS_SECTIONS) -- only "salary" matters here.
_TDS_SECTIONS = {"salary": ["192", "192A"]}

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
    assert data.regime == "old"
    assert data.extra_certificates == []

    # Every numeric field extracted (none left as None). regime_unparsed_reason
    # is deliberately excluded: it is populated ONLY when regime could not be
    # determined (mutually exclusive with regime itself being set -- see the
    # regime-extraction tests below), so it is expected to stay None here.
    import dataclasses
    for f in dataclasses.fields(data):
        if f.name in ("identity_checks", "extra_certificates", "regime_unparsed_reason"):
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


def test_two_certificate_pdf_flags_unparseable_extra_not_dropped(tmp_path):
    """The extra certificate here is Part-A-only (no Part B/Annexure-I) --
    it must be surfaced as `parsed=False` with a reason, never dropped and
    never folded into the aggregate totals as if it were zero."""
    pdf_path = tmp_path / "form16_two.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(two_certificates=True))
    data = pf.parse_form16(str(pdf_path))

    assert data.tan == fixture_gen.SYN_IND_FORM16_TAN
    assert data.identity_ok
    assert len(data.extra_certificates) == 1

    extra = data.extra_certificates[0]
    assert extra.tan == fixture_gen.SYN_IND_FORM16_EXTRA_TAN
    assert extra.certificate_no == fixture_gen.SYN_IND_FORM16_EXTRA_CERT
    assert extra.parsed is False
    assert extra.unparsed_reason
    assert extra.gross_salary_1d is None
    assert extra.tds_net_tax_payable_21 is None

    assert data.unparsed_certificates == [extra]

    # The unparsed certificate is EXCLUDED from the aggregate, not treated
    # as zero -- the aggregate is exactly the primary certificate's own
    # figures, same as the single-employer case.
    assert data.aggregate_gross_salary == data.total_1d
    assert data.aggregate_salary_tds == data.net_tax_payable_21


def test_multi_employer_pdf_aggregates_summed_salary_and_tds(tmp_path):
    """A genuine second employer certificate (its own full Part B /
    Annexure-I) must be parsed independently and its figures summed into the
    aggregate totals, with the per-employer breakdown still visible."""
    pdf_path = tmp_path / "form16_multi.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(multi_employer_certificates=True))
    data = pf.parse_form16(str(pdf_path))

    assert data.tan == fixture_gen.SYN_IND_FORM16_TAN
    assert data.identity_ok
    assert len(data.extra_certificates) == 1

    extra = data.extra_certificates[0]
    assert extra.tan == fixture_gen.SYN_IND_FORM16_SECOND_TAN
    assert extra.certificate_no == fixture_gen.SYN_IND_FORM16_SECOND_CERT
    assert extra.parsed is True
    assert extra.unparsed_reason is None
    assert extra.gross_salary_1d == fixture_gen.SYN_IND_FORM16_SECOND_GROSS_1D
    assert extra.tds_net_tax_payable_21 == fixture_gen.SYN_IND_FORM16_SECOND_NET_TAX_21
    assert data.unparsed_certificates == []

    # Per-employer breakdown remains visible (primary certificate's own
    # fields are untouched by the aggregation).
    assert data.total_1d == 500000.00
    assert data.net_tax_payable_21 == 25000.00

    # Aggregate is the sum across both employers.
    assert data.aggregate_gross_salary == 500000.00 + fixture_gen.SYN_IND_FORM16_SECOND_GROSS_1D
    assert data.aggregate_salary_tds == 25000.00 + fixture_gen.SYN_IND_FORM16_SECOND_NET_TAX_21


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
# parse_form16.py -- tax-regime extraction (PR 2)
# ---------------------------------------------------------------------------

def test_regime_explicitly_old_when_opted_out_yes(tmp_path):
    """"Yes" to opting out of 115BAC(1A) means the employee stayed with the
    OLD regime for TDS purposes -- data.regime must read "old", with no
    unparsed_reason."""
    pdf_path = tmp_path / "form16_old.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(regime_election="Yes"))
    data = pf.parse_form16(str(pdf_path))

    assert data.opted_out_115bac == "Yes"
    assert data.regime == "old"
    assert data.regime_unparsed_reason is None


def test_regime_explicitly_new_when_opted_out_no(tmp_path):
    """"No" to opting out of 115BAC(1A) means the employee stayed in the NEW
    regime -- data.regime must read "new", with no unparsed_reason."""
    pdf_path = tmp_path / "form16_new.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(regime_election="No"))
    data = pf.parse_form16(str(pdf_path))

    assert data.opted_out_115bac == "No"
    assert data.regime == "new"
    assert data.regime_unparsed_reason is None


def test_regime_not_determinable_stays_unset_and_flags(tmp_path):
    """When the 115BAC(1A) election is missing from the PDF entirely (never
    printed, or not extractable), regime MUST stay None -- never silently
    defaulted to either old or new -- and regime_unparsed_reason must be
    populated so the gap is impossible to miss."""
    pdf_path = tmp_path / "form16_no_election.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(regime_election=None))
    data = pf.parse_form16(str(pdf_path))

    assert data.opted_out_115bac is None
    assert data.regime is None
    assert data.regime_unparsed_reason
    assert "115BAC" in data.regime_unparsed_reason


# ---------------------------------------------------------------------------
# agent.py -- regime resolution / consumption (PR 2)
# ---------------------------------------------------------------------------

def test_agent_run_regime_undetermined_flags_when_no_override(tmp_path):
    """No --regime-override AND Form 16 does not state a determinable
    115BAC(1A) election -- the run must fall back to the entity's configured
    regime (default "new" for a generic/unconfigured entity) but flag the
    gap loudly via REGIME_UNDETERMINED_WARNING_MARKER, never silently."""
    import presentation

    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    form16_path = tmp_path / "form16_no_election.pdf"
    form16_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(regime_election=None))
    out_path = tmp_path / "out.xlsx"

    summary = agent.run(
        str(html_path), str(out_path),
        mapping_file=str(SYN_IND_MAPPING), form16_pdf=str(form16_path),
    )
    assert "Form16: regime NOT determinable" in summary
    assert presentation.REGIME_UNDETERMINED_WARNING_MARKER in summary
    assert "(regime=new)" in summary   # generic entity's default_regime, used as fallback


def test_agent_run_regime_override_wins_and_disagreement_surfaced(tmp_path):
    """An explicit --regime-override that disagrees with what Form 16 states
    must still WIN (it is a deliberate human choice), but the disagreement
    must be surfaced in the run summary, never swallowed."""
    import presentation

    html_path = tmp_path / "syn_ind.html"
    html_path.write_text(fixture_gen.build_syn_ind_html(), encoding="utf-8")
    form16_path = tmp_path / "form16_old.pdf"
    # Form 16 states "Yes" (opted out -> old regime); override to "new".
    form16_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf(regime_election="Yes"))
    out_path = tmp_path / "out.xlsx"

    summary = agent.run(
        str(html_path), str(out_path),
        mapping_file=str(SYN_IND_MAPPING), form16_pdf=str(form16_path),
        regime_override="new",
    )
    assert "Form16: regime per 115BAC(1A) election = old." in summary
    assert "(regime=new)" in summary   # the override wins, not the Form16-parsed "old"
    assert presentation.REGIME_OVERRIDE_MISMATCH_WARNING_MARKER in summary
    assert "override='new'" in summary
    assert "'old'" in summary   # Form16's implied regime named in the warning line


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
# verify.py -- Form16<->26AS s.192 salary TDS cross-check (GAP B)
# ---------------------------------------------------------------------------

def test_cross_check_form16_26as_salary_agrees_when_matching(tmp_path):
    pdf_path = tmp_path / "form16.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf())
    data = pf.parse_form16(str(pdf_path))
    assert data.tan == fixture_gen.SYN_IND_FORM16_TAN
    assert data.net_tax_payable_21 == 25000.00

    as26_data = as26_engine.As26Data(transactions=[
        as26_engine.As26Transaction(
            fixture_gen.SYN_IND_FORM16_TAN, "Synthetic Employer Pvt Ltd", "192",
            date(2024, 6, 1), 500000.0, 25000.0, 25000.0,
        ),
    ])

    results = book_verify.cross_check_form16_26as_salary(data, as26_data, _TDS_SECTIONS)
    assert len(results) == 1
    assert results[0].tan == fixture_gen.SYN_IND_FORM16_TAN
    assert results[0].ok

    summary = book_verify.summarize_form16_26as_salary(results)
    assert "0 mismatch" in summary


def test_cross_check_form16_26as_salary_fails_loud_on_mismatch(tmp_path):
    pdf_path = tmp_path / "form16.pdf"
    pdf_path.write_bytes(fixture_gen.build_syn_ind_form16_pdf())
    data = pf.parse_form16(str(pdf_path))
    assert data.net_tax_payable_21 == 25000.00

    # 26AS shows less TDS deposited under s.192 than Form16 claims was
    # deducted for the same TAN -- a real filing risk, must never be
    # silently accepted.
    as26_data = as26_engine.As26Data(transactions=[
        as26_engine.As26Transaction(
            fixture_gen.SYN_IND_FORM16_TAN, "Synthetic Employer Pvt Ltd", "192",
            date(2024, 6, 1), 500000.0, 20000.0, 20000.0,
        ),
    ])

    results = book_verify.cross_check_form16_26as_salary(data, as26_data, _TDS_SECTIONS)
    assert len(results) == 1
    r = results[0]
    assert not r.ok
    assert r.form16_tds == 25000.00
    assert r.as26_tds == 20000.00

    summary = book_verify.summarize_form16_26as_salary(results)
    assert "MISMATCH" in summary
    assert f"{r.form16_tds:.2f}" in summary
    assert f"{r.as26_tds:.2f}" in summary


def test_cross_check_form16_26as_salary_flags_tan_with_no_parsed_form16():
    # A TAN present in 26AS section 192 with no successfully parsed Form16
    # certificate for it must still surface as a result (form16_tds=None,
    # .ok forced False) -- never silently dropped from the comparison.
    as26_data = as26_engine.As26Data(transactions=[
        as26_engine.As26Transaction(
            "UNKNOWNTAN1", "Unmatched Employer Pvt Ltd", "192",
            date(2024, 6, 1), 300000.0, 15000.0, 15000.0,
        ),
    ])
    data = pf.Form16Data()  # no tan, no net_tax_payable_21 -- nothing parsed

    results = book_verify.cross_check_form16_26as_salary(data, as26_data, _TDS_SECTIONS)
    assert len(results) == 1
    r = results[0]
    assert r.tan == "UNKNOWNTAN1"
    assert r.form16_tds is None
    assert r.as26_tds == 15000.00
    assert not r.ok

    summary = book_verify.summarize_form16_26as_salary(results)
    assert "MISMATCH" in summary
    assert "no parsed Form16 certificate" in summary


def test_cross_check_form16_26as_salary_empty_when_nothing_to_compare():
    assert book_verify.cross_check_form16_26as_salary(None, None, _TDS_SECTIONS) == []
    data = pf.Form16Data(tan=fixture_gen.SYN_IND_FORM16_TAN, net_tax_payable_21=25000.00)
    assert book_verify.cross_check_form16_26as_salary(data, None, _TDS_SECTIONS) == []
    assert book_verify.cross_check_form16_26as_salary(None, as26_engine.As26Data(), _TDS_SECTIONS) == []


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
        entities_path=str(ROOT / "bundling" / "canonical" / "itr" / "entities.example.yaml"), entity_key="SYN-IND",
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
