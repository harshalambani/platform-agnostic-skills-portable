"""
ui/tabs/tds_journal_review.py -- "Journal Review" tab for the 26AS Journal
skill's TDS/TCS output.

The skill (src/agents/skill_26as_journal) parses a 26AS workbook and emits
two files side by side in the output folder:

  <stem>-tds-journals.csv         the GnuCash multi-split import (one row per
                                   split; Amount is signed, Debit +, Credit -).
  <stem>-tds-journals-review.csv  the audit: one row per deductor/collector,
                                   with the credit account it matched (or
                                   Liabilities:Suspense + "Needs Review" when
                                   it could not).

Deductors routed to Suspense today require hand-editing the CSV. This tab
gives that a proper review UI, built on the shared ui._review_engine skeleton
(searchable assign picker, multi-select, sort/filter, save payload bridge).
It mirrors ui/tabs/gnucash_review.py's Gradio wiring (output-folder dropdown +
refresh, GnuCash-book File picker for the account tree, CSS-hidden payload
textbox, DownloadButton toggled via `interactive`, Reset).

The one thing this screen must get right that a plain "edit one CSV" screen
doesn't: reassigning a deductor's Credit Account must rewrite BOTH files, and
the review row -> journal splits link is by Transaction ID, not row order.
See _apply_changes()'s docstring for the exact matching rules.

Save also regenerates a THIRD file, <stem>-tds-journals-partI.csv, containing
only the Part I (TDSJ/TCSJ) rows with every Part II (15GJ) transaction
dropped whole. It exists for users who post 15G/15H by hand in GnuCash
themselves: importing the full journal's 15GJ rows on top of that hand entry
double-books the Interest-on-FD -> NBFC reclassification. Regenerating it on
every Save (never hand-editing a copy) is the whole point -- see
_regenerate_part_i_split()'s docstring and build_tds_journals.split_part_ii()
for why a stale hand-filtered copy was the original bug.
"""
from __future__ import annotations

import csv
import shutil
from pathlib import Path

import gradio as gr

from ui import _config as _config_mod
from ui._review_engine import (
    Column,
    PickerItem,
    ReviewSpec,
    build_html,
    parse_payload,
    payload_box_css,
)

APP_ID = "tdsjr"
TARGET_COL = "Credit Account"

# Shared with the spec's also_set (see _spec()) so the "what does a user
# override look like" answer lives in exactly one place.
OVERRIDE_CONFIDENCE = "override"
OVERRIDE_BASIS = "User override (review)"


# ---------------------------------------------------------------------------
# Output-folder CSV scanner
# ---------------------------------------------------------------------------

