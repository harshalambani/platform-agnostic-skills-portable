"""
Synthetic Outlook .msg builder for tests.

Builds REAL OLE compound files (CFB v3, with a FAT, a mini stream and a mini
FAT) holding the MAPI streams the readers look at. No real mail is involved.
"""
from __future__ import annotations

import struct
from datetime import datetime, timezone
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

SECT = 512
MINI = 64
FREE = 0xFFFFFFFF
END = 0xFFFFFFFE
FATSECT = 0xFFFFFFFD
NOSTREAM = 0xFFFFFFFF
_EPOCH = 116444736000000000


# ---------------------------------------------------------------- CFB writer
class _Entry:
    def __init__(self, name, kind, data=b""):
        self.name = name
        self.kind = kind  # 1 storage, 2 stream, 5 root
        self.data = data
        self.kids = []
        self.left = self.right = self.child = NOSTREAM
        self.start = END
        self.size = 0


def _flatten(tree: dict, entry: _Entry, out: list):
    for name, val in tree.items():
        e = _Entry(name, 1 if isinstance(val, dict) else 2,
                   b"" if isinstance(val, dict) else val)
        entry.kids.append(e)
    # assign ids in order: breadth of this level first, then recurse
    for e, (name, val) in zip(entry.kids, tree.items()):
        out.append(e)
    for e, (name, val) in zip(entry.kids, tree.items()):
        if isinstance(val, dict):
            _flatten(val, e, out)


def _link(entry: _Entry, ids: dict):
    kids = sorted(entry.kids, key=lambda k: (len(k.name), k.name.upper()))

    def build(lo, hi):
        if lo >= hi:
            return NOSTREAM
        mid = (lo + hi) // 2
        k = kids[mid]
        k.left = build(lo, mid)
        k.right = build(mid + 1, hi)
        return ids[id(k)]

    entry.child = build(0, len(kids))
    for k in entry.kids:
        _link(k, ids)


