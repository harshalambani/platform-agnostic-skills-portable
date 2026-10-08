"""UI-23 -- sort the firm's documents by what is INSIDE them.

One optional drop zone takes the whole folder (or one .zip). Each file is
opened and recognised from its content -- never from its file name -- and put
into one of the roles the six pickers already cover:

    payout      monthly payout certificate / statement (both layouts)
    advisory    this year's Compensation summary (the year read from inside
                equals the reporting year)
    award       an EARLIER year's Compensation summary (award-year documents)
    llp         the LLP Statement of Account (PDF or the .eml it arrived in)
    schedule    the payment schedule

Everything else is skipped, with a reason on its row: the Target
compensation letter, a summary for a year after the reporting year, a
forward-looking summary, a superseded revision, a file that is not
recognised, a file that cannot be opened.

Rules that are never relaxed:
  * a file is in exactly one role (or skipped), never two;
  * the latest revision wins (letter date, then revision number); when two
    cannot be ordered the run STOPS and names both files -- it never picks;
  * a role the user picked by hand always wins over the drop zone;
  * a zip member with an unsafe path is rejected; the number of files and
    the total unpacked size are capped; nested archives and members that are
    not a PDF or an .eml are listed as skipped, never opened.

Pure of the reconciliation: nothing here touches a figure. Synthetic
fixtures only in tests.
"""
from __future__ import annotations

import hashlib
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from . import award_year as _award_year
from . import precheck as _precheck
from .parsers import advisory as _advisory_parser
from .parsers import llp_statement as _llp_parser
from .parsers import payment_schedule as _schedule_parser
from .parsers import payout_advice as _payout_parser

MAX_FILES = 300
MAX_TOTAL_BYTES = 300 * 1024 * 1024
_ARCHIVE_SUFFIXES = {".zip", ".7z", ".rar", ".gz", ".tgz", ".tar", ".bz2", ".xz"}
_KEEP_SUFFIXES = {".pdf", ".eml"}

ROLE_LABEL = {
    "payout": "Monthly payout document",
    "advisory": "Advisory (Compensation summary)",
    "award": "Earlier year's Advisory",
    "llp": "LLP Statement of Account",
    "schedule": "Payment schedule",
    "target": "Target compensation letter",
    "forward": "Forward-looking summary",
    "unknown": "Not recognised",
    "error": "Could not be read",
}

_FORWARD_RE = re.compile(r"compensation\s+summary", re.I)


class IntakeStop(Exception):
    """The run cannot go on: say why, in plain words."""


@dataclass
class Row:
    name: str
    recognised: str
    year: str
    outcome: str  # "used for ..." or "skipped"
    why: str = ""


@dataclass
class Sorted:
    rows: list = field(default_factory=list)
    advices: list = field(default_factory=list)       # payout file paths
    advisory: str = ""
    award: list = field(default_factory=list)
    llp: str = ""
    schedule: str = ""
    missing: list = field(default_factory=list)
    workdir: str = ""
    stop: str = ""

    def table_rows(self):
        return [(r.name, r.recognised, r.year, r.outcome + (f" - {r.why}" if r.why else ""))
                for r in self.rows]


# --------------------------------------------------------------------------
# Collecting the files (folder, loose files, zip) -- safe
# --------------------------------------------------------------------------

def _unsafe_member(name: str) -> bool:
    n = name.replace("\\", "/")
    p = PurePosixPath(n)
    return (n.startswith("/") or bool(re.match(r"^[A-Za-z]:", n))
            or ".." in p.parts)


