"""
agent.py -- Year-End Close. DIRECT mode, no LLM, no network.

One entity and one closing FY per run; one step per run. Steps:
  1 copy the final unclosed book from the GnuCash year folder to the Drive
    year folder (same filename);
  2 carry it into the next year: GnuCash folder, derived filename, registry
    entry, Drive folder and a shortcut to the new book;
  3 the user closes the books in GnuCash (this skill touches no book);
  4 check, read-only, that the NEW book is closed for the closing FY;
  5 list what can be cleaned up and write a Recycle Bin script for the user.

Hard rules (each has a negative test):
  * Nothing is ever deleted, moved, overwritten or renamed. A .gnucash file
    is only copied or read, never written.
  * Paths come only from configuration (the entity's folder name and the two
    year-end roots) and the book registry. Nothing is inferred from names and
    no shortcut is followed to find a book.
  * Any .LCK file in the source or target GnuCash folder stops the run.
  * One entity's run never reads or touches another entity's folders or
    registry entry.
  * The year is not marked done until step 4 passes.
"""
from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

from agents.outputs import ReplyWithOutputs, extra_output
from agents.skill_year_end_close import cleanup, closing_check, shortcuts

STEP_NAMES = {
    0: "Show where this year-end run stands",
    1: "Copy the final book to the Drive year folder",
    2: "Carry the book into the next year",
    3: "Close the books in GnuCash (you do this)",
    4: "Check the new book is closed",
    5: "List what can be cleaned up",
}

_FY_RE = re.compile(r"^(\d{4})-(\d{2})$")
_PAIR_RE = re.compile(r"(?<!\d)(\d{2})(\d{2})(?!\d)")


class Stop(Exception):
    """A step cannot go on. The message is shown to the user as is."""


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------

def _norm(p) -> str:
    return os.path.normcase(os.path.normpath(str(p)))


def parse_fy(fy: str) -> tuple[int, int]:
    """'2025-26' -> (2025, 2026). Raises Stop on anything else."""
    m = _FY_RE.match((fy or "").strip())
    if not m or int(m.group(2)) != (int(m.group(1)) + 1) % 100:
        raise Stop(f"The closing year '{fy}' is not a financial year like 2025-26.")
    start = int(m.group(1))
    return start, start + 1


def fy_label(start: int) -> str:
    return f"{start}-{str(start + 1)[-2:]}"


def derive_next_name(filename: str, start: int) -> str:
    """Move the single year pair in `filename` on by one year (2526 -> 2627).
    Zero or several pairs, or a pair that is not the closing year's, stop."""
    pairs = [m for m in _PAIR_RE.finditer(filename)
             if int(m.group(2)) == (int(m.group(1)) + 1) % 100]
    if len(pairs) != 1:
        raise Stop(
            f"The book name '{filename}' has {len(pairs)} year pairs (like 2526); "
            "exactly one is needed to work out the next year's name. "
            "Nothing was copied. Tell me the name to use.")
    m = pairs[0]
    expect = f"{start % 100:02d}{(start + 1) % 100:02d}"
    if m.group(0) != expect:
        raise Stop(
            f"The year pair in '{filename}' ({m.group(0)}) is not the closing year's "
            f"({expect}). Nothing was copied. Tell me the name to use.")
    nxt = f"{(start + 1) % 100:02d}{(start + 2) % 100:02d}"
    return filename[:m.start()] + nxt + filename[m.end():]


def _lock_files(folder: Path) -> list[Path]:
    try:
        if folder.is_dir():
            return [p for p in folder.iterdir() if p.is_file() and p.name.lower().endswith(".lck")]
    except OSError:
        pass
    return []


def _same_bytes(a: Path, b: Path) -> bool:
    return a.is_file() and b.is_file() and filecmp.cmp(str(a), str(b), shallow=False)


def _safe_copy(src: Path, dst: Path) -> None:
    """Copy src to a NEW file dst. Exclusive create: an existing dst is never
    written to. The source is opened read-only."""
    with open(src, "rb") as fin, open(dst, "xb") as fout:
        shutil.copyfileobj(fin, fout)
    shutil.copystat(src, dst)
    if not _same_bytes(src, dst):
        raise Stop(f"The copy at {dst} does not match the original. "
                   "Nothing was removed; check that file before going on.")


# ---------------------------------------------------------------------------
# run state
# ---------------------------------------------------------------------------

