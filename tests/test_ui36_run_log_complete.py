"""UI-36: the saved run log is complete -- result text, endpoint-check line,
cancel -- written once per run, with no secret in it. Synthetic data only."""
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from tests.test_cc08_early_checks_and_log import _handler, _logs   # noqa: E402
from ui import _runlog                                              # noqa: E402
from ui.tabs import _generic                                        # noqa: E402

KEY = "sk-synthetic-key-123456789"


def test_tick_updates_only_its_own_elapsed_line():
    log = ["**Running** — endpoint OK (synthetic)", "**Running** — still working (1s elapsed)"]
    _generic.tick_log(log, "**Running** — still working (2s elapsed)")
    assert log == ["**Running** — endpoint OK (synthetic)",
                   "**Running** — still working (2s elapsed)"]


def test_tick_never_removes_the_endpoint_check_line():                # NEGATIVE
    log = ["**Running** — endpoint OK (synthetic)"]
    _generic.tick_log(log, "**Running** — still working (1s elapsed)")
    _generic.tick_log(log, "**Running** — still working (2s elapsed)")
    assert log[0] == "**Running** — endpoint OK (synthetic)" and len(log) == 2
    log = ["**WARNING: continuing WITHOUT a working AI endpoint**"]
    _generic.tick_log(log, "**Running** — still working (1s elapsed)")
    assert log[0].startswith("**WARNING") and len(log) == 2


def test_saved_log_contains_the_skill_status_text(tmp_path, monkeypatch):
    def ok(**kw):
        Path(kw["output_path"]).write_text("a\n", encoding="utf-8")
        return "STATUS: READY. 4 mapped, 1 held. WARNING: one synthetic warning."
    h, logs = _handler(tmp_path, monkeypatch, ok)
    list(h("", "m"))
    text = _logs(logs)[0].read_text(encoding="utf-8")
    assert "STATUS: READY. 4 mapped, 1 held." in text and "one synthetic warning" in text


def test_a_cancelled_run_leaves_a_log_that_says_so(tmp_path, monkeypatch):
    h, logs = _handler(tmp_path, monkeypatch, lambda **kw: "__CANCELLED__")
    list(h("", "m"))
    [lf] = _logs(logs)
    assert "Cancelled" in lf.read_text(encoding="utf-8")


def test_one_log_file_per_run_on_every_exit_path(tmp_path, monkeypatch):   # NEGATIVE
    def boom(**kw):
        raise RuntimeError("synthetic failure")
    for fn in (boom, lambda **kw: "__CANCELLED__", lambda **kw: "ERROR: nothing"):
        sub = tmp_path / str(id(fn))
        sub.mkdir()
        h, logs = _handler(sub, monkeypatch, fn)
        list(h("", "m"))
        assert len(_logs(logs)) == 1


def test_no_api_key_bearer_or_password_in_a_saved_log(tmp_path, monkeypatch):   # NEGATIVE
    from ui import _config
    def ok(**kw):
        Path(kw["output_path"]).write_text("a\n", encoding="utf-8")
        return (f"called with key {KEY}; header Authorization: Bearer abcdef1234567890xyz; "
                f"api_key={KEY}; password=hunter2hunter2")
    h, logs = _handler(tmp_path, monkeypatch, ok)
    monkeypatch.setattr(_config, "load_portable_config",
                        lambda: {"active_endpoint": "e", "endpoints": {"e": {"api_key": KEY}}})
    monkeypatch.setattr(_config, "decrypt_api_key", lambda raw: raw)
    list(h("", "m"))
    text = _logs(logs)[0].read_text(encoding="utf-8")
    for leaked in (KEY, "abcdef1234567890xyz", "hunter2hunter2"):
        assert leaked not in text


def test_redact_leaves_ordinary_text_alone():                          # NEGATIVE
    assert _runlog.redact("Booked 3 rows in the bank account.") == "Booked 3 rows in the bank account."
