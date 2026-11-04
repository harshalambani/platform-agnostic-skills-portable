"""
SEC-15: passwords go to external tools on stdin, and logs mask sensitive values.

Covers:
  * agents.secret_redact: nested dicts/lists, argv-style lists, plain text;
  * the run log line (ui/_runner._format_event) and the per-run log file
    (ui/_runlog) for a tool call that carries a password;
  * the command lines of qpdf (Kroll ledger and bill readers), the HSBC
    pipeline (agent -> run_pipeline -> ocr_to_tsv -> qpdf) and Sort CC PDFs;
  * error paths: a failing tool must not echo the password;
  * every skill input that asks for a password is a masked input.

Negative tests are the point: each one asserts the secret does NOT appear.
Test secrets are obviously fake.
"""
from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for _p in (str(ROOT), str(SRC)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents import secret_redact as sr  # noqa: E402

SECRET = "s3cr3t-test"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _flat(obj) -> str:
    return repr(obj)


class _Result:
    def __init__(self, rc=0, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


# ---------------------------------------------------------------------------
# The redaction helper
# ---------------------------------------------------------------------------

def test_nested_dicts_and_lists_are_masked():
    event = {"tool": "decrypt", "args": {
        "path": "a.pdf",
        "pdf_password": SECRET,
        "opts": [{"token": SECRET}, {"note": "ok"}],
        "headers": {"Authorization": SECRET},
    }}
    out = sr.redact_value(event)
    assert SECRET not in _flat(out)
    assert out["args"]["path"] == "a.pdf"
    assert out["args"]["opts"][1] == {"note": "ok"}


@pytest.mark.parametrize("key", [
    "password", "passwd", "pwd", "pin", "secret", "token", "api_key",
    "pdf_password", "doc_password", "cas_password", "PASSWORD", "access_token",
])
def test_every_sensitive_key_is_masked(key):
    assert sr.redact_value({key: SECRET})[key] == "***"


def test_argv_flag_then_value_and_equals_forms_are_masked():
    argv = ["qpdf", "--password", SECRET, "--decrypt", "in.pdf", "out.pdf"]
    assert sr.redact_argv(argv) == ["qpdf", "--password", "***", "--decrypt",
                                    "in.pdf", "out.pdf"]
    assert sr.redact_argv(["qpdf", f"--password={SECRET}"]) == [
        "qpdf", "--password=***"]
    assert sr.redact_argv(["pdftoppm", "-upw", SECRET, "-png"]) == [
        "pdftoppm", "-upw", "***", "-png"]


def test_flags_that_name_a_source_are_not_masked():
    argv = ["qpdf", "--password-file=-", "--password-stdin", "in.pdf"]
    assert sr.redact_argv(argv) == argv
    assert sr.redact_argv(["x", "--password-file", "-", "y"]) == [
        "x", "--password-file", "-", "y"]


def test_text_forms_are_masked_but_ordinary_prose_is_not():
    text = (f"run --password {SECRET} then password: {SECRET}; "
            f'"pdf_password": "{SECRET}" --password={SECRET} -upw {SECRET}')
    assert SECRET not in sr.redact_text(text)
    prose = "The password is wrong, retry. A wrong password was entered."
    assert sr.redact_text(prose) == prose


def test_explicit_secret_is_masked_wherever_it_appears():
    assert SECRET not in sr.redact_text(f"free text with {SECRET} inside",
                                        secrets=(SECRET,))


def test_empty_and_none_values_pass_through():
    assert sr.redact_value({"password": ""}) == {"password": ""}
    assert sr.redact_value({"password": None}) == {"password": None}
    assert sr.redact_value(5) == 5


# ---------------------------------------------------------------------------
# The run log
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    {"input_path": "a.pdf", "password": SECRET},
    ["qpdf", "--password", SECRET, "a.pdf"],
    ["qpdf", f"--password={SECRET}", "a.pdf"],
    f"password={SECRET}",
    {"nested": {"pdf_password": SECRET}},
])
def test_logged_tool_call_never_contains_the_password(args):
    from ui._runner import _format_event
    line = _format_event({"step": 1, "type": "tool_call", "tool": "t",
                          "args": args}, 0)
    assert SECRET not in line
    assert "Calling tool" in line


