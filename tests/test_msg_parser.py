"""
tests/test_msg_parser.py — Unit tests for the MSG/Email Parser skill (Phase 6, task 6-5).

Tests cover:
  - .eml parsing with synthetic fixtures (sender, date, subject, body, attachments)
  - .msg parsing against real synthetic compound files
  - Unsupported extension error
  - Missing file error
  - Registry discovery (skill.yaml)

Run with:
    cd src && python -m pytest ../tests/test_msg_parser.py -v
"""
from __future__ import annotations

import email
import email.mime.multipart
import email.mime.text
import email.mime.base
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_msg_parser.agent import run, _parse_eml, _parse_msg  # noqa: E402
sys.path.insert(0, str(ROOT / "tests"))
from msg_fixtures import build_msg, signed_mime, utc  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers — synthetic .eml fixtures
# ---------------------------------------------------------------------------

def _make_eml(
    sender: str = "Alice <alice@example.com>",
    to: str = "Bob <bob@example.com>",
    subject: str = "Test Subject",
    body: str = "Hello, this is a test.",
    date: str = "Thu, 01 Jan 2026 12:00:00 +0000",
    attachments: list[tuple[str, bytes]] | None = None,
) -> bytes:
    """Build a raw RFC 5322 .eml as bytes."""
    if attachments:
        msg = email.mime.multipart.MIMEMultipart()
        msg.attach(email.mime.text.MIMEText(body, "plain"))
        for fname, data in attachments:
            att = email.mime.base.MIMEBase("application", "octet-stream")
            att.set_payload(data)
            att.add_header("Content-Disposition", "attachment", filename=fname)
            msg.attach(att)
    else:
        msg = email.mime.text.MIMEText(body, "plain")

    msg["From"] = sender
    msg["To"] = to
    msg["Subject"] = subject
    msg["Date"] = date
    return msg.as_bytes()


# ---------------------------------------------------------------------------
# .eml parsing
# ---------------------------------------------------------------------------

class TestParseEml:
    def test_basic_fields(self, tmp_path):
        eml = tmp_path / "test.eml"
        eml.write_bytes(_make_eml())

        result = _parse_eml(eml)
        assert result["sender"] == "Alice <alice@example.com>"
        assert result["subject"] == "Test Subject"
        assert "Hello" in result["body"]
        assert result["attachments"] == []

    def test_date_parsed_to_iso(self, tmp_path):
        eml = tmp_path / "test.eml"
        eml.write_bytes(_make_eml(date="Thu, 01 Jan 2026 12:00:00 +0000"))

        result = _parse_eml(eml)
        # Should be ISO 8601
        assert "2026-01-01" in result["date"]

    def test_attachments_extracted(self, tmp_path):
        eml = tmp_path / "test.eml"
        eml.write_bytes(_make_eml(
            attachments=[
                ("report.pdf", b"fake-pdf-data-12345"),
                ("notes.txt", b"some notes"),
            ]
        ))

        result = _parse_eml(eml)
        assert len(result["attachments"]) == 2
        names = {a["filename"] for a in result["attachments"]}
        assert "report.pdf" in names
        assert "notes.txt" in names
        # Size check
        pdf_att = next(a for a in result["attachments"] if a["filename"] == "report.pdf")
        assert pdf_att["size_bytes"] == len(b"fake-pdf-data-12345")

    def test_empty_body(self, tmp_path):
        eml = tmp_path / "test.eml"
        eml.write_bytes(_make_eml(body=""))

        result = _parse_eml(eml)
        assert result["body"] == ""

    def test_html_fallback(self, tmp_path):
        """If only HTML body exists, strip tags and use it."""
        msg = email.mime.text.MIMEText("<p>Hello <b>World</b></p>", "html")
        msg["From"] = "test@example.com"
        msg["Subject"] = "HTML only"
        msg["Date"] = "Thu, 01 Jan 2026 12:00:00 +0000"

        eml = tmp_path / "html.eml"
        eml.write_bytes(msg.as_bytes())

        result = _parse_eml(eml)
        assert "Hello" in result["body"]
        assert "<p>" not in result["body"]


# ---------------------------------------------------------------------------
# .msg parsing (real synthetic compound files, no mocks)
# ---------------------------------------------------------------------------

