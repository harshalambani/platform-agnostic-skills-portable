"""
tests/test_msg_parser.py — Unit tests for the MSG/Email Parser skill (Phase 6, task 6-5).

Tests cover:
  - .eml parsing with synthetic fixtures (sender, date, subject, body, attachments)
  - .msg parsing with mocked extract-msg
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
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from agents.skill_msg_parser.agent import run, _parse_eml, _parse_msg


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
# .msg parsing (mocked)
# ---------------------------------------------------------------------------

class TestParseMsg:
    def _mock_msg(self, sender="Bob", subject="RE: Test",
                  body="Reply body", date=None, attachments=None):
        mock = MagicMock()
        mock.sender = sender
        mock.subject = subject
        mock.body = body
        mock.date = date or datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        mock.attachments = attachments or []
        mock.close = MagicMock()
        return mock

    def test_basic_fields(self, tmp_path):
        mock = self._mock_msg()
        with patch("extract_msg.Message") as MockMessage:
            MockMessage.return_value = mock
            result = _parse_msg(tmp_path / "dummy.msg")

        assert result["sender"] == "Bob"
        assert result["subject"] == "RE: Test"
        assert result["body"] == "Reply body"
        assert "2026-01-01" in result["date"]
        assert result["attachments"] == []

    def test_msg_with_attachments(self, tmp_path):
        att = MagicMock()
        att.longFilename = "invoice.pdf"
        att.shortFilename = "inv.pdf"
        att.data = b"x" * 1024

        mock = self._mock_msg(attachments=[att])
        with patch("extract_msg.Message") as MockMessage:
            MockMessage.return_value = mock
            result = _parse_msg(tmp_path / "dummy.msg")

        assert len(result["attachments"]) == 1
        assert result["attachments"][0]["filename"] == "invoice.pdf"
        assert result["attachments"][0]["size_bytes"] == 1024

    def test_msg_close_called(self, tmp_path):
        mock = self._mock_msg()
        with patch("extract_msg.Message") as MockMessage:
            MockMessage.return_value = mock
            _parse_msg(tmp_path / "dummy.msg")
        mock.close.assert_called_once()

    def test_date_as_string(self, tmp_path):
        mock = self._mock_msg(date="2026-01-01 12:00:00")
        with patch("extract_msg.Message") as MockMessage:
            MockMessage.return_value = mock
            result = _parse_msg(tmp_path / "dummy.msg")
        assert result["date"] == "2026-01-01 12:00:00"


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
