"""
agent.py -- Partner Compensation Reconciliation. DIRECT mode, no LLM, no
network.

Stage 1 of this skill (see AGENT.md): the computation engine, the workbook
writer, and the tests. Stage 2 is the PDF parsers under parsers/ --
`payout_advice.py` (L1), `advisory.py` (L3) and `llp_statement.py` (L5)
are all implemented; `xlsx_26as` now has a real reader
(xlsx_26as_reader.py, Section A of the tie-out work); `gnucash_path` now
has a real, read-only tie-out (gnucash_tieout.py, Sections B and C): a
posted-already detector for every journal this run would emit, and a
balance tie-out for each of jv_emitter.ACCOUNT_KEYS's configured account
paths against the book's actual FY movement. See parsers/__init__.py.

run() has two entry paths:

  - Document-driven (the skill.yaml-facing path, and the only one exposed
    in the UI): entity/advices_dir/doc_password/advisory_path/
    llp_statement/gnucash_path/xlsx_26as. Required inputs missing fail
    loud, naming the input. Every OPTIONAL input (llp_statement,
    gnucash_path, xlsx_26as) that is absent -- or present but backed by a
    parser/reader that is still a Stage 2 placeholder, or fails its own
    content-dispatch check -- degrades its own reconciliation leg to an
    explicit "not available" note; it never fails the run, and never
    substitutes a zero or a default figure. The two REQUIRED documents
    (advices_dir, advisory_path) parse for real; a document that fails to
    open/parse (an unreadable/malformed PDF, a wrong password, or content
    that doesn't match the expected L1/L3 layout) still fails the whole
    run loud, naming the document and the reason -- see
    _run_from_documents()'s docstring. Once both required documents (and
    the optional L5 leg, if resolvable) parse, this path assembles them
    (mapper.build_input_data), computes the reconciliation
    (engine.build_report), writes the workbook, and -- if journal_path is
    supplied -- resolves/validates the entity's GnuCash account map and
    emits the journal CSV, exactly like the structured-input path below.
  - Structured-input (input_path): TEST-ONLY. Retained so the existing
    engine/writer/jv_emitter test suite keeps exercising build_report()
    directly without needing real PDF specimens. Deliberately absent from
    skill.yaml's `inputs:` so it never renders in the UI (see
    tests/test_skill_partner_comp_recon.py's manifest-shape guard test).

Architecture mirrors skill_mf_cas: `_load_input` is the ONLY function in
this package that touches the filesystem for the structured input path.
`mapper.build_input_data()` is pure (no I/O) and converts parsed L1/L3/L5
document records into `engine.build_report()`'s input shape.
`engine.build_report()` is pure (no I/O); `writer.write_report_workbook()`
is the only function that touches openpyxl; `jv_emitter.write_journal_csv()`
(Stage 1b, optional) is the only function that touches the journal CSV.
gnucash_path is READ ONLY everywhere in this package -- no function here
ever opens a write handle on a .gnucash file; account-path validation
(`_validate_accounts_against_book`) only ever calls
gnucash_accounts.read_postable_paths()/read_special_paths()/load_accounts(),
and the Section B/C tie-out (`gnucash_tieout.py`) only ever calls
skill_itr_workbook's parse_gnucash.parse_book() -- never anything that
could write.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import yaml

from .. import gnucash_accounts
from .engine import build_report
from .gnucash_tieout import build_balance_tieout, build_posted_check
from .jv_emitter import ACCOUNT_KEYS, JournalValidationError, build_journals, write_journal_csv
from .mapper import FinancialYearMismatchError, build_input_data
from .parsers import advisory as _advisory_parser
from .parsers import llp_statement as _llp_statement_parser
from .parsers import payment_schedule as _payment_schedule_parser
from .parsers import payout_advice as _payout_advice_parser
from .writer import write_report_workbook
from .xlsx_26as_reader import read_form_26as_tds_credit

# skill_itr_workbook/scripts is a separate package (not importable via the
# agents.* package path) that carries the entities.yaml loader this skill
# reuses rather than re-implementing its own -- same sys.path pattern
# skill_ais_reconcile/agent.py uses for the same reason. `config_path` (an
# existing run() parameter, previously accepted-but-unused on this entry
# path) is repurposed here as the path to that entities.yaml-shaped file:
# each entity's `partner_comp_accounts` field supplies its GnuCash account
# map (see configs.py / bundling/canonical/itr/entities.example.yaml), and
# its generic `extra_items["partner_comp_drivers"][<financial_year>]`
# supplies this skill's own rate/period drivers (firm's tax rate, capital
# rate, TDS section/rate/start-date, etc.) -- there is no dedicated
# tax-rules loader for these values anywhere else in the codebase, and
# entities.yaml's own closed-dataclass shape already carries a generic
# passthrough dict for exactly this kind of skill-specific extension.
_ITR_SCRIPTS = Path(__file__).resolve().parent.parent / "skill_itr_workbook" / "scripts"
if str(_ITR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ITR_SCRIPTS))
import configs  # noqa: E402


def _load_input(input_path: str) -> dict:
    """The ONLY function in this package that touches the filesystem for
    the structured input file. Accepts .yaml/.yml or .json."""
    path = Path(input_path)
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in (".yaml", ".yml"):
        return yaml.safe_load(text)
    if path.suffix.lower() == ".json":
        return json.loads(text)
    raise ValueError(
        f"Unsupported input file type '{path.suffix}'. Supply a .yaml, .yml, "
        "or .json structured input file (see skill.yaml's help text for the shape)."
    )


def _require(value: str, input_name: str, label: str) -> str | None:
    """Return a clear 'ERROR: ...' string naming `input_name` if a REQUIRED
    input is blank/missing, else None. Mirrors this codebase's convention
    (see AIS Reconcile / MF CAS agent.py) of never raising for a
    user-facing problem."""
    if not value or not str(value).strip():
        return f"ERROR: required input '{input_name}' ({label}) was not supplied."
    return None


def _resolve_optional_leg(label: str, path: str, parse_fn, password: str | None = None) -> str:
    """Resolve one OPTIONAL document-backed reconciliation leg to a status
    note. Never used for a required input -- those fail the whole run via
    _require() instead. An absent path degrades the leg to "not
    available", never a zero or a default. A path that IS supplied but
    whose parser is still a Stage 2 NotImplementedError placeholder
    degrades identically, naming that reason instead of crashing."""
    if not path:
        return f"{label}: not available (no document supplied)."
    try:
        parse_fn(path, password)
        return f"{label}: parsed from {path}."
    except NotImplementedError as e:
        return f"{label}: not available ({e})"


def _resolve_llp_leg(path: str, password: str | None) -> tuple[str, dict | None]:
    """Resolve the OPTIONAL L5 (LLP statement of account) leg to a
    (status note, parsed record or None) pair. Unlike _resolve_optional_leg
    (kept as-is for whatever else calls it), this also hands back the
    parsed record on success, since the mapper needs it for
    `interest_on_capital`. `llp_statement.py`'s parser is now real, so a
    genuinely malformed/wrong-content/wrong-password PDF can raise
    NotAnL5DocumentError (or a pdfplumber-level exception) rather than
    NotImplementedError -- both degrade this leg to a "not available"
    note, never an uncaught traceback and never a crash of the whole run."""
    label = "LLP statement of account"
    if not path:
        return f"{label}: not available (no document supplied).", None
    try:
        record = _llp_statement_parser.parse(path, password)
        return f"{label}: parsed from {path}.", record
    except NotImplementedError as e:
        return f"{label}: not available ({e})", None
    except Exception as e:
        return f"{label}: not available (could not parse {path}: {e})", None


def _resolve_schedule_leg(path: str, password: str | None) -> tuple[str, dict | None]:
    """Resolve the OPTIONAL L4 (incentive payment schedule) leg to a
    (status note, parsed record or None) pair -- same degradation contract
    as _resolve_llp_leg. `payment_schedule.py`'s parser is now real (see
    its own module docstring); a genuinely malformed/wrong-content/
    wrong-password PDF raises NotAPaymentScheduleError (or a
    pdfplumber-level exception), both of which degrade this leg to a "not
    available" note, never an uncaught traceback and never a crash of the
    whole run. Absent path degrades identically -- never a fabricated
    figure, never a block on the rest of the run."""
    label = "Incentive payment schedule"
    if not path:
        return f"{label}: not available (no document supplied).", None
    try:
        record = _payment_schedule_parser.parse(path, password)
        return f"{label}: parsed from {path}.", record
    except NotImplementedError as e:
        return f"{label}: not available ({e})", None
    except Exception as e:
        return f"{label}: not available (could not parse {path}: {e})", None


def _resolve_entity_config(entity: str, config_path: str | None) -> tuple["configs.EntityProfile | None", str | None]:
    """Look up `entity` in the entities.yaml-shaped file at `config_path`.
    Returns (profile, None) on success, (None, note) otherwise -- a note
    describing why no entity config is available (no config_path supplied,
    file missing, or entity key not found), never an exception. Absence
    of an entity profile is not itself a run-ending error: it simply means
    `accounts`/`drivers` stay unset, and every account-key/rate that would
    have come from it degrades to its own explicit CANNOT-RECONCILE/ERROR
    downstream (never guessed)."""
    if not config_path:
        return None, "entity config: not available (no config_path supplied)."
    try:
        entities = configs.load_entities(config_path)
    except (OSError, configs.ConfigValidationError) as e:
        return None, f"entity config: could not load {config_path} ({e})."
    profile = entities.get(entity)
    if profile is None:
        return None, f"entity config: {entity!r} not found in {config_path}."
    return profile, None


def _validate_accounts_against_book(accounts: dict, gnucash_path: str) -> list[str]:
    """Validate every configured account path against the GnuCash book at
    `gnucash_path` BEFORE anything is written. Returns a list of "ERROR:
    ..." strings (empty if every configured path is a valid posting
    target) -- one per account key that is missing, or resolves to a
    placeholder/hidden (non-postable) account, each naming the account
    key, the configured path, the reason, and (when a same-leaf-name
    account exists elsewhere in the book) the closest candidate path as a
    rename hint. Read-only: only ever calls
    gnucash_accounts.read_postable_paths()/read_special_paths()/
    load_accounts() -- never opens a write handle, never touches a
    .gnucash.LOG/backup file."""
    postable = gnucash_accounts.read_postable_paths(gnucash_path)
    special = gnucash_accounts.read_special_paths(gnucash_path)
    all_accounts = gnucash_accounts.load_accounts(gnucash_path)
    by_leaf: dict[str, list[str]] = {}
    for acc in all_accounts:
        if acc.path:
            by_leaf.setdefault(acc.leaf, []).append(acc.path)

    errors = []
    for key, path in accounts.items():
        if path in postable:
            continue
        if path in special:
            reason = "is a placeholder/hidden account and cannot be a posting target"
        else:
            reason = "does not exist in the supplied GnuCash book"
        leaf = path.rsplit(":", 1)[-1] if ":" in path else path
        candidates = [p for p in by_leaf.get(leaf, []) if p != path]
        hint = ""
        if candidates:
            hint = f" Closest candidate(s) by leaf name: {', '.join(sorted(candidates))}."
        errors.append(
            f"ERROR: partner_comp_accounts['{key}'] = {path!r} {reason}.{hint}"
        )
    return errors


def _resolve_accounts_for_journal(
    entity_profile: "configs.EntityProfile | None",
    entity: str,
    gnucash_path: str,
) -> tuple[dict, list[str], str | None]:
    """Section 4.3's three degradation rules for the journal leg, given
    that journal_path IS supplied (the caller only calls this when it is).
    Returns (accounts, book_validation_notes, error_or_None):

      - no partner_comp_accounts configured -> error naming the entity and
        every required account key (jv_emitter.ACCOUNT_KEYS).
      - accounts configured, no gnucash_path -> (accounts, [an explicit
        NOTE that paths could not be verified], None).
      - accounts configured, gnucash_path supplied -> validated against the
        book; any bad path returns an error naming it (before any write);
        all-good returns (accounts, [], None).
    """
    accounts = dict(entity_profile.partner_comp_accounts) if entity_profile else {}
    if not accounts:
        return (
            {},
            [],
            "ERROR: journal_path was supplied but entity "
            f"{entity!r} has no partner_comp_accounts configured -- the "
            f"following account keys are required: {', '.join(ACCOUNT_KEYS)}.",
        )
    if not gnucash_path:
        return (
            accounts,
            [
                "NOTE: journal accounts could not be verified against a GnuCash "
                "book -- no gnucash_path was supplied. The journal CSV below uses "
                "the paths configured in entities.yaml as-is."
            ],
            None,
        )
    validation_errors = _validate_accounts_against_book(accounts, gnucash_path)
    if validation_errors:
        return {}, [], "\n".join(validation_errors)
    return accounts, [], None


def _summarize_report(report, output_path: str, journal_line: str = "") -> str:
    """The shared reporting tail for both entry paths: variance WARNINGs,
    undecidable NOTEs, rate-change-suspect WARNINGs, one-off-roundness
    WARNINGs (or the single "all agree" line), the Workbook: line, and the
    Journal CSV: line if a journal was written. Factored out of the
    (pre-existing) structured-input path so the document-driven path
    reuses it verbatim rather than duplicating it."""
    variances = [r for r in report.reconciliation if r.agree is False]
    undecidable = [r for r in report.reconciliation if r.agree is None]
    suspects = len(report.rate_change_suspects)
    suspect_one_offs = [o for o in report.one_offs if o.status == "SUSPECT"]

    lines_out = [
        f"Partner Compensation Reconciliation for FY{report.financial_year} -- "
        f"{len(report.monthly)} month(s), {len(report.cohort_instalments)} cohort "
        "instalment(s).",
    ]
    if variances:
        lines_out.append(
            f"  WARNING: reconciliation variance in {len(variances)} category(ies) "
            "-- see Reconciliation/Exceptions sheets."
        )
    if undecidable:
        lines_out.append(
            f"  NOTE: {len(undecidable)} category(ies) could not be reconciled -- "
            "see Reconciliation/Exceptions sheets for the explicit reason."
        )
    if suspects:
        lines_out.append(
            f"  WARNING: mid-year capital rate change suspected in {suspects} "
            "cohort(s) -- see Capital/Exceptions sheets."
        )
    if suspect_one_offs:
        lines_out.append(
            f"  WARNING: {len(suspect_one_offs)} one-off gross-up(s) failed the "
            "roundness check -- see One-offs/Exceptions sheets."
        )
    if not variances and not undecidable and not suspects and not suspect_one_offs:
        lines_out.append("  All reconciliation categories agree; no exceptions raised.")
    lines_out.append(f"  Workbook: {output_path}")
    if journal_line:
        lines_out.append(journal_line)
    return "\n".join(lines_out)


def run(
    entity: str = "",
    advices_dir: str = "",
    doc_password: str | None = None,
    advisory_path: str = "",
    llp_statement: str = "",
    payment_schedule: str = "",
    gnucash_path: str = "",
    xlsx_26as: str = "",
    output_path: str = "",
    config_path: str | None = None,
    model_override: str | None = None,
    journal_path: str = "",
    input_path: str = "",
) -> str:
    """Skill entry point -- see the module docstring for the two entry
    paths. `input_path`, when supplied, takes the TEST-ONLY structured
    YAML/JSON path (unchanged from before this reshape); it is not part of
    skill.yaml's `inputs:` and never renders in the UI. Otherwise, this
    is the document-driven path described in _run_from_documents().
    """
    if input_path:
        return _run_from_structured_input(
            input_path=input_path,
            output_path=output_path,
            config_path=config_path,
            model_override=model_override,
            journal_path=journal_path,
        )
    return _run_from_documents(
        entity=entity,
        advices_dir=advices_dir,
        doc_password=doc_password,
        advisory_path=advisory_path,
        llp_statement=llp_statement,
        payment_schedule=payment_schedule,
        gnucash_path=gnucash_path,
        xlsx_26as=xlsx_26as,
        output_path=output_path,
        config_path=config_path,
        model_override=model_override,
        journal_path=journal_path,
    )


def _run_from_documents(
    *,
    entity: str,
    advices_dir: str,
    doc_password: str | None,
    advisory_path: str,
    llp_statement: str,
    payment_schedule: str,
    gnucash_path: str,
    xlsx_26as: str,
    output_path: str,
    config_path: str | None,
    model_override: str | None,
    journal_path: str,
) -> str:
    """Document-driven entry point (the skill.yaml-facing path).

    `parsers/advisory.py` (L3), `parsers/payout_advice.py` (L1),
    `parsers/llp_statement.py` (L5) and `parsers/payment_schedule.py` (L4)
    are all implemented (see their own module docstrings). `entity`,
    `gnucash_path` and `xlsx_26as` do not have a parser under parsers/ at
    all in this build -- they read an existing structured format rather
    than parse a free-form PDF. `xlsx_26as` is read by
    xlsx_26as_reader.read_form_26as_tds_credit() (Section A), feeding
    external["form_26as_total_credit"]. `gnucash_path` now feeds a real,
    read-only books tie-out (gnucash_tieout.py, Sections B and C: a
    posted-already check plus a per-account balance tie-out) once the
    report is built; it is separately used later, read-only, to validate
    configured account paths before a journal is written.

    Required inputs (entity, advices_dir, advisory_path) missing fail loud
    by name, before any parsing is attempted. Optional inputs
    (llp_statement, payment_schedule, gnucash_path, xlsx_26as) resolve to
    a per-leg status note FIRST, independent of whether the required
    documents can be parsed yet, so their "not available" degrade
    behaviour is observable even while a required leg fails. The two
    required documents then attempt to parse; ANY exception from that
    attempt (a document that fails its content-dispatch check, or
    pdfplumber choking on an unreadable/malformed/wrong-password PDF) is
    caught and turned into an "ERROR: ..." string naming the document and
    the underlying reason -- this function never raises for a user-facing
    problem. The optional L5 and L4 legs degrade the same way (see
    _resolve_llp_leg / _resolve_schedule_leg) rather than aborting the
    run. When the L4 schedule parses, its record takes precedence over
    the payout advices for `gross_share_of_profit` / `firm_tax_on_sop` /
    `firm_tax_others` inside mapper.build_input_data() -- any disagreement
    with the payout advices is reported as a mapper diagnostic, never
    blocking the run.

    Once both required documents (and the optional L5 leg, if resolvable)
    parse, their records are assembled by mapper.build_input_data() into
    engine.build_report()'s input shape (a financial-year disagreement
    between documents -- mapper.FinancialYearMismatchError, or plain
    ValueError if no year at all -- becomes an "ERROR: ..." string here,
    not a traceback), the reconciliation is computed, and the workbook is
    written (creating output_path's parent directories). If journal_path
    is supplied, the entity's `partner_comp_accounts` (from the
    entities.yaml-shaped file at config_path) are resolved and -- if
    gnucash_path is also supplied -- validated against that book (see
    _validate_accounts_against_book) BEFORE anything is written; a missing
    config or an invalid account path is an "ERROR: ..." string, never a
    partially-written journal. A JournalValidationError from
    jv_emitter.build_journals() is likewise turned into an "ERROR: ..."
    string. The final summary reuses _summarize_report() (the same
    reporting tail the structured-input path uses) plus the optional
    legs' status notes. Never opens a write handle on gnucash_path --
    read-only account-path validation and the Section B/C books tie-out
    only.
    """
    for value, name, label in (
        (entity, "entity", "Entity"),
        (advices_dir, "advices_dir", "Monthly partner payout certificates / payslips"),
        (advisory_path, "advisory_path", "Compensation advisory / target compensation advice"),
    ):
        err = _require(value, name, label)
        if err:
            return err

    advices_path = Path(advices_dir)
    if not advices_path.is_dir():
        return f"ERROR: advices_dir does not point to a directory: {advices_dir}"
    advice_pdfs = sorted(p for p in advices_path.iterdir() if p.suffix.lower() == ".pdf")
    if not advice_pdfs:
        return f"ERROR: no PDF files found in advices_dir: {advices_dir}"

    # Optional legs resolve first -- independent of whether the required
    # legs below can be parsed yet in this build.
    llp_note, llp_record = _resolve_llp_leg(llp_statement, doc_password)
    schedule_note, schedule_record = _resolve_schedule_leg(payment_schedule, doc_password)
    # gnucash_note starts as a placeholder here because the required
    # documents (advisory/advices) haven't been parsed yet, so `report`
    # (needed by build_posted_check/build_balance_tieout below) doesn't
    # exist yet -- this placeholder is only ever seen by an early-exit
    # ERROR path below. Once `report` is built successfully, this slot is
    # overwritten in place (via _gnucash_note_idx) with the real posted-
    # check note; the balance tie-out results are appended straight onto
    # report.reconciliation instead of surfacing as a single note.
    if not gnucash_path:
        gnucash_note = "GnuCash books tie-out: not available (no book supplied)."
    else:
        gnucash_note = f"GnuCash books tie-out: pending ({gnucash_path} not yet checked)."
    xlsx_note, form_26as_total_credit = read_form_26as_tds_credit(xlsx_26as)
    optional_notes = [llp_note, schedule_note, gnucash_note, xlsx_note]
    _gnucash_note_idx = 2

    # Required legs: the Advisory letter, then every monthly payout advice.
    # Any exception here (Stage 2 placeholder, content-dispatch mismatch,
    # or a pdfplumber-level failure to open/read the PDF) is a user-facing
    # problem, never a crash -- turned into an "ERROR: ..." string naming
    # the document and the reason.
    try:
        advisory_record = _advisory_parser.parse(advisory_path, doc_password)
    except Exception as e:
        lines = [
            f"ERROR: could not parse the Compensation advisory ({advisory_path}): {e}",
            "  Optional-leg status (unaffected by the error above):",
        ]
        lines.extend(f"  - {note}" for note in optional_notes)
        return "\n".join(lines)

    advice_records = []
    for pdf in advice_pdfs:
        try:
            advice_records.append(_payout_advice_parser.parse(str(pdf), doc_password))
        except Exception as e:
            lines = [
                f"ERROR: could not parse payout advice ({pdf}): {e}",
                "  Optional-leg status (unaffected by the error above):",
            ]
            lines.extend(f"  - {note}" for note in optional_notes)
            return "\n".join(lines)

    # Both required documents (and the optional L5 leg, if it resolved)
    # parsed successfully. Resolve the entity's config (accounts/drivers),
    # assemble the parsed records into engine.build_report()'s input
    # shape, compute the reconciliation, and write the workbook.
    entity_profile, entity_config_note = _resolve_entity_config(entity, config_path)
    if entity_config_note:
        optional_notes.append(entity_config_note)

    if entity_profile is not None:
        drivers_by_fy = entity_profile.extra_items.get("partner_comp_drivers") or {}
    else:
        drivers_by_fy = {}

    # firm_name is the FIRM's name (the LLP/partnership that pays out the
    # compensation), never the taxpayer's own name -- entity_profile.name
    # identifies the partner whose return/books this run is for, which is
    # a different entity entirely and must never be used here. The payment
    # schedule (L4) prints the firm's own name on its letterhead, so it is
    # the preferred source; a payout advice (L1) also prints it, so the
    # first advice record with a non-empty entity_name is the fallback.
    # Absent both, firm_name is "" -- never silently defaulted to the
    # taxpayer's name.
    firm_name = ""
    if schedule_record and schedule_record.get("entity_name"):
        firm_name = schedule_record["entity_name"]
    else:
        for rec in advice_records:
            if rec.get("entity_name"):
                firm_name = rec["entity_name"]
                break

    try:
        data = build_input_data(
            advisory_record=advisory_record,
            advice_records=advice_records,
            llp_record=llp_record,
            schedule_record=schedule_record,
            firm_name=firm_name,
        )
    except (FinancialYearMismatchError, ValueError) as e:
        lines = [f"ERROR: {e}", "  Optional-leg status (unaffected by the error above):"]
        lines.extend(f"  - {note}" for note in optional_notes)
        return "\n".join(lines)

    mapper_diagnostics = data.pop("_diagnostics", [])
    drivers = drivers_by_fy.get(data["financial_year"])
    if drivers is not None:
        data["drivers"] = drivers
    # Section A: feed the 26AS reader's result into the existing
    # external["form_26as_total_credit"] reconciliation leg (engine.py's
    # field_or_reason() treats a None value the same as the key being
    # absent, so this is safe to set unconditionally -- a failed/absent
    # read degrades the leg exactly as before, never a silent 0.0).
    data.setdefault("external", {})
    data["external"]["form_26as_total_credit"] = form_26as_total_credit

    try:
        report = build_report(data)
    except KeyError as e:
        return f"ERROR: input is missing required field {e}"

    # Section B/C: GnuCash tie-out. Both legs reuse jv_emitter.build_journals
    # to compute this run's implied journal (purely, no I/O) and compare it
    # read-only against gnucash_path -- see gnucash_tieout.py. Neither leg
    # can raise: every unavailable input (no book, no accounts configured,
    # an unresolvable account path, an unbuildable journal) degrades to an
    # explicit note/CANNOT-RECONCILE result rather than aborting the run.
    accounts_for_tieout = (
        dict(entity_profile.partner_comp_accounts)
        if (entity_profile and entity_profile.partner_comp_accounts) else {}
    )
    posted_check, gnucash_note = build_posted_check(
        report, accounts_for_tieout, gnucash_path, report.financial_year,
    )
    optional_notes[_gnucash_note_idx] = gnucash_note
    report.reconciliation.extend(
        build_balance_tieout(report, accounts_for_tieout, gnucash_path, report.financial_year)
    )

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_report_workbook(report, str(out_path), posted_check=posted_check)

    journal_line = ""
    account_notes: list[str] = []
    if journal_path:
        accounts, account_notes, accounts_error = _resolve_accounts_for_journal(
            entity_profile, entity, gnucash_path,
        )
        if accounts_error:
            return accounts_error
        try:
            journals = build_journals(report, accounts)
        except JournalValidationError as e:
            return f"ERROR: {e}"
        write_journal_csv(journals, journal_path)
        row_count = sum(len(j.splits) for j in journals)
        journal_line = (
            f"  Journal CSV: {journal_path} ({len(journals)} transaction(s), "
            f"{row_count} row(s))."
        )

    summary = _summarize_report(report, output_path, journal_line)
    lines = [summary, "  Optional-leg status:"]
    lines.extend(f"  - {note}" for note in optional_notes)
    lines.extend(f"  - {note}" for note in account_notes)
    lines.extend(f"  - {note}" for note in mapper_diagnostics)
    return "\n".join(lines)


def _run_from_structured_input(
    *,
    input_path: str,
    output_path: str,
    config_path: str | None,
    model_override: str | None,
    journal_path: str,
) -> str:
    """TEST-ONLY entry path (see module docstring). Read the structured
    YAML/JSON input for one financial year, compute the reconciliation
    (engine.build_report), write the 10-sheet workbook
    (writer.write_report_workbook) to output_path, and return a text
    summary. Never raises for a user-facing problem -- returns an
    "ERROR: ..." string instead, mirroring skill_mf_cas's convention.

    journal_path is optional (Stage 1b). When non-empty, also builds and
    writes the GnuCash multi-split journal CSV implied by the reconciled
    year (jv_emitter.build_journals / write_journal_csv) to that path, and
    mentions it (with transaction/row counts) in the returned summary. When
    empty, behaviour is byte-identical to before Stage 1b existed -- no CSV
    is written.
    """
    in_path = Path(input_path)
    if not in_path.is_file():
        return f"ERROR: file not found: {input_path}"

    try:
        data = _load_input(str(in_path))
    except (ValueError, OSError) as e:
        return f"ERROR: {e}"
    except Exception as e:  # malformed YAML/JSON
        return f"ERROR: could not parse '{input_path}' as structured input: {e}"

    if not isinstance(data, dict) or "financial_year" not in data:
        return (
            "ERROR: input file must be a mapping with at least a "
            "'financial_year' key (see skill.yaml's help text for the shape)."
        )

    try:
        report = build_report(data)
    except KeyError as e:
        return f"ERROR: input is missing required field {e}"

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    write_report_workbook(report, str(out_path))

    journal_line = ""
    if journal_path:
        accounts = data.get("accounts") or {}
        try:
            journals = build_journals(report, accounts)
        except JournalValidationError as e:
            return f"ERROR: {e}"
        write_journal_csv(journals, journal_path)
        row_count = sum(len(j.splits) for j in journals)
        journal_line = (
            f"  Journal CSV: {journal_path} ({len(journals)} transaction(s), "
            f"{row_count} row(s))."
        )

    return _summarize_report(report, output_path, journal_line)