def _extract_zip(zpath: Path, dest: Path, budget: dict, skipped: list, label: str) -> list:
    """Safely unpack the PDF/.eml members of one zip. Returns [(display, path)]."""
    out = []
    try:
        zf = zipfile.ZipFile(zpath)
    except (zipfile.BadZipFile, OSError):
        skipped.append((label, "not a readable zip file"))
        return out
    with zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            disp = f"{label}/{info.filename}"
            if _unsafe_member(info.filename):
                skipped.append((disp, "rejected: the path inside the zip leaves the archive folder"))
                continue
            if (info.external_attr >> 16) & 0o170000 == 0o120000:
                skipped.append((disp, "rejected: a link inside the zip"))
                continue
            suffix = PurePosixPath(info.filename).suffix.lower()
            if suffix in _ARCHIVE_SUFFIXES:
                skipped.append((disp, "a nested archive; it is not opened"))
                continue
            if suffix not in _KEEP_SUFFIXES:
                skipped.append((disp, "not a PDF or .eml file"))
                continue
            if info.flag_bits & 0x1:
                skipped.append((disp, "the zip member is encrypted; extract it first"))
                continue
            budget["files"] += 1
            if budget["files"] > MAX_FILES:
                raise IntakeStop(f"the documents hold more than {MAX_FILES} files; "
                                 "add only this year's documents")
            if budget["bytes"] + info.file_size > MAX_TOTAL_BYTES:
                raise IntakeStop("the documents are larger than the "
                                 f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB limit once unpacked")
            target = dest / f"{budget['files']:04d}_{PurePosixPath(info.filename).name}"
            written = 0
            with zf.open(info) as src, open(target, "wb") as dst:
                while True:
                    chunk = src.read(1 << 16)
                    if not chunk:
                        break
                    written += len(chunk)
                    budget["bytes"] += len(chunk)
                    if budget["bytes"] > MAX_TOTAL_BYTES:
                        raise IntakeStop("the documents are larger than the "
                                         f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB limit once unpacked")
                    dst.write(chunk)
            out.append((disp, str(target)))
    return out


def collect(value, workdir: Path) -> tuple[list, list]:
    """([(display name, path)], [(display name, reason skipped)]) for the drop
    zone value: a folder, one file, a .zip, or a list of those."""
    if not value:
        return [], []
    items = [Path(v) for v in value] if isinstance(value, (list, tuple)) else [Path(value)]
    files: list = []
    skipped: list = []
    budget = {"files": 0, "bytes": 0}

    def take(path: Path, disp: str):
        suffix = path.suffix.lower()
        if suffix == ".zip":
            sub = workdir / f"zip{budget['files']:04d}_{len(files)}"
            sub.mkdir(parents=True, exist_ok=True)
            files.extend(_extract_zip(path, sub, budget, skipped, disp))
        elif suffix in _ARCHIVE_SUFFIXES:
            skipped.append((disp, "an archive type that is not opened (use a .zip)"))
        elif suffix in _KEEP_SUFFIXES:
            budget["files"] += 1
            if budget["files"] > MAX_FILES:
                raise IntakeStop(f"the documents hold more than {MAX_FILES} files; "
                                 "add only this year's documents")
            budget["bytes"] += path.stat().st_size
            if budget["bytes"] > MAX_TOTAL_BYTES:
                raise IntakeStop("the documents are larger than the "
                                 f"{MAX_TOTAL_BYTES // (1024 * 1024)} MB limit")
            files.append((disp, str(path)))
        else:
            skipped.append((disp, "not a PDF or .eml file"))

    for item in items:
        if item.is_dir():
            for p in sorted(q for q in item.rglob("*") if q.is_file()):
                take(p, str(p.relative_to(item)).replace("\\", "/"))
        elif item.is_file():
            take(item, item.name)
    return files, skipped


# --------------------------------------------------------------------------
# Recognising one file by its content
# --------------------------------------------------------------------------

def _norm_fy(value) -> str | None:
    m = re.search(r"(\d{4})\s*[-/]\s*(\d{2,4})", str(value or ""))
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)[-2:]}"


