"""tests/test_ye01_year_end_skill.py -- YE-01 PR 2: the Year-End Close skill.

Synthetic data only: temp folders, tiny synthetic GnuCash XML books, a stubbed shortcut maker.
Every behaviour has a negative test (nothing deleted/moved/overwritten, refusals create nothing)."""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
for _p in (str(ROOT / "src" / "agents" / "skill_itr_workbook" / "scripts"), str(ROOT / "src"), str(ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from agents.skill_year_end_close import agent as ye            # noqa: E402
from agents.skill_year_end_close import cleanup, closing_check  # noqa: E402
from agents.skill_year_end_close import shortcuts              # noqa: E402

FY = "2025-26"
BOOK = "Syn_2526.gnucash"
NEXT_BOOK = "Syn_2627.gnucash"

# ---------------------------------------------------------------- synthetic GnuCash XML

_HEAD = ('<?xml version="1.0" encoding="utf-8" ?>\n'
         '<gnc-v2 xmlns:gnc="http://www.gnucash.org/XML/gnc" xmlns:act="http://www.gnucash.org/XML/act" '
         'xmlns:trn="http://www.gnucash.org/XML/trn" xmlns:split="http://www.gnucash.org/XML/split" '
         'xmlns:ts="http://www.gnucash.org/XML/ts"><gnc:book version="2.0.0">\n')
_ACCTS = [("r", "Root Account", "ROOT", None), ("a", "Bank", "ASSET", "r"), ("i", "Salary", "INCOME", "r"),
          ("x", "Food", "EXPENSE", "r"), ("e", "Opening", "EQUITY", "r")]


def _acct(i, name, typ, parent):
    par = f"<act:parent type=\"guid\">{parent}</act:parent>" if parent else ""
    return (f'<gnc:account version="2.0.0"><act:name>{name}</act:name><act:id type="guid">{i}</act:id>'
            f'<act:type>{typ}</act:type>{par}</gnc:account>\n')


def _txn(day, splits):
    sp = "".join(f'<trn:split><split:value>{v}</split:value><split:account type="guid">{a}</split:account></trn:split>'
                 for a, v in splits)
    return (f'<gnc:transaction version="2.0.0"><trn:date-posted><ts:date>{day} 00:00:00 +0000</ts:date>'
            f'</trn:date-posted><trn:splits>{sp}</trn:splits></gnc:transaction>\n')


def make_book(income=True, expense=True, closing_income=True, closing_expense=True):
    """Income 100 and expense 40 posted in the year; closing entries (to Equity) dated 31 March are optional
    per side. All four True == a properly closed book."""
    t = [_txn("2025-06-01", [("a", "10000/100"), ("i", "-10000/100")]) if income else "",
         _txn("2025-07-01", [("x", "4000/100"), ("a", "-4000/100")]) if expense else ""]
    if closing_income:
        t.append(_txn("2026-03-31", [("i", "10000/100"), ("e", "-10000/100")]))
    if closing_expense:
        t.append(_txn("2026-03-31", [("e", "4000/100"), ("x", "-4000/100")]))
    body = "".join(_acct(*a) for a in _ACCTS) + "".join(t)
    return (_HEAD + body + "</gnc:book></gnc-v2>").encode("utf-8")


OPEN_BOOK = make_book(closing_income=False, closing_expense=False)


# ---------------------------------------------------------------- environment

class Env:
    pass


def _entity(name, folder, books=None):
    d = {"name": name, "pan": "AAAAA0000A", "status": "Individual"}
    if folder:
        d["folder_name"] = folder
    if books:
        d["books"] = books
    return d


def _tree(path: Path) -> dict:
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(path.rglob("*")) if p.is_file()}


def _dirs(path: Path) -> set:
    return {str(p) for p in path.rglob("*")}


@pytest.fixture
def env(tmp_path, monkeypatch):
    e = Env()
    e.tmp = tmp_path
    e.drive = tmp_path / "Drive"
    e.gnc = tmp_path / "Profiles"
    e.drive.mkdir()
    e.gnc.mkdir()
    e.data = tmp_path / "Data"
    (e.data / "itr").mkdir(parents=True)
    (e.data / "settings").mkdir()
    e.ent = e.data / "itr" / "entities.yaml"
    e.settings = e.data / "settings" / "config.yaml"
    e.state = e.data / "year_end"
    e.out = tmp_path / "out" / "report.md"
    e.gdir = e.gnc / "20252026" / "Syn A"
    e.gdir.mkdir(parents=True)
    e.ddir = e.drive / FY / "Syn A"
    e.ddir.mkdir(parents=True)
    e.book = e.gdir / BOOK
    e.book.write_bytes(OPEN_BOOK)
    # entity B: its own folders and book, which A's run must never touch
    e.bdir = e.gnc / "20252026" / "Syn B"
    e.bdir.mkdir(parents=True)
    (e.bdir / "Other_2526.gnucash").write_bytes(OPEN_BOOK)
    e.write_entities({"SYN-A": _entity("Entity A", "Syn A", {FY: str(e.book)}),
                      "SYN-B": _entity("Entity B", "Syn B", {FY: str(e.bdir / "Other_2526.gnucash")})})
    e.write_settings(drive_accounts_root=str(e.drive), gnucash_profile_root=str(e.gnc))
    # the shortcut maker is stubbed: a dict stands in for the .lnk targets
    e.lnks = {}

    def fake_create(lnk, target):
        Path(lnk).write_bytes(b"LNK")
        e.lnks[str(lnk)] = str(target)

    monkeypatch.setattr(shortcuts, "create_shortcut", fake_create)
    monkeypatch.setattr(shortcuts, "read_shortcut_target", lambda lnk: e.lnks.get(str(lnk), ""))
    return e


def _write_entities(self, d):
    self.ent.write_text(yaml.safe_dump(d, sort_keys=True), encoding="utf-8")


def _write_settings(self, **kw):
    self.settings.write_text(yaml.safe_dump(kw), encoding="utf-8")


Env.write_entities = _write_entities
Env.write_settings = _write_settings


def go(e, step, entity="SYN-A", fy=FY):
    return ye.run(entity_key=entity, closing_fy=fy, step=str(step), output_path=str(e.out),
                  entities_path=str(e.ent), settings_path=str(e.settings), state_dir=str(e.state))


def books_of(e, key):
    return yaml.safe_load(e.ent.read_text(encoding="utf-8"))[key].get("books", {})


def snapshot(e):
    return {k: v for k, v in _tree(e.tmp).items() if k != str(e.out)}


def nothing_removed(before, after):
    """No pre-existing file is gone or changed, except the registry file and the run-state files."""
    skip = ("entities.yaml", "year_end")
    return all(k in after and after[k] == v for k, v in before.items() if not any(x in k for x in skip))


# ---------------------------------------------------------------- step 1

def test_step1_copies_the_book_to_the_drive_folder_under_the_same_name(env):
    before = snapshot(env)
    r = go(env, 1)
    assert r.startswith("Step 1 done"), r
    assert (env.ddir / BOOK).read_bytes() == OPEN_BOOK
    assert nothing_removed(before, snapshot(env))                    # NEGATIVE: nothing deleted/moved/changed
    assert env.book.read_bytes() == OPEN_BOOK                        # old book bytes unchanged


def test_step1_is_done_when_an_identical_copy_is_already_there(env):
    (env.ddir / BOOK).write_bytes(OPEN_BOOK)
    assert go(env, 1).startswith("Step 1 done")


def test_step1_stops_when_a_different_file_of_that_name_is_there(env):    # NEGATIVE: never overwrite
    (env.ddir / BOOK).write_bytes(b"something else")
    before = snapshot(env)
    r = go(env, 1)
    assert r.startswith("Stopped") and "NOT identical" in r
    assert snapshot(env) == before
    assert not ye.load_state(env.state, "SYN-A", FY)["steps"]


def test_step1_refused_while_a_lock_file_exists_in_the_source_folder(env):   # NEGATIVE
    (env.gdir / (BOOK + ".LCK")).write_bytes(b"")
    r = go(env, 1)
    assert r.startswith("Stopped") and "lock" in r.lower()
    assert not (env.ddir / BOOK).exists()


def test_step1_stops_when_the_drive_year_folder_is_missing_and_creates_nothing(env):    # NEGATIVE
    (env.ddir).rmdir()
    (env.ddir.parent).rmdir()
    before = _dirs(env.tmp)
    assert go(env, 1).startswith("Stopped")
    assert _dirs(env.tmp) - before == {str(env.out), str(env.out.parent)} or _dirs(env.tmp) >= before
    assert not (env.drive / FY).exists()


# ---------------------------------------------------------------- step 2

def test_step2_carries_the_book_into_the_next_year(env):
    go(env, 1)
    before = snapshot(env)
    r = go(env, 2)
    assert r.startswith("Step 2 done"), r
    new = env.gnc / "20262027" / "Syn A" / NEXT_BOOK
    assert new.read_bytes() == OPEN_BOOK
    assert books_of(env, "SYN-A")["2026-27"] == str(new)
    lnk = env.drive / "2026-27" / "Syn A" / (NEXT_BOOK + ".lnk")
    assert env.lnks[str(lnk)] == str(new)
    assert nothing_removed(before, snapshot(env))                    # NEGATIVE: nothing deleted/moved/changed
    assert books_of(env, "SYN-A")[FY] == str(env.book)               # the old registration is kept


def test_step2_needs_step1_first(env):                               # NEGATIVE
    before = _dirs(env.tmp)
    assert go(env, 2).startswith("Stopped")
    assert not (env.gnc / "20262027").exists() and _dirs(env.tmp) - before <= {str(env.out), str(env.out.parent)}


def test_step2_stops_on_a_different_file_at_the_new_book_path_and_changes_nothing(env):   # NEGATIVE
    go(env, 1)
    target = env.gnc / "20262027" / "Syn A"
    target.mkdir(parents=True)
    (target / NEXT_BOOK).write_bytes(b"different")
    before = snapshot(env)
    reg = books_of(env, "SYN-A")
    r = go(env, 2)
    assert r.startswith("Stopped") and "differs" in r
    assert snapshot(env) == before and books_of(env, "SYN-A") == reg
    assert not env.lnks


def test_step2_stops_on_a_shortcut_that_points_elsewhere_and_leaves_it_untouched(env):    # NEGATIVE
    go(env, 1)
    nd = env.drive / "2026-27" / "Syn A"
    nd.mkdir(parents=True)
    lnk = nd / (NEXT_BOOK + ".lnk")
    lnk.write_bytes(b"OLD")
    env.lnks[str(lnk)] = str(env.tmp / "elsewhere.gnucash")
    r = go(env, 2)
    assert r.startswith("Stopped") and "points somewhere else" in r
    assert lnk.read_bytes() == b"OLD" and env.lnks[str(lnk)].endswith("elsewhere.gnucash")
    assert not (env.gnc / "20262027").exists()                       # nothing was created before the stop
    assert "2026-27" not in books_of(env, "SYN-A")


def test_step2_a_correct_shortcut_already_there_is_accepted(env):
    go(env, 1)
    nd = env.drive / "2026-27" / "Syn A"
    nd.mkdir(parents=True)
    lnk = nd / (NEXT_BOOK + ".lnk")
    lnk.write_bytes(b"OK")
    env.lnks[str(lnk)] = str(env.gnc / "20262027" / "Syn A" / NEXT_BOOK)
    assert go(env, 2).startswith("Step 2 done")
    assert lnk.read_bytes() == b"OK"


@pytest.mark.parametrize("name", ["Syn_book.gnucash", "Syn_2526_2425.gnucash", "Syn_2526_2627.gnucash"])
def test_step2_a_book_name_without_exactly_one_year_pair_stops(env, name):         # NEGATIVE
    renamed = env.gdir / name
    env.book.rename(renamed)
    env.write_entities({"SYN-A": _entity("Entity A", "Syn A", {FY: str(renamed)}), "SYN-B": _entity("Entity B", "Syn B")})
    go(env, 1)
    r = go(env, 2)
    assert r.startswith("Stopped") and "year pair" in r
    assert not (env.gnc / "20262027").exists() and "2026-27" not in books_of(env, "SYN-A")


def test_step2_refused_while_a_lock_exists_in_the_target_folder(env):              # NEGATIVE
    go(env, 1)
    t = env.gnc / "20262027" / "Syn A"
    t.mkdir(parents=True)
    (t / "x.gnucash.LCK").write_bytes(b"")
    r = go(env, 2)
    assert r.startswith("Stopped") and "lock" in r.lower()
    assert not (t / NEXT_BOOK).exists() and "2026-27" not in books_of(env, "SYN-A")


def test_step2_stops_when_next_year_is_registered_to_another_book(env):            # NEGATIVE
    go(env, 1)
    env.write_entities({"SYN-A": _entity("Entity A", "Syn A", {FY: str(env.book), "2026-27": str(env.tmp / "z.gnucash")}),
                        "SYN-B": _entity("Entity B", "Syn B")})
    assert go(env, 2).startswith("Stopped")
    assert not (env.gnc / "20262027").exists()


def test_a_run_for_entity_a_never_touches_entity_b(env):                           # NEGATIVE
    b_before = _tree(env.bdir)
    b_reg = books_of(env, "SYN-B")
    for step in (1, 2, 3):
        go(env, step)
    assert _tree(env.bdir) == b_before
    assert books_of(env, "SYN-B") == b_reg
    assert not (env.gnc / "20262027" / "Syn B").exists() and not (env.drive / "2026-27" / "Syn B").exists()
    assert not (env.drive / FY / "Syn B").exists()


# ---------------------------------------------------------------- refusals create nothing

def test_an_entity_with_no_book_is_refused_and_nothing_is_created(env):            # NEGATIVE
    env.write_entities({"SYN-A": _entity("Entity A", "Syn A"), "SYN-B": _entity("Entity B", "Syn B")})
    before = _dirs(env.tmp)
    r = go(env, 1)
    assert "no books - not part of year-end" in r.lower()
    assert _dirs(env.tmp) - before <= {str(env.out), str(env.out.parent)}
    assert not env.state.exists()


def test_an_entity_with_a_book_only_for_another_year_is_refused(env):              # NEGATIVE
    env.write_entities({"SYN-A": _entity("Entity A", "Syn A", {"2024-25": str(env.book)}), "SYN-B": _entity("Entity B", "Syn B")})
    assert "no books - not part of year-end" in go(env, 1).lower()


@pytest.mark.parametrize("what", ["folder_name", "drive_accounts_root", "gnucash_profile_root"])
def test_an_unset_folder_name_or_root_is_refused_naming_it_and_nothing_is_created(env, what):   # NEGATIVE
    if what == "folder_name":
        env.write_entities({"SYN-A": _entity("Entity A", "", {FY: str(env.book)}), "SYN-B": _entity("Entity B", "Syn B")})
        word = "folder name"
    else:
        kw = {"drive_accounts_root": str(env.drive), "gnucash_profile_root": str(env.gnc)}
        kw[what] = ""
        env.write_settings(**kw)
        word = "Drive accounts root" if what.startswith("drive") else "GnuCash profile root"
    before = snapshot(env)
    dirs = _dirs(env.tmp)
    r = go(env, 1)
    assert r.startswith("Stopped") and word in r
    assert _dirs(env.tmp) - dirs <= {str(env.out), str(env.out.parent)}
    assert {k: v for k, v in snapshot(env).items() if k != str(env.out)} == before


def test_a_registered_book_outside_the_expected_gnucash_folder_stops(env):         # NEGATIVE
    stray = env.tmp / "stray" / BOOK
    stray.parent.mkdir()
    stray.write_bytes(OPEN_BOOK)
    env.write_entities({"SYN-A": _entity("Entity A", "Syn A", {FY: str(stray)}), "SYN-B": _entity("Entity B", "Syn B")})
    r = go(env, 1)
    assert r.startswith("Stopped") and "not inside the expected GnuCash folder" in r
    assert not (env.ddir / BOOK).exists()


def test_a_bad_closing_year_is_refused(env):                                       # NEGATIVE
    assert go(env, 1, fy="2025-27").startswith("Stopped")
    assert go(env, 1, fy="FY25").startswith("Stopped")


# ---------------------------------------------------------------- step 3 and step 4

def _prepare_to_step4(env, new_content):
    go(env, 1)
    go(env, 2)
    new = env.gnc / "20262027" / "Syn A" / NEXT_BOOK
    new.write_bytes(new_content)          # stands in for the user's GnuCash close
    return new


def test_step3_only_tells_the_user_and_changes_no_book(env):
    go(env, 1)
    go(env, 2)
    before = snapshot(env)
    r = go(env, 3)
    assert "Step 3 is yours" in r
    assert {k: v for k, v in snapshot(env).items() if "year_end" not in k} == {k: v for k, v in before.items() if "year_end" not in k}


def test_step4_passes_on_a_closed_book_and_marks_the_year_done(env):
    _prepare_to_step4(env, make_book())
    assert go(env, 4).startswith("Step 4 passed")
    assert ye.load_state(env.state, "SYN-A", FY)["done"] is True


def test_step4_fails_when_income_is_zero_but_expense_is_not(env):                  # NEGATIVE
    _prepare_to_step4(env, make_book(closing_income=True, closing_expense=False))
    r = go(env, 4)
    assert r.startswith("FAIL") and "Expense accounts are not zero" in r and "Income accounts are not zero" not in r
    assert "Food" in r and "Salary" not in r
    assert ye.load_state(env.state, "SYN-A", FY)["done"] is False


def test_step4_fails_when_expense_is_zero_but_income_is_not(env):                  # NEGATIVE
    _prepare_to_step4(env, make_book(closing_income=False, closing_expense=True))
    r = go(env, 4)
    assert r.startswith("FAIL") and "Income accounts are not zero" in r and "Expense accounts are not zero" not in r
    assert "Salary" in r and ye.load_state(env.state, "SYN-A", FY)["done"] is False


def test_step4_fails_when_balances_are_zero_but_there_are_no_closing_entries(env):    # NEGATIVE
    _prepare_to_step4(env, make_book(income=False, expense=False, closing_income=False, closing_expense=False))
    r = go(env, 4)
    assert r.startswith("FAIL") and "no closing entries" in r
    assert ye.load_state(env.state, "SYN-A", FY)["done"] is False


def test_step4_does_not_count_a_balancing_entry_dated_another_day_as_closing(env):  # NEGATIVE
    book = make_book(closing_income=False, closing_expense=False).decode()
    book = book.replace("</gnc:book>", ye_txn("2026-03-30", [("i", "10000/100"), ("e", "-10000/100")])
                        + ye_txn("2026-03-30", [("e", "4000/100"), ("x", "-4000/100")]) + "</gnc:book>")
    new = _prepare_to_step4(env, book.encode())
    res = closing_check.check_closed(new, 2026)
    assert not res.ok and res.closing_entries == 0 and not res.income_nonzero and not res.expense_nonzero


def ye_txn(day, splits):
    return _txn(day, splits)


def test_step4_failure_message_has_account_paths_but_no_amounts(env):              # NEGATIVE
    _prepare_to_step4(env, make_book(closing_expense=False))
    r = go(env, 4)
    assert "100" not in r and "40" not in r.replace("2026", "")


def test_step4_never_writes_the_book(env):                                         # NEGATIVE
    new = _prepare_to_step4(env, make_book())
    before = new.read_bytes()
    mtime = new.stat().st_mtime_ns
    go(env, 4)
    assert new.read_bytes() == before and new.stat().st_mtime_ns == mtime


def test_step4_refused_while_a_lock_exists(env):                                   # NEGATIVE
    new = _prepare_to_step4(env, make_book())
    (new.parent / (NEXT_BOOK + ".LCK")).write_bytes(b"")
    assert go(env, 4).startswith("Stopped")
    assert ye.load_state(env.state, "SYN-A", FY)["done"] is False


def test_a_year_marked_done_is_not_done_again_after_a_later_failing_check(env):    # NEGATIVE
    new = _prepare_to_step4(env, make_book())
    go(env, 4)
    new.write_bytes(make_book(closing_expense=False))
    assert go(env, 4).startswith("FAIL")
    assert ye.load_state(env.state, "SYN-A", FY)["done"] is False


def test_step4_an_unreadable_book_fails(env):                                      # NEGATIVE
    _prepare_to_step4(env, b"not a book")
    r = go(env, 4)
    assert r.startswith("FAIL") and ye.load_state(env.state, "SYN-A", FY)["done"] is False


# ---------------------------------------------------------------- step 5

def _done_year(env):
    _prepare_to_step4(env, make_book())
    assert go(env, 4).startswith("Step 4 passed")


def _litter(env):
    (env.gdir / (BOOK + ".20260401120000.gnucash")).write_bytes(b"b1")
    (env.gdir / (BOOK + ".20260401120000.log")).write_bytes(b"l1")
    (env.gdir / "notes.txt").write_bytes(b"keep")
    (env.gdir / "Other_2526.gnucash").write_bytes(b"another book")
    (env.ddir / (BOOK + ".lnk")).write_bytes(b"lnk")
    (env.ddir / "random.lnk").write_bytes(b"lnk2")


def test_step5_lists_only_backups_logs_and_the_old_shortcut_and_never_the_final_book(env):    # NEGATIVE
    _done_year(env)
    _litter(env)
    before = snapshot(env)
    r = go(env, 5)
    items = {Path(i.path).name for i in cleanup.list_cleanup(env.gdir, env.book, env.ddir)}
    assert items == {BOOK + ".20260401120000.gnucash", BOOK + ".20260401120000.log", BOOK + ".lnk"}
    script = Path(str(env.out)).with_name("report-cleanup.ps1")
    text = script.read_text(encoding="ascii")
    assert f"'{env.book}'" not in text
    for kept in ("notes.txt", "Other_2526.gnucash", "random.lnk", NEXT_BOOK):
        assert kept not in text
    assert nothing_removed(before, {k: v for k, v in snapshot(env).items()})   # step 5 deletes nothing
    assert env.book.exists() and (env.gdir / "notes.txt").exists()


def test_step5_script_is_pure_ascii_with_no_wildcard_and_recycles_each_item_once(env):
    _done_year(env)
    _litter(env)
    go(env, 5)
    raw = Path(str(env.out)).with_name("report-cleanup.ps1").read_bytes()
    raw.decode("ascii")
    text = raw.decode("ascii")
    assert "*" not in text and "?" not in text and "Remove-Item" not in text
    assert "Add-Type -AssemblyName Microsoft.VisualBasic" in text
    assert text.count("SendToRecycleBin") == 3
    assert text.count("Skipped (not there)") == 3 and text.count("Recycled:") == 3
    assert [ln for ln in text.splitlines() if ln.startswith("$p = ")].__len__() == 3


def test_step5_script_quotes_a_single_quote_in_a_path_and_skips_non_ascii_paths():
    a = cleanup.CleanupItem("C:\\x\\it's.log", 1, "d", "log")
    b = cleanup.CleanupItem("C:\\x\\caf\u00e9.log", 1, "d", "log")
    text = cleanup.build_script([a, b])
    text.encode("ascii")
    assert "it''s.log" in text and "caf" not in text                              # NEGATIVE: non-ASCII left out


def test_step5_refused_until_step4_has_passed_and_writes_no_script(env):           # NEGATIVE
    _prepare_to_step4(env, make_book(closing_expense=False))
    _litter(env)
    go(env, 4)
    assert go(env, 5).startswith("Stopped")
    assert not Path(str(env.out)).with_name("report-cleanup.ps1").exists()


def test_step5_refused_while_a_lock_exists(env):                                   # NEGATIVE
    _done_year(env)
    (env.gdir / (BOOK + ".LCK")).write_bytes(b"")
    assert go(env, 5).startswith("Stopped")


def test_step5_reports_the_exact_list_with_size_and_date(env):
    _done_year(env)
    _litter(env)
    r = go(env, 5)
    assert str(env.gdir / (BOOK + ".20260401120000.gnucash")) in r and "| 2 |" in r


def test_the_final_book_is_never_listed_even_if_it_looks_like_a_backup(tmp_path):  # NEGATIVE
    g = tmp_path / "g"
    g.mkdir()
    final = g / "x.gnucash.20260401120000.gnucash"
    final.write_bytes(b"f")
    assert cleanup.list_cleanup(g, final, tmp_path / "d") == []


# ---------------------------------------------------------------- state, status, helpers

def test_step0_reports_where_the_run_stands(env):
    go(env, 1)
    r = go(env, 0)
    assert "not done yet" in r and "1. Copy the final book to the Drive year folder: passed" in r


def test_a_run_can_resume_after_a_stop(env):
    (env.ddir / BOOK).write_bytes(b"x")
    assert go(env, 1).startswith("Stopped")
    (env.ddir / BOOK).write_bytes(OPEN_BOOK)
    assert go(env, 1).startswith("Step 1 done")
    assert go(env, 2).startswith("Step 2 done")


def test_the_report_is_written_to_the_output_path(env):
    r = go(env, 1)
    assert env.out.read_text(encoding="utf-8") == r


def test_nothing_in_the_skill_deletes_anything():                                  # NEGATIVE: source guard
    src = "".join(p.read_text(encoding="utf-8") for p in (ROOT / "src" / "agents" / "skill_year_end_close").glob("*.py"))
    for banned in ("os.remove", "os.unlink", "shutil.rmtree", ".unlink(", "os.rename", ".rename(", ".replace(Path",
                   "shutil.move", "os.replace", "rmdir"):
        assert banned not in src, banned


def test_derive_next_name():
    assert ye.derive_next_name("A_2526.gnucash", 2025) == "A_2627.gnucash"
    assert ye.derive_next_name("A 2526 final.gnucash", 2025) == "A 2627 final.gnucash"
    assert ye.derive_next_name("A_9900.gnucash", 2099) == "A_0001.gnucash"
    with pytest.raises(ye.Stop):
        ye.derive_next_name("A_2627.gnucash", 2025)      # not the closing year's pair


@pytest.mark.skipif(sys.platform != "win32", reason="Windows shortcuts")
def test_the_real_shortcut_functions_round_trip(tmp_path, monkeypatch):
    monkeypatch.undo()
    target = tmp_path / "t.gnucash"
    target.write_bytes(b"x")
    lnk = tmp_path / "t.gnucash.lnk"
    shortcuts.create_shortcut(lnk, target)
    assert Path(shortcuts.read_shortcut_target(lnk)).samefile(target)


def test_a_differing_file_stop_names_the_file_and_says_to_recycle_and_rerun(env):
    (env.ddir / BOOK).write_bytes(b"half")
    r1 = go(env, 1)
    assert str(env.ddir / BOOK) in r1 and "recycle that file and re-run" in r1
    (env.ddir / BOOK).write_bytes(OPEN_BOOK)
    go(env, 1)
    t = env.gnc / "20262027" / "Syn A"
    t.mkdir(parents=True)
    (t / NEXT_BOOK).write_bytes(b"half")
    r2 = go(env, 2)
    assert str(t / NEXT_BOOK) in r2 and "recycle that file and re-run" in r2


def test_step5_lists_a_non_ascii_path_under_not_in_the_script_with_size_and_date(env):
    _done_year(env)
    odd = env.gdir / ("café_" + BOOK + ".log")
    odd.write_bytes(b"123")
    r = go(env, 5)
    assert "Not in the script - recycle by hand" in r
    assert str(odd) in r and "| 3 |" in r
    text = Path(str(env.out)).with_name("report-cleanup.ps1").read_text(encoding="ascii")
    assert "caf" not in text