def test_tool_result_snippet_is_masked_too():
    from ui._runner import _format_event
    line = _format_event({"step": 2, "type": "tool_result", "tool": "t",
                          "snippet": f"failed with --password {SECRET}"}, 0)
    assert SECRET not in line


def test_run_log_file_and_outcome_do_not_hold_the_password(tmp_path):
    from ui import _runlog
    log = tmp_path / "run.log"
    _runlog.write_run_log(
        log, skill_name="x",
        run_log_lines=[f"cmd: qpdf --password {SECRET} a.pdf",
                       f"{{'pdf_password': '{SECRET}'}}"],
        traceback_text=f"RuntimeError: bad password={SECRET}")
    _runlog.append_outcome(log, f"Error: password: {SECRET}")
    assert SECRET not in log.read_text(encoding="utf-8")


def test_ordinary_log_text_is_unchanged(tmp_path):
    from ui import _runlog
    log = tmp_path / "run.log"
    _runlog.write_run_log(log, skill_name="x", run_log_lines=["Step 1 ok"])
    assert "Step 1 ok" in log.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# qpdf call sites (KR Choksey ledger and bills)
# ---------------------------------------------------------------------------

def _krc_modules():
    ledger = _load("sec15_krc_ledger",
                   SRC / "agents" / "skill_krc" / "scripts" / "parse_krc_ledger.py")
    bills = _load("sec15_krc_bills",
                  SRC / "agents" / "skill_krc_recon" / "scripts" / "parse_krc_bills.py")
    return ledger, bills


def test_krc_qpdf_command_lines_never_carry_the_password(monkeypatch):
    ledger, bills = _krc_modules()
    seen = []

    def fake_run(cmd, **kw):
        seen.append((list(cmd), kw.get("input")))
        return _Result(0)

    for mod, call in ((ledger, lambda: ledger.decrypt_pdf("a.pdf", SECRET, "b.pdf")),
                      (bills, lambda: bills.decrypt("a.pdf", SECRET, "b.pdf"))):
        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr(mod, "resolve_qpdf", lambda: "qpdf")
        seen.clear()
        call()
        cmd, stdin = seen[0]
        assert SECRET not in " ".join(cmd)
        assert "--password-file=-" in cmd
        assert stdin == SECRET + "\n"


def test_krc_qpdf_failure_does_not_echo_the_password(monkeypatch):
    ledger, bills = _krc_modules()

    def fake_run(cmd, **kw):
        return _Result(2, err=f"invalid password {SECRET}")

    for mod, call in ((ledger, lambda: ledger.decrypt_pdf("a.pdf", SECRET, "b.pdf")),
                      (bills, lambda: bills.decrypt("a.pdf", SECRET, "b.pdf"))):
        monkeypatch.setattr(mod.subprocess, "run", fake_run)
        monkeypatch.setattr(mod, "resolve_qpdf", lambda: "qpdf")
        with pytest.raises(RuntimeError) as ei:
            call()
        assert SECRET not in str(ei.value)


# ---------------------------------------------------------------------------
# HSBC: agent -> run_pipeline -> ocr_to_tsv -> qpdf / pdftoppm
# ---------------------------------------------------------------------------

HSBC_SCRIPTS = SRC / "agents" / "skill_hsbc" / "scripts"


def test_hsbc_agent_passes_the_password_on_stdin(monkeypatch, tmp_path):
    from agents.skill_hsbc import agent as hsbc
    seen = []

    def fake_run(cmd, **kw):
        seen.append((list(cmd), kw.get("input")))
        return _Result(1, err="boom")

    monkeypatch.setattr(hsbc.subprocess, "run", fake_run)
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(RuntimeError):
        hsbc._run_ocr_pipeline([pdf], password=SECRET)
    cmd, stdin = seen[0]
    assert SECRET not in " ".join(cmd)
    assert "--password" not in cmd
    assert cmd[cmd.index("--password-file") + 1] == "-"
    assert stdin == SECRET + "\n"


