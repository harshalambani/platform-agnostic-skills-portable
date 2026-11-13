"""
tests/test_ais_reconcile.py — Tests for the AIS decrypt + normalize
foundation layer (src/agents/skill_ais_reconcile/).

Uses a SYNTHETIC AIS JSON dict (fake PAN, fake dates, fake amounts) that we
encrypt ourselves with a throwaway password using the same scheme
decrypt.decrypt_ais expects, then round-trip it through decrypt -> normalize.
No real taxpayer data — nothing under Data/AIS/ is read or referenced.

Two synthetic fixture families are covered:
  - synthetic_ais(): hand-built DICT-keyed columnData rows (legacy/simple
    shape some fixtures use).
  - synthetic_ais_positional(): mirrors the REAL AIS export shape, where
    columnData rows are POSITIONAL LISTS of scalars, keyed by `seq` in a
    dict-descriptor columnLabel (l1-style) or by plain-string columnLabel
    headers aligned 1:1 by index (l2-style), with columnDataType as the
    authoritative parallel type list.

Run with:
    cd src && python -m pytest ../tests/test_ais_reconcile.py -v
"""
from __future__ import annotations

import copy
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
# configs.py (skill_itr_workbook) does a bare `import tags` assuming its own
# scripts/ dir is on sys.path directly (the convention used throughout
# tests/skill_itr_workbook/) -- add it so the package-path import below of
# configs.py (for Phase C's MappingEntry/MappingLoadResult) succeeds too.
_ITR_SCRIPTS = SRC / "agents" / "skill_itr_workbook" / "scripts"
if str(_ITR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ITR_SCRIPTS))

from agents.skill_ais_reconcile import decrypt as D  # noqa: E402
from agents.skill_ais_reconcile import normalize as N  # noqa: E402
from agents.skill_ais_reconcile import reconcile as R  # noqa: E402
from agents.skill_ais_reconcile import excel_writer as X  # noqa: E402
from agents.skill_ais_reconcile import reconcile_26as as R26  # noqa: E402
from agents.skill_itr_workbook.scripts.as26 import As26Data, As26Transaction  # noqa: E402

PAN = "ABCDE1234F"
DOB_ISO = "1985-06-15"
DOB_DDMMYYYY = "15061985"


# ---------------------------------------------------------------------------
# Synthetic fixture builder
# ---------------------------------------------------------------------------

def _column_label(field_, name, type_, sum_required="Y", display_feedback=False):
    return {
        "field": field_, "name": name, "type": type_, "seq": 1,
        "sumRequired": sum_required, "isEditable": True, "isMandatory": False,
        "displayFeedback": display_feedback, "infoCode": None,
        "validationRegex": None,
    }


def _tds_element():
    column_label = [
        _column_label("deductorName", "Name of Deductor", "string"),
        _column_label("section", "Section", "string"),
        _column_label("amtPaid", "Amount Paid", "decimal", display_feedback=True),
        _column_label("taxDeducted", "Tax Deducted", "decimal"),
        _column_label("dateOfPayment", "Date of Payment", "date"),
    ]
    l1 = {
        "columnLabel": column_label,
        "columnData": [
            {
                "deductorName": "SYNTHETIC BANK LIMITED",
                "section": "194A",
                "amtPaid": "250237.00",
                "taxDeducted": "25024.00",
                "dateOfPayment": "2026-01-15",
                "status": "Active",
                "transFeedback": None,
            },
            {
                # blank/None decimal must coerce to None, not crash
                "deductorName": "SYNTHETIC NBFC LTD",
                "section": "193",
                "amtPaid": "",
                "taxDeducted": None,
                "dateOfPayment": "2026-02-01",
                "status": "Active",
                "transFeedback": "Feedback submitted",
            },
        ],
        "columnDataType": {},
    }
    return {
        "title": "Interest from deposit",
        "l1Src": "TDS",
        "infoSrcId": "SFT-001",
        "l1": l1,
        "l2": None,
    }


def _sft_element():
    column_label = [
        _column_label("sellerName", "Name of Seller", "string"),
        _column_label("salesConsideration", "Sales Consideration", "decimal",
                       display_feedback=True),
    ]
    l1 = {
        "columnLabel": column_label,
        "columnData": [
            {
                "sellerName": "SYNTHETIC BROKING PVT LTD",
                "salesConsideration": "1000000.00",
                "status": "Active",
                "transFeedback": None,
            },
        ],
        "columnDataType": {},
    }
    return {
        "title": "Sale of securities",
        "l1Src": "SFT",
        "infoSrcId": "SFT-002",
        "l1": l1,
        "l2": l1,   # exercise the l2 branch too (same shape as l1)
    }


def _section_with_string_column_labels():
    """A header-like section where columnLabel entries are plain strings, not
    descriptor dicts — the normalizer must guard against this, not crash."""
    return {
        "title": "Other info",
        "l1Src": "OTHER",
        "infoSrcId": None,
        "l1": {
            "columnLabel": ["Some Header", "Another Header"],
            "columnData": [{"Some Header": "x", "Another Header": "y"}],
            "columnDataType": {},
        },
        "l2": None,
    }


def _column_label_seq(field_, name, type_, seq, display_feedback=False):
    """Dict-descriptor columnLabel entry for the POSITIONAL row shape: `seq`
    is the column index into the row, and (per real AIS files) only a subset
    of a row's columns are described this way."""
    return {
        "field": field_, "name": name, "type": type_, "seq": seq,
        "sumRequired": "Y", "isEditable": True, "isMandatory": False,
        "displayFeedback": display_feedback, "infoCode": None,
        "validationRegex": None,
    }


def _positional_tds_element():
    """Mirrors the REAL l1 shape: columnData rows are 15-element positional
    lists of scalars; columnLabel is a list of dict descriptors (only ~8 of
    the 15 columns described, keyed by `seq`); columnDataType is a parallel
    15-entry list of type strings that is authoritative for coercion. Also
    exercises status/transFeedback arriving as descriptors (popped into
    AisRow.status/trans_feedback, not left in `fields`)."""
    column_label = [
        _column_label_seq("deductorName", "Name of Deductor", "string", 0),
        _column_label_seq("section", "Section", "string", 1),
        _column_label_seq("amtPaid", "Amount Paid", "decimal", 3, display_feedback=True),
        _column_label_seq("taxDeducted", "Tax Deducted", "decimal", 5),
        _column_label_seq("dateOfPayment", "Date of Payment", "date", 7),
        _column_label_seq("status", "Status", "string", 9),
        _column_label_seq("transFeedback", "Transaction Feedback", "string", 10),
        _column_label_seq("infoCode", "Information Code", "string", 12),
    ]
    column_data_type = [
        "string", "string", "string", "decimal", "string", "decimal", "string",
        "date", "string", "string", "string", "string", "string", "string", "string",
    ]
    row_full = [
        "SYNTHETIC BANK LIMITED", "194A", None, "250237.00", None, "25024.00",
        None, "2026-01-15", None, "Active", "Feedback submitted", None,
        "INFO123", None, None,
    ]
    # Blank/None amtPaid + taxDeducted must coerce to None, not crash.
    row_blank_amounts = [
        "SYNTHETIC NBFC LTD", "193", None, "", None, None,
        None, "2026-02-01", None, "Active", None, None,
        "INFO456", None, None,
    ]
    l1 = {
        "columnLabel": column_label,
        "columnData": [row_full, row_blank_amounts],
        "columnDataType": column_data_type,
    }
    return {
        "title": "Interest from deposit (positional)",
        "l1Src": "TDS",
        "infoSrcId": "SFT-003",
        "l1": l1,
        "l2": None,
    }


def _positional_l2_element():
    """Mirrors the REAL l2 shape: columnData rows are 9-element positional
    lists; columnLabel is a list of PLAIN STRING headers aligned 1:1 by
    position (no dict descriptors at all). The reconciliation money fields
    (Amount / Derived Amount) live here."""
    column_label = [
        "Information Category", "Information Code", "Information Description",
        "Information Source", "Count", "Amount", "Information Category Code",
        "Derived Amount", "Qualifies For",
    ]
    column_data_type = [
        "string", "string", "string", "string", "numeric", "decimal",
        "string", "decimal", "string",
    ]
    row = [
        "Interest", "SFT-017", "Interest income reported by deductor", "AIS",
        "1", "50000.00", "SFT017", "48000.00", "Yes",
    ]
    l2 = {
        "columnLabel": column_label,
        "columnData": [row],
        "columnDataType": column_data_type,
    }
    return {
        "title": "Interest from deposit (positional l2)",
        "l1Src": "TDS",
        "infoSrcId": "SFT-004",
        "l1": None,
        "l2": l2,
    }


def synthetic_ais_positional() -> dict:
    """A minimal AIS dict exercising ONLY the real positional columnData
    shapes (see _positional_tds_element / _positional_l2_element) — kept
    separate from synthetic_ais() so the dict-row fixture's row-count
    assertions don't have to change."""
    return {
        "metadata": {
            "loggedInPan": PAN,
            "jsonVersion": "1.0",
            "downloadDate": "27-Jul-2026",
            "utilityVersion": "1.0.0",
            "sourceSharedFeedbackFeatureEnabled": True,
        },
        "header": {},
        "partA": {"columnLabel": ["PAN"], "columnData": [PAN]},
        "partB": {
            "sections": [
                {
                    "sectionKey": "tdsTcs",
                    "title": "TDS/TCS Information",
                    "heading": "B1",
                    "elements": [_positional_tds_element(), _positional_l2_element()],
                },
            ],
        },
        "rejectedFeedbacks": [],
        "footer": {},
        "fileSize": 999,
    }


def synthetic_ais() -> dict:
    return {
        "metadata": {
            "loggedInPan": PAN,
            "jsonVersion": "1.0",
            "downloadDate": "27-Jul-2026",
            "utilityVersion": "1.0.0",
            "sourceSharedFeedbackFeatureEnabled": True,
        },
        "header": {},
        "partA": {
            "columnLabel": ["PAN", "Aadhaar", "Name", "DOB", "Mobile", "Email", "Address"],
            "columnData": [PAN, "XXXX-XXXX-1234", "SYNTHETIC TAXPAYER", "15-Jun-1985",
                           "+91-9999999999", "synthetic@example.test", "123 Synthetic Street"],
        },
        "partB": {
            "sections": [
                {
                    "sectionKey": "tdsTcs",
                    "title": "TDS/TCS Information",
                    "heading": "B1",
                    "elements": [_tds_element()],
                },
                {
                    "sectionKey": "sft",
                    "title": "SFT Information",
                    "heading": "B2",
                    "elements": [_sft_element()],
                },
                {
                    # sectionKey with no `elements` key at all -- must be skipped, not crash
                    "sectionKey": "demandAndRefund",
                    "title": "Demand and Refund",
                    "heading": "B4",
                },
                {
                    # `elements` explicitly None -- same guard
                    "sectionKey": "paymentOfTaxes",
                    "title": "Payment of Taxes",
                    "heading": "B3",
                    "elements": None,
                },
                {
                    "sectionKey": "other-info",
                    "title": "Other Information",
                    "heading": "B7",
                    "elements": [_section_with_string_column_labels()],
                },
            ],
        },
        "rejectedFeedbacks": [],
        "footer": {},
        "fileSize": 12345,
    }


# ---------------------------------------------------------------------------
# decrypt.py
# ---------------------------------------------------------------------------

def test_derive_password_default_pepper():
    pw = D.derive_password(PAN, DOB_DDMMYYYY)
    assert pw == f"{PAN.lower()}GQ39%*g{DOB_DDMMYYYY}"


def test_derive_password_empty_pepper_variant():
    pw = D.derive_password(PAN, DOB_DDMMYYYY, pepper="")
    assert pw == f"{PAN.lower()}{DOB_DDMMYYYY}"


def test_derive_password_from_iso_date_matches_ddmmyyyy():
    assert D.derive_password_from_iso_date(PAN, DOB_ISO) == D.derive_password(PAN, DOB_DDMMYYYY)


def test_encrypt_decrypt_round_trip():
    ais = synthetic_ais()
    password = D.derive_password(PAN, DOB_DDMMYYYY)
    blob = D._encrypt_for_test(ais, password)
    out = D.decrypt_ais(blob, password)
    assert out == ais


def test_decrypt_wrong_password_raises_ais_decrypt_error():
    ais = synthetic_ais()
    password = D.derive_password(PAN, DOB_DDMMYYYY)
    blob = D._encrypt_for_test(ais, password)
    with pytest.raises(D.AisDecryptError):
        D.decrypt_ais(blob, "totally-wrong-password")


def test_decrypt_garbage_header_raises_ais_decrypt_error():
    with pytest.raises(D.AisDecryptError):
        D.decrypt_ais("not a valid ais blob at all", "whatever")


def test_decrypt_too_short_raises_ais_decrypt_error():
    with pytest.raises(D.AisDecryptError):
        D.decrypt_ais("abcd1234", "whatever")


def test_encrypt_decrypt_deterministic_with_fixed_iv_salt():
    ais = {"a": 1}
    password = "pw"
    iv = b"\x00" * 16
    salt = b"\x01" * 16
    blob1 = D._encrypt_for_test(ais, password, iv=iv, salt=salt)
    blob2 = D._encrypt_for_test(ais, password, iv=iv, salt=salt)
    assert blob1 == blob2
    assert D.decrypt_ais(blob1, password) == ais


# ---------------------------------------------------------------------------
# normalize.py
# ---------------------------------------------------------------------------

def test_normalize_metadata():
    n = N.normalize(synthetic_ais())
    assert n.json_version == "1.0"
    assert n.download_date == "27-Jul-2026"
    assert n.logged_in_pan == PAN


def test_normalize_part_a_zips_labels_and_data():
    n = N.normalize(synthetic_ais())
    assert n.part_a["PAN"] == PAN
    assert n.part_a["Name"] == "SYNTHETIC TAXPAYER"
    assert n.part_a["DOB"] == "15-Jun-1985"


def test_normalize_flattens_rows_across_sections():
    n = N.normalize(synthetic_ais())
    # 2 rows in tdsTcs l1, 1 row in sft l1 + 1 row in sft l2 (same block used
    # twice), 1 row in other-info l1 -> 5 total.
    assert len(n.rows) == 5


def test_normalize_decimal_coercion():
    n = N.normalize(synthetic_ais())
    tds_rows = [r for r in n.rows if r.section_key == "tdsTcs"]
    row0 = next(r for r in tds_rows if r.fields["deductorName"] == "SYNTHETIC BANK LIMITED")
    from decimal import Decimal
    assert row0.fields["amtPaid"] == Decimal("250237.00")
    assert row0.fields["taxDeducted"] == Decimal("25024.00")
    assert row0.category == "Interest from deposit"
    assert row0.section_title == "TDS/TCS Information"
    assert row0.info_src_id == "SFT-001"
    assert row0.level == "l1"


def test_normalize_blank_and_none_decimal_become_none_not_crash():
    n = N.normalize(synthetic_ais())
    row1 = next(r for r in n.rows if r.fields.get("deductorName") == "SYNTHETIC NBFC LTD")
    assert row1.fields["amtPaid"] is None
    assert row1.fields["taxDeducted"] is None


def test_normalize_feedback_fields_and_editable_flag():
    n = N.normalize(synthetic_ais())
    row0 = next(r for r in n.rows if r.fields.get("deductorName") == "SYNTHETIC BANK LIMITED")
    assert row0.status == "Active"
    assert row0.feedback_editable_field == "amtPaid"

    row1 = next(r for r in n.rows if r.fields.get("deductorName") == "SYNTHETIC NBFC LTD")
    assert row1.trans_feedback == "Feedback submitted"


def test_normalize_sft_feedback_editable_field_is_sales_consideration():
    n = N.normalize(synthetic_ais())
    sft_rows = [r for r in n.rows if r.section_key == "sft"]
    assert sft_rows
    for r in sft_rows:
        assert r.feedback_editable_field == "salesConsideration"


def test_normalize_l2_processed_same_shape_as_l1():
    n = N.normalize(synthetic_ais())
    levels = {r.level for r in n.rows if r.section_key == "sft"}
    assert levels == {"l1", "l2"}


def test_normalize_section_missing_elements_key_is_skipped_not_crashed():
    n = N.normalize(synthetic_ais())
    assert not any(r.section_key == "demandAndRefund" for r in n.rows)


def test_normalize_section_elements_none_is_skipped_not_crashed():
    n = N.normalize(synthetic_ais())
    assert not any(r.section_key == "paymentOfTaxes" for r in n.rows)


def test_normalize_string_column_labels_guarded_no_crash():
    """Some columnLabel entries are plain strings (header-like sections);
    those rows still show up but with no type-driven coercion applied."""
    n = N.normalize(synthetic_ais())
    other_rows = [r for r in n.rows if r.section_key == "other-info"]
    assert len(other_rows) == 1
    assert other_rows[0].fields == {"Some Header": "x", "Another Header": "y"}
    assert other_rows[0].feedback_editable_field is None


def test_normalize_summary_counts_per_section_category():
    n = N.normalize(synthetic_ais())
    summary = {(s.section_key, s.category): s.row_count for s in n.summary}
    assert summary[("tdsTcs", "Interest from deposit")] == 2
    assert summary[("sft", "Sale of securities")] == 2  # l1 + l2
    assert summary[("other-info", "Other info")] == 1  # element title, not section title


# ---------------------------------------------------------------------------
# REAL positional columnData shape (rows are lists, not dicts) — regression
# coverage for the bug where _normalize_level dropped every row because it
# only accepted dict-shaped rows. See _positional_tds_element /
# _positional_l2_element for the fixture shapes.
# ---------------------------------------------------------------------------

