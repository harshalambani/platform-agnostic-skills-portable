"""
tests/test_gnc_book.py -- agents.gnc_book (GNC-01 PR 1: the one shared book
opener).

Synthetic books only, built in tmp_path (gzip and plain). Golden tests assert
that read_xml_root() yields the same parse as the open-and-parse step each of
the current readers performs today. Negative tests assert every unreadable
case raises GncBookUnreadable, a cached root is not reused after the file
changes, a plain book is not rejected, and no caller's path convention moved.
"""
from __future__ import annotations

import gzip
import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"

from agents import gnc_book  # noqa: E402
from agents.gnc_book import GncBookUnreadable, clear_cache, read_xml_root  # noqa: E402

_NS = " ".join(
    f'xmlns:{p}="http://www.gnucash.org/XML/{p}"'
    for p in ("gnc", "act", "book", "cd", "cmdty", "slot", "split", "trn", "ts"))
_CUR = "<act:commodity><cmdty:space>CURRENCY</cmdty:space><cmdty:id>INR</cmdty:id></act:commodity>"


def _acc(name, guid, typ, parent=None, slots=None):
    p = f'<act:parent type="guid">{parent}</act:parent>' if parent else ""
    s = ""
    if slots:
        s = "<act:slots>" + "".join(
            f'<slot><slot:key>{k}</slot:key><slot:value type="string">{v}</slot:value></slot>'
            for k, v in slots) + "</act:slots>"
    return (f'<gnc:account version="2.0.0"><act:name>{name}</act:name>'
            f'<act:id type="guid">{guid}</act:id><act:type>{typ}</act:type>{_CUR}{p}{s}</gnc:account>')


def _txn(guid, d, num, desc, splits):
    sp = "".join(
        f'<trn:split><split:id type="guid">{guid}{i}</split:id>'
        f'<split:reconciled-state>n</split:reconciled-state>'
        f'<split:value>{v}</split:value><split:quantity>{q or v}</split:quantity>'
        f'<split:account type="guid">{a}</split:account></trn:split>'
        for i, (a, v, q) in enumerate(splits))
    return (f'<gnc:transaction version="2.0.0"><trn:id type="guid">{guid}</trn:id>'
            f'<trn:currency><cmdty:space>CURRENCY</cmdty:space><cmdty:id>INR</cmdty:id></trn:currency>'
            f'<trn:date-posted><ts:date>{d} 00:00:00 +0000</ts:date></trn:date-posted>'
            f'<trn:num>{num}</trn:num><trn:description>{desc}</trn:description>'
            f'<trn:splits>{sp}</trn:splits></gnc:transaction>')


def _book_xml(extra_txn: bool = False) -> str:
    body = "".join([
        _acc("Root Account", "r0", "ROOT"),
        _acc("Assets", "a1", "ASSET", "r0", [("placeholder", "true")]),
        _acc("Bank", "a2", "ASSET", "a1"),
        _acc("Demo Bank Savings", "a3", "BANK", "a2"),
        _acc("Old Demo Account", "a4", "BANK", "a2", [("hidden", "true")]),
        _acc("Stocks", "a5", "ASSET", "a1"),
        _acc("ACME LTD", "a6", "STOCK", "a5"),
        _acc("Expenses", "e1", "EXPENSE", "r0"),
        _acc("Food", "e2", "EXPENSE", "e1"),
        _acc("Income", "i1", "INCOME", "r0"),
        _acc("Salary", "i2", "INCOME", "i1"),
        _acc("Equity", "q1", "EQUITY", "r0"),
        _acc("Opening Balances", "q2", "EQUITY", "q1", [("equity-type", "opening-balance")]),
        _txn("t1", "2025-05-01", "100", "OPENING", [("a3", "100000/100", None), ("q2", "-100000/100", None)]),
        _txn("t2", "2025-06-10", "101", "SALARY JUNE", [("a3", "5000000/100", None), ("i2", "-5000000/100", None)]),
        _txn("t3", "2025-06-12", "102", "CAFE LUNCH", [("e2", "50000/100", None), ("a3", "-50000/100", None)]),
        _txn("t4", "2025-06-15", "103", "BUY ACME", [("a6", "100000/100", "10/1"), ("a3", "-100000/100", None)]),
    ] + ([_txn("t5", "2025-06-20", "104", "EXTRA", [("e2", "100/100", None), ("a3", "-100/100", None)])]
         if extra_txn else []))
    return (f'<?xml version="1.0" encoding="utf-8" ?>\n<gnc-v2 {_NS}>\n'
            f'<gnc:count-data cd:type="book">1</gnc:count-data>\n<gnc:book version="2.0.0">\n'
            f'<book:id type="guid">b0</book:id>\n{body}\n</gnc:book>\n</gnc-v2>\n')


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