def test_hsbc_agent_error_does_not_echo_the_password(monkeypatch, tmp_path):
    from agents.skill_hsbc import agent as hsbc

    def fake_run(cmd, **kw):
        return _Result(1, err=f"pipeline died; saw {SECRET} in input")

    monkeypatch.setattr(hsbc.subprocess, "run", fake_run)
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(Exception) as ei:
        hsbc._run_ocr_pipeline([pdf], password=SECRET)
    assert SECRET not in str(ei.value)


def test_hsbc_without_a_password_adds_no_password_option(monkeypatch, tmp_path):
    from agents.skill_hsbc import agent as hsbc
    seen = []

    def fake_run(cmd, **kw):
        seen.append((list(cmd), kw.get("input")))
        return _Result(1, err="boom")

    monkeypatch.setattr(hsbc.subprocess, "run", fake_run)
    pdf = tmp_path / "s.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    with pytest.raises(RuntimeError):
        hsbc._run_ocr_pipeline([pdf])
    cmd, stdin = seen[0]
    assert "--password-file" not in cmd and "--password" not in cmd
    assert stdin is None


def test_run_pipeline_hands_the_password_to_ocr_on_stdin(monkeypatch, tmp_path):
    rp = _load("sec15_run_pipeline", HSBC_SCRIPTS / "run_pipeline.py")
    seen = []

    def fake_run(cmd, **kw):
        seen.append((list(cmd), kw.get("input")))
        return _Result(1)   # stop after the first stage

    monkeypatch.setattr(rp.subprocess, "run", fake_run)
    monkeypatch.setattr(sys, "stdin", io.StringIO(SECRET + "\n"))
    monkeypatch.setattr(sys, "argv", [
        "run_pipeline.py", "--pdf-dir", str(tmp_path), "--work-dir",
        str(tmp_path / "w"), "--out", str(tmp_path / "o.xlsx"),
        "--password-file", "-"])
    with pytest.raises(SystemExit):
        rp.main()
    cmd, stdin = seen[0]
    assert SECRET not in " ".join(cmd)
    assert "--password" not in cmd
    assert stdin == SECRET + "\n"


def test_run_pipeline_refuses_a_password_on_the_command_line(monkeypatch, tmp_path):
    rp = _load("sec15_run_pipeline2", HSBC_SCRIPTS / "run_pipeline.py")
    monkeypatch.setattr(sys, "argv", [
        "run_pipeline.py", "--pdf-dir", str(tmp_path), "--work-dir",
        str(tmp_path / "w"), "--out", str(tmp_path / "o.xlsx"),
        "--password", SECRET])
    with pytest.raises(SystemExit):
        rp.main()   # argparse rejects the old flag; nothing is run


def _ocr_module(monkeypatch):
    ocr = _load("sec15_ocr_to_tsv", HSBC_SCRIPTS / "ocr_to_tsv.py")
    monkeypatch.setattr(ocr, "resolve_pdftoppm", lambda: "PDFTOPPM")
    monkeypatch.setattr(ocr, "resolve_qpdf", lambda: "QPDF")
    monkeypatch.setattr(ocr, "resolve_tesseract", lambda: "TESS")
    return ocr


