"""
msg_reader.py - minimal reader for Outlook .msg (OLE compound file) mail.

Built on olefile only (BSD) plus the stdlib ``email`` package. Shared by the
cc_sort and msg_parser skills. It reads exactly what those two skills use:
subject, sender, sent date, plain-text body, and the attachment names and
bytes.

Behaviour worth knowing:
  * Strings: Unicode (stream suffix 001F, UTF-16-LE) or 8-bit (001E, code page
    from property 3FFD0003; ISO-8859-15 when absent).
  * Sender: the transport-header From (RFC 2047 decoded) first, otherwise
    "name <address>" from the sender-name and sender-address properties.
  * Date: client submit time (00390040) as an aware datetime in the machine's
    local zone. None when the message is flagged unsent (0E070003 bit 8), when
    the property is missing, or when the value is a "null date" marker.
  * Body: plain-text stream only. Compressed-RTF-only messages give an empty
    body and a warning (no RTF decoding is attempted).
  * Attachment name: long filename, falling back to the short filename.
  * Embedded message attachments (attach method 5) are listed with
    ``embedded=True`` and no data; they are never opened.
  * Signed S/MIME mail (class ending smime or smime.multipartsigned): the real
    files live inside the single raw attachment as MIME; they are unwrapped
    with the stdlib ``email`` parser. Opaque S/MIME gives no attachments and a
    warning.
  * A file that is not an OLE compound file, is truncated, or has no message
    streams raises MsgReadError. The file handle is always closed.
"""
from __future__ import annotations

import collections
import email
import email.header
import email.message
import email.parser
import email.policy
import struct
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import olefile

_EPOCH_FILETIME = 116444736000000000
_NULL_DATE_FLOOR = 915000000000000000
_DEFAULT_ENCODING = "iso-8859-15"
_SIGNED_START = ("ipm.note", "report", "ipm.skypeteams.message")

_CODE_PAGE_NAMES = {
    20127: "ascii",
    28591: "iso-8859-1",
    28605: "iso-8859-15",
    10000: "mac_roman",
    65001: "utf-8",
    1200: "utf-16-le",
}


class MsgReadError(Exception):
    """The file is not a readable Outlook .msg."""


@dataclass
class MsgAttachment:
    filename: Optional[str]
    data: Optional[bytes]  # None when there is no byte payload
    embedded: bool = False  # an OLE embedded message (never opened)


@dataclass
class MsgMessage:
    subject: Optional[str] = None
    sender: Optional[str] = None
    date: Optional[datetime] = None
    body: str = ""
    message_class: Optional[str] = None
    attachments: list = field(default_factory=list)
    warnings: list = field(default_factory=list)


def _codec_for(page: int) -> Optional[str]:
    import codecs

    cand = _CODE_PAGE_NAMES.get(page) or f"cp{page}"
    try:
        codecs.lookup(cand)
        return cand
    except LookupError:
        return None


def _parse_props(raw: bytes, header_len: int) -> dict:
    """Fixed-size MAPI properties from a __properties_version1.0 stream.

    Keys are 8 hex chars, property id then type (e.g. '00390040')."""
    props = {}
    pos = header_len
    while pos + 16 <= len(raw):
        ptype, pid = struct.unpack_from("<HH", raw, pos)
        value = raw[pos + 8:pos + 16]
        props[f"{pid:04X}{ptype:04X}"] = value
        pos += 16
    return props


def _int32(props: dict, key: str) -> Optional[int]:
    v = props.get(key)
    return None if v is None else struct.unpack("<i", v[:4])[0]


def decode_rfc2047(text: str) -> str:
    text = text.replace("\r\n", "")
    out = []
    for part, enc in email.header.decode_header(text):
        if isinstance(part, bytes):
            try:
                out.append(part.decode(enc or "raw-unicode-escape"))
            except (LookupError, UnicodeDecodeError):
                out.append(part.decode("utf-8", errors="replace"))
        else:
            out.append(part)
    return "".join(out)


def _filetime_to_local(raw: int) -> Optional[datetime]:
    if raw < _EPOCH_FILETIME or raw > _NULL_DATE_FLOOR:
        return None
    stamp = (raw - _EPOCH_FILETIME) / 10000000.0
    try:
        return datetime.fromtimestamp(stamp, timezone.utc).astimezone()
    except (OverflowError, OSError, ValueError):
        return None


def _unwrap_mime(blob: bytes):
    """Breadth-first walk of a MIME tree, not entering message/* parts.

    Returns (attachments, plain_body_bytes, warnings). A data node counts as an
    attachment only when it carries a filename."""
    warnings: list = []
    root = email.message_from_bytes(blob, policy=email.policy.default)
    queue = collections.deque((root,))
    data_nodes = []
    while queue:
        node = queue.popleft()
        if node.get_content_maintype() == "multipart":
            payload = node.get_payload()
            if isinstance(payload, list):
                queue.extend(payload)
            else:
                data_nodes.append(node)
        else:
            data_nodes.append(node)

    attachments = []
    plain = None
    for node in data_nodes:
        name = node.get_filename()
        try:
            data = node.get_content()
            if isinstance(data, email.message.Message):
                data = data.as_bytes()
            elif isinstance(data, str):
                data = data.encode(node.get_content_charset("utf-8"))
            elif not isinstance(data, bytes):
                raise TypeError(type(data).__name__)
        except Exception as exc:  # noqa: BLE001 - report, never lose silently
            warnings.append(f"could not read MIME part {name or '(unnamed)'}: {exc}")
            continue
        if name:
            attachments.append(MsgAttachment(filename=name, data=data))
        elif node.get_content_type() == "text/plain" and plain is None:
            plain = data
    return attachments, plain, warnings


