"""CC-08: the Bank Service Charge / Drawings account checks run before payment
matching, and the "Full log" file holds the run's final error or result text.
Synthetic data only."""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_cc_spend_booking import agent as ccsb          # noqa: E402
from agents.skill_cc_spend_booking import matcher as M           # noqa: E402
from tests.test_cc_spend_booking import (                        # noqa: E402
    APR, MAY, MOD, BSC, DRAW, Guard, april_rows, basic, book_pay, go, may_rows, row, stmt, tie,
)


def _spy_match(monkeypatch):
    calls = []
    real = M.match_payments

    def spy(*a, **k):
        calls.append(1)
        return real(*a, **k)
    monkeypatch.setattr(ccsb.M, "match_payments", spy)
    return calls


def _no_fee_rows():
    return [r for r in april_rows() if r.kind != "fee"]


def _fee_free(tmp_path, monkeypatch, **kw):
    s1, s2 = stmt(APR, _no_fee_rows()), stmt(MAY, may_rows(pay=1700.0))
    return go(tmp_path, monkeypatch, [s1, s2], [tie(s1, printed=1700.0), tie(s2, printed=300.0)],
              [book_pay("p1", 1700, date(2025, 5, 12))], **kw)


# ---- Part A -----------------------------------------------------------------

def test_fee_rows_with_no_bsc_account_fail_before_matching(tmp_path, monkeypatch):
    calls = _spy_match(monkeypatch)
    reply, rows, _c, out = basic(tmp_path, monkeypatch, entity_extra={"bank_service_charge_account": None})
    assert reply.startswith("ERROR") and "bank_service_charge_account" in reply
    assert "Nothing was booked." in reply
    assert calls == []                      # matching never started
    assert rows == [] and not out.exists()


def test_fee_free_run_with_no_bsc_account_still_succeeds(tmp_path, monkeypatch):      # NEGATIVE
    calls = _spy_match(monkeypatch)
    reply, rows, _c, _o = _fee_free(tmp_path, monkeypatch, entity_extra={"bank_service_charge_account": None})
    assert not reply.startswith("ERROR") and calls and rows


def test_unusable_bsc_account_fails_early_with_the_existing_text(tmp_path, monkeypatch):
    calls = _spy_match(monkeypatch)
    monkeypatch.setattr(Guard, "blocked", {BSC: "it is a placeholder"})
    reply, rows, _c, _o = basic(tmp_path, monkeypatch)
    assert reply.startswith("ERROR") and "bank_service_charge_account" in reply and "cannot be used" in reply
    assert calls == [] and rows == []


def test_bsc_account_not_in_the_book_fails_early(tmp_path, monkeypatch):
    calls = _spy_match(monkeypatch)
    reply, rows, _c, _o = basic(tmp_path, monkeypatch,
                                entity_extra={"bank_service_charge_account": "Expenses:Nowhere"})
    assert "is not an account in the book" in reply and calls == [] and rows == []


def test_unusable_bsc_does_not_stop_a_fee_free_run(tmp_path, monkeypatch):            # NEGATIVE
    monkeypatch.setattr(Guard, "blocked", {BSC: "it is a placeholder"})
    reply, rows, _c, _o = _fee_free(tmp_path, monkeypatch)
    assert not reply.startswith("ERROR") and rows


def test_missing_emi_interest_account_does_not_stop_the_run(tmp_path, monkeypatch):   # NEGATIVE
    reply, rows, _c, _o = basic(tmp_path, monkeypatch, entity_extra={"card_emi_interest_account": None})
    assert not reply.startswith("ERROR") and rows


def test_cashback_with_no_drawings_account_fails_before_matching(tmp_path, monkeypatch):
    calls = _spy_match(monkeypatch)
    cb = row("CASHBACK", 50, (4, 12), kind="cashback", direction="Cr")
    s1, s2 = stmt(APR, _no_fee_rows() + [cb]), stmt(MAY, may_rows(pay=1650.0))
    reply, rows, _c, _o = go(tmp_path, monkeypatch, [s1, s2],
                             [tie(s1, printed=1650.0), tie(s2, printed=300.0)],
                             [book_pay("p1", 1650, date(2025, 5, 12))], entity_extra={"drawings_accounts": []})
    assert reply.startswith("ERROR") and "drawings_accounts" in reply and calls == [] and rows == []


