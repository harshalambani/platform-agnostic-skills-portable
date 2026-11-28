"""AUD-08: the two KRChoksey skills take the statement password on stdin.

Synthetic data only: a made-up password and a tiny made-up encrypted PDF.
"""
from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

LEDGER_SCRIPT = SRC / "agents" / "skill_krc" / "scripts" / "parse_krc_ledger.py"
BILLS_SCRIPT = SRC / "agents" / "skill_krc_recon" / "scripts" / "parse_krc_bills.py"
PW = "Zq7-made-up-pw-4471"


class _Done:
    def __init__(self, rc=0, out="", err=""):
        self.returncode, self.stdout, self.stderr = rc, out, err


def _capture(monkeypatch, mod, result):
    seen = {}

    def fake_run(cmd, **kw):
        seen["cmd"] = list(cmd)
        seen["input"] = kw.get("input")
        return result

    monkeypatch.setattr(mod.subprocess, "run", fake_run)
    return seen


def _agents():
    from agents.skill_krc import agent as ledger
    from agents.skill_krc_recon import agent as bills
    return ledger, bills


def _calls():
    ledger, bills = _agents()
    return [
        (ledger, lambda: ledger.run("a.pdf", PW, "o.xlsx")),
        (bills, lambda: bills.run("dir", "led.xlsx", PW, "o.xlsx")),
    ]


# ---- the agents ---------------------------------------------------------

def test_both_agents_pass_the_password_on_stdin_never_in_argv(monkeypatch):
    for mod, call in _calls():
        seen = _capture(monkeypatch, mod, _Done(0, "ok"))
        call()
        assert PW not in " ".join(seen["cmd"])
        assert not any(PW in a for a in seen["cmd"])
        assert "--password-stdin" in seen["cmd"]
        assert seen["input"] == PW + "\n"


def test_the_positional_password_slot_is_gone_from_both_agents(monkeypatch):
    ledger, bills = _agents()
    s = _capture(monkeypatch, ledger, _Done(0, "ok"))
    ledger.run("a.pdf", PW, "o.xlsx")
    assert s["cmd"][2:] == ["a.pdf", "o.xlsx", "--password-stdin"]
    s = _capture(monkeypatch, bills, _Done(0, "ok"))
    bills.run("dir", "led.xlsx", PW, "o.xlsx")
    assert s["cmd"][2:] == ["dir", "led.xlsx", "o.xlsx", "--password-stdin"]


def test_an_empty_password_sends_no_flag_and_no_stdin_as_before(monkeypatch):
    for mod, call in _calls():
        seen = _capture(monkeypatch, mod, _Done(0, "ok"))
        if mod.__name__.endswith("skill_krc.agent"):
            mod.run("a.pdf", "", "o.xlsx")
        else:
            mod.run("dir", "led.xlsx", "", "o.xlsx")
        assert "--password-stdin" not in seen["cmd"]
        assert seen["input"] is None


def test_an_error_from_the_script_never_returns_the_password(monkeypatch):            # NEGATIVE
    for mod, call in _calls():
        _capture(monkeypatch, mod, _Done(1, "", f"qpdf said bad password {PW} for file"))
        assert PW not in call()
        _capture(monkeypatch, mod, _Done(2, f"summary {PW}", f"warn {PW}"))
        assert PW not in call()


# ---- the scripts: old call shape is refused -------------------------------

def _run_script(script, args, stdin=None):
    return subprocess.run([sys.executable, str(script), *args], input=stdin,
                          capture_output=True, text=True, timeout=120)


@pytest.mark.parametrize("script,args", [
    (LEDGER_SCRIPT, ["a.pdf", PW, "o.xlsx"]),
    (BILLS_SCRIPT, ["dir", "led.xlsx", PW, "o.xlsx"]),
])
def test_called_the_old_way_a_script_exits_1_and_never_echoes_the_argument(script, args, tmp_path):   # NEGATIVE
    r = _run_script(script, args)
    assert r.returncode == 1
    assert "Usage" in r.stderr and "--password-stdin" in r.stderr
    assert PW not in r.stdout and PW not in r.stderr
    # and the extra argument is not silently taken as the password:
    r = _run_script(script, args, stdin=PW + "\n")
    assert r.returncode == 1 and PW not in r.stdout + r.stderr