def read_msg(path) -> MsgMessage:
    """Read an Outlook .msg. Raises MsgReadError when it cannot be read."""
    path = str(path)
    try:
        ole = olefile.OleFileIO(path)
    except Exception as exc:  # noqa: BLE001
        raise MsgReadError(f"not an OLE compound file: {exc}") from exc
    try:
        return _read(ole)
    except MsgReadError:
        raise
    except Exception as exc:  # noqa: BLE001
        raise MsgReadError(f"malformed or truncated .msg: {exc}") from exc
    finally:
        ole.close()


def _read(ole) -> MsgMessage:
    entries = ole.listdir(streams=True, storages=True)
    names = {"/".join(e) for e in entries}
    root_streams = {e[0] for e in entries if len(e) == 1}
    if "__properties_version1.0" not in names and not any(
        n.startswith("__substg1.0_") for n in root_streams
    ):
        raise MsgReadError("compound file has no message streams (not a .msg)")

    def stream(name: str) -> Optional[bytes]:
        if not ole.exists(name):
            return None
        data = ole.openstream(name).read()
        # olefile hands back whatever it could read from a cut-off file; a
        # short stream is a truncated file, never a smaller attachment.
        if len(data) != ole.get_size(name):
            raise MsgReadError(f"truncated .msg: stream {name} is incomplete")
        return data

    msg = MsgMessage()
    raw_props = stream("__properties_version1.0")
    props = _parse_props(raw_props, 32) if raw_props else {}

    # String encoding.
    flag = _int32(props, "340D0003")
    if flag is not None:
        unicode_strings = bool(flag & 0x40000)
    else:
        unicode_strings = any(e[-1].upper().endswith("001F") for e in entries)
    if unicode_strings:
        encoding = "utf-16-le"
        suffix = "001F"
    else:
        suffix = "001E"
        page = _int32(props, "3FFD0003")
        if page is None:
            encoding = _DEFAULT_ENCODING
        else:
            encoding = _codec_for(page)
            if encoding is None:
                msg.warnings.append(
                    f"unknown code page {page}; decoded as {_DEFAULT_ENCODING}")
                encoding = _DEFAULT_ENCODING

    def text(base: str, prefix: str = "") -> Optional[str]:
        raw = stream(f"{prefix}__substg1.0_{base}{suffix}")
        return None if raw is None else raw.decode(encoding, errors="replace")

    msg.message_class = text("001A")
    msg.subject = text("0037")

    # Sender.
    sender = None
    header_text = text("007D")
    if header_text:
        if header_text.startswith("Microsoft Mail Internet Headers Version 2.0"):
            header_text = header_text[43:].lstrip()
        hdr = email.parser.HeaderParser(policy=email.policy.compat32).parsestr(header_text)
        found = hdr["from"]
        if found is not None:
            sender = decode_rfc2047(str(found))
    if sender is None:
        name = text("0C1A")
        addr = text("5D01")
        if name is None:
            sender = addr
        else:
            sender = name + (f" <{addr}>" if addr is not None else "")
    msg.sender = sender

    # Date.
    flags = _int32(props, "0E070003") or 0
    raw_date = props.get("00390040")
    if not (flags & 8) and raw_date is not None:
        msg.date = _filetime_to_local(struct.unpack("<Q", raw_date)[0])

    # Raw attachments.
    attach_dirs = sorted({e[0] for e in entries if e[0].startswith("__attach")})
    raw_atts = []
    for d in attach_dirs:
        data = stream(f"{d}/__substg1.0_37010102")
        embedded = ole.exists(f"{d}/__substg1.0_3701000D")
        filename = (text("3707", d + "/") or text("3704", d + "/"))
        if data is None and not embedded:
            msg.warnings.append(f"attachment {d} has no data stream")
        raw_atts.append(MsgAttachment(filename=filename, data=data, embedded=embedded))

    # Body.
    body = text("1000")

    cls = (msg.message_class or "").lower()
    signed = cls.startswith(_SIGNED_START) and (
        cls.endswith("smime.multipartsigned") or cls.endswith("smime"))
    if signed:
        if len(raw_atts) == 1 and raw_atts[0].data is not None:
            inner, plain, warns = _unwrap_mime(raw_atts[0].data)
            msg.warnings.extend(warns)
            msg.attachments = inner
            if body is None and plain is not None:
                body = plain.decode("utf-8", errors="replace")
            if not inner:
                msg.warnings.append(
                    "signed/encrypted message has no readable MIME attachments "
                    f"(opaque S/MIME, {raw_atts[0].filename or 'unnamed attachment'}); "
                    "nothing could be extracted")
        else:
            msg.warnings.append(
                "signed message does not have exactly one raw attachment; "
                "listing its attachments as stored")
            msg.attachments = raw_atts
    else:
        msg.attachments = raw_atts

    if body is None:
        body = ""
        if ole.exists("__substg1.0_10090102"):
            msg.warnings.append(
                "message has only a compressed-RTF body; the text body is empty "
                "(RTF is not decoded)")
    msg.body = body
    return msg