def classify(path: str, display: str, password: str | None) -> dict:
    """What one file is, read from inside it. Never raises."""
    info = {"name": display, "path": path, "role": "unknown", "fy": None, "month": None,
            "letter_date": None, "revision": None, "why": "", "text_hash": ""}
    pdf_path = path
    is_eml = path.lower().endswith(".eml")
    if is_eml:
        try:
            pdf_path = _precheck.extract_pdf_from_eml(path)
        except Exception as e:  # noqa: BLE001
            info["why"] = f"an e-mail with no readable PDF attachment ({e})"
            return info
    text = _precheck.read_first_page_text(pdf_path, password)
    if text is None:
        info["role"] = "error"
        info["why"] = (_precheck.pdf_open_problem(pdf_path, password)
                       or "could not be opened (not a readable PDF)")
        return info
    info["text_hash"] = hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()
    info["letter_date"] = _award_year.parse_letter_date(text)
    info["revision"] = _award_year.parse_revision(text)
    kind = _precheck.classify_advisory_text(text)
    first_error = ""

    def attempt(parse, not_this):
        nonlocal first_error
        try:
            return parse(pdf_path, password)
        except not_this:
            return None
        except Exception as e:  # noqa: BLE001
            first_error = first_error or _precheck.explain_pdf_error(e, password)
            return None

    if is_eml:
        rec = attempt(_llp_parser.parse, (_llp_parser.NotAnL5DocumentError,))
        if rec is not None:
            info.update(role="llp", fy=_norm_fy(rec.get("financial_year")))
        else:
            info["why"] = first_error or "the e-mail's PDF is not an LLP Statement of Account"
        return info
    if kind == _award_year.KIND_TARGET:
        info["role"] = "target"
        return info
    if kind == _award_year.KIND_ADVISORY:
        rec = attempt(_advisory_parser.parse, (_advisory_parser.NotAnL3DocumentError,))
        if rec is None:
            info["role"], info["why"] = "error", first_error or "could not be parsed as an Advisory"
            return info
        info.update(role="advisory", fy=_norm_fy(rec.get("financial_year")),
                    instalments=[i for i in (rec.get("schedule_instalments") or [])
                                 if i.get("instalment_no") is not None or _award_year._is_arrears(i)])
        return info
    rec = attempt(_payout_parser.parse, (_payout_parser.NotAnL1DocumentError,))
    if rec is not None:
        info.update(role="payout", month=_precheck.record_month(rec))
        return info
    rec = attempt(_llp_parser.parse, (_llp_parser.NotAnL5DocumentError,))
    if rec is not None:
        info.update(role="llp", fy=_norm_fy(rec.get("financial_year")))
        return info
    rec = attempt(_schedule_parser.parse, (_schedule_parser.NotAPaymentScheduleError,))
    if rec is not None:
        info.update(role="schedule", fy=_norm_fy(rec.get("financial_year")))
        return info
    if _FORWARD_RE.search(text):
        info["role"] = "forward"
        return info
    if first_error:
        info["role"], info["why"] = "error", first_error
    return info


# --------------------------------------------------------------------------
# Choosing the latest of several revisions
# --------------------------------------------------------------------------

def _pick_latest(group: list) -> tuple[dict | None, str]:
    """(winner, how) for several documents that stand for the same thing.
    (None, "") when they cannot be ordered."""
    if len(group) == 1:
        return group[0], ""
    if len({d["text_hash"] for d in group}) == 1:
        return group[0], "identical copies"
    dates = [d["letter_date"] for d in group]
    if all(dates):
        top = max(dates)
        if dates.count(top) == 1:
            return group[dates.index(top)], "latest letter date"
    revs = [d["revision"] for d in group]
    if all(r is not None for r in revs):
        top = max(revs)
        if revs.count(top) == 1:
            return group[revs.index(top)], "highest revision number"
    return None, ""


def _single(group: list, what: str):
    """One document for a role that takes exactly one; stops when unorderable."""
    if not group:
        return None, [], ""
    win, how = _pick_latest(group)
    if win is None:
        names = " and ".join(d["name"] for d in group)
        raise IntakeStop(
            f"two {what} documents cannot be told apart ({names}): neither letter dates nor "
            "revision numbers say which is later. Pick the one to use by hand in the matching "
            "picker below, or leave only one in the folder.")
    return win, [d for d in group if d is not win], how


# --------------------------------------------------------------------------
# The sorter
# --------------------------------------------------------------------------