def test_normalize_positional_l1_rows_present_not_dropped():
    n = N.normalize(synthetic_ais_positional())
    assert len(n.rows) > 0
    l1_rows = [r for r in n.rows if r.level == "l1"]
    assert len(l1_rows) == 2


def test_normalize_positional_l1_seq_mapped_fields_and_decimal_coercion():
    n = N.normalize(synthetic_ais_positional())
    row0 = next(r for r in n.rows
                if r.level == "l1" and r.fields.get("deductorName") == "SYNTHETIC BANK LIMITED")
    from decimal import Decimal
    assert row0.fields["section"] == "194A"
    assert row0.fields["amtPaid"] == Decimal("250237.00")
    assert row0.fields["taxDeducted"] == Decimal("25024.00")
    assert row0.fields["dateOfPayment"] == "2026-01-15"
    assert row0.fields["infoCode"] == "INFO123"
    # Unmapped positional indices (no seq descriptor) must not leak into fields.
    assert len(row0.fields) == 6  # deductorName, section, amtPaid, taxDeducted, dateOfPayment, infoCode


def test_normalize_positional_l1_status_and_trans_feedback_land_on_row_not_fields():
    n = N.normalize(synthetic_ais_positional())
    row0 = next(r for r in n.rows
                if r.level == "l1" and r.fields.get("deductorName") == "SYNTHETIC BANK LIMITED")
    assert row0.status == "Active"
    assert row0.trans_feedback == "Feedback submitted"
    assert "status" not in row0.fields
    assert "transFeedback" not in row0.fields


def test_normalize_positional_l1_editable_field_from_seq_descriptor():
    n = N.normalize(synthetic_ais_positional())
    l1_rows = [r for r in n.rows if r.level == "l1"]
    for r in l1_rows:
        assert r.feedback_editable_field == "amtPaid"


def test_normalize_positional_l1_blank_decimal_becomes_none_not_crash():
    n = N.normalize(synthetic_ais_positional())
    row1 = next(r for r in n.rows
                if r.level == "l1" and r.fields.get("deductorName") == "SYNTHETIC NBFC LTD")
    assert row1.fields["amtPaid"] is None
    assert row1.fields["taxDeducted"] is None
    assert row1.status == "Active"
    assert row1.trans_feedback is None


def test_normalize_positional_l2_plain_string_headers_present_not_dropped():
    n = N.normalize(synthetic_ais_positional())
    l2_rows = [r for r in n.rows if r.level == "l2"]
    assert len(l2_rows) == 1


def test_normalize_positional_l2_amount_and_derived_amount_decimal_coercion():
    n = N.normalize(synthetic_ais_positional())
    row = next(r for r in n.rows if r.level == "l2")
    from decimal import Decimal
    assert row.fields["Amount"] == Decimal("50000.00")
    assert row.fields["Derived Amount"] == Decimal("48000.00")
    assert row.fields["Information Category"] == "Interest"
    assert row.fields["Information Code"] == "SFT-017"
    assert row.fields["Qualifies For"] == "Yes"


def test_normalize_positional_summary_counts_nonzero():
    n = N.normalize(synthetic_ais_positional())
    summary = {(s.section_key, s.category): s.row_count for s in n.summary}
    assert summary[("tdsTcs", "Interest from deposit (positional)")] == 2
    assert summary[("tdsTcs", "Interest from deposit (positional l2)")] == 1


def test_normalize_legacy_dict_row_path_still_covered():
    """Existing hand-built dict-row fixtures (synthetic_ais()) must keep
    working unchanged now that positional rows are also supported."""
    n = N.normalize(synthetic_ais())
    assert len(n.rows) == 5
    assert all(isinstance(r.fields, dict) for r in n.rows)


def test_normalize_open_ended_unknown_section_key_still_flattens():
    """A future/unknown sectionKey (e.g. GST/business receipts) must still
    normalize via the schema-driven path, not require a code change."""
    ais = synthetic_ais()
    ais["partB"]["sections"].append({
        "sectionKey": "futureBusinessReceipts",
        "title": "Future Business Receipts",
        "heading": "B99",
        "elements": [{
            "title": "GST turnover",
            "l1Src": "GST",
            "infoSrcId": "GST-001",
            "l1": {
                "columnLabel": [_column_label("turnover", "Turnover", "decimal")],
                "columnData": [{"turnover": "500000.00", "status": "Active",
                                "transFeedback": None}],
                "columnDataType": {},
            },
            "l2": None,
        }],
    })
    n = N.normalize(ais)
    from decimal import Decimal
    row = next(r for r in n.rows if r.section_key == "futureBusinessReceipts")
    assert row.fields["turnover"] == Decimal("500000.00")


def test_normalize_missing_sections_list_returns_empty_rows():
    ais = synthetic_ais()
    ais["partB"] = {}
    n = N.normalize(ais)
    assert n.rows == []
    assert n.summary == []


def test_normalize_does_not_mutate_input():
    ais = synthetic_ais()
    snapshot = copy.deepcopy(ais)
    N.normalize(ais)
    assert ais == snapshot


# ---------------------------------------------------------------------------
# Full round trip: encrypt synthetic -> decrypt -> normalize
# ---------------------------------------------------------------------------

def test_full_round_trip_decrypt_then_normalize():
    ais = synthetic_ais()
    password = D.derive_password(PAN, DOB_DDMMYYYY)
    blob = D._encrypt_for_test(ais, password)

    decrypted = D.decrypt_ais(blob, password)
    n = N.normalize(decrypted)

    assert n.logged_in_pan == PAN
    assert len(n.rows) == 5
    assert json.loads(json.dumps(decrypted)) == ais  # sanity: still plain JSON-able


# ---------------------------------------------------------------------------
# reconcile.py — AIS-internal reconciliation (Phase A)
#
# Unit tests build a NormalizedAis directly (bypassing decrypt/normalize) so
# the reconciliation math is exercised precisely and independent of the
# normalizer's own row-shape handling (already covered above).
# ---------------------------------------------------------------------------

from decimal import Decimal  # noqa: E402


def _row(section_key, category, level, fields, *, info_src_id="SRC-1",
         status=None, trans_feedback=None, feedback_editable_field=None,
         section_title=""):
    return N.AisRow(
        section_key=section_key, section_title=section_title, category=category,
        info_src_id=info_src_id, level=level, fields=fields, status=status,
        trans_feedback=trans_feedback, feedback_editable_field=feedback_editable_field,
    )


def _normalized(rows, *, pan=PAN, part_a=None):
    return N.NormalizedAis(
        json_version="1.0", download_date="27-Jul-2026", logged_in_pan=pan,
        part_a=part_a or {"Name": "SYNTHETIC TAXPAYER"}, rows=rows, summary=[],
    )


def test_reconcile_matching_element_no_flag():
    rows = [
        _row("tdsTcs", "Interest from deposit", "l1",
             {"deductorName": "BANK A", "amtPaid": Decimal("600")},
             feedback_editable_field="amtPaid", status="Active"),
        _row("tdsTcs", "Interest from deposit", "l1",
             {"deductorName": "BANK B", "amtPaid": Decimal("400")},
             feedback_editable_field="amtPaid", status="Active"),
        _row("tdsTcs", "Interest from deposit", "l2",
             {"Amount": Decimal("1000")}),
    ]
    report = R.reconcile_internal(_normalized(rows))
    assert len(report.elements) == 1
    el = report.elements[0]
    assert el.detail_sum == Decimal("1000")
    assert el.reported == Decimal("1000")
    assert el.delta_detail_vs_reported == Decimal("0")
    assert not el.flagged
    assert not el.flag_detail_mismatch
    assert report.flagged_element_count == 0


def test_reconcile_deliberate_mismatch_flags():
    rows = [
        _row("tdsTcs", "Interest from deposit", "l1",
             {"deductorName": "BANK A", "amtPaid": Decimal("600")},
             feedback_editable_field="amtPaid"),
        _row("tdsTcs", "Interest from deposit", "l1",
             {"deductorName": "BANK B", "amtPaid": Decimal("400")},
             feedback_editable_field="amtPaid"),
        _row("tdsTcs", "Interest from deposit", "l2",
             {"Amount": Decimal("900")}),   # 100 short of the detail sum
    ]
    report = R.reconcile_internal(_normalized(rows))
    el = report.elements[0]
    assert el.delta_detail_vs_reported == Decimal("100")
    assert el.flag_detail_mismatch is True
    assert el.flagged is True
    assert report.flagged_element_count == 1


def test_reconcile_delta_within_tolerance_not_flagged():
    rows = [
        _row("tdsTcs", "Interest from deposit", "l1",
             {"amtPaid": Decimal("1000.40")}, feedback_editable_field="amtPaid"),
        _row("tdsTcs", "Interest from deposit", "l2", {"Amount": Decimal("1000")}),
    ]
    el = R.reconcile_internal(_normalized(rows)).elements[0]
    assert abs(el.delta_detail_vs_reported) <= R.TOLERANCE
    assert el.flag_detail_mismatch is False


def test_reconcile_derived_present_flags_derivation():
    rows = [
        _row("sft", "Sale of securities", "l1",
             {"sellerName": "BROKER X", "salesConsideration": Decimal("1000")},
             feedback_editable_field="salesConsideration"),
        _row("sft", "Sale of securities", "l2",
             {"Amount": Decimal("1000"), "Derived Amount": Decimal("950")}),
    ]
    report = R.reconcile_internal(_normalized(rows))
    el = report.elements[0]
    assert el.reported == Decimal("1000")
    assert el.derived == Decimal("950")
    assert el.effective == Decimal("950")          # derived wins when present
    assert el.flag_derivation is True
    assert el.flagged is True
    # detail matches reported exactly, so ONLY the derivation flag fired
    assert el.flag_detail_mismatch is False


def test_reconcile_derived_equal_to_reported_no_derivation_flag():
    rows = [
        _row("sft", "Sale of securities", "l2",
             {"Amount": Decimal("1000"), "Derived Amount": Decimal("1000")}),
    ]
    el = R.reconcile_internal(_normalized(rows)).elements[0]
    assert el.flag_derivation is False
    assert el.effective == Decimal("1000")


def test_reconcile_derived_none_effective_falls_back_to_reported():
    rows = [_row("sft", "Sale of securities", "l2", {"Amount": Decimal("1000")})]
    el = R.reconcile_internal(_normalized(rows)).elements[0]
    assert el.derived is None
    assert el.effective == Decimal("1000")
    assert el.flag_derivation is False


def test_reconcile_count_mismatch_is_informational_not_a_loud_flag():
    """l2 "Count" is a coarser grain than l1 detail on real AIS (Count counts
    deductors/entries, l1 is per-transaction), so l1 rows legitimately
    outnumber Count. flag_count_mismatch is still COMPUTED (informational
    column) but must NOT contribute to `flagged` / the DIFFERENCE banner --
    otherwise nearly every taxpayer would fire a false positive. Here the money
    (detail vs reported) ties out, so the element is NOT flagged despite the
    count difference."""
    rows = [
        _row("tdsTcs", "Interest from deposit", "l1", {"amtPaid": Decimal("100")},
             feedback_editable_field="amtPaid", info_src_id="A"),
        _row("tdsTcs", "Interest from deposit", "l1", {"amtPaid": Decimal("200")},
             feedback_editable_field="amtPaid", info_src_id="A"),
        _row("tdsTcs", "Interest from deposit", "l2",
             {"Amount": Decimal("300"), "Count": "3"}, info_src_id="A"),
    ]
    report = R.reconcile_internal(_normalized(rows))
    el = report.elements[0]
    assert el.l1_row_count == 2
    assert el.l2_count == "3"
    assert el.flag_count_mismatch is True
    assert el.flagged is False
    assert report.flagged_element_count == 0


def test_reconcile_count_match_no_flag():
    rows = [
        _row("tdsTcs", "X", "l1", {"amtPaid": Decimal("100")},
             feedback_editable_field="amtPaid"),
        _row("tdsTcs", "X", "l2", {"Amount": Decimal("100"), "Count": "1"}),
    ]
    el = R.reconcile_internal(_normalized(rows)).elements[0]
    assert el.flag_count_mismatch is False


def test_reconcile_l1_only_no_l2_no_crash_no_flag():
    """An element with only l1 rows and no l2 aggregate at all (nothing to
    compare against) must not crash and must not spuriously flag."""
    rows = [
        _row("other-info", "Some category with no rollup", "l1",
             {"grossSalary": Decimal("50000")}, feedback_editable_field="grossSalary"),
    ]
    report = R.reconcile_internal(_normalized(rows))
    el = report.elements[0]
    assert el.reported is None
    assert el.detail_sum == Decimal("50000")
    assert el.delta_detail_vs_reported is None
    assert el.flagged is False


def test_reconcile_empty_paymentoftaxes_section_handled_gracefully():
    """A section with NO rows at all (modern AIS: no advance/self-assessment
    tax means paymentOfTaxes emits nothing) must not produce a group, crash,
    or appear in the rollups -- there's simply nothing to reconcile."""
    rows = [
        _row("tdsTcs", "Interest from deposit", "l1", {"amtPaid": Decimal("100")},
             feedback_editable_field="amtPaid"),
        _row("tdsTcs", "Interest from deposit", "l2", {"Amount": Decimal("100")}),
    ]
    report = R.reconcile_internal(_normalized(rows))
    assert not any(el.section_key == "paymentOfTaxes" for el in report.elements)
    assert "paymentOfTaxes" not in report.total_reported_by_section


def test_reconcile_empty_normalized_no_rows_at_all():
    report = R.reconcile_internal(_normalized([]))
    assert report.elements == []
    assert report.total_reported_by_section == {}
    assert report.total_tds_credit == Decimal("0")
    assert report.flagged_element_count == 0


def test_reconcile_total_tds_credit_sums_amount_deposited_and_deducted():
    rows = [
        _row("tdsTcs", "Interest from deposit", "l1",
             {"amtPaid": Decimal("1000"), "amountDeposited": Decimal("100")},
             feedback_editable_field="amtPaid", info_src_id="A"),
        _row("tdsTcs", "Interest from deposit", "l1",
             {"amtPaid": Decimal("2000"), "amountDeducted": Decimal("200")},
             feedback_editable_field="amtPaid", info_src_id="B"),
        # non-tdsTcs l1 rows must NOT contribute to the TDS credit total
        _row("sft", "Sale of securities", "l1", {"salesConsideration": Decimal("999")},
             feedback_editable_field="salesConsideration"),
    ]
    report = R.reconcile_internal(_normalized(rows))
    assert report.total_tds_credit == Decimal("300")


def test_reconcile_total_reported_by_section_rollup():
    rows = [
        _row("tdsTcs", "A", "l2", {"Amount": Decimal("100")}, info_src_id="A"),
        _row("tdsTcs", "B", "l2", {"Amount": Decimal("50")}, info_src_id="B"),
        _row("sft", "C", "l2", {"Amount": Decimal("10")}, info_src_id="C"),
    ]
    report = R.reconcile_internal(_normalized(rows))
    assert report.total_reported_by_section == {
        "tdsTcs": Decimal("150"), "sft": Decimal("10"),
    }


def test_reconcile_status_breakdown_counts_l1_statuses():
    rows = [
        _row("tdsTcs", "A", "l1", {"amtPaid": Decimal("1")}, status="Active",
             feedback_editable_field="amtPaid"),
        _row("tdsTcs", "A", "l1", {"amtPaid": Decimal("2")}, status="Active",
             feedback_editable_field="amtPaid"),
        _row("tdsTcs", "A", "l1", {"amtPaid": Decimal("3")}, status="Denied",
             feedback_editable_field="amtPaid"),
    ]
    el = R.reconcile_internal(_normalized(rows)).elements[0]
    assert el.status_breakdown == {"Active": 2, "Denied": 1}


def test_reconcile_external_matches_extension_point_defaults_empty():
    """Phase B (AIS <-> 26AS) plugs in by populating this field later; for
    Phase A it must always default to an empty list, never None/missing."""
    rows = [_row("tdsTcs", "A", "l1", {"amtPaid": Decimal("1")},
                 feedback_editable_field="amtPaid")]
    el = R.reconcile_internal(_normalized(rows)).elements[0]
    assert el.external_matches == []


def test_reconcile_report_metadata_and_taxpayer_name():
    rows = [_row("tdsTcs", "A", "l1", {"amtPaid": Decimal("1")},
                 feedback_editable_field="amtPaid")]
    report = R.reconcile_internal(_normalized(rows))
    assert report.json_version == "1.0"
    assert report.download_date == "27-Jul-2026"
    assert report.logged_in_pan == PAN
    assert report.taxpayer_name == "SYNTHETIC TAXPAYER"


def test_reconcile_numeric_string_editable_field_summed_into_detail():
    """Regression (found on real files): the feedback-editable field is chosen
    by displayFeedback, not by type, so a genuine money amount can arrive as a
    NUMERIC STRING (real case: other-info/Salary `grossSalary`, columnDataType
    "string"). It must still be coerced and summed into detail_sum, not
    dropped and not crash on Decimal + str."""
    rows = [
        _row("other-info", "Salary", "l1", {"grossSalary": "1200000"},
             feedback_editable_field="grossSalary", info_src_id="EMP1"),
        _row("other-info", "Salary", "l1", {"grossSalary": "300000.50"},
             feedback_editable_field="grossSalary", info_src_id="EMP1"),
        _row("other-info", "Salary", "l2", {"Amount": Decimal("1500000.50")},
             info_src_id="EMP1"),
    ]
    report = R.reconcile_internal(_normalized(rows))
    el = report.elements[0]
    assert el.detail_sum == Decimal("1500000.50")
    assert el.delta_detail_vs_reported == Decimal("0")
    assert not el.flagged


