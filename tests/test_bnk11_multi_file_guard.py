"""BNK-11: Convert to GnuCash must stop when it would read only one of several
uploaded statements, and must write nothing when it does."""
from __future__ import annotations

import pytest

import agents.skill_gnucash_pipeline.agent as pipeline_agent
from agents.banks import BankInfo

ONE_FILE_BANKS = {
    "ICICI": ("a.xls", "b.xls"),
    "HDFC": ("a.csv", "b.csv"),
    "SBM": ("a.xlsx", "b.xlsx"),
    "Other Bank (CSV)": ("a.csv", "b.csv"),
}


class _Spy:
    def __init__(self):
        self.calls = []

    def parse(self, path, password=None):
        self.calls.append(str(path))
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


def _calls(spy, normalised):
    return spy.calls + normalised


def _folder(tmp_path, names):
    d = tmp_path / "upload"
    d.mkdir()
    for n in names:
        (d / n).write_bytes(b"x")
    return d


@pytest.mark.parametrize("bank", list(ONE_FILE_BANKS))
def test_two_files_in_a_folder_stop_naming_both(monkeypatch, tmp_path, bank):
    a, b = ONE_FILE_BANKS[bank]
    d = _folder(tmp_path, [a, b])
    spy, norm, result, out_dir = _run(monkeypatch, tmp_path, bank, str(d))
    assert "\u274c" in result and a in result and b in result
    assert "one statement at a time" in result
    assert _calls(spy, norm) == []
    assert not out_dir.exists()


def test_icici_mixed_extensions_stop(monkeypatch, tmp_path):
    d = _folder(tmp_path, ["a.xls", "b.xlsx"])
    spy, norm, result, out_dir = _run(monkeypatch, tmp_path, "ICICI", str(d))
    assert "a.xls" in result and "b.xlsx" in result
    assert _calls(spy, norm) == [] and not out_dir.exists()


def test_two_unknown_extension_files_stop_instead_of_first_file_wins(monkeypatch, tmp_path):
    d = _folder(tmp_path, ["a.dat", "b.dat"])
    spy, norm, result, out_dir = _run(monkeypatch, tmp_path, "ICICI", str(d))
    assert "a.dat" in result and "b.dat" in result
    assert _calls(spy, norm) == [] and not out_dir.exists()


@pytest.mark.parametrize("bank", ["ICICI", "HDFC", "SBM", "Other Bank (CSV)",
                                  "Bank of Baroda", "HSBC"])
@pytest.mark.parametrize("shape", ["list", "comma"])
def test_two_entries_stop_for_every_bank(monkeypatch, tmp_path, bank, shape):
    p1, p2 = tmp_path / "one.pdf", tmp_path / "two.pdf"
    p1.write_bytes(b"x"); p2.write_bytes(b"x")
    given = [str(p1), str(p2)] if shape == "list" else f"{p1}, {p2}"
    spy, norm, result, out_dir = _run(monkeypatch, tmp_path, bank, given)
    assert "one.pdf" in result and "two.pdf" in result and "\u274c" in result
    assert _calls(spy, norm) == [] and not out_dir.exists()


@pytest.mark.parametrize("bank,name", [("ICICI", "s.xls"), ("HDFC", "s.csv"),
                                       ("SBM", "s.xlsx"), ("Other Bank (CSV)", "s.csv")])
@pytest.mark.parametrize("as_folder", [False, True])
def test_one_file_still_reaches_the_parser_once(monkeypatch, tmp_path, bank, name, as_folder):
    d = _folder(tmp_path, [name])
    target = d if as_folder else d / name
    spy, norm, result, _ = _run(monkeypatch, tmp_path, bank, str(target))
    assert _calls(spy, norm) == [str(d / name)]
    assert "more than one statement" not in result


def test_one_statement_beside_a_stray_file_still_runs(monkeypatch, tmp_path):
    d = _folder(tmp_path, ["s.xls", "notes.txt"])
    spy, norm, result, _ = _run(monkeypatch, tmp_path, "ICICI", str(d))
    assert spy.calls == [str(d / "s.xls")]


@pytest.mark.parametrize("bank", ["Bank of Baroda", "HSBC"])
def test_pdf_folders_for_bob_and_hsbc_pass_through(monkeypatch, tmp_path, bank):
    d = _folder(tmp_path, ["a.pdf", "b.pdf", "c.pdf"])
    spy, norm, result, _ = _run(monkeypatch, tmp_path, bank, str(d))
    assert len(spy.calls) == 1
    assert "more than one statement" not in result