def sort_documents(value, financial_year: str, password: str | None,
                   manual: dict | None = None) -> Sorted:
    """Sort the drop-zone documents into roles. `manual` names the roles the
    user picked by hand ({"payout": True, ...}); those roles are left to the
    pickers and every drop-zone file that would have gone there is skipped."""
    manual = manual or {}
    result = Sorted(workdir=tempfile.mkdtemp(prefix="pask-intake-"))
    work = Path(result.workdir)
    try:
        files, skipped = collect(value, work)
    except IntakeStop as e:
        result.stop = str(e)
        return result
    for disp, why in skipped:
        result.rows.append(Row(disp, ROLE_LABEL["unknown"], "-", "skipped", why))
    fy = _norm_fy(financial_year)
    if not fy:
        result.stop = ("choose the Financial year on the form first: the drop zone needs it to "
                       "tell this year's documents from earlier and later years")
        return result

    infos = [classify(p, d, password) for d, p in files]
    by_role: dict = {}
    for i in infos:
        by_role.setdefault(i["role"], []).append(i)

    def skip(i, why):
        result.rows.append(Row(i["name"], ROLE_LABEL.get(i["role"], i["role"]),
                               i["fy"] or (i["month"] or "-"), "skipped", why))

    def use(i, role_text, how=""):
        result.rows.append(Row(i["name"], ROLE_LABEL.get(i["role"], i["role"]),
                               i["fy"] or (i["month"] or "-"), f"used for {role_text}", how))

    def by_hand(i, role_name):
        skip(i, f"you picked the {role_name} by hand, and a manual pick always wins")

    months = set(_precheck.fy_months(fy))
    try:
        # --- monthly payout documents
        for i in by_role.get("payout", []):
            if manual.get("payout"):
                by_hand(i, "monthly payout documents")
            elif i["month"] and months and i["month"] not in months:
                skip(i, f"the month {i['month']} is outside FY {fy}")
            else:
                result.advices.append(i["path"])
                use(i, "the monthly payout documents")
        # --- Advisories: this year, earlier years, later years
        this_year, earlier = [], {}
        for i in by_role.get("advisory", []):
            if not i["fy"]:
                skip(i, "the financial year could not be read from inside the document")
            elif i["fy"] == fy:
                this_year.append(i)
            elif i["fy"] < fy:
                earlier.setdefault(i["fy"], []).append(i)
            else:
                skip(i, f"FY {i['fy']} is after the reporting year FY {fy}; it is never used as this year's")
        win, losers, how = _single(this_year, f"FY {fy} Advisory")
        if win is not None:
            if manual.get("advisory"):
                by_hand(win, "Advisory")
            else:
                result.advisory = win["path"]
                use(win, "this year's Advisory", how)
            for i in losers:
                skip(i, f"superseded by {win['name']} ({how})")
        for efy, group in sorted(earlier.items()):
            win, losers, how = _single(group, f"FY {efy} Advisory")
            if manual.get("award"):
                by_hand(win, "award-year documents")
            else:
                result.award.append(win["path"])
                use(win, f"award-year documents (FY {efy})", how)
            for i in losers:
                skip(i, f"superseded by {win['name']} ({how})")
        # --- LLP statement and payment schedule: one each, for this year
        for role, attr, what, mname in (("llp", "llp", "LLP Statement of Account", "LLP Statement"),
                                        ("schedule", "schedule", "payment schedule", "payment schedule")):
            group = []
            for i in by_role.get(role, []):
                if i["fy"] and i["fy"] != fy:
                    skip(i, f"it is for FY {i['fy']}, not FY {fy}")
                else:
                    group.append(i)
            win, losers, how = _single(group, what)
            if win is not None:
                if manual.get(role):
                    by_hand(win, mname)
                else:
                    setattr(result, attr, win["path"])
                    use(win, f"the {what}", how)
                for i in losers:
                    skip(i, f"superseded by {win['name']} ({how})")
    except IntakeStop as e:
        result.stop = str(e)
    # --- look-alikes and the rest
    for i in by_role.get("target", []):
        skip(i, "a Target compensation letter carries no year-end figures; it is never used as the Advisory")
    for i in by_role.get("forward", []):
        skip(i, "a forward-looking compensation summary, not a year-end Advisory")
    for i in by_role.get("unknown", []):
        skip(i, i["why"] or "not recognised as any of the firm's documents")
    for i in by_role.get("error", []):
        skip(i, i["why"])

    got = {"payout": bool(result.advices), "advisory": bool(result.advisory),
           "llp": bool(result.llp), "schedule": bool(result.schedule),
           "award": bool(result.award)}
    names = (("payout", "monthly payout documents"), ("advisory", f"this year's (FY {fy}) Advisory"),
             ("llp", "LLP Statement of Account"), ("schedule", "payment schedule"),
             ("award", "earlier years' Advisories (award-year documents)"))
    for role, label in names:
        if not got[role] and not manual.get(role):
            result.missing.append(f"Still missing: {label}.")
    return result


# --------------------------------------------------------------------------
# Presentation
# --------------------------------------------------------------------------

HEADERS = ("File", "Recognised as", "Year read from inside", "Used for / skipped and why")


def inputs_text(result: Sorted) -> str:
    """The Inputs table (markdown) plus a 'still missing' line per empty role."""
    def cell(s):
        return str(s).replace("|", "/").replace("\n", " ")
    lines = ["**Inputs: what each file was recognised as**", "",
             "| " + " | ".join(HEADERS) + " |", "|" + "---|" * len(HEADERS)]
    for r in result.table_rows():
        lines.append("| " + " | ".join(cell(c) for c in r) + " |")
    if not result.rows:
        lines.append("| (no files found) | | | |")
    for m in result.missing:
        lines.append("")
        lines.append(m)
    lines.append("")
    return "\n".join(lines)


def cleanup(result: Sorted) -> None:
    if result.workdir:
        shutil.rmtree(result.workdir, ignore_errors=True)