def test_late_backstop_still_blocks_a_fee_journal_with_no_account(tmp_path, monkeypatch):
    """The early check is skipped when the fee row is not visible to it; the journal check still stops the run."""
    monkeypatch.setattr(ccsb, "_fee_row", lambda r, rx: False)
    reply, rows, _c, _o = basic(tmp_path, monkeypatch, entity_extra={"bank_service_charge_account": None})
    assert reply.startswith("ERROR") and "bank_service_charge_account" in reply and rows == []


def test_the_reply_carries_per_phase_timings(tmp_path, monkeypatch):
    reply, rows, _c, _o = basic(tmp_path, monkeypatch)
    text = str(reply)
    tail = text.splitlines()[-1]
    assert tail.startswith("Timing: ")
    for phase in ("parse", "match", "journals", "map", "already-booked check", "write"):
        assert phase in tail


# ---- Part B -----------------------------------------------------------------

SECRET = "Zx9-synthetic-pass"


def _handler(tmp_path, monkeypatch, run_fn, *, directory=False):
    from ui import _config
    from ui.tabs import _generic
    out = tmp_path / "outputs"
    out.mkdir(exist_ok=True)
    monkeypatch.setattr(_config, "output_dir", lambda: out)
    monkeypatch.setattr(_config, "data_root_dir", lambda: tmp_path)
    monkeypatch.setattr(_config, "download_staging_dir", lambda: tmp_path)
    monkeypatch.setattr(_config, "load_portable_config", lambda: {})
    monkeypatch.setattr(_config, "materialize_legacy_config", lambda active: tmp_path / "legacy.yaml")
    import agents.registry as reg
    monkeypatch.setattr(reg, "load_run_function", lambda skill: run_fn)
    pw = SimpleNamespace(name="pw", type="password", label="Password", required=False, file_types=None, options=[])
    skill = SimpleNamespace(
        name="cc_test", display_name="CC test", description="", inputs=[pw],
        output=SimpleNamespace(type="directory" if directory else "file", suffix="cc", extension=".csv",
                               download_label="Download"),
        requires=SimpleNamespace(native_binaries=[], external_tools=[], llm=False),
        run_args={"output_path": "{output_path}", "password": "{inputs.pw}"}, mode="direct", entry_point="")
    return _generic._make_run_handler(skill), tmp_path / "logs"


def _logs(d):
    return sorted(d.glob("*.log"))


def test_a_failing_runs_log_contains_the_error_text(tmp_path, monkeypatch):
    def boom(**kw):
        raise RuntimeError(f"statement could not be opened with {kw['password']}")
    h, logs = _handler(tmp_path, monkeypatch, boom)
    list(h(SECRET, "m"))
    [lf] = _logs(logs)
    text = lf.read_text(encoding="utf-8")
    assert "Error: run failed" in text and "statement could not be opened" in text
    assert SECRET not in text                                                      # NEGATIVE


def test_a_run_that_produced_nothing_logs_its_error(tmp_path, monkeypatch):
    h, logs = _handler(tmp_path, monkeypatch, lambda **kw: "ERROR: nothing was booked for synthetic reasons")
    last = list(h("", "m"))[-1][0]
    assert "did not finish successfully" in last
    text = _logs(logs)[0].read_text(encoding="utf-8")
    assert "did not finish successfully" in text and "nothing was booked for synthetic reasons" in text


def test_a_successful_runs_log_has_the_result_line_and_the_rest_is_unchanged(tmp_path, monkeypatch):
    def ok(**kw):
        Path(kw["output_path"]).write_text("a,b\n", encoding="utf-8")
        return "Booked 3 synthetic rows."
    h, logs = _handler(tmp_path, monkeypatch, ok)
    list(h("", "m"))
    text = _logs(logs)[0].read_text(encoding="utf-8")
    assert "=== Run log" in text and "=== Outcome ===" in text
    assert "Done." in text and "Booked 3 synthetic rows." in text


def test_no_password_value_appears_in_the_log_of_a_successful_run(tmp_path, monkeypatch):   # NEGATIVE
    def ok(**kw):
        Path(kw["output_path"]).write_text("a,b\n", encoding="utf-8")
        return f"opened the file with {kw['password']}"
    h, logs = _handler(tmp_path, monkeypatch, ok)
    list(h(SECRET, "m"))
    text = _logs(logs)[0].read_text(encoding="utf-8")
    assert SECRET not in text and "opened the file with ***" in text


def test_redact_ignores_empty_secrets():                                              # NEGATIVE
    from ui import _runlog
    assert _runlog.redact("abc", ["", None]) == "abc"
    assert _runlog.redact("abc xyz", ["xyz"]) == "abc ***"