def _scan_review_csvs() -> list[tuple[str, str]]:
    """Find *-tds-journals-review.csv files in the output dir, newest first."""
    try:
        out_dir = _config_mod.output_dir()
    except Exception:
        return []
    csvs = sorted(
        out_dir.glob("*-tds-journals-review.csv"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    return [(p.name, str(p)) for p in csvs[:20]]


def _journal_path_for(review_path: str) -> Path:
    """<stem>-tds-journals-review.csv -> <stem>-tds-journals.csv (sibling)."""
    p = Path(review_path)
    stem = p.name[: -len("-review.csv")] if p.name.endswith("-review.csv") else p.stem
    return p.with_name(stem + ".csv")


# ---------------------------------------------------------------------------
# GnuCash account tree extraction -- identical approach to gnucash_review's
# _extract_account_tree (same postable-accounts filter).
# ---------------------------------------------------------------------------

def _extract_account_tree(gnucash_file: str) -> list[str]:
    try:
        from agents.gnucash_accounts import load_accounts, postable_accounts
        accounts = postable_accounts(load_accounts(gnucash_file))
    except Exception:
        return []
    return sorted({a.path for a in accounts if a.path and ":" in a.path})


# ---------------------------------------------------------------------------
# Row loading + presentation (computed in Python, per the engine's core rule)
# ---------------------------------------------------------------------------

def _row_presentation(row: dict) -> None:
    """Fill in the engine's _tags / _rowclass / _badges / _note keys in place."""
    needs_review = (row.get("Needs Review") or "").strip().lower() == "yes"
    credit_account = row.get("Credit Account") or ""
    is_suspense = "suspense" in credit_account.lower()
    missing_account = (row.get("Account Exists") or "").strip().upper() == "NO"
    unbalanced = (row.get("Balanced") or "").strip().upper() == "NO"

    tags: list[str] = []
    if needs_review:
        tags.append("needs_review")
    if is_suspense:
        tags.append("suspense")
    if missing_account:
        tags.append("missing_account")
    if unbalanced:
        tags.append("unbalanced")
    if not tags:
        tags.append("matched")
    row["_tags"] = tags

    # unbalanced is the loudest failure state -- a transaction whose splits
    # don't sum to zero is a harder error than an unrecognised account name --
    # so it's ranked with (not below) needs_review/suspense, above
    # missing_account, and it must never fall through to a plain row.
    if needs_review or is_suspense or unbalanced:
        row["_rowclass"] = "accent-red"
    elif missing_account:
        row["_rowclass"] = "accent-amber"
    else:
        row["_rowclass"] = "accent-green"

    badges: dict = {}
    if is_suspense:
        badges[TARGET_COL] = {"text": "SUSPENSE", "cls": "red"}
    elif missing_account:
        badges[TARGET_COL] = {"text": "NO ACCOUNT", "cls": "amber"}
    if badges:
        row["_badges"] = badges

    row["_note"] = row.get("Basis") or ""


def _load_review_rows(review_path: str) -> list[dict]:
    with open(review_path, "r", encoding="utf-8", errors="replace") as f:
        rows = list(csv.DictReader(f))
    for row in rows:
        _row_presentation(row)
    return rows


def _spec(picker_items: list[PickerItem], review_path: str, gnucash_path: str = "") -> ReviewSpec:
    return ReviewSpec(
        app_id=APP_ID,
        columns=[
            Column("Sr", "Sr", sort="number"),
            Column("Deductor", "Deductor"),
            Column("Section", "Section"),
            Column("Category", "Category"),
            Column(TARGET_COL, "Credit Account"),
            Column("Confidence", "Confidence"),
            Column("Account Exists", "Account Exists"),
            Column("Balanced", "Balanced"),
            Column("Debit", "Debit", sort="number"),
            Column("Credit", "Credit", sort="number"),
            Column("Needs Review", "Needs Review"),
        ],
        target_col=TARGET_COL,
        payload_var="_tdsJrSavePayload",
        picker_label="Assign account:",
        picker_placeholder="Type to search accounts…",
        picker_items=picker_items,
        status_options=[
            ("needs_review", "Needs review"),
            ("suspense", "Suspense"),
            ("missing_account", "Missing account"),
            ("unbalanced", "Unbalanced"),
            ("matched", "Matched"),
        ],
        status_label="Show:",
        default_sort="Sr",
        # Deliberately NOT set: each deductor appears exactly once in this
        # review, so "apply to matching" has nothing meaningful to match on.
        apply_matching_on="",
        also_set={"Confidence": OVERRIDE_CONFIDENCE, "Basis": OVERRIDE_BASIS},
        context={"review_path": review_path, "gnucash_path": gnucash_path},
    )


def _load_review_data(review_path: str, gnucash_path: str) -> str:
    if not review_path:
        return "<p>Select a review CSV, then click Load.</p>"

    review_p = Path(review_path)
    if not review_p.is_file():
        return f"<p>Review CSV not found: {review_p.name}</p>"

    rows = _load_review_rows(str(review_p))
    if not rows:
        return "<p>Review CSV is empty -- nothing to review.</p>"

    accounts: list[str] = []
    if gnucash_path:
        gc_p = Path(gnucash_path)
        if gc_p.is_file():
            accounts = _extract_account_tree(str(gc_p))
    if not accounts:
        accounts = sorted({r.get(TARGET_COL, "") for r in rows if r.get(TARGET_COL)})

    picker_items = [PickerItem(value=a, primary=a) for a in accounts]
    spec = _spec(picker_items, str(review_p), gnucash_path or "")
    return payload_box_css(spec.payload_box_id) + build_html(spec, rows)


# ---------------------------------------------------------------------------
# Save logic -- kept as small, Gradio-free module functions so they are
# unit-testable directly (see tests/test_tds_journal_review.py).
# ---------------------------------------------------------------------------

def _fy_prefix_from(journal_rows: list[dict]) -> str:
    """Parse the fy_prefix off the journal CSV's existing Transaction IDs.

    Transaction IDs are built as f"{fy_prefix}-{kind}{sr:02d}" -- everything
    before the LAST '-' is the prefix. Deliberately not re-derived from the
    review CSV's FY: a run in a different FY must still resolve correctly
    against whatever prefix is actually in the journal file on disk.
    """
    for row in journal_rows:
        txn_id = (row.get("Transaction ID") or "").strip()
        if "-" in txn_id:
            return txn_id.rsplit("-", 1)[0]
    return ""


def _txn_id_for(fy_prefix: str, sr: str, category: str) -> str:
    # Single source of truth for category -> Transaction ID series lives in
    # build_tds_journals.py (CATEGORY_SERIES / series_for_category) — imported
    # rather than restated here so this file and the journal builder can never
    # drift apart (see that module for why: a wrong series here would build an
    # id absent from the journal CSV and _find_credit_split would silently
    # find nothing, dropping the row on Save without any indication why).
    try:
        import sys as _sys

        _src = Path(__file__).resolve().parent.parent.parent / "src"
        if str(_src) not in _sys.path:
            _sys.path.insert(0, str(_src))
        from agents.skill_26as_journal.scripts.build_tds_journals import (
            series_for_category,
        )
    except ImportError as e:
        # The single source of truth could not even be loaded -- never guess
        # a series in that situation, raise loudly with the cause attached.
        raise ImportError(
            "_txn_id_for: could not load the category->series table from "
            "build_tds_journals.series_for_category; refusing to guess a "
            "Transaction ID series"
        ) from e
    # ValueError from an unrecognised category is intentionally NOT caught
    # here -- series_for_category raises it precisely so an unknown category
    # fails loud instead of silently producing a wrong (and thus missing)
    # Transaction ID. Callers must handle it, not this function.
    kind = series_for_category(category)
    try:
        sr_num = int(str(sr).strip())
    except ValueError:
        sr_num = 0
    return f"{fy_prefix}-{kind}{sr_num:02d}"


def _find_credit_split(
    journal_rows: list[dict], txn_id: str, old_credit_account: str,
) -> tuple[int | None, str | None]:
    """Locate the unique credit split for `txn_id` within journal_rows.

    Returns (row_index, error) -- error is None on a clean, unambiguous match.
    Rule 1 (primary): the split whose Account equals old_credit_account.
    Rule 2 (fallback): if that finds nothing, the unique split with
    Amount < 0. Neither resolving to exactly one row is reported, not guessed.
    """
    txn_indices = [i for i, r in enumerate(journal_rows)
                   if (r.get("Transaction ID") or "").strip() == txn_id]
    if not txn_indices:
        return None, f"no splits found for Transaction ID {txn_id!r}"

    primary = [i for i in txn_indices
               if (journal_rows[i].get("Account") or "") == old_credit_account]
    if len(primary) == 1:
        return primary[0], None
    if len(primary) > 1:
        return None, (
            f"ambiguous: {len(primary)} splits in {txn_id!r} match account "
            f"{old_credit_account!r} -- skipped"
        )

    def _amount(i: int) -> float:
        try:
            return float(journal_rows[i].get("Amount") or 0)
        except ValueError:
            return 0.0

    negative = [i for i in txn_indices if _amount(i) < 0]
    if len(negative) == 1:
        return negative[0], None
    if len(negative) == 0:
        return None, f"no credit (negative-amount) split found in {txn_id!r} -- skipped"
    return None, (
        f"ambiguous: {len(negative)} negative-amount splits in {txn_id!r} -- skipped"
    )


def _load_split_part_ii():
    """Import split_part_ii / part_i_path_for / write_rows_csv from
    build_tds_journals the same defensive way _txn_id_for (above) imports
    series_for_category -- both pull from that module's single source of
    truth, and both must fail loud rather than silently guess if it can't be
    loaded (a guess here could regenerate the Part I file wrong, or not at
    all, without any indication why)."""
    try:
        import sys as _sys

        _src = Path(__file__).resolve().parent.parent.parent / "src"
        if str(_src) not in _sys.path:
            _sys.path.insert(0, str(_src))
        from agents.skill_26as_journal.scripts.build_tds_journals import (
            part_i_path_for,
            split_part_ii,
            write_rows_csv,
        )
    except ImportError as e:
        raise ImportError(
            "_load_split_part_ii: could not load split_part_ii from "
            "build_tds_journals; refusing to guess the Part I split"
        ) from e
    return split_part_ii, part_i_path_for, write_rows_csv


def _regenerate_part_i_split(
    journal_rows: list[dict], journal_p: Path,
) -> tuple[str | None, list[dict], list[dict], list[str]]:
    """Regenerate the Part-I-only sibling of journal_p from the just-written
    journal_rows, so a hand-filtered copy can never go stale -- this is
    called on every Save, in lockstep with the journal/review rewrite (see
    build_tds_journals.write_part_i_split's docstring for the same
    delete-if-not-needed rule this mirrors).

    Returns (part_i_path_or_None, part_i_rows, part_ii_rows, split_problems).
    """
    split_part_ii, part_i_path_for, write_rows_csv = _load_split_part_ii()
    part_i_rows, part_ii_rows, split_problems = split_part_ii(journal_rows)
    part_i_path = part_i_path_for(journal_p)
    if not part_ii_rows:
        if part_i_path.exists():
            part_i_path.unlink()
        return None, part_i_rows, part_ii_rows, split_problems
    write_rows_csv(part_i_rows, part_i_path)
    return str(part_i_path), part_i_rows, part_ii_rows, split_problems


def _verify_balanced(journal_rows: list[dict], tolerance: float = 0.01) -> list[str]:
    """Re-verify every transaction's Amount column sums to zero.

    Returns a list of human-readable problems (empty when everything balances).
    """
    totals: dict[str, float] = {}
    for row in journal_rows:
        txn_id = (row.get("Transaction ID") or "").strip()
        try:
            amt = float(row.get("Amount") or 0)
        except ValueError:
            amt = 0.0
        totals[txn_id] = totals.get(txn_id, 0.0) + amt

    problems = []
    for txn_id, total in totals.items():
        if abs(total) > tolerance:
            problems.append(f"{txn_id}: splits sum to {total:.2f} (expected 0.00)")
    return problems


def _apply_changes(
    review_rows: list[dict], journal_rows: list[dict], changes: list[dict],
    known_accounts: set[str] | None = None,
) -> tuple[list[dict], list[dict], list[str], int]:
    """Apply `changes` (from the review payload) to both row sets in place.

    `changes` entries carry at least {Sr, Category, Credit Account, _orig}
    (the engine's payload shape -- see _review_engine.build_html's syncPayload
    -- plus whatever other columns are in the spec). `_orig` is the credit
    account BEFORE this edit (used to find the old split).

    `known_accounts` is the real GnuCash account tree (from
    _extract_account_tree), used to (re)set "Account Exists" on a changed row.
    When None (no book was loaded), the column is left untouched rather than
    written with a value that can't be justified -- checking the new account
    against the *journal CSV's own* accounts would be tautological (the
    account was just written into it) and checks the wrong source of truth
    anyway (a book's chart, not what happens to already appear in this run's
    output).

    Returns (updated_review_rows, updated_journal_rows, problems, applied) --
    problems is a list of skip/ambiguity messages (including the
    blank-input case) and applied is an explicit count of changes that were
    actually written, not `len(changes) - len(problems)` (a change dropped by
    the blank-input guard previously appended nothing to problems, so it was
    silently counted as applied). Rows are mutated by Sr (Category
    disambiguates the two ID series), never by list position, so this is safe
    to call with review_rows in a different order than the CSV was loaded in.
    """
    fy_prefix = _fy_prefix_from(journal_rows)
    review_by_sr = {
        (str(r.get("Sr", "")).strip(), (r.get("Category") or "").strip()): r
        for r in review_rows
    }
    problems: list[str] = []
    applied = 0

    for ch in changes:
        sr = str(ch.get("Sr", "")).strip()
        category = (ch.get("Category") or "").strip()
        new_account = (ch.get(TARGET_COL) or "").strip()
        old_account = (ch.get("_orig") or "").strip()
        if not sr or not new_account:
            problems.append(
                f"Sr {sr or '?'}/{category or '?'}: blank Sr or Credit Account "
                "-- skipped"
            )
            continue

        review_row = review_by_sr.get((sr, category))
        if review_row is None:
            problems.append(f"Sr {sr}/{category}: no matching review row -- skipped")
            continue

        if not fy_prefix:
            problems.append(
                f"Sr {sr}/{category}: could not determine fy_prefix from journal "
                "CSV -- skipped"
            )
            continue

        try:
            txn_id = _txn_id_for(fy_prefix, sr, category)
        except (ValueError, ImportError) as e:
            problems.append(
                f"Sr {sr}/{category or '?'}: could not determine Transaction ID "
                f"series for category {category or '?'!r} -- skipped ({e})"
            )
            continue
        idx, err = _find_credit_split(journal_rows, txn_id, old_account)
        if err:
            problems.append(f"Sr {sr}/{category} ({txn_id}): {err}")
            continue

        journal_rows[idx]["Account"] = new_account
        review_row["Credit Account"] = new_account
        review_row["Confidence"] = OVERRIDE_CONFIDENCE
        review_row["Basis"] = OVERRIDE_BASIS
        if known_accounts is not None:
            review_row["Account Exists"] = "yes" if new_account in known_accounts else "NO"
        applied += 1

    return review_rows, journal_rows, problems, applied


# ---------------------------------------------------------------------------
# CSV read/write helpers
# ---------------------------------------------------------------------------

_JOURNAL_HEADERS = ["Date", "Transaction ID", "Number", "Description",
                    "Account", "Amount", "Currency"]
_REVIEW_HEADERS = ["Sr", "Deductor", "Section", "Category", "Credit Account",
                   "Confidence", "Account Exists", "Balanced", "Debit",
                   "Credit", "Needs Review", "Basis"]


def _read_csv_rows(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        return list(csv.DictReader(f))


def _write_csv_rows(path: Path, headers: list[str], rows: list[dict]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def _stage_for_download(path: Path) -> str | None:
    """Copy `path` into the download staging dir and return the staged path,
    or None (with the caller responsible for reporting the failure)."""
    staging = _config_mod.download_staging_dir()
    staging.mkdir(parents=True, exist_ok=True)
    staged = staging / path.name
    shutil.copy2(path, staged)
    return str(staged.resolve())


def _save_changes(
    changes_json: str,
) -> tuple[str, "gr.update", "gr.update"]:
    """Process a Save from the review UI -- rewrites the journal and review
    CSVs, regenerates the Part-I-only sibling (excluding any 15G/15H rows) in
    lockstep, re-verifies balance on both the full journal and the Part I
    split independently, and stages both CSVs for download.

    Returns (status_markdown, download_file_update, part_i_download_update).
    """
    no_change = (
        gr.update(interactive=False, value=None),
        gr.update(interactive=False, value=None),
    )
    if not changes_json or not changes_json.strip():
        return ("No changes to save.",) + no_change

    try:
        payload = parse_payload(changes_json)
    except ValueError as e:
        return (f"Error parsing changes: {e}",) + no_change

    changes = payload["changes"]
    context = payload["context"]
    review_path = context.get("review_path", "")
    gnucash_path = context.get("gnucash_path", "")

    if not changes:
        return ("No changes to save.",) + no_change
    if not review_path:
        return ("Error: no review CSV path in payload -- nothing was saved.",) + no_change

    review_p = Path(review_path)
    if not review_p.is_file():
        return (f"Error: review CSV not found: {review_p}",) + no_change

    journal_p = _journal_path_for(str(review_p))
    if not journal_p.is_file():
        return (f"Error: journal CSV not found: {journal_p}",) + no_change

    review_rows = _read_csv_rows(review_p)
    journal_rows = _read_csv_rows(journal_p)

    known_accounts: set[str] | None = None
    if gnucash_path and Path(gnucash_path).is_file():
        known_accounts = set(_extract_account_tree(gnucash_path))

    review_rows, journal_rows, problems, applied = _apply_changes(
        review_rows, journal_rows, changes, known_accounts=known_accounts,
    )

    _write_csv_rows(review_p, _REVIEW_HEADERS, review_rows)
    _write_csv_rows(journal_p, _JOURNAL_HEADERS, journal_rows)

    balance_problems = _verify_balanced(journal_rows)

    # Regenerate the Part-I-only file from the rows just written, so an edit
    # made on this screen (e.g. a credit-account reassignment) is reflected
    # in it immediately -- a hand-filtered copy of the pre-edit journal would
    # otherwise silently go stale the moment this Save runs.
    try:
        part_i_path, part_i_rows, part_ii_rows, split_problems = \
            _regenerate_part_i_split(journal_rows, journal_p)
    except ImportError as e:
        part_i_path, part_i_rows, part_ii_rows, split_problems = None, [], [], []
        problems = list(problems) + [f"Part I split not regenerated: {e}"]

    part_i_balance_problems = _verify_balanced(part_i_rows) if part_i_rows else []
    excluded_txns = sorted({(r.get("Transaction ID") or "").strip()
                            for r in part_ii_rows if r.get("Transaction ID")})

    lines = ["**Saved**", ""]
    lines.append(f"Applied {applied} of {len(changes)} change(s).")
    lines.append(f"Rewrote {review_p.name} and {journal_p.name}.")
    if problems:
        lines.append("")
        lines.append(f"{len(problems)} change(s) skipped (not guessed):")
        for p in problems:
            lines.append(f"  - {p}")
    if balance_problems:
        lines.append("")
        lines.append(
            "**WARNING: re-verification found unbalanced transaction(s) after "
            "save** -- an account rename should never change split sums, so "
            "this points at something else going wrong. Investigate before "
            "importing:"
        )
        for p in balance_problems:
            lines.append(f"  - {p}")
    else:
        lines.append("")
        lines.append("Balance re-verification: all transactions sum to 0.00 (ok).")

    # This paragraph is the user's only defence against picking the wrong
    # file to import, so it states plainly, in numbers, what each file is.
    lines.append("")
    if part_i_path:
        lines.append(
            f"{len(excluded_txns)} transaction(s) excluded from "
            f"{Path(part_i_path).name} because they are 15G/15H (Part II) "
            f"reclassifications -- **if you post Part II by hand in "
            f"GnuCash, import {Path(part_i_path).name}, not "
            f"{journal_p.name}, or the reclassification will be "
            f"double-booked.**"
        )
        if part_i_balance_problems:
            lines.append(
                "**WARNING: the Part I split itself does not balance after "
                "re-verification** -- dropping whole transactions must "
                "never change split sums; investigate before importing "
                "either file:"
            )
            for p in part_i_balance_problems:
                lines.append(f"  - {p}")
        else:
            lines.append(
                f"Part I split re-verification: all {len(part_i_rows)} "
                "row(s) sum to 0.00 per transaction (ok)."
            )
    else:
        lines.append(
            "No 15G/15H (Part II) transactions in this run -- there is "
            f"nothing to exclude, so {journal_p.name} is the only file "
            "to import."
        )
    if split_problems:
        lines.append("")
        lines.append(
            "Warning: could not determine the series for some Transaction "
            "ID(s) -- kept in the Part I file, not dropped:"
        )
        for p in split_problems:
            lines.append(f"  - {p}")

    download_path: str | None = None
    try:
        download_path = _stage_for_download(journal_p)
    except Exception as e:
        lines.append("")
        lines.append(f"Warning: could not stage download -- {e}")

    part_i_download_path: str | None = None
    if part_i_path:
        try:
            part_i_download_path = _stage_for_download(Path(part_i_path))
        except Exception as e:
            lines.append("")
            lines.append(f"Warning: could not stage Part I download -- {e}")

    journal_update = (gr.update(value=download_path, interactive=True)
                      if download_path else gr.update(interactive=False, value=None))
    part_i_update = (gr.update(value=part_i_download_path, interactive=True)
                     if part_i_download_path else gr.update(interactive=False, value=None))

    return "\n".join(lines), journal_update, part_i_update


# ---------------------------------------------------------------------------
# Gradio tab renderer
# ---------------------------------------------------------------------------

def render(container_tab=None) -> None:
    """Render the Journal Review tab. Must be called inside gr.Tab()."""
    gr.Markdown(
        "## Journal Review\n\n"
        "Review deductors the 26AS Journal skill routed to "
        "`Liabilities:Suspense` (or otherwise flagged Needs Review). Select "
        "a review CSV and GnuCash book, click Load, assign the correct "
        "credit account via the searchable dropdown, select rows, Apply to "
        "selected, then Save -- this rewrites both the journal and review "
        "CSVs and re-verifies every transaction still balances. Save also "
        "regenerates a second, Part-I-only CSV excluding any 15G/15H rows: "
        "download the full journal to import everything, or the Part I "
        "file if you post 15G/15H by hand in GnuCash yourself."
    )

    initial_csvs = _scan_review_csvs()

    with gr.Row():
        csv_dropdown = gr.Dropdown(
            label="TDS/TCS Review CSV",
            choices=initial_csvs,
            value=initial_csvs[0][1] if initial_csvs else None,
            allow_custom_value=True,
            scale=4,
        )
        refresh_btn = gr.Button("↻", scale=0, min_width=40)
        gnucash_file = gr.File(
            label="GnuCash book (.gnucash)",
            file_types=[".gnucash"],
            type="filepath",
        )

    load_btn = gr.Button("Load for Review", variant="primary")

    refresh_btn.click(
        fn=lambda: gr.update(choices=_scan_review_csvs()),
        inputs=[],
        outputs=[csv_dropdown],
    )

    if container_tab is not None:
        def _rescan_newest():
            choices = _scan_review_csvs()
            return gr.update(choices=choices,
                             value=(choices[0][1] if choices else None))
        container_tab.select(fn=_rescan_newest, inputs=[], outputs=[csv_dropdown])

    review_html = gr.HTML(value="<p><em>Load a CSV to begin reviewing.</em></p>")

    with gr.Row():
        save_btn = gr.Button("Save & Export", variant="primary")
        reset_btn = gr.Button("Reset", variant="secondary")
    save_result = gr.Markdown("")
    download_file = gr.DownloadButton(
        label="Download corrected journal CSV", visible=True, interactive=False,
        variant="primary",
    )
    # Interactive only when this run actually produced a Part II (15G/15H)
    # row -- with none to exclude there is nothing to distinguish this file
    # from the full journal, so offering it would just invite picking the
    # wrong download for no reason.
    part_i_download_file = gr.DownloadButton(
        label="Download Part I only (excludes 15G/15H)", visible=True,
        interactive=False, variant="secondary",
    )

    _payload_box = gr.Textbox(
        value="", show_label=False, container=False, lines=1,
        elem_id=f"{APP_ID}-payload-box",
    )

    load_btn.click(
        fn=_load_review_data,
        inputs=[csv_dropdown, gnucash_file],
        outputs=review_html,
    )
    save_btn.click(
        fn=_save_changes,
        inputs=[_payload_box],
        outputs=[save_result, download_file, part_i_download_file],
        js="(x) => window._tdsJrSavePayload || ''",
    )

    def _handle_reset():
        choices = _scan_review_csvs()
        return (
            gr.update(choices=choices, value=(choices[0][1] if choices else None)),
            gr.update(value=None),                                   # gnucash_file
            "<p><em>Load a CSV to begin reviewing.</em></p>",        # review_html
            "",                                                       # save_result
            gr.update(interactive=False, value=None),                 # download_file
            gr.update(interactive=False, value=None),                 # part_i_download_file
            "",                                                       # _payload_box
        )

    reset_btn.click(
        fn=_handle_reset,
        inputs=[],
        outputs=[csv_dropdown, gnucash_file, review_html, save_result,
                 download_file, part_i_download_file, _payload_box],
        js="() => { window._tdsJrSavePayload = ''; }",
    )
