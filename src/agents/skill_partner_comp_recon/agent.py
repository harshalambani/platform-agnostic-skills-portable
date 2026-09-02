"""
agent.py -- Partner Compensation Reconciliation. DIRECT mode, no LLM, no
network.

Stage 1 of this skill (see AGENT.md): the computation engine, the workbook
writer, and the tests. Stage 2 is the PDF parsers under parsers/ -- most
are guarded placeholders that raise NotImplementedError until a real
specimen of each document exists; `payout_advice.py` (L1) and
`advisory.py` (L3) are now implemented. See parsers/__init__.py.

run() has two entry paths:

  - Document-driven (the skill.yaml-facing path, and the only one exposed
    in the UI): entity/advices_dir/doc_password/advisory_path/
    llp_statement/gnucash_path/xlsx_26as. Required inputs missing fail
    loud, naming the input. Every OPTIONAL input (llp_statement,
    gnucash_path, xlsx_26as) that is absent -- or present but backed by a
    parser that is still a Stage 2 placeholder -- degrades its own
    reconciliation leg to an explicit "not available" note; it never
    fails the run, and never substitutes a zero or a default figure. The
    two REQUIRED documents (advices_dir, advisory_path) now parse for
    real; a document that fails to open/parse (an unreadable/malformed
    PDF, a wrong password, or content that doesn't match the expected L1/
    L3 layout) still fails the whole run loud, naming the document and the
    reason -- see _run_from_documents()'s docstring. Parsing both required
    documents successfully does not yet assemble a workbook (that
    remaining wiring is out of this build's scope, see the same
    docstring).
  - Structured-input (input_path): TEST-ONLY. Retained so the existing
    engine/writer/jv_emitter test suite keeps exercising build_report()
    directly without needing real PDF specimens. Deliberately absent from
    skill.yaml's `inputs:` so it never renders in the UI (see
    tests/test_skill_partner_comp_recon.py's manifest-shape guard test).

Architecture mirrors skill_mf_cas: `_load_input` is the ONLY function in
this package that touches the filesystem for the structured input path.
`engine.build_report()` is pure (no I/O); `writer.write_report_workbook()`
is the only function that touches openpyxl; `jv_emitter.write_journal_csv()`
(Stage 1b, optional) is the only function that touches the journal CSV.
gnucash_path is READ ONLY everywhere in this package -- no function here
ever opens a write handle on a .gnucash file.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from .engine import build_report
from .jv_emitter import JournalValidationError, build_journals, write_journal_csv
from .parsers import advisory as _advisory_parser
from .parsers import llp_statement as _llp_statement_parser
from .parsers import payout_advice as _payout_advice_parser
from .writer import write_report_workbook


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


def run(
    entity: str = "",
    advices_dir: str = "",
    doc_password: str | None = None,
    advisory_path: str = "",
    llp_statement: str = "",
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
    gnucash_path: str,
    xlsx_26as: str,
    output_path: str,
    config_path: str | None,
    model_override: str | None,
    journal_path: str,
) -> str:
    """Document-driven entry point (the skill.yaml-facing path).

    `parsers/advisory.py` (L3) and `parsers/payout_advice.py` (L1) are now
    implemented (see their own module docstrings); `parsers/llp_statement.py`
    is still a guarded NotImplementedError placeholder pending a real,
    de-identified specimen of that document (see AGENT.md's "Stage 2"
    section). `entity`, `gnucash_path` and `xlsx_26as` do not have a parser
    under parsers/ at all in this build (gnucash_path/xlsx_26as read an
    existing format rather than parse a free-form PDF, and are wired here
    as always-degraded legs rather than invented reader logic);
    `parsers/payment_schedule.py` has no corresponding input in this
    reshaped manifest at all -- the incentive payment-schedule leg remains
    CANNOT RECONCILE, unchanged from before.

    Required inputs (entity, advices_dir, advisory_path) missing fail loud
    by name, before any parsing is attempted. Optional inputs
    (llp_statement, gnucash_path, xlsx_26as) resolve to a per-leg status
    note FIRST, independent of whether the required documents can be
    parsed yet, so their "not available" degrade behaviour is observable
    even while a required leg fails. The two required documents then
    attempt to parse; ANY exception from that attempt (a Stage 2
    NotImplementedError placeholder, a document that fails its
    content-dispatch check, or pdfplumber choking on an unreadable/
    malformed/wrong-password PDF) is caught and turned into an "ERROR: ..."
    string naming the document and the underlying reason -- this function
    never raises for a user-facing problem. Once both required documents
    parse successfully, this function reports that fact (and the optional
    legs' status) but does NOT yet assemble their figures into
    engine.build_report()'s input shape or write a workbook -- wiring the
    parsed L1/L3 records (plus the still-placeholder L2/LLP-statement/
    payment-schedule legs) into that dict shape is a further stage, out of
    this PR's scope. Never opens a write handle on gnucash_path --
    read-only tie-out only, and only once implemented.
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
    llp_note = _resolve_optional_leg(
        "LLP statement of account", llp_statement, _llp_statement_parser.parse, doc_password,
    )
    if not gnucash_path:
        gnucash_note = "GnuCash books tie-out: not available (no book supplied)."
    else:
        gnucash_note = (
            "GnuCash books tie-out: not available (reader not yet implemented in this "
            f"build; {gnucash_path} was supplied but never opened -- this skill only ever "
            "opens a GnuCash book read-only, and only once this leg is implemented)."
        )
    if not xlsx_26as:
        xlsx_note = "26AS TDS-credit tie-out: not available (no workbook supplied)."
    else:
        xlsx_note = (
            "26AS TDS-credit tie-out: not available (reader not yet implemented in this "
            f"build; {xlsx_26as} was supplied but not read)."
        )
    optional_notes = [llp_note, gnucash_note, xlsx_note]

    # Required legs: the Advisory letter, then every monthly payout advice.
    # Any exception here (Stage 2 placeholder, content-dispatch mismatch,
    # or a pdfplumber-level failure to open/read the PDF) is a user-facing
    # problem, never a crash -- turned into an "ERROR: ..." string naming
    # the document and the reason.
    try:
        _advisory_parser.parse(advisory_path, doc_password)
    except Exception as e:
        lines = [
            f"ERROR: could not parse the Compensation advisory ({advisory_path}): {e}",
            "  Optional-leg status (unaffected by the error above):",
        ]
        lines.extend(f"  - {note}" for note in optional_notes)
        return "\n".join(lines)

    for pdf in advice_pdfs:
        try:
            _payout_advice_parser.parse(str(pdf), doc_password)
        except Exception as e:
            lines = [
                f"ERROR: could not parse payout advice ({pdf}): {e}",
                "  Optional-leg status (unaffected by the error above):",
            ]
            lines.extend(f"  - {note}" for note in optional_notes)
            return "\n".join(lines)

    # Both required documents parsed successfully. Assembling their
    # figures into engine.build_report()'s input shape (and writing a
    # workbook) is a further stage, out of this PR's scope -- see this
    # function's docstring.
    lines = ["Partner Compensation Reconciliation: documents parsed successfully."]
    lines.extend(f"  - {note}" for note in optional_notes)
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