@pytest.fixture
def books(tmp_path):
    xml = _book_xml()
    plain = tmp_path / "demo_plain.gnucash"
    gz = tmp_path / "demo_gz.gnucash"
    plain.write_text(xml, encoding="utf-8")
    gz.write_bytes(gzip.compress(xml.encode("utf-8")))
    return {"plain": plain, "gz": gz, "xml": xml}


def _canon(root: ET.Element) -> bytes:
    return ET.tostring(root)


# --------------------------------------------------------------------------
# Golden: same parse as each current reader's own open-and-parse step
# --------------------------------------------------------------------------

def _sniff_fromstring(p: Path) -> ET.Element:
    """gnucash_accounts._read_root / coverage._read_root / closing_check._read_book /
    26as tools._existing_account_paths: read_bytes + sniff + ET.fromstring."""
    raw = p.read_bytes()
    return ET.fromstring(gzip.decompress(raw) if raw[:2] == b"\x1f\x8b" else raw)


def _gzip_open_parse(p: Path) -> ET.Element:
    """Intercompany load_book, pipeline balance, reconciler, xml_extractor,
    build_tds_journals (gzip.open + ET.parse). Gzip books only."""
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return ET.parse(f).getroot()


def _text_fromstring(p: Path) -> ET.Element:
    """parse_gnucash.parse_book: text read (gzip or plain) + ET.fromstring."""
    opener = gzip.open if p.read_bytes()[:2] == b"\x1f\x8b" else open
    with opener(p, "rt", encoding="utf-8", errors="replace") as f:
        return ET.fromstring(f.read())


@pytest.mark.parametrize("kind", ["gz", "plain"])
def test_matches_sniff_readers(books, kind):
    assert _canon(read_xml_root(books[kind])) == _canon(_sniff_fromstring(books[kind]))


@pytest.mark.parametrize("kind", ["gz", "plain"])
def test_matches_parse_gnucash_text_reader(books, kind):
    assert _canon(read_xml_root(books[kind])) == _canon(_text_fromstring(books[kind]))


def test_matches_gzip_open_readers_on_gzip_book(books):
    assert _canon(read_xml_root(books["gz"])) == _canon(_gzip_open_parse(books["gz"]))


def test_gz_and_plain_give_the_same_tree(books):
    assert _canon(read_xml_root(books["gz"])) == _canon(read_xml_root(books["plain"]))


@pytest.mark.parametrize("kind", ["gz", "plain"])
def test_matches_real_importable_readers(books, kind):
    from agents import gnucash_accounts as ga
    from agents.skill_gnucash_coverage import agent as cov
    from agents.skill_year_end_close import closing_check as cc

    want = _canon(read_xml_root(books[kind]))
    assert _canon(ga._read_root(books[kind])) == want
    assert _canon(cov._read_root(books[kind])) == want
    assert _canon(cc._read_book(books[kind])) == want


@pytest.mark.parametrize("kind", ["gz", "plain"])
def test_parse_gnucash_book_from_shared_root(books, kind):
    """parse_book (its own reader) and parse_book_text over the shared root's
    serialisation give the same accounts and transactions."""
    from agents.skill_itr_workbook.scripts import parse_gnucash as pg

    own = pg.parse_book(books[kind])
    text = ET.tostring(read_xml_root(books[kind]), encoding="unicode")
    shared = pg.parse_book_text(text)
    assert own.accounts.keys() == shared.accounts.keys()
    assert [(a.name, a.type) for a in own.accounts.values()] == \
           [(a.name, a.type) for a in shared.accounts.values()]
    assert len(own.transactions) == len(shared.transactions) == 4


def test_account_and_transaction_content(books):
    root = read_xml_root(books["gz"])
    gnc = "{http://www.gnucash.org/XML/gnc}"
    assert len(list(root.iter(f"{gnc}account"))) == 13
    nums = sorted(n.text for n in root.iter("{http://www.gnucash.org/XML/trn}num"))
    assert nums == ["100", "101", "102", "103"]


# --------------------------------------------------------------------------
# Cache
# --------------------------------------------------------------------------

def test_book_is_parsed_once(books, monkeypatch):
    calls = []
    real = ET.fromstring
    monkeypatch.setattr(gnc_book.ET, "fromstring", lambda d: (calls.append(1), real(d))[1])
    a = read_xml_root(books["gz"])
    b = read_xml_root(books["gz"])
    assert a is b
    assert len(calls) == 1


def test_cache_not_reused_after_file_changes(tmp_path):
    p = tmp_path / "b.gnucash"
    p.write_bytes(gzip.compress(_book_xml().encode()))
    first = read_xml_root(p)
    assert len(first.findall(".//{http://www.gnucash.org/XML/gnc}transaction")) == 4
    p.write_bytes(gzip.compress(_book_xml(extra_txn=True).encode()))
    second = read_xml_root(p)
    assert second is not first
    assert len(second.findall(".//{http://www.gnucash.org/XML/gnc}transaction")) == 5


