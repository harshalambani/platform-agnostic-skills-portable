"""
agents/bank_common — shared, bank-agnostic utilities for statement parsers.

Everything here is pure/stateless and format-generic: header-row detection,
alias-table column mapping, amount/date normalization, garbled-PDF-text-layer
detection, and uniform password-error handling. A bank parser owns only its
own alias tables and format-specific glue; the mechanics live here once.

HDFC's implementation (``skill_hdfc/agent.py``) is the reference this package
was promoted from — verbatim logic, so migrating HDFC onto it is a pure code
move with zero behavior change.
"""
from __future__ import annotations

from agents.bank_common.normalize import clean_amount, normalise_date
from agents.bank_common.password import is_password_error, password_error_message
from agents.bank_common.tabular import find_header_row, map_columns
from agents.bank_common.text_quality import text_layer_usable

__all__ = [
    "clean_amount",
    "normalise_date",
    "is_password_error",
    "password_error_message",
    "find_header_row",
    "map_columns",
    "text_layer_usable",
]