def cfb_bytes(tree: dict) -> bytes:
    """tree: {name: bytes (stream) | dict (storage)} -> compound file bytes."""
    root = _Entry("Root Entry", 5)
    entries = [root]
    _flatten(tree, root, entries)
    ids = {id(e): i for i, e in enumerate(entries)}
    _link(root, ids)

    # Mini stream: streams smaller than 4096 bytes.
    mini_data = bytearray()
    mini_fat = []
    for e in entries:
        if e.kind != 2:
            continue
        e.size = len(e.data)
        if e.size == 0:
            e.start = END
        elif e.size < 4096:
            n = -(-e.size // MINI)
            e.start = len(mini_fat)
            for i in range(n):
                mini_fat.append(len(mini_fat) + 1 if i < n - 1 else END)
            mini_data += e.data.ljust(n * MINI, b"\0")
    root.size = len(mini_data)

    # Regular sectors: laid out [FAT][dir][minifat][ministream][big streams].
    n_dir = -(-len(entries) // 4)
    n_minifat = -(-len(mini_fat) // 128) if mini_fat else 0
    n_ministream = -(-len(mini_data) // SECT) if mini_data else 0
    big = [e for e in entries if e.kind == 2 and e.size >= 4096]
    big_secs = [-(-e.size // SECT) for e in big]
    body = n_dir + n_minifat + n_ministream + sum(big_secs)
    n_fat = 1
    while n_fat * 128 < body + n_fat:
        n_fat += 1
    assert n_fat <= 109

    fat = [FREE] * (n_fat * 128)
    pos = 0
    for i in range(n_fat):
        fat[pos] = FATSECT
        pos += 1

    def chain(count):
        nonlocal pos
        if count == 0:
            return END
        first = pos
        for i in range(count):
            fat[pos] = pos + 1 if i < count - 1 else END
            pos += 1
        return first

    dir_start = chain(n_dir)
    minifat_start = chain(n_minifat)
    root.start = chain(n_ministream)
    for e, c in zip(big, big_secs):
        e.start = chain(c)

    out = bytearray()
    hdr = bytearray(512)
    hdr[0:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", hdr, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<I", hdr, 44, n_fat)
    struct.pack_into("<I", hdr, 48, dir_start)
    struct.pack_into("<I", hdr, 56, 4096)
    struct.pack_into("<I", hdr, 60, minifat_start)
    struct.pack_into("<I", hdr, 64, n_minifat)
    struct.pack_into("<I", hdr, 68, END)
    struct.pack_into("<I", hdr, 72, 0)
    for i in range(109):
        struct.pack_into("<I", hdr, 76 + 4 * i, i if i < n_fat else FREE)
    out += hdr

    for i in range(n_fat):
        out += struct.pack("<128I", *fat[i * 128:(i + 1) * 128])

    d = bytearray()
    for e in entries:
        nm = e.name.encode("utf-16-le") + b"\0\0"
        rec = bytearray(128)
        rec[0:len(nm)] = nm
        struct.pack_into("<H", rec, 64, len(nm))
        rec[66] = e.kind
        rec[67] = 1
        struct.pack_into("<III", rec, 68, e.left, e.right, e.child)
        struct.pack_into("<I", rec, 116, e.start if e.kind != 1 else 0)
        struct.pack_into("<Q", rec, 120, e.size if e.kind != 1 else 0)
        d += rec
    out += bytes(d).ljust(n_dir * SECT, b"\0")

    if n_minifat:
        mf = mini_fat + [FREE] * (n_minifat * 128 - len(mini_fat))
        out += struct.pack(f"<{len(mf)}I", *mf)
    if n_ministream:
        out += bytes(mini_data).ljust(n_ministream * SECT, b"\0")
    for e, c in zip(big, big_secs):
        out += e.data.ljust(c * SECT, b"\0")
    return bytes(out)


# ------------------------------------------------------------- MAPI helpers
def filetime(dt: datetime) -> int:
    return int(dt.timestamp() * 10000000) + _EPOCH


def _prop(pid: int, ptype: int, value: bytes) -> bytes:
    return struct.pack("<HHI", ptype, pid, 6) + value.ljust(8, b"\0")


def int_prop(pid, v):
    return _prop(pid, 0x0003, struct.pack("<i", v))


def time_prop(pid, raw_filetime):
    return _prop(pid, 0x0040, struct.pack("<Q", raw_filetime))


def props_stream(entries: list, header: int) -> bytes:
    return b"\0" * header + b"".join(entries)


def _s(pid: int, text: str, unicode_: bool, cp: str):
    suffix = "001F" if unicode_ else "001E"
    enc = "utf-16-le" if unicode_ else cp
    return f"__substg1.0_{pid:04X}{suffix}", text.encode(enc)


def build_msg(
    *,
    subject="Statement",
    sender_name="Alice Sender",
    sender_email="alice@example.invalid",
    header_from=None,
    body="Hello",
    date=None,
    raw_filetime=None,
    unsent=False,
    unicode_=True,
    codepage=1252,
    cp_name="cp1252",
    attachments=(),
    embedded=(),
    msg_class="IPM.Note",
    rtf_only=False,
    rtf_raw=None,
    no_body=False,
    omit_codepage=False,
    omit_unicode_flag=False,
) -> bytes:
    """attachments: iterable of (long_name|None, short_name|None, bytes).
    embedded: iterable of (display name, subject) for OLE embedded messages."""
    tree: dict = {}
    props = []
    if not unicode_:
        if not omit_unicode_flag:
            props.append(int_prop(0x340D, 0))
        if not omit_codepage:
            props.append(int_prop(0x3FFD, codepage))
    else:
        props.append(int_prop(0x340D, 0x00040000))
    flags = 8 if unsent else 0
    props.append(int_prop(0x0E07, flags))
    if raw_filetime is None and date is not None:
        raw_filetime = filetime(date)
    if raw_filetime is not None:
        props.append(time_prop(0x0039, raw_filetime))

    def put(streams, pid, text):
        if text is None:
            return
        n, b = _s(pid, text, unicode_, cp_name)
        streams[n] = b

    put(tree, 0x001A, msg_class)
    put(tree, 0x0037, subject)
    if header_from is not None:
        put(tree, 0x007D, f"From: {header_from}\r\nSubject: x\r\n")
    put(tree, 0x0C1A, sender_name)
    put(tree, 0x5D01, sender_email)
    if not no_body and not rtf_only:
        put(tree, 0x1000, body)
    if rtf_only:
        raw = rb"{\rtf1\ansi\deff0 {\fonttbl{\f0 Arial;}}Hello rtf only}"
        raw = rtf_raw or raw
        # "MELA" = stored uncompressed (MS-OXRTFCP).
        tree["__substg1.0_10090102"] = (
            struct.pack("<II4sI", len(raw) + 12, len(raw), b"MELA", 0) + raw)

    idx = 0
    for long_, short, data in attachments:
        folder: dict = {}
        folder["__properties_version1.0"] = props_stream([int_prop(0x3705, 1)], 8)
        put(folder, 0x3707, long_)
        put(folder, 0x3704, short)
        folder["__substg1.0_37010102"] = data
        tree[f"__attach_version1.0_#{idx:08X}"] = folder
        idx += 1
    for disp, subj in embedded:
        inner = {"__properties_version1.0": props_stream([int_prop(0x340D, 0x00040000)], 24)}
        inner["__substg1.0_0037001F"] = subj.encode("utf-16-le")
        inner["__substg1.0_001A001F"] = "IPM.Note".encode("utf-16-le")
        inner["__nameid_version1.0"] = {
            "__substg1.0_00020102": b"",
            "__substg1.0_00030102": b"",
            "__substg1.0_00040102": b"",
        }
        folder = {
            "__properties_version1.0": props_stream([int_prop(0x3705, 5)], 8),
            "__substg1.0_3701000D": inner,
        }
        n, b = _s(0x3001, disp, True, "utf-16-le")
        folder[n] = b
        tree[f"__attach_version1.0_#{idx:08X}"] = folder
        idx += 1

    hdr = struct.pack("<8xIIII8x", idx, idx, 0, idx)
    tree["__properties_version1.0"] = hdr + b"".join(props)
    tree["__nameid_version1.0"] = {
        "__substg1.0_00020102": b"",
        "__substg1.0_00030102": b"",
        "__substg1.0_00040102": b"",
    }
    return cfb_bytes(tree)


def signed_mime(files, *, with_text=True) -> bytes:
    """A multipart/signed MIME blob holding the given (name, bytes) files."""
    inner = MIMEMultipart("mixed")
    if with_text:
        inner.attach(MIMEText("Please find attached.", "plain"))
    for name, data in files:
        part = MIMEApplication(data, "pdf" if name.lower().endswith(".pdf") else "octet-stream")
        part.add_header("Content-Disposition", "attachment", filename=name)
        inner.attach(part)
    outer = MIMEMultipart("signed", protocol="application/pkcs7-signature")
    outer.attach(inner)
    sig = MIMEApplication(b"\x30\x82\x00\x04fake", "pkcs7-signature", name="smime.p7s")
    sig.add_header("Content-Disposition", "attachment", filename="smime.p7s")
    outer.attach(sig)
    return outer.as_bytes()


def utc(y, m, d, hh=0, mm=0, ss=0):
    return datetime(y, m, d, hh, mm, ss, tzinfo=timezone.utc)
