"""BNK-11 (kept by BNK-01): Convert to GnuCash must stop when it would read only
one of several uploaded statements, and must write nothing when it does.

Since BNK-01 every dedicated bank reads the whole upload through its own reader
(see test_bnk01_statements.py), so this guard is the second line of defence and
only fires on the route that reads a single file: Other Bank (CSV)."""
from __future__ import annotations

import pytest

import agents.skill_gnucash_pipeline.agent as pipeline_agent
from agents.banks import BankInfo

CROSS = chr(0x274C)

DEDICATED = {
    "ICICI": ("a.xls", "b.xls"),
    "HDFC": ("a.csv", "b.csv"),
    "SBM": ("a.xlsx", "b.xlsx"),
    "Bank of Baroda": ("a.pdf", "b.pdf"),
    "HSBC": ("a.pdf", "b.pdf"),
}


class _Spy:
    def __init__(self):
        self.calls = []

    def parse(self, path, password=None, **kw):
        self.calls.append(path)
        raise RuntimeError("test-sentinel-stop")


def _run(monkeypatch, tmp_path, bank, statement_files):
    spy = _Spy()
    normalised = []
    info = BankInfo(bank_key=bank.lower(), display_name=bank, skill_name=bank,
                    package="agents.skill_stub")
    monkeypatch.setattr(pipeline_agent, "discover_banks",
                        lambda: [] if bank == "Other Bank (CSV)" else [info])
    monkeypatch.setattr(pipeline_agent, "load_bank_skill", lambda i: spy)

    def _fake_norm(**kw):
        normalised.append(kw["input_file"])
        raise RuntimeError("test-sentinel-stop")

    monkeypatch.setattr(pipeline_agent, "_normalise_to_canonical", _fake_norm)
    out_dir = tmp_path / "never_made"
    out = out_dir / "out.csv"
    result = pipeline_agent.run(
        bank=bank, statement_files=statement_files,
        gnucash_file=str(tmp_path / "nonexistent.gnucash"), output_path=str(out),
    )
    return spy, normalised, result, out_dir


def _folder(tmp_path, names):
    d = tmp_path / "upload"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"x")
    return d


def test_other_bank_two_files_in_a_folder_stop_naming_both(monkeypatch, tmp_path):
    d = _folder(tmp_path, ["a.csv", "b.csv"])
    spy, norm, result, out_dir = _run(monkeypatch, tmp_path, "Other Bank (CSV)", str(d))
    assert CROSS in result and "a.csv" in result and "b.csv" in result
    assert "one statement at a time" in result
    assert spy.calls == [] and norm == []
    assert not out_dir.exists()


@pytest.mark.parametrize("shape", ["list", "comma"])
def test_other_bank_two_entries_stop(monkeypatch, tmp_path, shape):
    p1, p2 = tmp_path / "one.csv", tmp_path / "two.csv"
    p1.write_bytes(b"x"); p2.write_bytes(b"x")
    given = [str(p1), str(p2)] if shape == "list" else f"{p1}, {p2}"
    spy, norm, result, out_dir = _run(monkeypatch, tmp_path, "Other Bank (CSV)", given)
    assert "one.csv" in result and "two.csv" in result and CROSS in result
    assert spy.calls == [] and norm == [] and not out_dir.exists()


def test_other_bank_one_file_still_reaches_the_normaliser_once(monkeypatch, tmp_path):
    d = _folder(tmp_path, ["s.csv"])
    spy, norm, result, _ = _run(monkeypatch, tmp_path, "Other Bank (CSV)", str(d))
    assert norm == [str(d / "s.csv")]
    assert "more than one statement" not in result


@pytest.mark.parametrize("bank", list(DEDICATED))
def test_dedicated_bank_gets_the_whole_upload_and_is_not_stopped_here(monkeypatch, tmp_path, bank):
    """BNK-01: no 'one statement at a time' stop for a dedicated bank; the
    bank's reader receives every entry of the upload, unchanged."""
    a, b = DEDICATED[bank]
    d = _folder(tmp_path, [a, b])
    spy, norm, result, _ = _run(monkeypatch, tmp_path, bank, str(d))
    assert spy.calls == [[str(d)]]
    assert "more than one statement" not in result


@pytest.mark.parametrize("bank", list(DEDICATED))
def test_dedicated_bank_gets_a_list_of_files_as_given(monkeypatch, tmp_path, bank):
    p1, p2 = tmp_path / "one.pdf", tmp_path / "two.pdf"
    p1.write_bytes(b"x"); p2.write_bytes(b"x")
    spy, norm, result, _ = _run(monkeypatch, tmp_path, bank, [str(p1), str(p2)])
    assert spy.calls == [[str(p1), str(p2)]]


def test_guard_function_never_fires_for_a_dedicated_bank(monkeypatch):
    info = BankInfo(bank_key="icici", display_name="ICICI", skill_name="icici",
                    package="agents.skill_stub")
    monkeypatch.setattr(pipeline_agent, "discover_banks", lambda: [info])
    assert pipeline_agent._multi_file_stop("ICICI", ["a.xls", "b.xls"]) is None
    # ... and still fires for the CSV route (second guard stays in place).
    assert pipeline_agent._multi_file_stop("Other Bank (CSV)", ["a.csv", "b.csv"])