def test_reconcile_non_numeric_editable_field_skipped_not_crash():
    """A feedback-editable field holding genuine text (not a number) drops out
    of detail_sum rather than crashing; with no numeric detail at all and an
    l2 present, detail_sum is None so no detail-vs-reported flag can fire."""
    rows = [
        _row("other-info", "Remarks", "l1", {"remark": "SEE NOTE"},
             feedback_editable_field="remark", info_src_id="R1"),
        _row("other-info", "Remarks", "l2", {"Amount": Decimal("0")},
             info_src_id="R1"),
    ]
    report = R.reconcile_internal(_normalized(rows))
    el = report.elements[0]
    assert el.detail_sum is None
    assert el.flag_detail_mismatch is False


def test_reconcile_taxpayer_name_from_name_of_assessee_label():
    """Real AIS partA labels the name "Name of Assessee" / "Name of Entity",
    not "Name"; the name lookup must match on the "name" substring and skip
    the PAN/Aadhaar identity lines."""
    rows = [_row("tdsTcs", "A", "l1", {"amtPaid": Decimal("1")},
                 feedback_editable_field="amtPaid")]
    part_a = {
        "Permanent Account Number (PAN)": PAN,
        "Aadhaar Number": "XXXX",
        "Name of Assessee": "SYNTHETIC ASSESSEE",
    }
    report = R.reconcile_internal(_normalized(rows, part_a=part_a))
    assert report.taxpayer_name == "SYNTHETIC ASSESSEE"


def test_reconcile_multiple_elements_grouped_independently():
    rows = [
        _row("tdsTcs", "Interest", "l1", {"amtPaid": Decimal("100")},
             feedback_editable_field="amtPaid", info_src_id="A"),
        _row("tdsTcs", "Interest", "l2", {"Amount": Decimal("100")}, info_src_id="A"),
        _row("tdsTcs", "Dividend", "l1", {"amtPaid": Decimal("50")},
             feedback_editable_field="amtPaid", info_src_id="B"),
        _row("tdsTcs", "Dividend", "l2", {"Amount": Decimal("999")}, info_src_id="B"),
    ]
    report = R.reconcile_internal(_normalized(rows))
    assert len(report.elements) == 2
    by_cat = {el.category: el for el in report.elements}
    assert not by_cat["Interest"].flagged
    assert by_cat["Dividend"].flagged


def test_reconcile_full_pipeline_from_positional_synthetic_ais():
    """Integration: decrypt -> normalize -> reconcile over the REAL-shaped
    positional fixture (synthetic_ais_positional), to make sure reconcile
    works on normalize.py's actual output, not just hand-built AisRow lists."""
    n = N.normalize(synthetic_ais_positional())
    report = R.reconcile_internal(n)
    assert len(report.elements) == 2   # the l1-only element + the l2-only element
    assert report.logged_in_pan == PAN


# ---------------------------------------------------------------------------
# excel_writer.py — standalone .xlsx AIS-internal reconciliation report
# ---------------------------------------------------------------------------

from openpyxl import load_workbook  # noqa: E402


def _reco_report(elements, **overrides):
    defaults = dict(
        json_version="1.0", download_date="27-Jul-2026", logged_in_pan=PAN,
        taxpayer_name="SYNTHETIC TAXPAYER",
        total_reported_by_section={}, total_tds_credit=Decimal("0"),
        flagged_element_count=sum(1 for e in elements if e.flagged),
    )
    defaults.update(overrides)
    return R.AisRecoReport(elements=elements, **defaults)


def _element(section_key="tdsTcs", category="Interest", info_src_id="A", *,
             detail_sum=Decimal("100"), reported=Decimal("100"), derived=None,
             l1_row_count=1, l2_count=None, flag_detail_mismatch=False,
             flag_derivation=False, flag_count_mismatch=False, status_breakdown=None):
    from collections import Counter
    delta = (detail_sum - reported) if (detail_sum is not None and reported is not None) else None
    return R.ElementReco(
        section_key=section_key, category=category, info_src_id=info_src_id,
        l1_row_count=l1_row_count, detail_sum=detail_sum, reported=reported,
        derived=derived, effective=derived if derived is not None else reported,
        l2_count=l2_count, delta_detail_vs_reported=delta,
        flag_detail_mismatch=flag_detail_mismatch, flag_derivation=flag_derivation,
        flag_count_mismatch=flag_count_mismatch,
        status_breakdown=status_breakdown or Counter({"Active": l1_row_count}),
    )


def test_excel_writer_smoke_banner_flags_and_sheet_names(tmp_path):
    clean = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"))
    mismatched = _element("tdsTcs", "Dividend", detail_sum=Decimal("500"),
                          reported=Decimal("300"), flag_detail_mismatch=True)
    report = _reco_report(
        [clean, mismatched],
        total_reported_by_section={"tdsTcs": Decimal("400")},
        total_tds_credit=Decimal("40"),
    )

    out = tmp_path / "ais-reco.xlsx"
    X.write_reco_workbook(report, str(out))
    assert out.exists()

    wb = load_workbook(out)
    assert set(wb.sheetnames) == {"Summary", "tdsTcs", "Flags"}

    summary = wb["Summary"]
    assert summary["A1"].value == "1 DIFFERENCE FOUND"

    flags = wb["Flags"]
    assert flags["B2"].value == "Dividend"   # only the flagged element made it in

    # A red-flagged cell exists on the mismatched row in the detail sheet.
    detail = wb["tdsTcs"]
    red_cells = [
        c for row in detail.iter_rows(min_row=2, max_row=detail.max_row)
        for c in row
        if c.fill is not None and c.fill.fgColor and c.fill.fgColor.rgb == "00FFC7CE"
    ]
    assert red_cells, "expected at least one red-filled cell for the flagged row"


def test_excel_writer_no_differences_green_banner(tmp_path):
    clean = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"))
    report = _reco_report([clean])

    out = tmp_path / "ais-reco-clean.xlsx"
    X.write_reco_workbook(report, str(out))

    wb = load_workbook(out)
    summary = wb["Summary"]
    assert summary["A1"].value == "NO DIFFERENCES"

    flags = wb["Flags"]
    assert flags["A2"].value == "No flagged elements."


def test_excel_writer_creates_one_sheet_per_section_present(tmp_path):
    tds = _element("tdsTcs", "Interest")
    sft = _element("sft", "Sale of securities")
    other = _element("other-info", "Salary")
    report = _reco_report([tds, sft, other])

    out = tmp_path / "test_ais_reco_sections.xlsx"
    X.write_reco_workbook(report, str(out))
    wb = load_workbook(out)
    assert {"Summary", "tdsTcs", "sft", "other-info", "Flags"} <= set(wb.sheetnames)


def test_excel_writer_unknown_future_section_still_gets_a_sheet(tmp_path):
    """A sectionKey this writer has never seen (e.g. a future GST section)
    must still get its own detail sheet, not be dropped."""
    el = _element("futureBusinessReceipts", "GST turnover")
    report = _reco_report([el])

    out = tmp_path / "test_ais_reco_future_section.xlsx"
    X.write_reco_workbook(report, str(out))
    wb = load_workbook(out)
    assert "futureBusinessReceipts" in wb.sheetnames


def test_excel_writer_full_pipeline_from_positional_synthetic_ais(tmp_path):
    """Integration: decrypt -> normalize -> reconcile -> write, over the
    REAL-shaped positional fixture, end to end."""
    n = N.normalize(synthetic_ais_positional())
    report = R.reconcile_internal(n)

    out = tmp_path / "ais-reco-e2e.xlsx"
    X.write_reco_workbook(report, str(out))

    wb = load_workbook(out)
    assert "Summary" in wb.sheetnames
    assert "Flags" in wb.sheetnames
    assert "tdsTcs" in wb.sheetnames
    # 2 elements in that fixture (one l1-only, one l2-only) -> 2 data rows on the tdsTcs sheet
    detail = wb["tdsTcs"]
    data_rows = [r for r in detail.iter_rows(min_row=2) if r[0].value is not None]
    assert len(data_rows) == 2


# ---------------------------------------------------------------------------
# reconcile_26as.py — Phase B: AIS <-> 26AS TDS-credit tie-out
# ---------------------------------------------------------------------------

from datetime import date  # noqa: E402


def _tds_row(title, *, quarter, amount_deposited, info_src_id="A", amt_paid=None):
    return _row(
        "tdsTcs", title, "l1",
        {
            "amtPaid": amt_paid if amt_paid is not None else amount_deposited * 10,
            "amountDeposited": amount_deposited,
            "quarter": quarter,
            "transactionDate": "15-Jun-2025",
        },
        feedback_editable_field="amtPaid", info_src_id=info_src_id, status="Active",
    )


def _txn(deductor_name, *, txn_date, tds_deposited, tan="TAN00001A", section="194A"):
    return As26Transaction(
        tan=tan, deductor_name=deductor_name, section=section, txn_date=txn_date,
        amount=tds_deposited * 10.0, tax_deducted=tds_deposited, tds_deposited=tds_deposited,
    )


def test_reconcile_26as_aggregate_matches_no_flag():
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"))]
    n = _normalized(rows)
    as26 = As26Data(transactions=[_txn("BANK A", txn_date=date(2025, 5, 10), tds_deposited=100.0)])

    report = R26.reconcile_ais_vs_26as(n, as26)
    assert report.total_ais_tds_credit == Decimal("100")
    assert report.total_26as_tds_deposited == Decimal("100")
    assert report.delta_aggregate == Decimal("0")
    assert report.flag_aggregate_mismatch is False


def test_reconcile_26as_aggregate_mismatch_flags():
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"))]
    n = _normalized(rows)
    as26 = As26Data(transactions=[_txn("BANK A", txn_date=date(2025, 5, 10), tds_deposited=60.0)])

    report = R26.reconcile_ais_vs_26as(n, as26)
    assert report.delta_aggregate == Decimal("40")
    assert report.flag_aggregate_mismatch is True


def test_reconcile_26as_aggregate_within_tolerance_not_flagged():
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100.40"))]
    n = _normalized(rows)
    as26 = As26Data(transactions=[_txn("BANK A", txn_date=date(2025, 5, 10), tds_deposited=100.0)])
    report = R26.reconcile_ais_vs_26as(n, as26)
    assert report.flag_aggregate_mismatch is False


def test_reconcile_26as_per_quarter_split_correctness():
    rows = [
        _tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"), info_src_id="A"),
        _tds_row("Interest from deposit", quarter="Quarter 3", amount_deposited=Decimal("50"), info_src_id="B"),
    ]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=100.0),   # May -> FY Q1
        _txn("BANK B", txn_date=date(2025, 11, 1), tds_deposited=50.0),  # Nov -> FY Q3
    ])
    report = R26.reconcile_ais_vs_26as(n, as26)
    by_q = {q.quarter: q for q in report.quarters}
    assert len(report.quarters) == 4
    assert by_q[1].ais_tds_credit == Decimal("100")
    assert by_q[1].as26_tds_deposited == Decimal("100")
    assert by_q[1].flagged is False
    assert by_q[3].ais_tds_credit == Decimal("50")
    assert by_q[3].as26_tds_deposited == Decimal("50")
    # untouched quarters are present with zeros, not omitted
    assert by_q[2].ais_tds_credit == Decimal("0")
    assert by_q[2].as26_tds_deposited == Decimal("0")
    assert by_q[2].flagged is False


def test_reconcile_26as_per_quarter_mismatch_flags_only_that_quarter():
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"))]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=100.0),   # Q1: matches
        _txn("BANK X", txn_date=date(2025, 11, 1), tds_deposited=25.0),  # Q3: AIS side has nothing
    ])
    report = R26.reconcile_ais_vs_26as(n, as26)
    by_q = {q.quarter: q for q in report.quarters}
    assert by_q[1].flagged is False
    assert by_q[3].flagged is True
    assert report.flagged_quarters == [3]
    # aggregate also breaks since the totals differ overall
    assert report.flag_aggregate_mismatch is True


def test_reconcile_26as_quarter_extraction_lenient_formats():
    assert R26._extract_fy_quarter("Q1") == 1
    assert R26._extract_fy_quarter("Quarter 3") == 3
    assert R26._extract_fy_quarter("4") == 4
    assert R26._extract_fy_quarter("QTR-2") == 2
    assert R26._extract_fy_quarter(None) is None
    assert R26._extract_fy_quarter("unparseable") is None


def test_reconcile_26as_decimal_float_boundary():
    """as26 amounts are native floats (per As26Transaction); the tie-out must
    convert them to Decimal via str() at the boundary, never mixing float
    and Decimal arithmetic (which would raise TypeError)."""
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("33.33"))]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=33.33),
    ])
    report = R26.reconcile_ais_vs_26as(n, as26)
    assert isinstance(report.total_26as_tds_deposited, Decimal)
    assert isinstance(report.delta_aggregate, Decimal)
    assert report.total_26as_tds_deposited == Decimal("33.33")
    assert report.flag_aggregate_mismatch is False


def test_reconcile_26as_empty_as26_no_crash():
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"))]
    n = _normalized(rows)
    report = R26.reconcile_ais_vs_26as(n, As26Data())
    assert report.total_26as_tds_deposited == Decimal("0")
    assert report.flag_aggregate_mismatch is True   # 100 vs 0 is a real difference


def test_reconcile_26as_empty_ais_no_crash():
    n = _normalized([])
    as26 = As26Data(transactions=[_txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=100.0)])
    report = R26.reconcile_ais_vs_26as(n, as26)
    assert report.total_ais_tds_credit == Decimal("0")


# ---------------------------------------------------------------------------
# reconcile_26as.py — per-income-category tie-out (interest/dividend/other)
# ---------------------------------------------------------------------------

# A minimal tds_sections map, same shape as20 classify_section expects:
# category -> list of section codes.
TDS_SECTIONS = {
    "interest": ["194A"],
    "dividend": ["194"],
}


def test_reconcile_26as_category_classification_keyword_based():
    assert R26._classify_ais_category("Interest from deposit") == "interest"
    assert R26._classify_ais_category("Interest on Income Tax Refund") == "interest"
    assert R26._classify_ais_category("Dividend") == "dividend"
    assert R26._classify_ais_category("Dividend (Domestic Company)") == "dividend"
    assert R26._classify_ais_category("Salary") == "salary"
    assert R26._classify_ais_category("Salary received from employer") == "salary"
    assert R26._classify_ais_category("Rent received") == "other"
    assert R26._classify_ais_category(None) == "other"


def test_reconcile_26as_category_tieout_interest_and_dividend_match():
    rows = [
        _tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"),
                 info_src_id="A"),
        _tds_row("Dividend", quarter="Q1", amount_deposited=Decimal("50"), info_src_id="B"),
    ]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=100.0, section="194A"),
        _txn("COMPANY X", txn_date=date(2025, 5, 1), tds_deposited=50.0, section="194"),
    ])
    report = R26.reconcile_ais_vs_26as(n, as26, tds_sections=TDS_SECTIONS)
    by_cat = {c.category: c for c in report.categories}
    assert by_cat["interest"].ais_credit == Decimal("100")
    assert by_cat["interest"].as26_deposited == Decimal("100")
    assert by_cat["interest"].flagged is False
    assert by_cat["dividend"].ais_credit == Decimal("50")
    assert by_cat["dividend"].as26_deposited == Decimal("50")
    assert by_cat["dividend"].flagged is False


def test_reconcile_26as_category_tieout_deliberate_mismatch_flags():
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"))]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=40.0, section="194A"),
    ])
    report = R26.reconcile_ais_vs_26as(n, as26, tds_sections=TDS_SECTIONS)
    by_cat = {c.category: c for c in report.categories}
    assert by_cat["interest"].delta == Decimal("60")
    assert by_cat["interest"].flagged is True
    assert report.flagged_categories == ["interest"]


def test_reconcile_26as_category_tieout_non_income_category_falls_to_other():
    rows = [_tds_row("Receipt of amount by Partners from Partnership Firm", quarter="Q1", amount_deposited=Decimal("20"))]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("PAYER Z", txn_date=date(2025, 5, 1), tds_deposited=20.0, section="194B"),
    ])
    report = R26.reconcile_ais_vs_26as(n, as26, tds_sections=TDS_SECTIONS)
    by_cat = {c.category: c for c in report.categories}
    assert "other" in by_cat
    assert by_cat["other"].ais_credit == Decimal("20")
    assert by_cat["other"].as26_deposited == Decimal("20")   # 194B classifies to None -> "other"
    assert by_cat["other"].flagged is False


def test_reconcile_26as_category_tieout_present_on_only_one_side():
    """A category present on only one side still shows up (union), with a
    zero on the missing side, rather than being silently dropped."""
    rows = [_tds_row("Dividend", quarter="Q1", amount_deposited=Decimal("75"))]
    n = _normalized(rows)
    as26 = As26Data(transactions=[])
    report = R26.reconcile_ais_vs_26as(n, as26, tds_sections=TDS_SECTIONS)
    by_cat = {c.category: c for c in report.categories}
    assert by_cat["dividend"].ais_credit == Decimal("75")
    assert by_cat["dividend"].as26_deposited == Decimal("0")
    assert by_cat["dividend"].flagged is True


