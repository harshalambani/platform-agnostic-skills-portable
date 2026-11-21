"""Bank Skills -- one tab, every bank (BNK-01).

``run`` picks the statements (the upload, or HSBC's folder box) and hands them
to the shared run in ``bank_common.bank_run``.
"""
from __future__ import annotations

from agents.bank_common.bank_run import run_statements


def run(bank, statement_files, hsbc_folder, password, allow_balance_break,
        output_path, config_path=None, model_override=None):
    folder = (hsbc_folder or "").strip()
    uploaded = (statement_files or "").strip()
    if folder and uploaded:
        return ("ERROR - two sources: give either the uploaded file(s) or the "
                "folder, not both.")
    return run_statements(bank, folder or uploaded, output_path,
                          password=password, allow_balance_break=allow_balance_break)