def test_cache_not_reused_when_only_mtime_changes(tmp_path):
    p = tmp_path / "b.gnucash"
    p.write_text(_book_xml(), encoding="utf-8")
    first = read_xml_root(p)
    st = p.stat()
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    assert read_xml_root(p) is not first


def test_clear_cache_forces_a_reparse(books):
    a = read_xml_root(books["plain"])
    clear_cache()
    assert read_xml_root(books["plain"]) is not a


def test_a_book_that_becomes_unreadable_is_not_served_from_cache(tmp_path):
    p = tmp_path / "b.gnucash"
    p.write_text(_book_xml(), encoding="utf-8")
    read_xml_root(p)
    p.write_bytes(b"")
    with pytest.raises(GncBookUnreadable):
        read_xml_root(p)


# --------------------------------------------------------------------------
# Negatives: one exception type, never a None / empty root
# --------------------------------------------------------------------------

def test_plain_book_is_not_rejected(books):
    root = read_xml_root(books["plain"])
    assert root is not None and len(root) > 0


def test_plain_xml_with_gz_extension_and_gzip_with_plain_extension(tmp_path):
    """The extension is never trusted; only the magic bytes are."""
    xml = _book_xml()
    a = tmp_path / "looks_plain.xml"
    a.write_bytes(gzip.compress(xml.encode()))
    b = tmp_path / "looks_gz.gz"
    b.write_text(xml, encoding="utf-8")
    assert _canon(read_xml_root(a)) == _canon(read_xml_root(b))


def _unreadable_cases(tmp_path):
    cases = {}
    cases["missing"] = tmp_path / "nope.gnucash"
    empty = tmp_path / "empty.gnucash"
    empty.write_bytes(b"")
    cases["empty"] = empty
    good = gzip.compress(_book_xml().encode())
    trunc = tmp_path / "trunc.gnucash"
    trunc.write_bytes(good[: len(good) // 2])
    cases["truncated_gzip"] = trunc
    junk = tmp_path / "junk.gnucash"
    junk.write_bytes(b"this is not xml at all")
    cases["not_xml"] = junk
    nobook = tmp_path / "nobook.gnucash"
    nobook.write_text('<?xml version="1.0"?><other><x/></other>', encoding="utf-8")
    cases["xml_without_book"] = nobook
    nobook_gz = tmp_path / "nobook_gz.gnucash"
    nobook_gz.write_bytes(gzip.compress(b"<root/>"))
    cases["gz_xml_without_book"] = nobook_gz
    sqlite = tmp_path / "sqlite.gnucash"
    sqlite.write_bytes(b"SQLite format 3\x00" + b"\x00" * 100)
    cases["sqlite"] = sqlite
    cases["directory"] = tmp_path
    return cases


@pytest.mark.parametrize("name", [
    "missing", "empty", "truncated_gzip", "not_xml", "xml_without_book",
    "gz_xml_without_book", "sqlite", "directory"])
def test_unreadable_raises_one_exception_type(tmp_path, name):
    p = _unreadable_cases(tmp_path)[name]
    with pytest.raises(GncBookUnreadable) as ei:
        read_xml_root(p)
    assert str(p) in str(ei.value)
    assert ei.value.reason


def test_unreadable_is_never_cached_as_a_result(tmp_path):
    p = tmp_path / "junk.gnucash"
    p.write_bytes(b"junk")
    for _ in range(2):
        with pytest.raises(GncBookUnreadable):
            read_xml_root(p)


# --------------------------------------------------------------------------
# No caller changed: the four path conventions stay as they were
# --------------------------------------------------------------------------

def test_path_conventions_unchanged(books):
    from agents import gnucash_accounts as ga
    from agents.skill_itr_workbook.scripts import parse_gnucash as pg

    accts = ga.load_accounts(books["gz"])
    paths = {a.path for a in accts}
    # "A:B" without the root account name
    assert "Assets:Bank:Demo Bank Savings" in paths
    assert not any(p.startswith("Root Account") for p in paths)
    # parse_gnucash keeps its own "A/B" form
    book = pg.parse_book(books["gz"])
    assert "Assets/Bank/Demo Bank Savings" in {a.path for a in book.accounts.values()}


def test_corrupt_gzip_body_always_raises_gnc_book_unreadable(tmp_path):
    """Flipped bytes inside the deflate body: never a zlib.error."""
    import zlib

    good = gzip.compress(_book_xml().encode())
    raised_zlib = 0
    for pos in range(12, len(good) - 8, 7):
        bad = bytearray(good)
        bad[pos] ^= 0xFF
        p = tmp_path / f"bad_{pos}.gnucash"
        p.write_bytes(bytes(bad))
        try:
            gzip.decompress(bytes(bad))
        except zlib.error:
            raised_zlib += 1
        except Exception:
            pass
        clear_cache()
        try:
            read_xml_root(p)
        except GncBookUnreadable:
            pass
        except zlib.error:
            pytest.fail(f"zlib.error escaped at byte {pos}")
    assert raised_zlib >= 1, "no corrupted position exercised zlib.error"