def test_reconcile_26as_category_tieout_skipped_when_no_tds_sections():
    """tds_sections=None (the default) -> no section->category mapping to
    classify the 26AS side by, so the category tie-out is skipped entirely
    -- aggregate and per-quarter still run normally."""
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"))]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=100.0, section="194A"),
    ])
    report = R26.reconcile_ais_vs_26as(n, as26)   # no tds_sections
    assert report.categories == []
    assert report.flagged_categories == []
    # aggregate + quarter unaffected
    assert report.flag_aggregate_mismatch is False
    assert len(report.quarters) == 4


# ---------------------------------------------------------------------------
# excel_writer.py — "26AS Tie-out" sheet (Phase B)
# ---------------------------------------------------------------------------

def test_excel_writer_26as_tieout_sheet_smoke(tmp_path):
    rows = [
        _tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"),
                 info_src_id="A"),
    ]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=60.0, section="194A"),   # mismatch
    ])
    ais_report = R.reconcile_internal(n)
    tieout_report = R26.reconcile_ais_vs_26as(n, as26, tds_sections=TDS_SECTIONS)

    out = tmp_path / "ais-reco-with-26as.xlsx"
    X.write_reco_workbook(ais_report, str(out), report_26as=tieout_report)

    wb = load_workbook(out)
    assert "26AS Tie-out" in wb.sheetnames
    ws = wb["26AS Tie-out"]
    assert ws["A1"].value == "1 DIFFERENCE FOUND"

    # a red-filled cell exists somewhere on the sheet (the flagged aggregate
    # and/or the flagged Q1 row and/or the flagged interest category row)
    red_cells = [
        c for row in ws.iter_rows()
        for c in row
        if c.fill is not None and c.fill.fgColor and c.fill.fgColor.rgb == "00FFC7CE"
    ]
    assert red_cells, "expected at least one red-filled cell on the tie-out sheet"


def test_excel_writer_26as_tieout_no_differences_green_banner(tmp_path):
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"),
                      info_src_id="A")]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=100.0, section="194A"),
    ])
    ais_report = R.reconcile_internal(n)
    tieout_report = R26.reconcile_ais_vs_26as(n, as26, tds_sections=TDS_SECTIONS)

    out = tmp_path / "ais-reco-with-26as-clean.xlsx"
    X.write_reco_workbook(ais_report, str(out), report_26as=tieout_report)

    wb = load_workbook(out)
    ws = wb["26AS Tie-out"]
    assert ws["A1"].value == "NO DIFFERENCES"


def test_excel_writer_26as_tieout_no_tds_sections_shows_placeholder(tmp_path):
    rows = [_tds_row("Interest from deposit", quarter="Q1", amount_deposited=Decimal("100"),
                      info_src_id="A")]
    n = _normalized(rows)
    as26 = As26Data(transactions=[
        _txn("BANK A", txn_date=date(2025, 5, 1), tds_deposited=100.0, section="194A"),
    ])
    ais_report = R.reconcile_internal(n)
    tieout_report = R26.reconcile_ais_vs_26as(n, as26)   # no tds_sections

    out = tmp_path / "ais-reco-with-26as-no-categories.xlsx"
    X.write_reco_workbook(ais_report, str(out), report_26as=tieout_report)

    wb = load_workbook(out)
    ws = wb["26AS Tie-out"]
    cell_values = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
    assert any("tds_sections not supplied" in str(v) for v in cell_values)


def test_excel_writer_omits_26as_sheet_when_not_provided(tmp_path):
    """Existing single-argument call keeps working unchanged: no 26AS
    report -> no 26AS Tie-out sheet, everything else as before."""
    clean = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"))
    report = _reco_report([clean])

    out = tmp_path / "ais-reco-no-26as.xlsx"
    X.write_reco_workbook(report, str(out))

    wb = load_workbook(out)
    assert "26AS Tie-out" not in wb.sheetnames
    assert set(wb.sheetnames) == {"Summary", "tdsTcs", "Flags"}


# ---------------------------------------------------------------------------
# reconcile_books.py -- Phase C: AIS <-> GnuCash books reconciliation
# ---------------------------------------------------------------------------

from agents.skill_ais_reconcile import reconcile_books as RB  # noqa: E402
from agents.skill_itr_workbook.scripts.parse_gnucash import (  # noqa: E402
    Account, Book, Split, Transaction,
)
from agents.skill_itr_workbook.scripts.configs import MappingEntry, MappingLoadResult  # noqa: E402

YEAR_KEY = "2025-26"


def _acct(guid, name, type_, parent_guid=None):
    return Account(guid=guid, name=name, type=type_, parent_guid=parent_guid)


def _split(account_guid, value):
    from fractions import Fraction
    return Split(guid=f"sp-{account_guid}-{value}", account_guid=account_guid,
                 value=Fraction(value), quantity=Fraction(value))


def _txn_book(guid, *, posted, splits):
    return Transaction(guid=guid, date_posted=posted, description="synthetic", splits=splits)


def _mapping(entries: dict[str, str]) -> MappingLoadResult:
    """entries: guid -> tag"""
    return MappingLoadResult(
        entries={guid: MappingEntry(guid=guid, path=guid, tag=tag) for guid, tag in entries.items()},
        warnings=[],
    )


def _books_element(category, detail_sum, *, info_src_id="A"):
    return _element("tdsTcs", category, info_src_id, detail_sum=detail_sum, reported=detail_sum)