def _state_file(state_dir: Path, entity_key: str, fy: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", entity_key)
    return Path(state_dir) / f"{safe}__{fy}.json"


def load_state(state_dir, entity_key: str, fy: str) -> dict:
    f = _state_file(Path(state_dir), entity_key, fy)
    try:
        data = json.loads(f.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("steps"), dict):
            data.setdefault("done", False)
            return data
    except (OSError, ValueError):
        pass
    return {"entity": entity_key, "fy": fy, "steps": {}, "done": False}


def save_state(state_dir, entity_key: str, fy: str, state: dict) -> None:
    f = _state_file(Path(state_dir), entity_key, fy)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def _mark(state: dict, step: int, passed: bool) -> None:
    state["steps"][str(step)] = {"passed": passed, "at": datetime.now().isoformat(timespec="seconds")}
    if step == 4:
        state["done"] = bool(passed)


def _passed(state: dict, step: int) -> bool:
    return bool(state["steps"].get(str(step), {}).get("passed"))


# ---------------------------------------------------------------------------
# the context: everything comes from config and the registry
# ---------------------------------------------------------------------------

class Ctx:
    pass


def _registry():
    import agents.skill_itr_workbook as pkg  # noqa: PLC0415
    scripts = Path(pkg.__file__).resolve().parent / "scripts"
    if str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    import configs  # noqa: PLC0415
    from ui import _book_registry as reg  # noqa: PLC0415
    return configs, reg


def build_ctx(entity_key: str, closing_fy: str, entities_path, settings_path) -> Ctx:
    configs, reg = _registry()
    start, end = parse_fy(closing_fy)
    c = Ctx()
    c.entity, c.fy, c.start, c.end = entity_key, fy_label(start), start, end
    c.next_fy = fy_label(start + 1)
    c.reg, c.entities_path = reg, Path(entities_path)

    try:
        entities = configs.load_entities(c.entities_path)
    except Exception:
        raise Stop("The entities file could not be read. Nothing was created.")
    profile = entities.get(entity_key)
    if profile is None:
        raise Stop("That entity is not in the entities file. Nothing was created.")

    # No registered book for the closing year: not part of year-end.
    book_raw = reg.list_books(entity_key, entities_path=c.entities_path).get(c.fy)
    if not book_raw:
        raise Stop(f"No books - not part of year-end. This entity has no registered book for "
                   f"{c.fy}. Nothing was created.")

    roots = configs.read_year_end_roots(settings_path)
    missing = []
    if not (profile.folder_name or "").strip() or configs.check_folder_name(profile.folder_name):
        missing.append("the entity's folder name (Entities screen)")
    for key, label in (("drive_accounts_root", "the Drive accounts root"),
                       ("gnucash_profile_root", "the GnuCash profile root")):
        if not roots.get(key) or configs.check_root_folder(roots[key]):
            missing.append(f"{label} (Entities screen, year-end folders)")
    if missing:
        raise Stop("Year-end cannot start: " + "; ".join(missing)
                   + " is not set or not valid. Nothing was created.")

    c.folder = profile.folder_name.strip()
    c.drive_root, c.gnc_root = Path(roots["drive_accounts_root"]), Path(roots["gnucash_profile_root"])
    c.gnc_dir = c.gnc_root / f"{start}{end}" / c.folder
    c.next_gnc_dir = c.gnc_root / f"{start + 1}{end + 1}" / c.folder
    c.drive_dir = c.drive_root / c.fy / c.folder
    c.next_drive_dir = c.drive_root / c.next_fy / c.folder

    c.book = Path(book_raw)
    if _norm(c.book.parent) != _norm(c.gnc_dir):
        raise Stop(f"The registered {c.fy} book is not inside the expected GnuCash folder "
                   f"({c.gnc_dir}). Fix the registry or the folder first. Nothing was changed.")
    if not c.book.is_file():
        raise Stop(f"The registered {c.fy} book file is not there ({c.book}). Nothing was changed.")
    return c


def _guard_locks(*folders: Path) -> None:
    for f in folders:
        locks = _lock_files(f)
        if locks:
            names = ", ".join(p.name for p in locks)
            raise Stop(f"A lock file ({names}) is in {f}: a book there is open in GnuCash. "
                       "Close GnuCash, then run this step again. Nothing was changed.")


# ---------------------------------------------------------------------------
# the steps
# ---------------------------------------------------------------------------

def step1(c: Ctx, state: dict) -> str:
    _guard_locks(c.gnc_dir, c.next_gnc_dir)
    if not c.drive_dir.is_dir():
        raise Stop(f"The Drive year folder is not there ({c.drive_dir}). Create it, or check the "
                   "Drive accounts root. Nothing was copied.")
    dest = c.drive_dir / c.book.name
    if dest.exists():
        if _same_bytes(c.book, dest):
            _mark(state, 1, True)
            return f"Step 1 done: the Drive folder already holds an identical copy.\n\n- {dest}"
        raise Stop(f"{dest} is already there and is NOT identical to the book. "
                   "Nothing was overwritten. Look at both files and decide. "
                   "If an earlier copy was interrupted, recycle that file and re-run.")
    _safe_copy(c.book, dest)
    _mark(state, 1, True)
    return (f"Step 1 done: the final book is copied to the Drive year folder.\n\n"
            f"- From: {c.book}\n- To: {dest}\n\n"
            "The old Drive shortcut is left as it is.")


def step2(c: Ctx, state: dict) -> str:
    if not _passed(state, 1):
        raise Stop("Run step 1 first.")
    _guard_locks(c.gnc_dir, c.next_gnc_dir)
    new_name = derive_next_name(c.book.name, c.start)
    new_book = c.next_gnc_dir / new_name
    new_lnk = c.next_drive_dir / (new_name + ".lnk")
    for root, label in ((c.gnc_root, "GnuCash profile root"), (c.drive_root, "Drive accounts root")):
        if not root.is_dir():
            raise Stop(f"The {label} folder is not there ({root}). Nothing was created.")

    # Check every stop condition before anything is created.
    if new_book.exists() and not _same_bytes(c.book, new_book):
        raise Stop(f"{new_book} is already there and differs from the closing book. "
                   "Nothing was overwritten. Look at both files and decide. "
                   "If an earlier copy was interrupted, recycle that file and re-run.")
    registered = c.reg.list_books(c.entity, entities_path=c.entities_path).get(c.next_fy)
    if registered and _norm(registered) != _norm(new_book):
        raise Stop(f"{c.next_fy} is already registered to a different book ({registered}). "
                   "Nothing was changed.")
    lnk_exists = new_lnk.exists()
    if lnk_exists:
        target = shortcuts.read_shortcut_target(new_lnk)
        if _norm(target) != _norm(new_book):
            raise Stop(f"{new_lnk} is already there and points somewhere else. "
                       "It was left untouched. Look at it and decide.")

    done = []
    c.next_gnc_dir.mkdir(parents=True, exist_ok=True)
    if new_book.exists():
        done.append(f"- GnuCash: the new book was already there and is identical: {new_book}")
    else:
        _safe_copy(c.book, new_book)
        done.append(f"- GnuCash: copied the book to {new_book}")
    if registered:
        done.append(f"- Registry: {c.next_fy} was already registered to it")
    else:
        c.reg.set_book(c.entity, c.next_fy, new_book, entities_path=c.entities_path)
        done.append(f"- Registry: {c.next_fy} now points at the new book")
    c.next_drive_dir.mkdir(parents=True, exist_ok=True)
    if lnk_exists:
        done.append(f"- Drive: the shortcut was already there and points at the new book: {new_lnk}")
    else:
        shortcuts.create_shortcut(new_lnk, new_book)
        done.append(f"- Drive: shortcut created: {new_lnk}")
    _mark(state, 2, True)
    return "Step 2 done.\n\n" + "\n".join(done)


def step3(c: Ctx, state: dict) -> str:
    if not _passed(state, 2):
        raise Stop("Run step 2 first.")
    new_book = _new_book(c)
    _mark(state, 3, True)
    return ("Step 3 is yours. This skill changes nothing here.\n\n"
            f"1. Open the NEW year's book in GnuCash: {new_book}\n"
            f"2. Run the 31 March close for {c.fy} in that book, then save and close GnuCash.\n"
            "3. Come back and run step 4.\n\n"
            "Do not close the old year's book.")


def _new_book(c: Ctx) -> Path:
    raw = c.reg.list_books(c.entity, entities_path=c.entities_path).get(c.next_fy)
    if not raw:
        raise Stop(f"{c.next_fy} has no registered book. Run step 2 first.")
    book = Path(raw)
    if _norm(book.parent) != _norm(c.next_gnc_dir):
        raise Stop(f"The registered {c.next_fy} book is not inside the expected GnuCash folder "
                   f"({c.next_gnc_dir}). Nothing was changed.")
    if not book.is_file():
        raise Stop(f"The registered {c.next_fy} book file is not there ({book}).")
    return book


def step4(c: Ctx, state: dict) -> str:
    if not _passed(state, 2):
        raise Stop("Run step 2 first.")
    _guard_locks(c.gnc_dir, c.next_gnc_dir)
    book = _new_book(c)
    res = closing_check.check_closed(book, c.end)
    if res.ok:
        _mark(state, 4, True)
        return (f"Step 4 passed: the new book is closed for {c.fy}.\n\n"
                f"- Income and Expense accounts are all zero at 31 March {c.end}.\n"
                f"- Closing entries found dated 31 March {c.end}: {res.closing_entries}.\n\n"
                f"{c.fy} is now marked done. Step 5 lists what can be cleaned up.")
    _mark(state, 4, False)
    lines = [f"FAIL: step 4 did not pass. {c.fy} is NOT marked done.", ""]
    lines += [f"- {f}" for f in res.failed]
    if res.note:
        lines.append(f"- {res.note}")
    if res.income_nonzero:
        lines += ["", "Income accounts not at zero:"] + [f"- {p}" for p in res.income_nonzero]
    if res.expense_nonzero:
        lines += ["", "Expense accounts not at zero:"] + [f"- {p}" for p in res.expense_nonzero]
    lines += ["", "Run the 31 March close in the new book (step 3), save, close GnuCash, "
              "and run step 4 again."]
    return "\n".join(lines)


def step5(c: Ctx, state: dict, output_path: str) -> str:
    if not _passed(state, 4) or not state.get("done"):
        raise Stop("Step 4 has not passed, so nothing is offered for clean-up yet.")
    _guard_locks(c.gnc_dir, c.next_gnc_dir)
    items = cleanup.list_cleanup(c.gnc_dir, c.book, c.drive_dir)
    lines = [f"Step 5: clean-up list for {c.fy}. Nothing has been deleted.", ""]
    if not items:
        lines.append("There is nothing to clean up.")
        return "\n".join(lines)
    lines.append("| What | Path | Size (bytes) | Date |")
    lines.append("|---|---|---|---|")
    lines += [f"| {i.kind} | {i.path} | {i.size} | {i.modified} |" for i in items]
    lines += ["", f"The final book is kept: {c.book}", ""]
    usable = [i for i in items if cleanup.script_safe(i)]
    skipped = [i for i in items if not cleanup.script_safe(i)]
    script_path = Path(output_path).with_name(Path(output_path).stem + "-cleanup.ps1")
    script_path.write_bytes(cleanup.build_script(usable).encode("ascii"))
    lines.append(f"Recycle Bin script (you run it; each file goes to the Recycle Bin): {script_path}")
    if skipped:
        lines += ["", "Not in the script - recycle by hand (the path has characters the script cannot hold safely):",
                  "", "| What | Path | Size (bytes) | Date |", "|---|---|---|---|"]
        lines += [f"| {i.kind} | {i.path} | {i.size} | {i.modified} |" for i in skipped]
    return ReplyWithOutputs("\n".join(lines),
                            [extra_output("cleanup_script", script_path, "Recycle Bin script")])


def status(c: Ctx, state: dict) -> str:
    lines = [f"Year-end for {c.fy}: {'DONE' if state.get('done') else 'not done yet'}.", ""]
    for n in range(1, 6):
        s = state["steps"].get(str(n))
        mark = "not run" if not s else ("passed" if s["passed"] else "did not pass")
        lines.append(f"{n}. {STEP_NAMES[n]}: {mark}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------

def _step_number(step) -> int:
    m = re.match(r"\s*(\d)", str(step or ""))
    n = int(m.group(1)) if m else -1
    if n not in STEP_NAMES:
        raise Stop("Pick a step (0 to 5).")
    return n


def run(
    entity_key: str,
    closing_fy: str,
    step: str,
    output_path: str,
    entities_path: str = "Data/itr/entities.yaml",
    settings_path: str = "Data/settings/config.yaml",
    state_dir: str = "Data/year_end",
    config_path: str = "",
    model_override: str = None,
) -> str:
    """Run ONE step of the year-end close for one entity and one closing FY.
    The reply is also written to output_path (the report)."""
    try:
        n = _step_number(step)
        if not (entity_key or "").strip():
            raise Stop("Pick an entity.")
        ctx = build_ctx(entity_key.strip(), closing_fy, entities_path, settings_path)
        state = load_state(state_dir, ctx.entity, ctx.fy)
        if n == 0:
            reply = status(ctx, state)
        else:
            if n == 5:
                reply = step5(ctx, state, output_path)
            else:
                reply = {1: step1, 2: step2, 3: step3, 4: step4}[n](ctx, state)
            save_state(state_dir, ctx.entity, ctx.fy, state)
    except Stop as stop:
        reply = f"Stopped. {stop}"
    except OSError as exc:
        reply = f"Stopped. A file operation failed ({type(exc).__name__}). Nothing was removed. Check the folders and run the step again."
    try:
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        Path(output_path).write_text(str(reply), encoding="utf-8")
    except OSError:
        pass
    return reply