class TestParseMsg:
    def _write(self, tmp_path, **kw):
        p = tmp_path / "m.msg"
        p.write_bytes(build_msg(**kw))
        return p

    def test_basic_fields(self, tmp_path):
        p = self._write(tmp_path, sender_name="Bob", sender_email="bob@example.invalid",
                        subject="RE: Test", body="Reply body", date=utc(2026, 1, 1, 12))
        result = _parse_msg(p)
        assert result["sender"] == "Bob <bob@example.invalid>"
        assert result["subject"] == "RE: Test"
        assert result["body"] == "Reply body"
        assert result["date"].startswith("2026-01-01") or result["date"].startswith("2026-01-02")
        assert result["attachments"] == []
        assert "warnings" not in result

    def test_msg_with_attachments(self, tmp_path):
        p = self._write(tmp_path, attachments=[("invoice.pdf", "inv.pdf", b"x" * 1024)])
        result = _parse_msg(p)
        assert result["attachments"] == [{"filename": "invoice.pdf", "size_bytes": 1024}]

    def test_date_is_aware_local_iso(self, tmp_path):
        p = self._write(tmp_path, date=utc(2026, 1, 1, 12))
        d = datetime.fromisoformat(_parse_msg(p)["date"])
        assert d.tzinfo is not None
        assert d == datetime(2026, 1, 1, 12, tzinfo=timezone.utc)

    def test_unsent_and_null_date_are_empty_string(self, tmp_path):
        p = self._write(tmp_path, date=utc(2026, 1, 1), unsent=True)
        assert _parse_msg(p)["date"] == ""
        p2 = self._write(tmp_path, raw_filetime=0x7FFFFFFFFFFFFFFF)
        assert _parse_msg(p2)["date"] == ""

    def test_embedded_message_has_size_zero(self, tmp_path):
        p = self._write(tmp_path, embedded=[("Fwd", "inner subject")])
        result = _parse_msg(p)
        assert result["attachments"][0]["size_bytes"] == 0

    def test_rtf_only_gives_empty_body_and_warning(self, tmp_path):
        p = self._write(tmp_path, rtf_only=True)
        result = _parse_msg(p)
        assert result["body"] == ""
        assert result["warnings"] and "RTF" in result["warnings"][0]

    def test_signed_lists_inner_files_not_smime_p7m(self, tmp_path):
        blob = signed_mime([("a.pdf", b"%PDF-1")])
        p = self._write(tmp_path, msg_class="IPM.Note.SMIME.MultipartSigned",
                        attachments=[("smime.p7m", None, blob)])
        names = [a["filename"] for a in _parse_msg(p)["attachments"]]
        assert "a.pdf" in names and "smime.p7m" not in names

    def test_garbage_file_raises_naming_the_file(self, tmp_path):
        from agents.msg_reader import MsgReadError
        p = tmp_path / "junk.msg"
        p.write_bytes(b"this is not a compound file at all")
        with pytest.raises(MsgReadError, match="junk.msg"):
            _parse_msg(p)

    def test_no_extract_msg_import_needed(self, tmp_path):
        import builtins
        real = builtins.__import__

        def guard(name, *a, **k):
            assert name.split(".")[0] != "extract_msg"
            return real(name, *a, **k)

        p = self._write(tmp_path)
        with patch("builtins.__import__", guard):
            _parse_msg(p)


# ---------------------------------------------------------------------------
# run() entry point
# ---------------------------------------------------------------------------

class TestRun:
    def test_eml_produces_json(self, tmp_path):
        eml = tmp_path / "input.eml"
        eml.write_bytes(_make_eml(subject="Invoice"))
        out = tmp_path / "output.json"

        result_str = run(str(eml), str(out))
        assert out.is_file()
        result = json.loads(result_str)
        assert result["subject"] == "Invoice"

    def test_unsupported_extension(self, tmp_path):
        txt = tmp_path / "file.txt"
        txt.write_text("nope")
        with pytest.raises(ValueError, match="Unsupported"):
            run(str(txt), str(tmp_path / "out.json"))

    def test_missing_file(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            run(str(tmp_path / "no.eml"), str(tmp_path / "out.json"))


# ---------------------------------------------------------------------------
# Registry discovery
# ---------------------------------------------------------------------------

class TestRegistryDiscovery:
    def test_msg_parser_in_registry(self):
        from agents.registry import discover
        skills = discover(refresh=True)
        names = {s.name for s in skills}
        assert "MSG Parser" in names

    def test_msg_parser_mode_is_direct(self):
        from agents.registry import get
        skill = get("MSG Parser")
        assert skill is not None
        assert skill.mode == "direct"

    def test_msg_parser_output_extension(self):
        from agents.registry import get
        skill = get("MSG Parser")
        assert skill.output.extension == ".json"
