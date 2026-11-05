"""
Tests for agents.msg_reader and the cc_sort .msg path, on synthetic real OLE
compound files (see msg_fixtures.py). No mocks, no real mail.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
for p in (str(SRC), str(ROOT / "tests")):
    if p not in sys.path:
        sys.path.insert(0, p)

from agents.msg_reader import MsgReadError, read_msg  # noqa: E402
from msg_fixtures import build_msg, cfb_bytes, signed_mime, utc  # noqa: E402

SORT = SRC / "agents" / "skill_cc_sort" / "scripts" / "extract_sort_cc_pdfs.py"
_spec = importlib.util.spec_from_file_location("extract_sort_msgreader", SORT)
sort_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(sort_mod)

PDF = b"%PDF-1.4\n" + bytes(range(256)) * 3 + b"\n%%EOF"


def write(tmp_path, name="m.msg", **kw):
    p = tmp_path / name
    p.write_bytes(build_msg(**kw))
    return p


def pdfs_under(folder: Path):
    return sorted(folder.rglob("*")) if folder.exists() else []


# ---------------------------------------------------------------- reader
class TestStringsAndNames:
    def test_unicode_fields(self, tmp_path):
        m = read_msg(write(tmp_path, subject="Statement ₹ é",
                           sender_name="Zoë", sender_email="z@example.invalid"))
        assert m.subject == "Statement ₹ é"
        assert m.sender == "Zoë <z@example.invalid>"

    def test_ansi_subject_decoded_by_code_page(self, tmp_path):
        m = read_msg(write(tmp_path, subject="Café “x”", unicode_=False,
                           codepage=1252, cp_name="cp1252"))
        assert m.subject == "Café “x”"

    def test_ansi_other_code_page(self, tmp_path):
        m = read_msg(write(tmp_path, subject="Привет",
                           unicode_=False, codepage=1251, cp_name="cp1251"))
        assert m.subject == "Привет"

    def test_ansi_without_code_page_defaults_to_iso_8859_15(self, tmp_path):
        m = read_msg(write(tmp_path, subject="€", unicode_=False,
                           cp_name="iso-8859-15", omit_codepage=True))
        assert m.subject == "€"

    def test_unknown_code_page_warns_not_crashes(self, tmp_path):
        m = read_msg(write(tmp_path, subject="abc", unicode_=False, codepage=99999,
                           cp_name="ascii"))
        assert m.subject == "abc"
        assert any("code page" in w for w in m.warnings)

    def test_rfc2047_header_from_wins(self, tmp_path):
        m = read_msg(write(tmp_path, header_from="=?utf-8?q?J=C3=BCrgen?= <j@example.invalid>",
                           sender_name="Other", sender_email="o@example.invalid"))
        assert m.sender == "Jürgen <j@example.invalid>"

    def test_sender_falls_back_to_name_and_address(self, tmp_path):
        m = read_msg(write(tmp_path, sender_name="Alice", sender_email="a@example.invalid"))
        assert m.sender == "Alice <a@example.invalid>"


class TestAttachments:
    def test_long_name_preferred_short_is_fallback(self, tmp_path):
        m = read_msg(write(tmp_path, attachments=[
            ("Long Name.pdf", "LONGNA~1.pdf", PDF),
            (None, "SHORT.pdf", PDF),
        ]))
        assert [a.filename for a in m.attachments] == ["Long Name.pdf", "SHORT.pdf"]

    def test_unicode_attachment_name(self, tmp_path):
        m = read_msg(write(tmp_path, attachments=[("über ₹.pdf", None, PDF)]))
        assert m.attachments[0].filename == "über ₹.pdf"

    def test_ansi_attachment_name(self, tmp_path):
        m = read_msg(write(tmp_path, unicode_=False, attachments=[("café.pdf", None, PDF)]))
        assert m.attachments[0].filename == "café.pdf"

    def test_no_attachments(self, tmp_path):
        assert read_msg(write(tmp_path)).attachments == []

    def test_many_attachments_and_non_pdf_listed(self, tmp_path):
        m = read_msg(write(tmp_path, attachments=[
            ("a.pdf", None, PDF), ("b.txt", None, b"hello"), ("c.pdf", None, PDF + b"2")]))
        assert [a.filename for a in m.attachments] == ["a.pdf", "b.txt", "c.pdf"]
        assert m.attachments[1].data == b"hello"

    def test_large_attachment_bytes_identical(self, tmp_path):
        big = os.urandom(20000)  # beyond the mini-stream cut-off
        m = read_msg(write(tmp_path, attachments=[("big.pdf", None, big)]))
        assert m.attachments[0].data == big

    def test_embedded_message_not_recursed(self, tmp_path):
        m = read_msg(write(tmp_path, embedded=[("Fwd: x", "inner")]))
        assert len(m.attachments) == 1
        assert m.attachments[0].embedded is True
        assert m.attachments[0].data is None


class TestDate:
    def test_aware_and_equal_to_local_conversion(self, tmp_path):
        when = utc(2026, 3, 14, 9, 26, 53)
        m = read_msg(write(tmp_path, date=when))
        assert m.date.tzinfo is not None
        assert m.date == when
        assert m.date.utcoffset() == when.astimezone().utcoffset()

    def test_unsent_gives_none(self, tmp_path):
        assert read_msg(write(tmp_path, date=utc(2026, 1, 1), unsent=True)).date is None

    def test_null_date_gives_none(self, tmp_path):
        assert read_msg(write(tmp_path, raw_filetime=0x7FFFFFFFFFFFFFFF)).date is None

    def test_missing_date_gives_none(self, tmp_path):
        assert read_msg(write(tmp_path)).date is None

    def test_pre_epoch_gives_none(self, tmp_path):
        assert read_msg(write(tmp_path, raw_filetime=1000)).date is None


class TestBody:
    def test_plain_body(self, tmp_path):
        m = read_msg(write(tmp_path, body="Hello body"))
        assert m.body == "Hello body" and not m.warnings

    def test_rtf_only_empty_body_and_warning(self, tmp_path):
        m = read_msg(write(tmp_path, rtf_only=True))
        assert m.body == ""
        assert any("RTF" in w for w in m.warnings)

    def test_no_rtf_stream_no_warning(self, tmp_path):
        m = read_msg(write(tmp_path, no_body=True))
        assert m.body == "" and m.warnings == []


class TestSigned:
    def test_signed_unwraps_inner_pdf(self, tmp_path):
        blob = signed_mime([("stmt.pdf", PDF)])
        m = read_msg(write(tmp_path, msg_class="IPM.Note.SMIME.MultipartSigned",
                           attachments=[("smime.p7m", None, blob)]))
        # The detached signature is a named MIME part, so it is listed like any
        # other named part (same rule as the reference reader); the PDF is there.
        by_name = {a.filename: a for a in m.attachments}
        assert set(by_name) == {"stmt.pdf", "smime.p7s"}
        assert by_name["stmt.pdf"].data == PDF

    def test_signed_never_lists_the_raw_p7m_wrapper(self, tmp_path):
        blob = signed_mime([("stmt.pdf", PDF)])
        m = read_msg(write(tmp_path, msg_class="IPM.Note.SMIME.MultipartSigned",
                           attachments=[("smime.p7m", None, blob)]))
        assert "smime.p7m" not in [a.filename for a in m.attachments]

    def test_opaque_smime_no_attachments_and_warning_names_file(self, tmp_path):
        m = read_msg(write(tmp_path, msg_class="IPM.Note.SMIME",
                           attachments=[("smime.p7m", None, b"\x30\x82\x01\x00opaque")]))
        assert m.attachments == []
        assert any("opaque S/MIME" in w and "smime.p7m" in w for w in m.warnings)

    def test_signed_with_two_raw_attachments_listed_as_stored(self, tmp_path):
        m = read_msg(write(tmp_path, msg_class="IPM.Note.SMIME.MultipartSigned",
                           attachments=[("a.pdf", None, PDF), ("b.pdf", None, PDF)]))
        assert [a.filename for a in m.attachments] == ["a.pdf", "b.pdf"]
        assert any("exactly one" in w for w in m.warnings)

    def test_signed_embedded_message_not_recursed(self, tmp_path):
        from email.message import EmailMessage
        inner = EmailMessage()
        inner["Subject"] = "forwarded"
        inner.set_content("x")
        outer = EmailMessage()
        outer.set_content("body")
        outer.add_attachment(inner)  # message/rfc822 part
        outer.add_attachment(PDF, maintype="application", subtype="pdf", filename="k.pdf")
        m = read_msg(write(tmp_path, msg_class="IPM.Note.SMIME",
                           attachments=[("smime.p7m", None, outer.as_bytes())]))
        assert "k.pdf" in [a.filename for a in m.attachments]


class TestFailures:
    def test_garbage_is_not_ole(self, tmp_path):
        p = tmp_path / "g.msg"
        p.write_bytes(b"not ole" * 100)
        with pytest.raises(MsgReadError):
            read_msg(p)

    def test_empty_file(self, tmp_path):
        p = tmp_path / "e.msg"
        p.write_bytes(b"")
        with pytest.raises(MsgReadError):
            read_msg(p)

    def test_truncated_file(self, tmp_path):
        raw = build_msg(attachments=[("a.pdf", None, os.urandom(20000))])
        p = tmp_path / "t.msg"
        p.write_bytes(raw[: len(raw) // 2])
        with pytest.raises(MsgReadError):
            read_msg(p)

    def test_non_message_compound_file(self, tmp_path):
        p = tmp_path / "doc.msg"
        p.write_bytes(cfb_bytes({"WordDocument": b"x" * 100, "1Table": b"y" * 50}))
        with pytest.raises(MsgReadError, match="no message streams"):
            read_msg(p)

    def test_missing_file(self, tmp_path):
        with pytest.raises(MsgReadError):
            read_msg(tmp_path / "nope.msg")

    def test_handle_closed_on_success_and_failure(self, tmp_path):
        good = write(tmp_path)
        read_msg(good)
        good.unlink()  # fails on Windows if the handle were still open
        bad = tmp_path / "bad.msg"
        bad.write_bytes(cfb_bytes({"WordDocument": b"x"}))
        with pytest.raises(MsgReadError):
            read_msg(bad)
        bad.unlink()
        assert not bad.exists()

    def test_unknown_class_real_msg_is_read(self, tmp_path):
        m = read_msg(write(tmp_path, msg_class="IPM.Appointment", subject="Meet"))
        assert m.subject == "Meet"


# ---------------------------------------------------------------- cc_sort path
class TestCcSortExtraction:
    def run(self, tmp_path, msg_path, structure=None):
        out = tmp_path / "out"
        out.mkdir(exist_ok=True)
        files = sort_mod.extract_pdfs_from_cdfv2_msg(msg_path, out, structure)
        return out, files

    def test_extracts_only_pdfs_byte_identical(self, tmp_path):
        p = write(tmp_path, attachments=[("a.pdf", None, PDF), ("n.txt", None, b"t"),
                                         ("B.PDF", None, PDF + b"B")])
        out, files = self.run(tmp_path, p)
        assert len(files) == 2
        assert sorted(f.read_bytes() for f in files) == sorted([PDF, PDF + b"B"])
        assert not any(f.name.endswith("n.txt") for f in out.rglob("*"))

    def test_short_name_fallback_extracts(self, tmp_path):
        p = write(tmp_path, attachments=[(None, "SHORT.pdf", PDF)])
        _, files = self.run(tmp_path, p)
        assert len(files) == 1 and files[0].read_bytes() == PDF

    def test_no_attachments_writes_nothing(self, tmp_path):
        out, files = self.run(tmp_path, write(tmp_path))
        assert files == [] and [f for f in out.rglob("*") if f.is_file()] == []

    def test_embedded_message_writes_nothing(self, tmp_path):
        out, files = self.run(tmp_path, write(tmp_path, embedded=[("Fwd", "x")]))
        assert files == [] and [f for f in out.rglob("*") if f.is_file()] == []

    def test_signed_extracts_pdf_not_p7m(self, tmp_path):
        blob = signed_mime([("stmt.pdf", PDF)])
        p = write(tmp_path, msg_class="IPM.Note.SMIME.MultipartSigned",
                  attachments=[("smime.p7m", None, blob)])
        out, files = self.run(tmp_path, p)
        assert len(files) == 1 and files[0].read_bytes() == PDF
        assert not any("p7m" in f.name for f in out.rglob("*"))

    def test_opaque_smime_writes_nothing_and_warns(self, tmp_path, capsys):
        p = write(tmp_path, msg_class="IPM.Note.SMIME",
                  attachments=[("smime.p7m", None, b"\x30\x82\x01opaque")])
        out, files = self.run(tmp_path, p)
        assert files == [] and [f for f in out.rglob("*") if f.is_file()] == []
        assert "opaque S/MIME" in capsys.readouterr().out

    @pytest.mark.parametrize("kind", ["truncated", "garbage", "non-message"])
    def test_unreadable_writes_nothing(self, tmp_path, kind, capsys):
        p = tmp_path / "x.msg"
        if kind == "truncated":
            raw = build_msg(attachments=[("a.pdf", None, os.urandom(20000))])
            p.write_bytes(raw[: len(raw) // 2])
        elif kind == "garbage":
            p.write_bytes(b"\x00garbage" * 50)
        else:
            p.write_bytes(cfb_bytes({"WordDocument": b"x" * 10}))
        out, files = self.run(tmp_path, p, "by_date")
        assert files == []
        assert [f for f in out.rglob("*")] == []
        assert "Skipping" in capsys.readouterr().out

    def test_by_date_uses_local_month(self, tmp_path):
        # Pick an instant whose month differs between UTC and the local zone.
        local_off = datetime(2026, 4, 15, tzinfo=timezone.utc).astimezone().utcoffset()
        if local_off is None or local_off.total_seconds() == 0:
            pytest.skip("local UTC offset is 0 (e.g. a UTC CI runner): "
                        "local month == UTC month, nothing to distinguish")
        instant = utc(2026, 3, 31, 23, 55) if local_off.total_seconds() > 0 \
            else utc(2026, 4, 1, 0, 5)
        local = instant.astimezone()
        if local.month == instant.month:
            pytest.skip("offset too small to cross a month boundary at this instant")
        p = write(tmp_path, date=instant, attachments=[("a.pdf", None, PDF)])
        out, files = self.run(tmp_path, p, "by_date")
        assert files[0].parent.name == f"{local.year:04d}-{local.month:02d}"
        assert files[0].parent.name != f"{instant.year:04d}-{instant.month:02d}"

    def test_by_date_uses_a_month_folder_in_any_zone(self, tmp_path):
        instant = utc(2026, 6, 15, 12)
        p = write(tmp_path, date=instant, attachments=[("a.pdf", None, PDF)])
        _, files = self.run(tmp_path, p, "by_date")
        assert files[0].parent.name == f"{instant.astimezone():%Y-%m}"

    @pytest.mark.parametrize("variant", ["unsent", "null"])
    def test_by_date_without_date_uses_plain_folder(self, tmp_path, variant):
        kw = {"date": utc(2026, 1, 1), "unsent": True} if variant == "unsent" \
            else {"raw_filetime": 0x7FFFFFFFFFFFFFFF}
        p = write(tmp_path, attachments=[("a.pdf", None, PDF)], **kw)
        out, files = self.run(tmp_path, p, "by_date")
        assert files[0].parent == out

    def test_by_sender_and_subject_folders(self, tmp_path):
        p = write(tmp_path, sender_name="Alice", subject="Jan Stmt",
                  attachments=[("a.pdf", None, PDF)])
        out, files = self.run(tmp_path, p, "by_sender")
        assert files[0].parent.name == "Alice"
        out2 = tmp_path / "o2"
        out2.mkdir()
        f2 = sort_mod.extract_pdfs_from_cdfv2_msg(p, out2, "by_subject")
        assert f2[0].parent.name == "Jan Stmt"
