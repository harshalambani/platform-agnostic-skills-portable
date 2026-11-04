"""LEG-12: the test suite must not leave files behind.

(a) mapping-rules yamls must not land in bundling/templates/DefaultData/settings/
(b) the HSBC single-PDF path must not leave hsbc_run_* folders in the temp dir
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT), str(ROOT / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

TEMPLATE_SETTINGS = ROOT / "bundling" / "templates" / "DefaultData" / "settings"


def _settings_files():
    return {p.name for p in TEMPLATE_SETTINGS.iterdir()}


def test_template_settings_holds_only_tracked_files_after_rules_calls(tmp_path):
    """Drive rules_path/save with a temp book and the TEMPLATE config -- the
    exact combination that used to write into the repo."""
    from agents.skill_gnucash_account_mapper import persistent_rules as pr

    before = _settings_files()
    assert before == {"config.yaml"}, f"template settings already polluted: {before}"

    book = tmp_path / "LeakProbe2526.gnucash"
    book.write_bytes(b"x")
    cfg = str(TEMPLATE_SETTINGS / "config.yaml")
    rp = pr.rules_path(str(book), cfg)
    pr.save_overrides_batch(str(book), [{"patterns": ["PROBE"], "account": "Expenses:X"}], cfg)

    assert rp.parent.resolve() != TEMPLATE_SETTINGS.resolve()
    assert _settings_files() == before, "a rules file leaked into the template settings"


def test_hsbc_single_pdf_run_leaves_no_hsbc_run_folder(tmp_path, monkeypatch):
    import agents.skill_hsbc.agent as hsbc_agent

    snap = lambda: {p.name for p in Path(tempfile.gettempdir()).glob("hsbc_run_*")}  # noqa: E731
    before = snap()

    pdf = tmp_path / "stmt.pdf"
    pdf.write_bytes(b"%PDF-1.4 fake")

    class _Done:
        returncode = 1
        stdout = ""
        stderr = "stop"

    monkeypatch.setattr(hsbc_agent.subprocess, "run", lambda *a, **k: _Done())
    try:
        hsbc_agent.run(str(pdf), str(tmp_path / "work"), str(tmp_path / "out.xlsx"))
    except Exception:  # noqa: BLE001 -- the stub subprocess fails on purpose
        pass

    assert snap() - before == set(), "hsbc_run_* folder leaked into the temp dir"