@pytest.mark.parametrize("script", [LEDGER_SCRIPT, BILLS_SCRIPT])
def test_the_usage_text_names_no_positional_password(script):
    r = _run_script(script, [])
    assert r.returncode == 1
    assert "<password>" not in r.stderr
    assert "[--password-stdin]" in r.stderr
    assert "<password>" not in script.read_text(encoding="utf-8")


def test_no_krc_subprocess_call_passes_a_password_variable_in_its_argv():              # NEGATIVE
    files = list((SRC / "agents" / "skill_krc").rglob("*.py")) + \
            list((SRC / "agents" / "skill_krc_recon").rglob("*.py"))
    for f in files:
        text = f.read_text(encoding="utf-8")
        lists = re.findall(r"subprocess\.run\(\s*(\[[^\]]*\])", text)
        lists += re.findall(r"\bargs\s*=\s*(\[[^\]]*\])", text)      # argv built first, then run
        for argv in lists:
            bare = re.sub(r"\"[^\"]*\"|'[^']*'", "", argv)      # flag strings such as --password-file are fine
            assert not re.search(r"\b(password|pw|passwd)\b", bare), (f.name, argv)


# ---- end to end on a synthetic encrypted PDF -------------------------------

from qpdf_helper import needs_qpdf  # noqa: E402  (PIPE-19: skip locally, fail on CI)


def _encrypted_pdf(path: Path, password: str) -> Path:
    from reportlab.lib.pdfencrypt import StandardEncryption
    from reportlab.pdfgen import canvas
    c = canvas.Canvas(str(path), encrypt=StandardEncryption(userPassword=password))
    c.drawString(72, 700, "synthetic page with no ledger rows")
    c.save()
    return path


@needs_qpdf
def test_ledger_script_decrypts_with_the_stdin_password(tmp_path):
    pdf = _encrypted_pdf(tmp_path / "s.pdf", PW)
    out = tmp_path / "o.xlsx"
    ok = _run_script(LEDGER_SCRIPT, [str(pdf), str(out), "--password-stdin"], stdin=PW + "\n")
    # Decryption worked: the run got as far as looking for rows (the page has none).
    assert "no ledger rows" in ok.stderr and "decryption failed" not in ok.stderr
    assert PW not in ok.stdout + ok.stderr
    bad = _run_script(LEDGER_SCRIPT, [str(pdf), str(out), "--password-stdin"], stdin="wrong-pw-1\n")     # NEGATIVE
    assert bad.returncode == 1 and "decryption failed" in bad.stderr
    assert PW not in bad.stdout + bad.stderr and "wrong-pw-1" not in bad.stdout + bad.stderr
    nopw = _run_script(LEDGER_SCRIPT, [str(pdf), str(out)])
    assert nopw.returncode == 1 and "decryption failed" in nopw.stderr


@needs_qpdf
def test_bills_script_decrypts_with_the_stdin_password(tmp_path):
    cn = tmp_path / "cn"
    cn.mkdir()
    _encrypted_pdf(cn / "n.pdf", PW)
    led = tmp_path / "led.xlsx"
    led.write_bytes(b"x")
    out = tmp_path / "o.xlsx"
    ok = _run_script(BILLS_SCRIPT, [str(cn), str(led), str(out), "--password-stdin"], stdin=PW + "\n")
    assert "qpdf failed" not in ok.stderr
    assert PW not in ok.stdout + ok.stderr
    bad = _run_script(BILLS_SCRIPT, [str(cn), str(led), str(out), "--password-stdin"], stdin="wrong-pw-2\n")   # NEGATIVE
    assert bad.returncode == 1 and "qpdf failed" in bad.stderr
    assert PW not in bad.stdout + bad.stderr and "wrong-pw-2" not in bad.stdout + bad.stderr


def test_a_password_ending_in_spaces_keeps_them_only_the_newline_is_stripped(tmp_path, monkeypatch):
    import importlib.util
    import io
    spec = importlib.util.spec_from_file_location("aud08_ledger", LEDGER_SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["aud08_ledger"] = mod
    spec.loader.exec_module(mod)
    got = {}
    monkeypatch.setattr(mod, "decrypt_pdf", lambda p, pw, s: got.setdefault("pw", pw) and (_ for _ in ()).throw(RuntimeError("stop")))
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4\n")
    monkeypatch.setattr(sys, "argv", ["x", str(pdf), str(tmp_path / "o.xlsx"), "--password-stdin"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("pw with space  \r\n"))
    assert mod.main() == 1
    assert got["pw"] == "pw with space  "
