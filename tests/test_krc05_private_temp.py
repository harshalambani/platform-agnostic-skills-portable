"""KRC-05: contract notes are decrypted in a private temp folder, not /tmp."""
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
SCRIPT = SRC / "agents" / "skill_krc_recon" / "scripts" / "parse_krc_bills.py"


@pytest.fixture()
def mod():
    sys.path.insert(0, str(SRC))
    spec = importlib.util.spec_from_file_location("krc05_bills", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    sys.modules["krc05_bills"] = m
    spec.loader.exec_module(m)
    yield m
    sys.modules.pop("krc05_bills", None)


def _fake(mod, monkeypatch, seen, text="Net Amount Receivable/Payable By Client\n", fail=False):
    def decrypt(pdf, pw, out):
        seen["out"] = Path(out)
        seen["parent"] = Path(out).parent.resolve()
        Path(out).write_bytes(b"decrypted")

    def text_of(path):
        assert Path(path).exists()
        if fail:
            raise ValueError("unreadable")
        return text

    monkeypatch.setattr(mod, "decrypt", decrypt)
    monkeypatch.setattr(mod, "text_of", text_of)
    monkeypatch.setattr(mod, "parse_trade", lambda t, f: ({"security": "X"}, []))


def test_decrypted_copy_is_in_a_private_folder_and_removed(mod, monkeypatch):
    seen = {}
    _fake(mod, monkeypatch, seen)
    rec, _ = mod.parse_cn("C:/somewhere/n.pdf", "pw")
    out = seen["out"]
    assert "pa-skills-cn-" in str(out.parent.name)
    # resolve both sides: a runner's TEMP can be an 8.3 short path
    assert Path(tempfile.gettempdir()).resolve() in seen["parent"].parents
    assert not out.exists() and not out.parent.exists()
    assert "client_code" in rec


def test_copy_is_gone_after_a_read_failure(mod, monkeypatch):  # NEGATIVE
    seen = {}
    _fake(mod, monkeypatch, seen, fail=True)
    with pytest.raises(ValueError):
        mod.parse_cn("n.pdf", "pw")
    assert not seen["out"].exists() and not seen["out"].parent.exists()


def test_copy_is_gone_after_a_decrypt_failure(mod, monkeypatch):  # NEGATIVE
    seen = {}

    def decrypt(pdf, pw, out):
        seen["out"] = Path(out)
        Path(out).write_bytes(b"partial")
        raise RuntimeError("qpdf failed")

    monkeypatch.setattr(mod, "decrypt", decrypt)
    with pytest.raises(RuntimeError):
        mod.parse_cn("n.pdf", "pw")
    assert not seen["out"].parent.exists()


def test_nothing_is_written_to_a_shared_tmp_folder(mod, monkeypatch, tmp_path):  # NEGATIVE
    fake_tmp = tmp_path / "tmp-root"
    fake_tmp.mkdir()
    monkeypatch.setattr(tempfile, "tempdir", str(fake_tmp))
    seen = {}
    _fake(mod, monkeypatch, seen)
    mod.parse_cn("n.pdf", "pw")
    assert list(fake_tmp.iterdir()) == []
    assert not Path("/tmp/_cn_n.pdf").exists()
    assert "/tmp/" not in SCRIPT.read_text(encoding="utf-8").replace("%TEMP% / /tmp", "")