def test_ocr_command_lines_never_carry_the_password(monkeypatch, tmp_path):
    ocr = _ocr_module(monkeypatch)
    calls = []

    def fake_run(cmd, **kw):
        calls.append((list(cmd), kw.get("input")))
        if cmd[0] == "QPDF":
            Path(cmd[-1]).write_bytes(b"%PDF-1.4\n")   # the decrypted copy
        if cmd[0] == "TESS":
            Path(cmd[2] + ".tsv").write_text("x", encoding="utf-8")
        if cmd[0] == "PDFTOPPM":
            png_stem = Path(cmd[-1])
            (png_stem.parent / "page-1.png").write_bytes(b"png")
        return _Result(0)

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    pdf = tmp_path / "stmt.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    work = tmp_path / "work"
    work.mkdir()
    ocr.ocr_pdf(pdf, work, password=SECRET)

    assert calls, "nothing ran"
    for cmd, _ in calls:
        assert SECRET not in " ".join(cmd)
        assert "-upw" not in cmd
    qpdf_cmd, qpdf_in = next(c for c in calls if c[0][0] == "QPDF")
    assert "--password-file=-" in qpdf_cmd
    assert qpdf_in == SECRET + "\n"
    # pdftoppm read the decrypted copy, and the copy is gone afterwards.
    ppm_cmd, _ = next(c for c in calls if c[0][0] == "PDFTOPPM")
    assert str(pdf) not in ppm_cmd
    assert not list(work.glob("_decrypted_*.pdf"))


def test_ocr_wrong_password_error_does_not_echo_it(monkeypatch, tmp_path):
    ocr = _ocr_module(monkeypatch)

    def fake_run(cmd, **kw):
        return _Result(2, err=f"invalid password: {SECRET}")

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    pdf = tmp_path / "stmt.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    work = tmp_path / "work"
    work.mkdir()
    with pytest.raises(RuntimeError) as ei:
        ocr.ocr_pdf(pdf, work, password=SECRET)
    assert SECRET not in str(ei.value)


def test_ocr_without_a_password_runs_pdftoppm_directly(monkeypatch, tmp_path):
    ocr = _ocr_module(monkeypatch)
    calls = []

    def fake_run(cmd, **kw):
        calls.append(list(cmd))
        if cmd[0] == "PDFTOPPM":
            (Path(cmd[-1]).parent / "page-1.png").write_bytes(b"png")
        if cmd[0] == "TESS":
            Path(cmd[2] + ".tsv").write_text("x", encoding="utf-8")
        return _Result(0)

    monkeypatch.setattr(ocr.subprocess, "run", fake_run)
    pdf = tmp_path / "stmt.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    work = tmp_path / "work"
    work.mkdir()
    ocr.ocr_pdf(pdf, work)
    assert not any(c[0] == "QPDF" for c in calls)
    assert str(pdf) in next(c for c in calls if c[0] == "PDFTOPPM")


# ---------------------------------------------------------------------------
# Sort CC PDFs: the agent starts the script without the password in argv
# ---------------------------------------------------------------------------

def test_cc_sort_agent_does_not_put_the_password_in_argv(monkeypatch, tmp_path):
    from agents.skill_cc_sort import agent as ccs
    captured = {}

    def fake_run_script(script, args, stdin_text=None):
        captured["args"] = list(args)
        captured["stdin"] = stdin_text
        return 1   # stop here; "Sort failed" is returned

    monkeypatch.setattr(ccs, "_run_script", fake_run_script)
    monkeypatch.setattr(ccs.shutil, "which", lambda name: "qpdf")
    out = ccs.run(str(tmp_path), str(tmp_path / "out"), password=SECRET)
    assert SECRET not in " ".join(captured["args"])
    assert SECRET not in out
    assert captured["stdin"] == SECRET + "\n"


def test_cc_sort_script_reads_the_password_from_stdin_flag():
    src = (SRC / "agents" / "skill_cc_sort" / "scripts"
           / "extract_sort_cc_pdfs.py").read_text(encoding="utf-8")
    assert "--password-stdin" in src
    assert "sys.stdin.readline()" in src


# ---------------------------------------------------------------------------
# UI: every password input is masked
# ---------------------------------------------------------------------------

def test_every_password_named_skill_input_is_a_masked_input():
    bad = []
    for f in sorted((SRC / "agents").glob("*/skill.yaml")):
        data = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        for inp in data.get("inputs") or []:
            name = str(inp.get("name", "")).lower()
            if any(k in name for k in ("password", "passwd", "pwd", "secret")):
                if inp.get("type") != "password":
                    bad.append((f.parent.name, inp.get("name"), inp.get("type")))
    assert not bad, f"unmasked password inputs: {bad}"