def test_reconcile_books_nearest_ancestor_tag_resolution():
    """A tag on a parent account covers an untagged child: the child's FY
    sum rolls up into the parent's tag bucket."""
    root = _acct("root", "Root", "ROOT")
    parent = _acct("p1", "Interest Income", "INCOME", parent_guid="root")
    child = _acct("c1", "Bank FD Interest", "INCOME", parent_guid="p1")
    book = Book(
        accounts={"root": root, "p1": parent, "c1": child},
        transactions=[
            _txn_book("t1", posted=date(2025, 5, 1),
                      splits=[_split("c1", -100)]),  # INCOME flips sign -> +100
        ],
    )
    mapping = _mapping({"p1": "OS_INTEREST_BANK"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([_books_element("Interest from deposit", Decimal("100"))],
                     total_tds_credit=Decimal("0")),
        book, mapping, YEAR_KEY,
    )
    interest = next(c for c in report.categories if c.category == "interest")
    assert interest.books_income == Decimal("100")
    assert interest.ais_income == Decimal("100")
    assert not interest.flagged


def test_reconcile_books_category_mismatch_flags():
    acct = _acct("a1", "Dividend Income", "INCOME")
    book = Book(
        accounts={"a1": acct},
        transactions=[_txn_book("t1", posted=date(2025, 6, 1), splits=[_split("a1", -50)])],
    )
    mapping = _mapping({"a1": "OS_DIVIDEND"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([_books_element("Dividend", Decimal("500"))], total_tds_credit=Decimal("0")),
        book, mapping, YEAR_KEY,
    )
    dividend = next(c for c in report.categories if c.category == "dividend")
    assert dividend.ais_income == Decimal("500")
    assert dividend.books_income == Decimal("50")
    assert dividend.delta == Decimal("450")
    assert dividend.flagged


def test_reconcile_books_tds_credit_tie_out_match():
    acct = _acct("t1", "TDS on Interest", "ASSET")
    book = Book(
        accounts={"t1": acct},
        transactions=[_txn_book("tx1", posted=date(2025, 7, 1), splits=[_split("t1", 74)])],
    )
    mapping = _mapping({"t1": "TAXPAID_TDS_INTEREST"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([], total_tds_credit=Decimal("74")),
        book, mapping, YEAR_KEY,
    )
    assert report.ais_tds_credit == Decimal("74")
    assert report.books_tds_credit == Decimal("74")
    assert not report.flag_tds_mismatch


def test_reconcile_books_tds_credit_tie_out_mismatch():
    acct = _acct("t1", "TDS on Interest", "ASSET")
    book = Book(
        accounts={"t1": acct},
        transactions=[_txn_book("tx1", posted=date(2025, 7, 1), splits=[_split("t1", 50)])],
    )
    mapping = _mapping({"t1": "TAXPAID_TDS_INTEREST"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([], total_tds_credit=Decimal("74")),
        book, mapping, YEAR_KEY,
    )
    assert report.delta_tds == Decimal("24")
    assert report.flag_tds_mismatch


def test_reconcile_books_tds_credit_expense_account_is_debit_positive():
    """Regression: real family books post TDS to an EXPENSE account ("TDS on
    Interest"), not an ASSET receivable. account_fy_sum flips EXPENSE to ITR
    presentation sign (a debit of +74 becomes -74), which used to make
    books_tds_credit NEGATIVE and fire a spurious mismatch against the
    (positive) AIS/26AS sides. The TDS bucket must land debit-positive
    regardless of whether TDS is modelled as EXPENSE or ASSET."""
    acct = _acct("t1", "TDS on Interest", "EXPENSE")
    book = Book(
        accounts={"t1": acct},
        transactions=[_txn_book("tx1", posted=date(2025, 7, 1), splits=[_split("t1", 74)])],
    )
    mapping = _mapping({"t1": "TAXPAID_TDS_INTEREST"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([], total_tds_credit=Decimal("74")),
        book, mapping, YEAR_KEY,
    )
    assert report.books_tds_credit == Decimal("74")
    assert not report.flag_tds_mismatch


def test_reconcile_books_tds_credit_mixed_expense_and_asset_accounts():
    """A book that splits TDS across an EXPENSE account and an ASSET
    receivable must sum both as positive credits (74 + 26 = 100), not net
    them against each other via the sign flip."""
    exp = _acct("t1", "TDS on Interest", "EXPENSE")
    asset = _acct("t2", "TDS Receivable", "ASSET")
    book = Book(
        accounts={"t1": exp, "t2": asset},
        transactions=[
            _txn_book("tx1", posted=date(2025, 7, 1), splits=[_split("t1", 74)]),
            _txn_book("tx2", posted=date(2025, 8, 1), splits=[_split("t2", 26)]),
        ],
    )
    mapping = _mapping({"t1": "TAXPAID_TDS_INTEREST", "t2": "TAXPAID_TDS_DIVIDEND"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([], total_tds_credit=Decimal("100")),
        book, mapping, YEAR_KEY,
    )
    assert report.books_tds_credit == Decimal("100")
    assert not report.flag_tds_mismatch


def test_reconcile_books_income_not_in_ais_completeness_list():
    """Books show salary income; AIS reports none -- informational list."""
    acct = _acct("s1", "Salary Income", "INCOME")
    book = Book(
        accounts={"s1": acct},
        transactions=[_txn_book("tx1", posted=date(2025, 5, 1), splits=[_split("s1", -1000)])],
    )
    mapping = _mapping({"s1": "SALARY_GROSS"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([], total_tds_credit=Decimal("0")),
        book, mapping, YEAR_KEY,
    )
    assert "salary" in report.books_income_not_in_ais
    assert "salary" not in report.ais_income_not_in_books


def test_reconcile_books_ais_income_not_in_books_completeness_list():
    """AIS reports interest income; books have no corresponding posting --
    the loud call-out since books is primary."""
    report = RB.reconcile_ais_vs_books(
        _reco_report([_books_element("Interest from deposit", Decimal("300"))],
                     total_tds_credit=Decimal("0")),
        Book(accounts={}, transactions=[]),
        _mapping({}),
        YEAR_KEY,
    )
    assert "interest" in report.ais_income_not_in_books
    assert "interest" not in report.books_income_not_in_ais


def test_reconcile_books_untagged_income_account_count():
    tagged = _acct("a1", "Salary Income", "INCOME")
    untagged = _acct("a2", "Misc Income", "INCOME")
    book = Book(
        accounts={"a1": tagged, "a2": untagged},
        transactions=[
            _txn_book("t1", posted=date(2025, 5, 1), splits=[_split("a1", -100)]),
            _txn_book("t2", posted=date(2025, 5, 2), splits=[_split("a2", -200)]),
        ],
    )
    mapping = _mapping({"a1": "SALARY_GROSS"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([], total_tds_credit=Decimal("0")), book, mapping, YEAR_KEY,
    )
    assert report.untagged_income_account_count == 1


def test_reconcile_books_decimal_float_boundary():
    """account_fy_sum returns a native float (via Fraction); the report's
    books_income must come back as an exact Decimal, not a float artifact."""
    acct = _acct("a1", "Bank FD Interest", "INCOME")
    from fractions import Fraction
    book = Book(
        accounts={"a1": acct},
        transactions=[_txn_book("t1", posted=date(2025, 5, 1),
                                 splits=[Split(guid="sp1", account_guid="a1",
                                               value=Fraction(-1, 10), quantity=Fraction(-1, 10))])],
    )
    mapping = _mapping({"a1": "OS_INTEREST_BANK"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([], total_tds_credit=Decimal("0")), book, mapping, YEAR_KEY,
    )
    interest = next(c for c in report.categories if c.category == "interest")
    assert isinstance(interest.books_income, Decimal)
    assert interest.books_income == Decimal("0.1")


def test_reconcile_books_untagged_child_excludes_tagged_parent_from_untagged_count():
    """An account whose ancestor IS mapped should not count as untagged, even
    though the account itself carries no direct mapping entry."""
    parent = _acct("p1", "Interest Income", "INCOME")
    child = _acct("c1", "Bank FD Interest", "INCOME", parent_guid="p1")
    book = Book(
        accounts={"p1": parent, "c1": child},
        transactions=[_txn_book("t1", posted=date(2025, 5, 1), splits=[_split("c1", -10)])],
    )
    mapping = _mapping({"p1": "OS_INTEREST_BANK"})

    report = RB.reconcile_ais_vs_books(
        _reco_report([], total_tds_credit=Decimal("0")), book, mapping, YEAR_KEY,
    )
    assert report.untagged_income_account_count == 0


def test_excel_writer_books_sheet_smoke(tmp_path):
    acct = _acct("a1", "Bank FD Interest", "INCOME")
    book = Book(
        accounts={"a1": acct},
        transactions=[_txn_book("t1", posted=date(2025, 5, 1), splits=[_split("a1", -500)])],
    )
    mapping = _mapping({"a1": "OS_INTEREST_BANK"})
    ais_report = _reco_report([_books_element("Interest from deposit", Decimal("100"))],
                               total_tds_credit=Decimal("0"))
    books_report = RB.reconcile_ais_vs_books(ais_report, book, mapping, YEAR_KEY)

    out = tmp_path / "ais-reco-with-books.xlsx"
    X.write_reco_workbook(ais_report, str(out), report_books=books_report)

    wb = load_workbook(out)
    assert "Books Reconciliation" in wb.sheetnames
    # placed right after Summary
    assert wb.sheetnames[0] == "Summary"
    assert wb.sheetnames[1] == "Books Reconciliation"

    ws = wb["Books Reconciliation"]
    red_cells = [
        c for row in ws.iter_rows()
        for c in row
        if c.fill is not None and c.fill.fgColor and c.fill.fgColor.rgb == "00FFC7CE"
    ]
    assert red_cells, "expected at least one red-filled cell (interest category flagged)"


def test_excel_writer_omits_books_sheet_when_not_provided(tmp_path):
    """Existing calls without report_books keep working unchanged."""
    clean = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"))
    report = _reco_report([clean])

    out = tmp_path / "ais-reco-no-books.xlsx"
    X.write_reco_workbook(report, str(out))

    wb = load_workbook(out)
    assert "Books Reconciliation" not in wb.sheetnames
    assert set(wb.sheetnames) == {"Summary", "tdsTcs", "Flags"}


# ---------------------------------------------------------------------------
# feedback.py -- Phase D: advisory AIS portal-feedback suggestions
# ---------------------------------------------------------------------------

from agents.skill_ais_reconcile import feedback as F  # noqa: E402


def test_feedback_derivation_flag_suggestion():
    el = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"),
                  derived=Decimal("80"), flag_derivation=True)
    report = _reco_report([el])

    suggestions = F.suggest_feedback(report)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.suggested_action == F.FEEDBACK_NOT_FULLY_CORRECT
    assert s.confidence == "low"
    assert s.section_key == "tdsTcs"
    assert s.category == "Interest"


def test_feedback_detail_mismatch_falls_back_to_review():
    el = _element("tdsTcs", "Dividend", detail_sum=Decimal("500"), reported=Decimal("300"),
                  flag_detail_mismatch=True)
    report = _reco_report([el])

    suggestions = F.suggest_feedback(report)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.suggested_action == F.FEEDBACK_REVIEW
    assert s.confidence == "low"


def test_feedback_ais_income_not_in_books_review_medium():
    report = _reco_report([], total_tds_credit=Decimal("0"))
    books = RB.AisBooksReport(
        categories=[], ais_tds_credit=Decimal("0"), books_tds_credit=Decimal("0"),
        delta_tds=Decimal("0"), flag_tds_mismatch=False,
        books_income_not_in_ais=[], ais_income_not_in_books=["interest"],
        untagged_income_account_count=0,
    )

    suggestions = F.suggest_feedback(report, books=books)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.category == "interest"
    assert s.suggested_action == F.FEEDBACK_REVIEW
    assert s.confidence == "medium"


def test_feedback_books_income_not_in_ais_review_medium():
    report = _reco_report([], total_tds_credit=Decimal("0"))
    books = RB.AisBooksReport(
        categories=[], ais_tds_credit=Decimal("0"), books_tds_credit=Decimal("0"),
        delta_tds=Decimal("0"), flag_tds_mismatch=False,
        books_income_not_in_ais=["salary"], ais_income_not_in_books=[],
        untagged_income_account_count=0,
    )

    suggestions = F.suggest_feedback(report, books=books)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.category == "salary"
    assert s.suggested_action == F.FEEDBACK_REVIEW
    assert s.confidence == "medium"


def test_feedback_ais_income_not_in_books_duplicate_variant_when_also_derivation_flagged():
    el = _element("tdsTcs", "interest", detail_sum=Decimal("100"), reported=Decimal("100"),
                  derived=Decimal("80"), flag_derivation=True)
    report = _reco_report([el])
    books = RB.AisBooksReport(
        categories=[], ais_tds_credit=Decimal("0"), books_tds_credit=Decimal("0"),
        delta_tds=Decimal("0"), flag_tds_mismatch=False,
        books_income_not_in_ais=[], ais_income_not_in_books=["interest"],
        untagged_income_account_count=0,
    )

    suggestions = F.suggest_feedback(report, books=books)
    actions = {s.suggested_action for s in suggestions}
    assert F.FEEDBACK_NOT_FULLY_CORRECT in actions   # from the derivation flag itself
    assert F.FEEDBACK_REVIEW in actions               # ais_income_not_in_books
    assert F.FEEDBACK_DUPLICATE in actions             # the duplicate-hint variant
    dup = next(s for s in suggestions if s.suggested_action == F.FEEDBACK_DUPLICATE)
    assert dup.confidence == "low"


def test_feedback_26as_flagged_category_review_medium():
    report = _reco_report([], total_tds_credit=Decimal("0"))
    as26_report = R26.Ais26asReport(
        total_ais_tds_credit=Decimal("100"), total_26as_tds_deposited=Decimal("50"),
        delta_aggregate=Decimal("50"), flag_aggregate_mismatch=True,
        quarters=[], categories=[
            R26.CategoryTieOut(category="interest", ais_credit=Decimal("100"),
                                as26_deposited=Decimal("50"), delta=Decimal("50"), flagged=True),
        ],
    )

    suggestions = F.suggest_feedback(report, as26=as26_report)
    assert len(suggestions) == 1
    s = suggestions[0]
    assert s.category == "interest"
    assert s.suggested_action == F.FEEDBACK_REVIEW
    assert s.confidence == "medium"


def test_feedback_26as_aggregate_mismatch_no_category_granularity():
    """flag_aggregate_mismatch true but no flagged categories (tds_sections
    wasn't supplied) -- still surfaces one suggestion, not silently dropped."""
    report = _reco_report([], total_tds_credit=Decimal("0"))
    as26_report = R26.Ais26asReport(
        total_ais_tds_credit=Decimal("100"), total_26as_tds_deposited=Decimal("50"),
        delta_aggregate=Decimal("50"), flag_aggregate_mismatch=True,
        quarters=[], categories=[],
    )

    suggestions = F.suggest_feedback(report, as26=as26_report)
    assert len(suggestions) == 1
    assert suggestions[0].suggested_action == F.FEEDBACK_REVIEW


def test_feedback_no_flags_produces_no_suggestions():
    el = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"))
    report = _reco_report([el])
    assert F.suggest_feedback(report) == []


def test_feedback_dedupe_same_section_category_info_src_action():
    el = _element("tdsTcs", "Interest", info_src_id="A", detail_sum=Decimal("100"),
                  reported=Decimal("100"), derived=Decimal("80"), flag_derivation=True)
    report = _reco_report([el, el])   # duplicate element -> would double-fire without dedupe
    suggestions = F.suggest_feedback(report)
    assert len(suggestions) == 1


def test_feedback_sort_order_medium_before_low():
    el = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"),
                  derived=Decimal("80"), flag_derivation=True)   # low confidence
    report = _reco_report([el])
    books = RB.AisBooksReport(
        categories=[], ais_tds_credit=Decimal("0"), books_tds_credit=Decimal("0"),
        delta_tds=Decimal("0"), flag_tds_mismatch=False,
        books_income_not_in_ais=["salary"], ais_income_not_in_books=[],  # medium confidence
        untagged_income_account_count=0,
    )

    suggestions = F.suggest_feedback(report, books=books)
    confidences = [s.confidence for s in suggestions]
    assert confidences.index("medium") < confidences.index("low")


def test_feedback_confidence_never_high():
    el_deriv = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"),
                         derived=Decimal("80"), flag_derivation=True)
    el_detail = _element("tdsTcs", "Dividend", detail_sum=Decimal("500"), reported=Decimal("300"),
                          flag_detail_mismatch=True)
    report = _reco_report([el_deriv, el_detail])
    books = RB.AisBooksReport(
        categories=[], ais_tds_credit=Decimal("0"), books_tds_credit=Decimal("0"),
        delta_tds=Decimal("0"), flag_tds_mismatch=False,
        books_income_not_in_ais=["salary"], ais_income_not_in_books=["other"],
        untagged_income_account_count=0,
    )
    as26_report = R26.Ais26asReport(
        total_ais_tds_credit=Decimal("100"), total_26as_tds_deposited=Decimal("50"),
        delta_aggregate=Decimal("50"), flag_aggregate_mismatch=True,
        quarters=[], categories=[
            R26.CategoryTieOut(category="interest", ais_credit=Decimal("100"),
                                as26_deposited=Decimal("50"), delta=Decimal("50"), flagged=True),
        ],
    )

    suggestions = F.suggest_feedback(report, books=books, as26=as26_report)
    assert suggestions
    assert all(s.confidence in ("low", "medium") for s in suggestions)


def test_excel_writer_feedback_sheet_smoke_and_advisory_header(tmp_path):
    el = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"),
                  derived=Decimal("80"), flag_derivation=True)
    report = _reco_report([el])
    suggestions = F.suggest_feedback(report)

    out = tmp_path / "ais-reco-with-feedback.xlsx"
    X.write_reco_workbook(report, str(out), suggestions=suggestions)

    wb = load_workbook(out)
    assert "Feedback Suggestions" in wb.sheetnames
    ws = wb["Feedback Suggestions"]

    cell_values = [c.value for row in ws.iter_rows() for c in row if c.value is not None]
    assert any("ADVISORY ONLY" in str(v) for v in cell_values)
    assert any(F.FEEDBACK_NOT_FULLY_CORRECT == v for v in cell_values)
    # never a scare-red fill on this sheet -- these are suggestions, not errors
    red_cells = [
        c for row in ws.iter_rows()
        for c in row
        if c.fill is not None and c.fill.fgColor and c.fill.fgColor.rgb == "00FFC7CE"
    ]
    assert not red_cells


def test_excel_writer_omits_feedback_sheet_when_not_provided(tmp_path):
    clean = _element("tdsTcs", "Interest", detail_sum=Decimal("100"), reported=Decimal("100"))
    report = _reco_report([clean])

    out = tmp_path / "ais-reco-no-feedback.xlsx"
    X.write_reco_workbook(report, str(out))

    wb = load_workbook(out)
    assert "Feedback Suggestions" not in wb.sheetnames


# ---------------------------------------------------------------------------
# agent.py -- Phase E: run() orchestration (direct-mode skill entry point)
# ---------------------------------------------------------------------------

import yaml  # noqa: E402
from agents.skill_ais_reconcile import agent as AG  # noqa: E402

ENTITY_KEY = "syn-partner"
# PAN[3:9] = "DE1234" -> masked "XXX" + "DE1234" + "X"
AIS_FILENAME = "XXXDE1234X_2025-26_AIS.json"


def _write_entities_yaml(tmp_path, *, key=ENTITY_KEY, pan=PAN, status="Individual",
                          dob=DOB_ISO, doi=None) -> Path:
    fields = {"name": "Synthetic Taxpayer", "pan": pan, "status": status}
    if dob:
        fields["dob"] = dob
    if doi:
        fields["doi"] = doi
    path = tmp_path / "entities.yaml"
    path.write_text(yaml.safe_dump({key: fields}), encoding="utf-8")
    return path


def _write_encrypted_ais(tmp_path, *, filename=AIS_FILENAME) -> Path:
    ais = synthetic_ais()
    password = D.derive_password_from_iso_date(PAN, DOB_ISO)
    blob = D._encrypt_for_test(ais, password)
    path = tmp_path / filename
    path.write_text(blob, encoding="utf-8")
    return path


def _write_mapping_stub(tmp_path, *, key=ENTITY_KEY) -> Path:
    """agent.py only checks mapping_path.is_file() before calling the
    (monkeypatched) configs.load_mapping -- content is irrelevant here."""
    mdir = tmp_path / "mappings"
    mdir.mkdir(exist_ok=True)
    path = mdir / f"{key}.mapping.yaml"
    path.write_text("[]", encoding="utf-8")
    return path


def _synthetic_book(*, in_fy: bool):
    """A tiny one-account book with a single INCOME transaction either
    inside or outside YEAR_KEY's FY window, for the FY-match check."""
    root = _acct("root", "Root", "ROOT")
    interest = _acct("i1", "Interest Income", "INCOME", parent_guid="root")
    posted = date(2025, 6, 1) if in_fy else date(2020, 6, 1)
    book = Book(
        accounts={"root": root, "i1": interest},
        transactions=[_txn_book("t1", posted=posted, splits=[_split("i1", -1000)])],
    )
    return book


def _empty_26as_workbook(tmp_path, *, name="Synthetic Taxpayer", pan=PAN, fy="2025-26") -> Path:
    from openpyxl import Workbook
    wb = Workbook()
    ws = wb.active
    ws.title = "Part I"
    ws.append(["Sr.No", "Name of Deductor", "TAN", "Section"])  # header-only, no data rows
    ident = []
    if name:
        ident.append(f"Assessee Name: {name}")
    if pan:
        ident.append(f"PAN: {pan}")
    if fy:
        ident.append(f"Financial Year: {fy}")
    if ident:  # row 2 carries the workbook's own assessee, as the 26AS skill writes it
        ws.cell(row=2, column=1, value="  |  ".join(ident))
    path = tmp_path / "synthetic-26AS.xlsx"
    wb.save(path)
    return path


def test_run_no_entity_match_returns_error(tmp_path):
    entities_path = _write_entities_yaml(tmp_path, pan="ZZZZZ0000Z")  # won't mask to AIS_FILENAME's prefix
    ais_path = _write_encrypted_ais(tmp_path)
    out = tmp_path / "out.xlsx"

    summary = AG.run(str(ais_path), str(out), entity_key=ENTITY_KEY, entities_path=str(entities_path))

    assert summary.startswith("ERROR:")
    assert "XXXDE1234X" in summary
    assert not out.exists()


def test_run_ais_internal_only_resolves_entity_and_writes_workbook(tmp_path, monkeypatch):
    entities_path = _write_entities_yaml(tmp_path)
    ais_path = _write_encrypted_ais(tmp_path)
    out = tmp_path / "out.xlsx"

    summary = AG.run(str(ais_path), str(out), entity_key=ENTITY_KEY, entities_path=str(entities_path))

    assert "Synthetic Taxpayer" in summary
    assert "2025-26" in summary
    assert "AIS-internal:" in summary
    assert "Feedback:" in summary
    assert out.is_file()
    wb = load_workbook(out)
    assert "Summary" in wb.sheetnames
    assert "Books Reconciliation" not in wb.sheetnames


def test_run_with_matching_fy_book_runs_books_phase_no_warning(tmp_path, monkeypatch):
    entities_path = _write_entities_yaml(tmp_path)
    ais_path = _write_encrypted_ais(tmp_path)
    _write_mapping_stub(tmp_path)
    gnucash_path = tmp_path / "book.gnucash"
    gnucash_path.write_text("not real xml -- parse_book is monkeypatched", encoding="utf-8")
    out = tmp_path / "out.xlsx"

    book = _synthetic_book(in_fy=True)
    mapping = _mapping({"i1": "OS_INTEREST_BANK"})
    monkeypatch.setattr(AG.pg, "parse_book", lambda path: book)
    monkeypatch.setattr(AG.configs, "load_mapping", lambda path: mapping)

    summary = AG.run(str(ais_path), str(out), entity_key=ENTITY_KEY, gnucash_path=str(gnucash_path),
                      entities_path=str(entities_path))

    assert "WARNING" not in summary
    assert "Books:" in summary
    wb = load_workbook(out)
    assert wb.sheetnames[1] == "Books Reconciliation"


def test_run_fy_mismatch_warning_fires(tmp_path, monkeypatch):
    entities_path = _write_entities_yaml(tmp_path)
    ais_path = _write_encrypted_ais(tmp_path)
    _write_mapping_stub(tmp_path)
    gnucash_path = tmp_path / "wrong-year-book.gnucash"
    gnucash_path.write_text("not real xml -- parse_book is monkeypatched", encoding="utf-8")
    out = tmp_path / "out.xlsx"

    book = _synthetic_book(in_fy=False)
    mapping = _mapping({"i1": "OS_INTEREST_BANK"})
    monkeypatch.setattr(AG.pg, "parse_book", lambda path: book)
    monkeypatch.setattr(AG.configs, "load_mapping", lambda path: mapping)

    summary = AG.run(str(ais_path), str(out), entity_key=ENTITY_KEY, gnucash_path=str(gnucash_path),
                      entities_path=str(entities_path))

    assert "WARNING" in summary
    assert "no transactions in FY 2025-26" in summary
    assert "Books:" in summary  # Phase C still ran (sheet still exists)
    wb = load_workbook(out)
    assert "Books Reconciliation" in wb.sheetnames


def test_run_with_26as_workbook_runs_26as_phase(tmp_path):
    entities_path = _write_entities_yaml(tmp_path)
    ais_path = _write_encrypted_ais(tmp_path)
    xlsx_path = _empty_26as_workbook(tmp_path)
    out = tmp_path / "out.xlsx"

    summary = AG.run(str(ais_path), str(out), entity_key=ENTITY_KEY, xlsx_path=str(xlsx_path), entities_path=str(entities_path))

    assert "26AS:" in summary
    wb = load_workbook(out)
    assert "26AS Tie-out" in wb.sheetnames


def test_run_huf_entity_uses_doi_for_password(tmp_path):
    """HUF entities derive the decrypt password from doi, not dob -- confirm
    the wrong-field case fails loud rather than silently trying dob."""
    entities_path = _write_entities_yaml(
        tmp_path, key="hufentity", pan=PAN, status="HUF", dob=None, doi=None,
    )
    ais_path = _write_encrypted_ais(tmp_path)
    out = tmp_path / "out.xlsx"

    summary = AG.run(str(ais_path), str(out), entity_key="hufentity", entities_path=str(entities_path))

    assert summary.startswith("ERROR:")
    assert "doi" in summary


# ---------------------------------------------------------------------------
# ITR-20: comma-grouped amounts must parse (they used to be lost as None)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw, expected", [
    ("9,99,999.99", Decimal("999999.99")),     # Indian grouping
    ("1,00,000", Decimal("100000")),
    ("12,34,56,789.50", Decimal("123456789.50")),
    ("1,234,567.89", Decimal("1234567.89")),   # western grouping
    ("1,000", Decimal("1000")),
    ("1000.50", Decimal("1000.50")),           # plain
    ("  2500  ", Decimal("2500")),             # surrounding whitespace
    ("-1,00,000.25", Decimal("-100000.25")),   # negative
    ("-75", Decimal("-75")),
    (500, Decimal("500")),                     # already numeric
    (12.5, Decimal("12.5")),
    (Decimal("7.25"), Decimal("7.25")),
])
def test_itr20_parse_amount_accepts_grouped_and_plain(raw, expected):
    assert N.parse_amount(raw) == expected
    assert N._coerce(raw, "decimal") == expected


@pytest.mark.parametrize("raw", ["abc", "", "   ", "1,2,3x", "--5", "1,23,45", "12,34", ",100",
                                 "100,", "1,,000", "NaN", "Infinity", None, True])
def test_itr20_negative_garbage_never_becomes_a_number(raw):
    """NEGATIVE: junk must stay None (never 0, never a guessed number)."""
    assert N.parse_amount(raw) is None
    assert N._coerce(raw, "decimal") is None


def _ais_with(tds_elements=(), sft_elements=()):
    """Synthetic AIS whose partB holds exactly the given tdsTcs / sft elements."""
    ais = synthetic_ais()
    ais["partB"]["sections"] = [
        {"sectionKey": "tdsTcs", "title": "TDS/TCS Information", "heading": "B1",
         "elements": list(tds_elements)},
        {"sectionKey": "sft", "title": "SFT Information", "heading": "B2",
         "elements": list(sft_elements)},
    ]
    return ais


def _income_element(title, source, amt_paid, *, tds="0", l1src="TDS", info="X-1", tan=None):
    cols = [_column_label("deductorName", "Name", "string"),
            _column_label("amtPaid", "Amount paid", "decimal", display_feedback=True),
            _column_label("taxDeducted", "TDS", "decimal")]
    row = {"deductorName": source, "amtPaid": amt_paid, "taxDeducted": tds,
           "status": "Active", "transFeedback": None}
    if tan:
        row["tan"] = tan
    return {"title": title, "l1Src": l1src, "infoSrcId": info,
            "l1": {"columnLabel": cols, "columnData": [row], "columnDataType": {}}, "l2": None}


def _income_book(tag, guid, amount):
    acct = _acct(guid, "Synthetic Income", "INCOME")
    return (Book(accounts={guid: acct},
                 transactions=[_txn_book("t-" + guid, posted=date(2025, 8, 1),
                                         splits=[_split(guid, -amount)])]),
            _mapping({guid: tag}))


def _books_tieout(ais, book, mapping):
    n = N.normalize(ais)
    return RB.reconcile_ais_vs_books(R.reconcile_internal(n), book, mapping, YEAR_KEY, n)


def _cat(report, name):
    return next(c for c in report.categories if c.category == name)


def test_itr20_tds_row_with_comma_amounts_end_to_end():
    n = N.normalize(_ais_with([_income_element("Interest from deposit", "SYNTHETIC BANK",
                                               "9,99,999.99", tds="1,00,000")]))
    l1 = next(r for r in n.rows if r.section_key == "tdsTcs" and r.level == "l1")
    assert l1.fields["amtPaid"] == Decimal("999999.99")
    assert l1.fields["taxDeducted"] == Decimal("100000")


def test_itr20_comma_amounts_do_not_report_income_missing_from_ais():
    """NEGATIVE: the head is present in the AIS, so it must never be called 'books income not in AIS',
    and the AIS amount must not be lost (0) -- the old false-gap behaviour."""
    ais = _ais_with([_income_element("Interest from deposit", "SYNTHETIC BANK", "9,99,999.99")])
    book, mapping = _income_book("OS_INTEREST_BANK", "i1", 999999.99)
    rep = _books_tieout(ais, book, mapping)
    interest = _cat(rep, "interest")
    assert interest.ais_income == Decimal("999999.99")
    assert not interest.flagged
    assert "interest" not in rep.books_income_not_in_ais


# ---------------------------------------------------------------------------
# LEG-02: SFT dividend/interest widening, each source counted once
# ---------------------------------------------------------------------------

def test_leg02_sft_only_dividend_is_counted():
    ais = _ais_with(sft_elements=[_income_element("Dividend", "SYNTHETIC CO", "5000", l1src="SFT")])
    book, mapping = _income_book("OS_DIVIDEND", "d1", 5000)
    rep = _books_tieout(ais, book, mapping)
    div = _cat(rep, "dividend")
    assert div.ais_income == Decimal("5000")
    assert div.from_sft_only == Decimal("5000") and div.from_tds_section == Decimal("0")
    assert not div.flagged
    assert any("counted from SFT" in note for note in rep.source_notes)


def test_leg02_same_source_in_both_sections_counted_once():
    ais = _ais_with(
        [_income_element("Interest from deposit", "Synthetic Bank Ltd", "8000", tds="800")],
        [_income_element("Interest from deposit", "SYNTHETIC BANK LTD.", "8000", l1src="SFT",
                         info="S-1")])
    book, mapping = _income_book("OS_INTEREST_BANK", "i1", 8000)
    rep = _books_tieout(ais, book, mapping)
    interest = _cat(rep, "interest")
    assert interest.ais_income == Decimal("8000")
    assert interest.ais_income != Decimal("16000")      # NEGATIVE: the double total must never appear
    assert interest.from_tds_section == Decimal("8000") and interest.from_sft_only == Decimal("0")
    assert not interest.flagged
    assert any("both sections" in n and "NOT added" in n for n in rep.source_notes)


def test_leg02_same_tan_different_name_counted_once():
    ais = _ais_with(
        [_income_element("Dividend", "Synthetic Co", "3000", tan="TANX00001A")],
        [_income_element("Dividend", "Totally Different Spelling", "3000", l1src="SFT", tan="TANX00001A")])
    rep = _books_tieout(ais, *_income_book("OS_DIVIDEND", "d1", 3000))
    assert _cat(rep, "dividend").ais_income == Decimal("3000")


def test_leg02_unidentifiable_sft_not_added_over_existing_tds_income():
    """NEGATIVE: an SFT row with no source identity might be the TDS income again; never add it."""
    sft = _income_element("Interest from deposit", "", "4000", l1src="SFT", info="S-2")
    ais = _ais_with([_income_element("Interest from deposit", "Synthetic Bank", "4000")], [sft])
    rep = _books_tieout(ais, *_income_book("OS_INTEREST_BANK", "i1", 4000))
    assert _cat(rep, "interest").ais_income == Decimal("4000")
    assert any("cannot be identified" in n for n in rep.source_notes)


def test_leg02_different_source_in_sft_is_added():
    ais = _ais_with(
        [_income_element("Interest from deposit", "Alpha Finance", "1000")],
        [_income_element("Interest from deposit", "Beta Capital", "2000", l1src="SFT")])
    rep = _books_tieout(ais, *_income_book("OS_INTEREST_BANK", "i1", 3000))
    interest = _cat(rep, "interest")
    assert interest.ais_income == Decimal("3000")
    assert interest.from_tds_section == Decimal("1000") and interest.from_sft_only == Decimal("2000")


def test_leg02_securities_sft_never_enters_income():
    """NEGATIVE: purchase/sale of securities (and anything not interest/dividend) stays out."""
    ais = _ais_with(sft_elements=[
        _income_element("Sale of securities", "Synthetic Broker", "1000000", l1src="SFT", info="S-3"),
        _income_element("Purchase of securities", "Synthetic Broker", "900000", l1src="SFT", info="S-4"),
        _income_element("Purchase of mutual fund units", "Synthetic AMC", "50000", l1src="SFT", info="S-5"),
    ])
    n = N.normalize(ais)
    total, tds_part, sft_part, _notes, _possible = RB._ais_by_category(R.reconcile_internal(n), n)
    assert total == {} and sft_part == {}


def test_leg02_nil_tds_row_is_counted():
    ais = _ais_with([_income_element("Dividend", "Synthetic Co", "7000", tds="0")])
    rep = _books_tieout(ais, *_income_book("OS_DIVIDEND", "d1", 7000))
    div = _cat(rep, "dividend")
    assert div.ais_income == Decimal("7000")
    assert div.from_tds_section == Decimal("7000")
    assert not div.flagged


def test_leg02_without_normalized_behaviour_is_tds_only():
    """NEGATIVE: callers that do not pass normalized get the old TDS-only total, no SFT leakage."""
    ais = _ais_with([_income_element("Dividend", "Synthetic Co", "100")],
                    [_income_element("Dividend", "Other Co", "900", l1src="SFT")])
    n = N.normalize(ais)
    book, mapping = _income_book("OS_DIVIDEND", "d1", 100)
    rep = RB.reconcile_ais_vs_books(R.reconcile_internal(n), book, mapping, YEAR_KEY)
    assert _cat(rep, "dividend").ais_income == Decimal("100")


def test_leg02_notes_say_which_section(tmp_path):
    ais = _ais_with([_income_element("Interest from deposit", "Alpha Finance", "1000")],
                    [_income_element("Interest from deposit", "Beta Capital", "2000", l1src="SFT")])
    rep = _books_tieout(ais, *_income_book("OS_INTEREST_BANK", "i1", 3000))
    assert any("TDS section" in x for x in rep.source_notes)
    assert any("counted from SFT" in x for x in rep.source_notes)


# ---------------------------------------------------------------------------
# LEG-02 identity: payer name comes from the element's l2 "Information Source"
# (real shape: positional rows, no name on l1, TAN-shaped id in tdsTcs, PAN-shaped .SFT id in sft)
# ---------------------------------------------------------------------------

def _real_element(section, title, info_id, source, amount):
    l1 = {
        "columnLabel": [_column_label_seq("tsnId", "Txn", "string", 0),
                        _column_label_seq("amtPaid", "Amount", "decimal", 1, display_feedback=True),
                        _column_label_seq("quarter", "Quarter", "string", 2)],
        "columnData": [["TSN-1", amount, "Q1"]],
        "columnDataType": ["string", "decimal", "string"],
    }
    l2 = {
        "columnLabel": ["Information Category", "Information Source", "Count", "Amount"],
        "columnData": [[title, source, "1", amount]],
        "columnDataType": ["string", "string", "numeric", "decimal"],
    }
    return {"title": title, "l1Src": "TDS" if section == "tdsTcs" else "SFT",
            "infoSrcId": info_id, "l1": l1, "l2": l2}


def _tds_el(title, source, amount, n=1):
    return _real_element("tdsTcs", title, f"TANAA000{n}A", source, amount)


def _sft_el(title, source, amount, n=1):
    return _real_element("sft", title, f"ZZZZZ000{n}Z.SFT012", source, amount)


def _tieout(tds, sft, tag="OS_DIVIDEND", book_amount=0):
    ais = _ais_with(tds, sft)
    n = N.normalize(ais)
    book, mapping = _income_book(tag, "g1", book_amount)
    return RB.reconcile_ais_vs_books(R.reconcile_internal(n), book, mapping, YEAR_KEY, n), n


def test_leg02_real_shape_sft_only_dividend_payer_is_added():
    rep, n = _tieout([_tds_el("Dividend", "ALPHA FINANCE LIMITED (TANAA0001A)", "1000")],
                     [_sft_el("Dividend", "GAMMA FOODS LIMITED (ZZZZZ0001Z)", "400")])
    div = _cat(rep, "dividend")
    assert div.ais_income == Decimal("1400")
    assert div.from_tds_section == Decimal("1000") and div.from_sft_only == Decimal("400")
    assert not rep.possible_same_payers


def test_leg02_real_shape_never_reaches_unknown_path_when_l2_name_present():
    """Regression this fix exists for: real rows carry no name on l1; the l2 name must be used."""
    rep, n = _tieout([_tds_el("Dividend", "ALPHA FINANCE LIMITED (TANAA0001A)", "1000")],
                     [_sft_el("Dividend", "GAMMA FOODS LIMITED (ZZZZZ0001Z)", "400")])
    assert not any("cannot be identified" in x for x in rep.source_notes)
    assert _cat(rep, "dividend").from_sft_only == Decimal("400")


def test_leg02_two_sft_elements_same_new_payer_added_once_each_one_note():
    rep, n = _tieout([_tds_el("Dividend", "ALPHA FINANCE LIMITED (TANAA0001A)", "1000")],
                     [_sft_el("Dividend", "GAMMA FOODS LTD (ZZZZZ0001Z)", "400", n=1),
                      _sft_el("Dividend", "Gamma Foods Limited (ZZZZZ0001Z)", "100", n=2)])
    assert _cat(rep, "dividend").from_sft_only == Decimal("500")
    assert sum("counted from SFT" in x for x in rep.source_notes) == 1


def test_leg02_exact_name_with_different_bracketed_ids_is_not_added():
    """NEGATIVE: same payer, different ids -> total is the TDS amount only."""
    rep, n = _tieout([_tds_el("Dividend", "ALPHA FINANCE LTD (TANAA0001A)", "1000")],
                     [_sft_el("Dividend", "Alpha Finance Limited (ZZZZZ0009Z)", "1000")])
    div = _cat(rep, "dividend")
    assert div.ais_income == Decimal("1000") and div.ais_income != Decimal("2000")
    assert div.from_sft_only == Decimal("0")
    assert any("both sections" in x for x in rep.source_notes)


def test_leg02_bracketed_id_is_never_part_of_the_name():
    """NEGATIVE: ids must not decide the match."""
    assert RB._norm_name("ALPHA FINANCE LTD (id1)") == RB._norm_name("Alpha Finance Limited (id2)")
    assert "ID1" not in RB._norm_name("ALPHA FINANCE LTD (id1)")


def test_leg02_near_match_not_added_and_listed_for_the_user():
    """NEGATIVE: same first word, different rest -> NOT added, and visible in the possible-same list."""
    rep, n = _tieout([_tds_el("Dividend", "ALPHA FINANCE LIMITED (TANAA0001A)", "1000")],
                     [_sft_el("Dividend", "ALPHA HOLDINGS LIMITED (ZZZZZ0001Z)", "700")])
    div = _cat(rep, "dividend")
    assert div.ais_income == Decimal("1000") and div.from_sft_only == Decimal("0")
    assert len(rep.possible_same_payers) == 1
    msg = rep.possible_same_payers[0]
    assert "Alpha Holdings" in msg and "Alpha Finance" in msg and "NOT added" in msg


def test_leg02_near_match_reaches_the_workbook(tmp_path):
    rep, n = _tieout([_tds_el("Dividend", "ALPHA FINANCE LIMITED (TANAA0001A)", "1000")],
                     [_sft_el("Dividend", "ALPHA HOLDINGS LIMITED (ZZZZZ0001Z)", "700")])
    ws_wb = X._write_books_sheet
    from openpyxl import Workbook
    wb = Workbook()
    ws_wb(wb, rep)
    ws = wb["Books Reconciliation"]
    text = " ".join(str(c.value) for r in ws.iter_rows() for c in r if c.value is not None)
    assert "may be the same payer" in text and "please check" in text


def test_leg02_unidentified_sft_not_added_when_tds_head_has_income():
    """NEGATIVE: no l2 name and no l1 name field -> keep the conservative rule."""
    sft = _sft_el("Dividend", "", "300")
    rep, n = _tieout([_tds_el("Dividend", "ALPHA FINANCE LIMITED (TANAA0001A)", "1000")], [sft])
    assert _cat(rep, "dividend").ais_income == Decimal("1000")
    assert any("cannot be identified" in x for x in rep.source_notes)


def test_leg02_unidentified_sft_added_when_tds_head_has_none():
    rep, n = _tieout([], [_sft_el("Dividend", "", "300")])
    assert _cat(rep, "dividend").ais_income == Decimal("300")


def test_leg02_real_shape_securities_sft_still_not_added():
    """NEGATIVE: purchase and sale categories stay out even with a clean l2 name."""
    rep, n = _tieout([_tds_el("Dividend", "ALPHA FINANCE LIMITED (TANAA0001A)", "1000")],
                     [_sft_el("Sale of securities", "GAMMA BROKING LIMITED (ZZZZZ0001Z)", "900000"),
                      _sft_el("Purchase of securities", "GAMMA BROKING LIMITED (ZZZZZ0001Z)", "800000", n=2)])
    assert _cat(rep, "dividend").ais_income == Decimal("1000")


def test_leg02_filler_only_names_do_not_match_and_count_as_unidentified():
    """NEGATIVE: 'THE COMPANY LIMITED' normalises to nothing; two such names must not match each other."""
    assert RB._norm_name("THE COMPANY LIMITED") == ""
    rep, n = _tieout([_tds_el("Dividend", "THE COMPANY LIMITED (TANAA0001A)", "1000")],
                     [_sft_el("Dividend", "The Company Ltd (ZZZZZ0001Z)", "1000")])
    assert _cat(rep, "dividend").ais_income == Decimal("1000")
    assert not any("both sections" in x for x in rep.source_notes)
    assert any("cannot be identified" in x for x in rep.source_notes)


# ---------------------------------------------------------------------------
# ITR-21: the filed return against the AIS (and the books, and the 26AS)
# All data below is synthetic: invented PAN/TANs/names/amounts.
# ---------------------------------------------------------------------------

from agents.skill_ais_reconcile import reconcile_return as RR  # noqa: E402

_FR = AG.filed_return_engine
TAN_A = "TANAA0001A"
TAN_B = "TANBB0002B"
TAN_C = "TANCC0003C"


def _credit_el(section, title, info_id, source, amount, credit="0"):
    """Like _real_element, plus an amountDeposited column (the TDS credit)."""
    el = _real_element(section, title, info_id, source, amount)
    el["l1"]["columnLabel"].append(_column_label_seq("amountDeposited", "TDS deposited", "decimal", 3))
    el["l1"]["columnData"][0].append(credit)
    el["l1"]["columnDataType"].append("decimal")
    return el


def _tds_credit_el(title, source, amount, credit, tan):
    return _credit_el("tdsTcs", title, tan, f"{source} ({tan})", amount, credit)


def _itr1_json(rows=None, tds=None, extra=None, pan=PAN, ay="2026"):
    itr = {
        "Form_ITR1": ({"AssessmentYear": ay} if ay is not None else {}),
        "PersonalInfo": {"PAN": pan},
        "ITR1_IncomeDeductions": {
            "GrossSalary": 0, "Salary": 0, "NetSalary": 0,
            "IncomeOthSrc": 3500, "GrossTotIncome": 3500, "TotalIncome": 3500,
            "OthersInc": {"OthersIncDtlsOthSrc": rows if rows is not None else [
                {"OthSrcNatureDesc": "SAV", "OthSrcOthAmount": 500},
                {"OthSrcNatureDesc": "IFD", "OthSrcOthAmount": 2000},
                {"OthSrcNatureDesc": "DIV", "OthSrcOthAmount": 1000,
                 "DividendInc": {"DateRange": {"Upto15Of6": 1000}}},
            ]},
        },
        "TDSonOthThanSals": {
            "TDSonOthThanSal": tds if tds is not None else [{
                "EmployerOrDeductorOrCollectDetl": {
                    "TAN": TAN_A, "EmployerOrDeductorOrCollecterName": "ALPHA BANK"},
                "TDSSection": "194A", "AmtForTaxDeduct": 2000,
                "TotTDSOnAmtPaid": 200, "ClaimOutOfTotTDSOnAmtPaid": 200, "DeductedYr": 2026}],
            "TotalTDSonOthThanSals": 200,
        },
        "ScheduleTDS3Dtls": {"TotalTDS3Details": 0},
        "ScheduleTCS": {"TotalSchTCS": 0},
        "TaxPaid": {"TaxesPaid": {"TDS": 200}, "BalTaxPayable": 0},
    }
    itr.update(extra or {})
    return {"ITR": {"ITR1": itr}}


def _itr23_json(form="ITR3", tds1=None, tds2=None, os_block=None, salary=0, pan=PAN, ay="2026"):
    itr = {
        "Form_" + form: ({"AssessmentYear": ay} if ay is not None else {}),
        "PartA_GEN1": {"PersonalInfo": {"PAN": pan}},
        "PartB-TI": {"Salaries": salary, "IncomeFromHP": 0, "GrossTotalIncome": 3500,
                     "TotalIncome": 3500, "IncFromOS": {"TotIncFromOS": 3500},
                     "CapGain": {"TotalCapGains": 0}, "DeductionsUnderScheduleVIA": 0},
        "ScheduleOS": {"IncOthThanOwnRaceHorse": os_block if os_block is not None else {
            "IntrstFrmSavingBank": 500, "IntrstFrmTermDeposit": 2000,
            "IntrstFrmIncmTaxRefund": 0, "IntrstFrmOthers": 0, "DividendGross": 1000}},
        "ScheduleTDS2": {"TDSOthThanSalaryDtls": tds2 if tds2 is not None else [],
                         "TotalTDSonOthThanSals": 0},
        "ScheduleTDS3": {"TotalTDS3OnOthThanSal": 0},
        "ScheduleTCS": {"TCS": [], "TotalSchTCS": 0},
    }
    if tds1 is not None:
        itr["ScheduleS"] = {"TotalGrossSalary": salary, "TotIncUnderHeadSalaries": salary}
        itr["ScheduleTDS1"] = {"TDSonSalary": tds1, "TotalTDSonSalaries": 0}
    return {"ITR": {form: itr}}


def _tds2_row(tan, claimed, *, section="194A", head="OS", owner="S", gross=2000):
    return {"TANOfDeductor": tan, "TDSSection": section, "HeadOfIncome": head,
            "TDSCreditName": owner, "GrossAmount": gross, "BroughtFwdTDSAmt": 0, "AmtCarriedFwd": 0,
            "TaxDeductCreditDtls": {"TaxDeductedOwnHands": claimed, "TaxClaimedOwnHands": claimed,
                                    "TaxDeductedIncome": 0, "TaxClaimedIncome": 0,
                                    "TaxDeductedTDS": 0, "TaxClaimedTDS": 0}}


def _parse(tmp_path, payload, name="ret.json"):
    p = tmp_path / name
    p.write_text(json.dumps(payload), encoding="utf-8")
    return _FR.parse_filed_return(p, ENTITY_KEY)


def _tie(fr, tds=(), sft=(), **kw):
    n = N.normalize(_ais_with(list(tds), list(sft)))
    return RR.reconcile_return(fr, R.reconcile_internal(n), n, **kw)


def _head(rep, name):
    return next((h for h in rep.heads if h.head == name), None)


def _ded(rep, tan):
    return next((d for d in rep.deductors if d.tan == tan), None)


_BASE_TDS = [
    _tds_credit_el("Interest from deposit", "ALPHA BANK", "2500", "200", TAN_A),
    _tds_credit_el("Dividend", "GAMMA FOODS LIMITED", "1000", "0", TAN_B),
]


# ---- parser -----------------------------------------------------------------

def test_itr21_parse_itr1_pan_salary_os_and_tds(tmp_path):
    fr = _parse(tmp_path, _itr1_json())
    assert fr.itr_form == "ITR1" and fr.pan == PAN
    assert fr.salary["gross_salary"] == 0
    assert fr.schedule_os["interest_sb"] == 500 and fr.schedule_os["interest_td"] == 2000
    assert fr.schedule_os["dividend_gross"] == 1000
    assert fr.tds_rows_extracted and len(fr.tds_rows) == 1
    row = fr.tds_rows[0]
    assert row["tan"] == TAN_A and row["claimed"] == 200 and row["credit_owner"] == "S"
    assert fr.tds_totals["other"] == 200 and fr.tcs_total == 0


def test_itr21_parse_itr2_and_itr3_tds_rows(tmp_path):
    for form in ("ITR2", "ITR3"):
        payload = _itr23_json(form, tds1=[{
            "EmployerOrDeductorOrCollectDetl": {"TAN": TAN_C, "EmployerOrDeductorOrCollecterName": "OMEGA LTD"},
            "IncChrgSal": 100000, "TotalTDSSal": 5000}], tds2=[_tds2_row(TAN_A, 200)], salary=100000)
        fr = _parse(tmp_path, payload, f"{form}.json")
        assert fr.itr_form == form and fr.pan == PAN
        assert fr.salary["gross_salary"] == 100000
        kinds = {r["tan"]: (r["kind"], r["claimed"]) for r in fr.tds_rows}
        assert kinds == {TAN_C: ("salary", 5000), TAN_A: ("other", 200)}


def test_itr21_parse_itr23_dividend_split_keys_only_when_present(tmp_path):
    fr = _parse(tmp_path, _itr23_json(os_block={"IntrstFrmSavingBank": 1, "DividendGross": 5}))
    assert "dividend_22e" not in fr.schedule_os and "dividend_oth_than_22e" not in fr.schedule_os
    fr = _parse(tmp_path, _itr23_json(os_block={"IntrstFrmSavingBank": 1, "DividendOthThan22e": 3,
                                                "Dividend22e": 2}), "b.json")
    assert fr.schedule_os["dividend_oth_than_22e"] == 3 and fr.schedule_os["dividend_22e"] == 2


def test_itr21_old_filed_return_fixture_unchanged():
    """NEGATIVE: the ITR-2 fixture's pre-ITR-21 outputs are exactly as before."""
    fx = Path(__file__).resolve().parent / "skill_itr_workbook" / "fixtures" / "syn_ind_filed_return.json"
    fr = _FR.parse_filed_return(fx, "SYN-IND")
    assert fr.itr_form == "ITR2" and fr.not_extracted_fields == []
    assert set(fr.schedule_os) == {"interest_sb", "interest_td", "interest_refund", "interest_other",
                                   "dividend_gross"}
    assert fr.schedule_os["interest_sb"] == 500 and fr.schedule_os["dividend_gross"] == 750
    assert fr.heads["Salaries"] == 800000 and fr.taxes_paid["TDS"] == 50000


def test_itr21_unsupported_form_fails_loud_no_partial_result(tmp_path):
    """NEGATIVE: ITR-4 is not read at all -- no half-filled FiledReturn comes back."""
    p = tmp_path / "itr4.json"
    p.write_text(json.dumps({"ITR": {"ITR4": {"PersonalInfo": {"PAN": PAN}}}}), encoding="utf-8")
    with pytest.raises(_FR.UnsupportedReturnForm):
        _FR.parse_filed_return(p, ENTITY_KEY)


def test_itr21_unknown_itr1_nature_code_is_not_extracted_not_mapped(tmp_path):
    """NEGATIVE: an unknown code is neither interest nor other income."""
    rows = [{"OthSrcNatureDesc": "SAV", "OthSrcOthAmount": 500},
            {"OthSrcNatureDesc": "ZZZ", "OthSrcOthAmount": 7777}]
    fr = _parse(tmp_path, _itr1_json(rows=rows))
    assert any("'ZZZ'" in x for x in fr.return_not_extracted)
    assert fr.schedule_os["interest_sb"] == 500
    assert fr.schedule_os["interest_td"] == 0 and fr.schedule_os["other_income"] == 0
    assert 7777 not in fr.schedule_os.values()


def test_itr21_itr1_salary_tds_key_is_not_extracted(tmp_path):
    """NEGATIVE: an ITR-1 salary-TDS block is flagged, never read into the TDS rows."""
    fr = _parse(tmp_path, _itr1_json(extra={"TDSonSalaries": {"TDSonSalary": [{"TotalTDSSal": 9999}]}}))
    assert any(x.startswith("ITR-1 salary TDS (TDSonSalaries)") for x in fr.return_not_extracted)
    assert all(r["claimed"] != 9999 for r in fr.tds_rows)
    assert all(r["kind"] != "salary" for r in fr.tds_rows)


def test_itr21_unreadable_claimed_amount_is_not_extracted(tmp_path):
    tds = [{"EmployerOrDeductorOrCollectDetl": {"TAN": TAN_A, "EmployerOrDeductorOrCollecterName": "A"},
            "TDSSection": "194A", "ClaimOutOfTotTDSOnAmtPaid": "n/a"}]
    fr = _parse(tmp_path, _itr1_json(tds=tds))
    assert fr.tds_rows[0]["claimed"] == "NOT-EXTRACTED"
    rep = _tie(fr, _BASE_TDS)
    assert "claimed amount NOT-EXTRACTED" in _ded(rep, TAN_A).note and _ded(rep, TAN_A).flagged


# ---- guard ------------------------------------------------------------------

def _fake_fr(**kw):
    base = dict(source_format="json", pan=PAN, assessment_year="2026", itr_form="ITR1")
    base.update(kw)
    return type("FR", (), base)()


def test_itr21_guard_accepts_matching_pan_and_year():
    assert RR.check_return_guard(_fake_fr(), PAN, YEAR_KEY) is None
    assert RR.check_return_guard(_fake_fr(assessment_year="2026"), PAN, "2025-26") is None


def test_itr21_guard_pan_mismatch_is_an_error():
    """NEGATIVE: someone else's return is never tied out."""
    msg = RR.check_return_guard(_fake_fr(pan="ZZZZZ9999Z"), PAN, YEAR_KEY)
    assert msg.startswith("ERROR") and "different PAN" in msg
    assert PAN not in msg and "ZZZZZ9999Z" not in msg     # masked in the line


def test_itr21_guard_ay_mismatch_is_an_error():
    """NEGATIVE: a return for another year is never tied out."""
    msg = RR.check_return_guard(_fake_fr(assessment_year="2025"), PAN, "2025-26")
    assert msg.startswith("ERROR") and "assessment year" in msg


def test_itr21_guard_missing_pan_and_pdf_are_errors():
    assert RR.check_return_guard(_fake_fr(pan="NOT-EXTRACTED"), PAN, YEAR_KEY).startswith("ERROR")
    assert RR.check_return_guard(_fake_fr(source_format="pdf"), PAN, YEAR_KEY).startswith("ERROR")


# ---- tie-out ----------------------------------------------------------------

def test_itr21_tieout_clean_return_has_no_flags(tmp_path):
    rep = _tie(_parse(tmp_path, _itr1_json()), _BASE_TDS)
    assert _head(rep, "interest").ret == Decimal("2500") and _head(rep, "interest").ais == Decimal("2500")
    assert _head(rep, "dividend").ret == Decimal("1000") and _head(rep, "dividend").ais == Decimal("1000")
    assert rep.flagged_heads == []
    assert _ded(rep, TAN_A).claimed == Decimal("200") and _ded(rep, TAN_A).ais == Decimal("200")
    assert not _ded(rep, TAN_A).flagged


def test_itr21_head_in_return_absent_from_ais_is_flagged(tmp_path):
    """NEGATIVE: declared dividend with no AIS dividend must not tie silently."""
    rep = _tie(_parse(tmp_path, _itr1_json()),
               [_tds_credit_el("Interest from deposit", "ALPHA BANK", "2500", "200", TAN_A)])
    h = _head(rep, "dividend")
    assert h.flagged and h.ais == Decimal("0") and "absent from the AIS" in h.note
    assert "dividend" in rep.flagged_heads and "interest" not in rep.flagged_heads


def test_itr21_head_in_ais_not_in_return_is_flagged(tmp_path):
    rows = [{"OthSrcNatureDesc": "SAV", "OthSrcOthAmount": 500},
            {"OthSrcNatureDesc": "IFD", "OthSrcOthAmount": 2000}]
    rep = _tie(_parse(tmp_path, _itr1_json(rows=rows)), _BASE_TDS)
    h = _head(rep, "dividend")
    assert h.flagged and h.ret == Decimal("0") and "not declared in the return" in h.note


def test_itr21_tan_in_ais_not_claimed_is_flagged(tmp_path):
    """NEGATIVE: TDS the AIS shows for a TAN, missing from the claim, must not pass."""
    tds = _BASE_TDS + [_tds_credit_el("Interest from deposit", "BETA BANK", "100", "10", TAN_C)]
    rep = _tie(_parse(tmp_path, _itr1_json()), tds)
    d = _ded(rep, TAN_C)
    assert d is not None and d.flagged and d.claimed == Decimal("0") and d.ais == Decimal("10")
    assert "not claimed" in d.note
    assert not _ded(rep, TAN_A).flagged


def test_itr21_tan_claimed_but_absent_from_ais_is_flagged(tmp_path):
    """NEGATIVE: a claim with no AIS support must not pass."""
    tds = [{"EmployerOrDeductorOrCollectDetl": {"TAN": TAN_C, "EmployerOrDeductorOrCollecterName": "OMEGA"},
            "TDSSection": "194A", "AmtForTaxDeduct": 500, "TotTDSOnAmtPaid": 50,
            "ClaimOutOfTotTDSOnAmtPaid": 50}]
    rep = _tie(_parse(tmp_path, _itr1_json(tds=tds)), _BASE_TDS)
    d = _ded(rep, TAN_C)
    assert d.flagged and d.ais == Decimal("0") and "absent from the AIS" in d.note


def test_itr21_payer_in_tds_and_sft_counted_once_in_ais_column(tmp_path):
    """NEGATIVE: the same dividend payer in both AIS sections is not double counted."""
    tds = [_tds_credit_el("Dividend", "GAMMA FOODS LIMITED", "1000", "0", TAN_B)]
    sft = [_sft_el("Dividend", "GAMMA FOODS LIMITED (ZZZZZ0001Z)", "1000")]
    rep = _tie(_parse(tmp_path, _itr1_json()), tds, sft)
    assert _head(rep, "dividend").ais == Decimal("1000")
    assert _head(rep, "dividend").ais != Decimal("2000")


def test_itr21_ais_and_books_figures_are_the_books_reconciliation_figures(tmp_path):
    ais = _ais_with(list(_BASE_TDS), [])
    n = N.normalize(ais)
    book, mapping = _income_book("OS_DIVIDEND", "g1", 1000)
    books_rep = RB.reconcile_ais_vs_books(R.reconcile_internal(n), book, mapping, YEAR_KEY, n)
    rep = RR.reconcile_return(_parse(tmp_path, _itr1_json()), R.reconcile_internal(n), n, books_report=books_rep)
    h = _head(rep, "dividend")
    assert h.ais == _cat(books_rep, "dividend").ais_income
    assert h.books == _cat(books_rep, "dividend").books_income == Decimal("1000")
    assert rep.books_given


def test_itr21_return_differs_from_books_is_flagged(tmp_path):
    n = N.normalize(_ais_with(list(_BASE_TDS), []))
    book, mapping = _income_book("OS_DIVIDEND", "g1", 400)
    books_rep = RB.reconcile_ais_vs_books(R.reconcile_internal(n), book, mapping, YEAR_KEY, n)
    rep = RR.reconcile_return(_parse(tmp_path, _itr1_json()), R.reconcile_internal(n), n, books_report=books_rep)
    h = _head(rep, "dividend")
    assert h.flagged and "differs from the books" in h.note and h.delta_books == Decimal("600")


def test_itr21_credit_in_another_persons_hands_not_added_to_claimed(tmp_path):
    """NEGATIVE: a row whose credit sits with someone else is listed and flagged, never claimed."""
    payload = _itr23_json(tds2=[_tds2_row(TAN_A, 200), _tds2_row(TAN_A, 80, owner="O")])
    fr = _parse(tmp_path, payload)
    rep = _tie(fr, _BASE_TDS)
    d = _ded(rep, TAN_A)
    assert d.claimed == Decimal("200") and d.claimed != Decimal("280")
    assert d.flagged and "another person's hands" in d.note


def test_itr21_only_other_hands_row_claims_nothing(tmp_path):
    fr = _parse(tmp_path, _itr23_json(tds2=[_tds2_row(TAN_A, 80, owner="O")]))
    d = _ded(_tie(fr, _BASE_TDS), TAN_A)
    assert d.claimed == Decimal("0") and d.flagged


def test_itr21_partner_rows_are_separate_from_other_sources(tmp_path):
    payload = _itr23_json(tds2=[_tds2_row(TAN_A, 200), _tds2_row(TAN_C, 300, section="94T", head="BP")])
    tds = _BASE_TDS + [_tds_credit_el("Receipt of amount by Partners from Partnership Firm", "OMEGA LLP", "3000", "300", TAN_C)]
    rep = _tie(_parse(tmp_path, payload), tds)
    assert [d.tan for d in rep.partner_rows] == [TAN_C]
    assert rep.partner_rows[0].claimed == Decimal("300") and not rep.partner_rows[0].flagged
    assert _ded(rep, TAN_C) is None                    # not folded into the other-sources block
    assert _head(rep, "interest").ais == Decimal("2500")  # and not into a head


def test_itr21_partner_claim_without_ais_194t_row_is_flagged(tmp_path):
    payload = _itr23_json(tds2=[_tds2_row(TAN_C, 300, section="94T", head="BP")])
    rep = _tie(_parse(tmp_path, payload), _BASE_TDS)
    assert rep.partner_rows[0].flagged and "absent from the AIS 194T rows" in rep.partner_rows[0].note


def test_itr21_26as_third_column(tmp_path):
    t = As26Transaction(tan=TAN_A, deductor_name="ALPHA BANK", section="194A", txn_date=date(2025, 8, 1),
                        amount=Decimal("2000"), tax_deducted=Decimal("150"), tds_deposited=Decimal("150"))
    as26 = As26Data(transactions=[t])
    rep = _tie(_parse(tmp_path, _itr1_json()), _BASE_TDS, as26=as26)
    d = _ded(rep, TAN_A)
    assert d.as26 == Decimal("150") and d.flagged and "claimed differs from the 26AS" in d.note
    assert rep.as26_given


def test_itr21_itr1_salary_only_tan_not_compared_but_noted(tmp_path):
    tds = _BASE_TDS + [_tds_credit_el("Salary", "EMPLOYER LTD", "100000", "5000", TAN_C)]
    rep = _tie(_parse(tmp_path, _itr1_json()), tds)
    assert _ded(rep, TAN_C) is None
    assert any(TAN_C in x and "not compared" in x for x in rep.notes)


def test_itr21_unreadable_head_is_flagged_not_tied(tmp_path):
    fr = _parse(tmp_path, _itr23_json(os_block={"IntrstFrmSavingBank": 500, "IntrstFrmTermDeposit": 2000,
                                                "IntrstFrmIncmTaxRefund": 0}))
    rep = _tie(fr, _BASE_TDS)
    assert _head(rep, "dividend").ret is None and _head(rep, "dividend").flagged
    assert any("dividend" in x for x in rep.not_extracted)


# ---- workbook / feedback ----------------------------------------------------

def test_itr21_sheet_lists_not_extracted_first_in_red(tmp_path):
    from openpyxl import Workbook
    fr = _parse(tmp_path, _itr1_json(rows=[{"OthSrcNatureDesc": "ZZZ", "OthSrcOthAmount": 1}]))
    rep = _tie(fr, _BASE_TDS)
    wb = Workbook()
    X._write_return_sheet(wb, rep)
    ws = wb["Return Tie-out"]
    texts = [(c.value, c.font.color.rgb if c.font and c.font.color else None)
             for r in ws.iter_rows() for c in r if c.value]
    ne_idx = next(i for i, (v, _) in enumerate(texts) if str(v).startswith("NOT-EXTRACTED from the return"))
    head_idx = next(i for i, (v, _) in enumerate(texts) if str(v).startswith("Heads:"))
    assert ne_idx < head_idx
    assert any("'ZZZ'" in str(v) for v, _ in texts[ne_idx:head_idx])


def test_itr21_feedback_only_ever_suggests_review(tmp_path):
    rep = _tie(_parse(tmp_path, _itr1_json()),
               _BASE_TDS + [_tds_credit_el("Interest from deposit", "BETA BANK", "100", "10", TAN_C)])
    sugg = F._return_suggestions(rep)
    assert sugg and all(s.suggested_action == F.FEEDBACK_REVIEW for s in sugg)
    assert F._return_suggestions(None) == []


# ---- agent end to end -------------------------------------------------------

def _run_agent(tmp_path, monkeypatch, payload=None, *, raw=None, out_name="out.xlsx", pass_return=True):
    entities_path = _write_entities_yaml(tmp_path)
    ais_path = _write_encrypted_ais(tmp_path)
    out = tmp_path / out_name
    kwargs = {}
    if pass_return:
        rp = tmp_path / "return.json"
        rp.write_text(raw if raw is not None else json.dumps(payload), encoding="utf-8")
        kwargs["filed_return_path"] = str(rp)
    summary = AG.run(str(ais_path), str(out), entity_key=ENTITY_KEY, entities_path=str(entities_path), **kwargs)
    return summary, load_workbook(out)


def test_itr21_agent_valid_return_writes_the_sheet(tmp_path, monkeypatch):
    summary, wb = _run_agent(tmp_path, monkeypatch, _itr1_json())
    assert "Return: ITR1 tied out" in summary and "ERROR" not in summary
    assert "Return Tie-out" in wb.sheetnames


def test_itr21_agent_no_return_path_keeps_the_sheet_set(tmp_path, monkeypatch):
    """NEGATIVE: without filed_return_path no ITR-21 sheet or line appears."""
    summary, wb = _run_agent(tmp_path, monkeypatch, pass_return=False)
    assert "Return Tie-out" not in wb.sheetnames and "Return:" not in summary
    base = wb.sheetnames
    _, wb2 = _run_agent(tmp_path, monkeypatch, _itr1_json(), out_name="out2.xlsx")
    assert wb2.sheetnames == base + ["Return Tie-out"] or [s for s in wb2.sheetnames if s != "Return Tie-out"] == base


def test_itr21_agent_pan_mismatch_skips_with_error_no_sheet(tmp_path, monkeypatch):
    summary, wb = _run_agent(tmp_path, monkeypatch, _itr1_json(pan="ZZZZZ9999Z"))
    assert "ERROR" in summary and "different PAN" in summary
    assert "Return Tie-out" not in wb.sheetnames


def test_itr21_agent_ay_mismatch_skips_with_error_no_sheet(tmp_path, monkeypatch):
    """NEGATIVE: Form_ITR2.AssessmentYear "2025" against FY 2025-26 is never tied out."""
    summary, wb = _run_agent(tmp_path, monkeypatch, _itr23_json("ITR2", ay="2025"))
    assert "ERROR" in summary and "assessment year 2025" in summary
    assert "Return Tie-out" not in wb.sheetnames


def test_itr21_agent_missing_ay_skips_with_error_no_sheet(tmp_path, monkeypatch):
    """NEGATIVE: a return whose year is unknown is never tied out."""
    summary, wb = _run_agent(tmp_path, monkeypatch, _itr1_json(ay=None))
    assert "ERROR" in summary and "does not state its assessment year" in summary
    assert "Return Tie-out" not in wb.sheetnames


def test_itr21_parse_assessment_year_all_three_forms(tmp_path):
    assert _parse(tmp_path, _itr1_json(), "a.json").assessment_year == "2026"
    for form in ("ITR2", "ITR3"):
        assert _parse(tmp_path, _itr23_json(form, ay="2026"), form + ".json").assessment_year == "2026"


def test_itr21_missing_ay_stays_not_extracted(tmp_path):
    fr = _parse(tmp_path, _itr1_json(ay=None))
    assert fr.assessment_year == "NOT-EXTRACTED" and "assessment year" in fr.return_not_extracted
    assert RR.check_return_guard(fr, PAN, YEAR_KEY).startswith("ERROR")


def test_itr21_guard_missing_ay_is_an_error():
    assert RR.check_return_guard(_fake_fr(assessment_year="NOT-EXTRACTED"), PAN, YEAR_KEY).startswith("ERROR")


def test_itr21_other_ais_categories_never_land_in_a_head_or_partner(tmp_path):
    """NEGATIVE: rent, foreign remittance and foreign travel are not interest/dividend/salary/partner."""
    tds = list(_BASE_TDS) + [
        _tds_credit_el("Rent payment", "LANDLORD", "50000", "5000", "TANRR0004R"),
        _tds_credit_el("Outward foreign remittance/purchase of foreign currency", "FX DEALER", "70000", "700", "TANFX0005F"),
        _tds_credit_el("Foreign travel", "TRAVEL CO", "90000", "900", "TANFT0006T")]
    rep = _tie(_parse(tmp_path, _itr1_json()), tds)
    assert _head(rep, "interest").ais == Decimal("2500") and _head(rep, "dividend").ais == Decimal("1000")
    assert _head(rep, "salary") is None or _head(rep, "salary").ais == Decimal("0")
    assert rep.partner_rows == []
    cats = RR._ais_by_tan(N.normalize(_ais_with(tds, [])))
    assert cats[3] == {}                                  # nothing in the 194T bucket
    for tan in ("TANRR0004R", "TANFX0005F", "TANFT0006T"):
        assert tan not in cats[0] and tan not in cats[2]  # ITR-22: not a credit at all


def test_itr21_partner_category_matched_whole_label_only(tmp_path):
    label = "  Receipt of amount by Partners  from Partnership Firm "
    n = N.normalize(_ais_with([_tds_credit_el(label, "OMEGA LLP", "3000", "300", TAN_C)], []))
    assert RR._ais_by_tan(n)[3] == {TAN_C: Decimal("300")}
    n = N.normalize(_ais_with([_tds_credit_el("Partner rent 194T note", "X", "3000", "300", TAN_C)], []))
    assert RR._ais_by_tan(n)[3] == {}


def test_itr21_agent_itr4_fails_loud_no_sheet(tmp_path, monkeypatch):
    summary, wb = _run_agent(tmp_path, monkeypatch, {"ITR": {"ITR4": {"PersonalInfo": {"PAN": PAN}}}})
    assert "ERROR" in summary and "ITR4" in summary
    assert "Return Tie-out" not in wb.sheetnames


def test_itr21_agent_garbage_return_is_an_error_not_a_crash(tmp_path, monkeypatch):
    summary, wb = _run_agent(tmp_path, monkeypatch, raw="not json at all")
    assert "ERROR" in summary and "Return Tie-out" not in wb.sheetnames


# ---- ITR-22: TCS / payer-side / unknown AIS categories are no TDS credit -----

AC = __import__("agents.skill_ais_reconcile.ais_categories", fromlist=["x"])
_TCS_FX = "Outward foreign remittance/purchase of foreign currency"
_TAN_RENT, _TAN_FX, _TAN_TRV, _TAN_UNK = "TANRR0004R", "TANFX0005F", "TANFT0006T", "TANUK0007U"


def _extras():
    return [
        _tds_credit_el("Rent payment", "LANDLORD", "50000", "5000", _TAN_RENT),
        _tds_credit_el(_TCS_FX, "FX DEALER", "70000", "700", _TAN_FX),
        _tds_credit_el("Foreign travel", "TRAVEL CO", "90000", "900", _TAN_TRV),
    ]


def _norm(tds):
    return N.normalize(_ais_with(list(tds), []))


def test_itr22_classifier_exact_labels():
    for lab in ("Dividend", "Interest from deposit", "Interest from others", "Salary",
                "Receipt of amount by Partners from Partnership Firm", "  INTEREST  from   deposit "):
        assert AC.classify_label(lab) == AC.CREDIT
    assert AC.classify_label("Foreign travel") == AC.TCS and AC.classify_label(_TCS_FX) == AC.TCS
    assert AC.classify_label("Rent payment") == AC.PAYER_SIDE


def test_itr22_label_match_is_whole_label_not_substring():
    """NEGATIVE: a label merely containing interest/travel/rent is not misclassified."""
    for lab in ("Interest from NBFC bonds", "Interest", "Dividend (Domestic)", "Salary received",
                "Foreign travel insurance", "Domestic travel", "Rent payment by tenant",
                "Rent", "Outward foreign remittance", "", None):
        assert AC.classify_label(lab) == AC.UNKNOWN, lab


def test_itr22_credit_only_ais_totals_unchanged_pinned():
    """Credit-row totals are exactly the pre-ITR-22 numbers."""
    tds = [_tds_credit_el("Interest from deposit", "ALPHA BANK", "2500", "200", TAN_A),
           _tds_credit_el("Dividend", "GAMMA FOODS LIMITED", "1000", "30", TAN_B),
           _tds_credit_el("Salary", "EMPLOYER LTD", "100000", "5000", TAN_C)]
    n = _norm(tds)
    rep = R.reconcile_internal(n)
    assert rep.total_tds_credit == Decimal("5230") and rep.uncounted_tds == []
    as26 = R26.reconcile_ais_vs_26as(n, As26Data(transactions=[]), None)
    assert as26.total_ais_tds_credit == Decimal("5230") and as26.uncounted_tds == []
    cr, _names, _cats, t194 = RR._ais_by_tan(n)
    assert cr == {TAN_A: Decimal("200"), TAN_B: Decimal("30"), TAN_C: Decimal("5000")} and t194 == {}


def test_itr22_tcs_and_rent_never_reach_any_tds_credit_total():
    """NEGATIVE: all three checks leave TCS and payer-side rows out of the credit."""
    n = _norm(list(_BASE_TDS) + _extras())
    assert R.reconcile_internal(n).total_tds_credit == Decimal("200")
    as26 = R26.reconcile_ais_vs_26as(n, As26Data(transactions=[]), None)
    assert as26.total_ais_tds_credit == Decimal("200")
    cr = RR._ais_by_tan(n)[0]
    assert set(cr) == {TAN_A, TAN_B}
    assert sum(cr.values()) == Decimal("200")


def test_itr22_books_tds_credit_excludes_tcs_and_rent():
    n = _norm(list(_BASE_TDS) + _extras())
    book, mapping = _income_book("OS_DIVIDEND", "g1", 1000)
    rb = RB.reconcile_ais_vs_books(R.reconcile_internal(n), book, mapping, YEAR_KEY, n)
    assert rb.ais_tds_credit == Decimal("200")


def test_itr22_rent_row_gives_no_not_claimed_line(tmp_path):
    """NEGATIVE: a Rent payment row is not 'TDS in the AIS, not claimed'."""
    rep = _tie(_parse(tmp_path, _itr1_json()), list(_BASE_TDS) + _extras())
    assert _ded(rep, _TAN_RENT) is None and _ded(rep, _TAN_FX) is None and _ded(rep, _TAN_TRV) is None
    assert not any("not claimed" in d.note for d in rep.deductors)
    assert any(lab.startswith("TDS you deducted as payer - not your credit") and _TAN_RENT in txt
               for lab, txt in rep.informational)


def test_itr22_unknown_category_listed_not_summed(tmp_path):
    """NEGATIVE: an unrecognised label is reported by label and count, in no total."""
    unk = [_tds_credit_el("Brand new AIS category", "SOMEONE", "1000", "999", _TAN_UNK),
           _tds_credit_el("Brand new AIS category", "SOMEONE ELSE", "1000", "1", "TANUK0008U")]
    n = _norm(list(_BASE_TDS) + unk)
    rep = R.reconcile_internal(n)
    assert rep.total_tds_credit == Decimal("200") and rep.uncounted_tds == [("Brand new AIS category", 2)]
    assert R26.reconcile_ais_vs_26as(n, As26Data(transactions=[]), None).uncounted_tds == [("Brand new AIS category", 2)]
    rr = _tie(_parse(tmp_path, _itr1_json()), list(_BASE_TDS) + unk)
    assert rr.uncounted_tds == [("Brand new AIS category", 2)]
    assert _ded(rr, _TAN_UNK) is None


def test_itr22_uncounted_line_visible_on_all_four_sheets(tmp_path):
    unk = [_tds_credit_el("Brand new AIS category", "SOMEONE", "1000", "999", _TAN_UNK)]
    n = _norm(list(_BASE_TDS) + unk)
    rep = R.reconcile_internal(n)
    book, mapping = _income_book("OS_DIVIDEND", "g1", 1000)
    rb = RB.reconcile_ais_vs_books(rep, book, mapping, YEAR_KEY, n)
    r26 = R26.reconcile_ais_vs_26as(n, As26Data(transactions=[]), None)
    rr = RR.reconcile_return(_parse(tmp_path, _itr1_json()), rep, n)
    X.write_reco_workbook(rep, str(tmp_path / "w.xlsx"), report_26as=r26, report_books=rb, report_return=rr)
    wbk = load_workbook(tmp_path / "w.xlsx")
    hits = 0
    for ws in wbk.worksheets:
        texts = [str(c.value) for r in ws.iter_rows() for c in r if c.value]
        if any(AC.NOT_COUNTED_TEXT in t for t in texts):
            assert any("Brand new AIS category (1 row)" in t for t in texts)
            hits += 1
    assert hits >= 4        # internal, 26AS, books, return


def test_itr22_tcs_equal_does_not_flag_unequal_does(tmp_path):
    tcs_rows = [_tds_credit_el(_TCS_FX, "FX DEALER", "70000", "700", _TAN_FX),
                _tds_credit_el("Foreign travel", "TRAVEL CO", "90000", "900", _TAN_TRV)]
    base = _itr1_json()
    base["ITR"]["ITR1"]["ScheduleTCS"] = {"TotalSchTCS": 1600}
    rep = _tie(_parse(tmp_path, base), list(_BASE_TDS) + tcs_rows)
    assert rep.tcs is not None and rep.tcs.ais == Decimal("1600") and rep.tcs.ret == Decimal("1600")
    assert not rep.tcs.flagged and rep.flagged_count == 0
    assert sorted(t for t, _n, _a in rep.tcs.by_tan) == sorted([_TAN_FX, _TAN_TRV])
    base["ITR"]["ITR1"]["ScheduleTCS"] = {"TotalSchTCS": 700}
    rep = _tie(_parse(tmp_path, base, "b.json"), list(_BASE_TDS) + tcs_rows)
    assert rep.tcs.flagged and "differs" in rep.tcs.note and rep.flagged_count == 1


def test_itr22_tcs_in_ais_return_without_schedule_is_flagged_plainly(tmp_path):
    payload = _itr1_json()
    del payload["ITR"]["ITR1"]["ScheduleTCS"]
    rep = _tie(_parse(tmp_path, payload), list(_BASE_TDS) + _extras()[1:2])
    assert rep.tcs.flagged and rep.tcs.ret is None and "no readable TCS schedule" in rep.tcs.note
    # with no AIS TCS either, it says so plainly and does not flag
    rep = _tie(_parse(tmp_path, payload, "c.json"), list(_BASE_TDS))
    assert rep.tcs is None and rep.flagged_count == 0
    assert any(lab == "TCS" and "no TCS schedule" in txt for lab, txt in rep.informational)


def test_itr22_tcs_in_return_absent_from_ais_is_flagged(tmp_path):
    payload = _itr1_json()
    payload["ITR"]["ITR1"]["ScheduleTCS"] = {"TotalSchTCS": 500}
    rep = _tie(_parse(tmp_path, payload), list(_BASE_TDS))
    assert rep.tcs.flagged and "absent from the AIS" in rep.tcs.note


def test_itr22_agent_summary_names_unrecognised_category(tmp_path, monkeypatch):
    entities_path = _write_entities_yaml(tmp_path)
    ais = _ais_with(list(_BASE_TDS) + [_tds_credit_el("Brand new AIS category", "SOMEONE", "1000", "999", _TAN_UNK)])
    ais_path = tmp_path / AIS_FILENAME
    ais_path.write_text(D._encrypt_for_test(ais, D.derive_password_from_iso_date(PAN, DOB_ISO)), encoding="utf-8")
    summary = AG.run(str(ais_path), str(tmp_path / "o.xlsx"), entity_key=ENTITY_KEY, entities_path=str(entities_path))
    assert AC.NOT_COUNTED_TEXT in summary and "Brand new AIS category (1 row)" in summary
    assert "total TDS credit reported 200" in summary
