"""
agents/bank_common/password.py — uniform password-protected-PDF error
handling shared by bank PDF parsers.

Moved verbatim from ``skill_hdfc/agent.py``'s inline password-error detection
in ``_parse_pdf_pdfplumber``, which remains the reference implementation. The
password itself is NEVER part of either function's input or output.
"""
from __future__ import annotations


def is_password_error(e: Exception) -> bool:
    """Best-effort detection of a "this PDF needs a password" failure.

    pdfminer's ``PDFPasswordIncorrect`` (missing/wrong password) has no
    message of its own; pdfplumber wraps it in a ``PdfminerException`` whose
    ``str()`` is also empty — so the check looks at the exception chain
    (cause / wrapped args), not just ``str(e)``.
    """
    candidates = [e, getattr(e, "__cause__", None), *e.args]
    return any(
        c is not None and (
            "password" in type(c).__name__.lower()
            or "password" in str(c).lower()
            or "encrypt" in str(c).lower()
        )
        for c in candidates
    )


def password_error_message(hint: str = "", doc_type: str = "PDF") -> str:
    """Uniform, actionable message for a password-protected document — never
    echoes the password itself. ``hint`` is a bank/skill-specific pointer to
    where the password usually comes from (e.g. "for HDFC often the Cust
    ID"). ``doc_type`` defaults to "PDF" (every existing caller is a PDF
    parser); pass e.g. "xlsx" for a non-PDF caller such as skill_mf_cas's
    encrypted-workbook path."""
    base = f"{doc_type} is password-protected — supply the statement password"
    return f"{base} ({hint})." if hint else f"{base}."
