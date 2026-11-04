"""tests/test_gnucash_pipeline_normalise.py — regression coverage for the
skill_gnucash_pipeline generic CSV normalisation path (item 0a/0b).

Covers:
  - _NORMALISE_PROMPT renders without KeyError (the literal JSON example in
    the prompt previously broke .format() for every HDFC-CSV/XLS and
    Other-Bank(CSV) run).
  - _find_generic_header_row tolerates preamble rows above the real header.
  - _sanitize_and_validate_mapping rejects hallucinated header names and
    strips unknown keys, rather than silently trusting the LLM reply.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_gnucash_pipeline.agent import (
    _NORMALISE_PROMPT,
    _find_generic_header_row,
    _sanitize_and_validate_mapping,
)


def test_normalise_prompt_renders_without_keyerror():
    """Regression: the prompt's literal JSON example must be escaped ({{ }})
    so .format(headers=..., sample=...) doesn't raise KeyError on '"Date"'."""
    rendered = _NORMALISE_PROMPT.format(headers='["Date", "Amount"]', sample="  Date=01/04/25, Amount=100")
    assert '"Date": "Txn Date"' in rendered
    assert "{headers}" not in rendered
    assert "{sample}" not in rendered


def test_find_generic_header_row_tolerates_preamble():
    rows = [
        ["Statement of Account"],
        ["Account No: 12345"],
        [],
        ["****"],
        ["Txn Date", "Narration", "Debit", "Credit", "Balance"],
        ["01/04/2025", "SALARY CREDIT", "", "5000.00", "105000.00"],
    ]
    assert _find_generic_header_row(rows) == 4


def test_find_generic_header_row_no_preamble():
    rows = [
        ["Txn Date", "Narration", "Debit", "Credit", "Balance"],
        ["01/04/2025", "SALARY CREDIT", "", "5000.00", "105000.00"],
    ]
    assert _find_generic_header_row(rows) == 0


def test_sanitize_and_validate_mapping_accepts_valid_reply():
    headers = ["Txn Date", "Narration", "Debit", "Credit", "Balance"]
    reply = json.dumps({
        "Date": "Txn Date",
        "Transaction ID": None,
        "Description": "Narration",
        "Account": None,
        "Deposit": "Credit",
        "Withdrawal": "Debit",
        "Balance": "Balance",
        "Currency": None,
    })
    mapping = _sanitize_and_validate_mapping(reply, headers)
    assert mapping["Date"] == "Txn Date"
    assert mapping["Deposit"] == "Credit"


def test_sanitize_and_validate_mapping_strips_unknown_keys():
    headers = ["Txn Date", "Narration", "Debit", "Credit", "Balance"]
    reply = json.dumps({
        "Date": "Txn Date",
        "extra_hallucinated_key": "whatever",
    })
    mapping = _sanitize_and_validate_mapping(reply, headers)
    assert "extra_hallucinated_key" not in mapping


def test_sanitize_and_validate_mapping_rejects_header_not_in_file():
    headers = ["Txn Date", "Narration", "Debit", "Credit", "Balance"]
    reply = json.dumps({
        "Date": "Txn Date",
        "Deposit": "Credit Amount (INR)",  # hallucinated -- not a real header
    })
    with pytest.raises(ValueError, match="not present in the file"):
        _sanitize_and_validate_mapping(reply, headers)


def test_sanitize_and_validate_mapping_rejects_invalid_json():
    headers = ["Txn Date", "Narration"]
    with pytest.raises(ValueError, match="not valid JSON"):
        _sanitize_and_validate_mapping("not json at all", headers)


def test_sanitize_and_validate_mapping_strips_markdown_fences():
    headers = ["Txn Date", "Narration"]
    reply = "```json\n" + json.dumps({"Date": "Txn Date"}) + "\n```"
    mapping = _sanitize_and_validate_mapping(reply, headers)
    assert mapping["Date"] == "Txn Date"
