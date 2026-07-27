"""
agent.py -- AIS Reconcile -- DIRECT mode, no LLM.

Orchestrates the four reconciliations built in Phases A-D (all pure,
no-I/O modules) around one piece of I/O-heavy plumbing: resolving the
right entity from the encrypted AIS export's own filename, deriving its
decrypt password, and (optionally) pulling in a 26AS workbook and/or a
GnuCash book to tie out against. This module is the one place in the
skill allowed to touch the filesystem -- decrypt.py / normalize.py /
reconcile.py / reconcile_26as.py / reconcile_books.py / feedback.py /
excel_writer.py are all pure functions over already-loaded data.

Entity resolution (no chicken-and-egg with the encrypted PAN): the AIS
export's own filename carries a *masked* PAN prefix (e.g.
"XXXPA3059X_2025-26_AIS.json" -> prefix "XXXPA3059X", FY "2025-26").
We load entities.yaml, mask each candidate's real PAN the same way
("XXX" + pan[3:9] + "X"), and match. No entity_key input is needed (or
possible) up front -- if no entity's masked PAN matches the prefix, the
run fails loud with an ERROR string rather than guessing.

v1 scope note (mirrors reconcile_books.py's own docstring): the AIS side
of the books tie-out only looks at tdsTcs-reported income, to avoid
double-counting against sft/other AIS sections that may describe the
same underlying transaction from a different angle.

Feedback suggestions (Phase D) are ADVISORY ONLY -- see feedback.py's
module docstring. Nothing in this skill submits anything to the AIS
portal on the taxpayer's behalf.
"""
from __future__ import annotations

import sys
from pathlib import Path

from agents.skill_ais_reconcile import decrypt as D
from agents.skill_ais_reconcile import normalize as N
from agents.skill_ais_reconcile import reconcile as R
from agents.skill_ais_reconcile import reconcile_26as as R26
from agents.skill_ais_reconcile import reconcile_books as RB
from agents.skill_ais_reconcile import feedback as F
from agents.skill_ais_reconcile import excel_writer as XL

# skill_itr_workbook/scripts is a separate package (not importable via the
# agents.* package path) that carries the entity/mapping/rules/26AS/gnucash
# loaders this skill reuses rather than re-implementing -- same sys.path
# pattern skill_itr_workbook/agent.py uses for its own scripts/ dir.
_ITR_SCRIPTS = Path(__file__).resolve().parent.parent / "skill_itr_workbook" / "scripts"
if str(_ITR_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_ITR_SCRIPTS))
import configs  # noqa: E402
import parse_gnucash as pg  # noqa: E402
import rules as rules_engine  # noqa: E402
import as26 as as26_engine  # noqa: E402
from openpyxl.utils.exceptions import InvalidFileException  # noqa: E402


def _masked_prefix(pan: str) -> str:
    """Mirror the AIS portal's own PAN-masking convention for the filename
    prefix: first 3 + last 1 char literal "X", middle 6 chars of the real
    PAN kept -- e.g. real PAN "ABCPA3059X" -> masked "XXXPA3059X"."""
    return "XXX" + pan[3:9] + "X"


def _resolve_entity_from_prefix(entities_path: str, prefix: str) -> "configs.EntityProfile | str":
    """Find the entity whose masked PAN matches `prefix`. Returns the
    EntityProfile on success, or an ERROR string on failure (missing
    entities.yaml, or no entity's PAN masks to this prefix) -- deliberately
    fail-loud, never a best-effort/default profile, since guessing the
    wrong entity here would decrypt with the wrong password anyway."""
    try:
        entities = configs.load_entities(entities_path)
    except (OSError, configs.ConfigValidationError) as e:
        return (
            f"ERROR: could not load entities.yaml at "
            f"{Path(entities_path).resolve()} ({e})"
        )

    for entity in entities.values():
        if not entity.pan or len(entity.pan) < 10:
            continue
        if _masked_prefix(entity.pan) == prefix:
            return entity

    return (
        f"ERROR: no entity in {Path(entities_path).resolve()} has a PAN matching "
        f"the masked prefix {prefix!r} from the AIS filename -- add/check the "
        f"entity's pan in entities.yaml (don't guess which entity this is)."
    )


def run(
    ais_path: str,
    output_path: str,
    gnucash_path: str = None,
    xlsx_path: str = None,
    config_path: str = "config.yaml",
    model_override: str = None,
    entities_path: str = "Data/itr/entities.yaml",
    rules_dir: str = "Data/itr/rules",
) -> str:
    """
    Decrypt and reconcile an encrypted AIS JSON export against itself
    (Phase A, always), an optional 26AS workbook (Phase B), and an optional
    GnuCash book (Phase C), then derive advisory portal-feedback
    suggestions from the deltas (Phase D) and write everything to a single
    .xlsx workbook at output_path. Deterministic, no LLM -- config_path /
    model_override are accepted for skill-runner-interface symmetry with
    the other skills but are unused here.

    Entity resolution has no explicit entity_key input: the AIS export's
    own filename carries a masked PAN prefix (see module docstring), which
    is matched against entities.yaml's `pan` field. No match -> a clear
    ERROR string (this skill never guesses which taxpayer an AIS belongs
    to).

    entities_path/rules_dir are keyword-only conveniences (mirrors
    skill_itr_workbook's own run() params) defaulting to the production
    Data/itr/ convention paths; skill.yaml anchors both via the
    {data_root} run_args token so a run reads the same Data/itr/... in
    both source and frozen builds.

    gnucash_path, when supplied, must be the book for the SAME FY as the
    AIS export -- if it has no transactions in that FY window, the summary
    is prefixed with a loud WARNING (Phase C's tie-out sheet still gets
    written, so the mismatch is visible in both places, but the numbers in
    it are almost certainly meaningless for a wrong-year book).

    xlsx_path, when supplied, must be a 26AS skill output workbook (Part I
    sheet) for the same entity/year -- feeds the TDS-credit tie-out.
    """
    basename = Path(ais_path).stem
    parts = basename.split("_")
    if len(parts) < 2:
        return (
            f"ERROR: AIS filename {Path(ais_path).name!r} doesn't match the expected "
            f"<maskedPAN>_<FY>_... convention (e.g. XXXPA3059X_2025-26_AIS.json) -- "
            "can't infer the entity or the FY from it."
        )
    prefix, year_key = parts[0], parts[1]

    entity = _resolve_entity_from_prefix(entities_path, prefix)
    if isinstance(entity, str):
        return entity

    date_iso = entity.dob if entity.status == "Individual" else entity.doi
    if not date_iso:
        date_field = "dob" if entity.status == "Individual" else "doi"
        return (
            f"ERROR: entity {entity.key!r} ({entity.status}) has no {date_field} set in "
            "entities.yaml -- can't derive the AIS decrypt password without it."
        )
    try:
        password = D.derive_password_from_iso_date(entity.pan, date_iso)
    except ValueError as e:
        return f"ERROR: entity {entity.key!r}'s {date_iso!r} is not a valid ISO date ({e})."

    try:
        raw_text = Path(ais_path).read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        return f"ERROR: could not read AIS file {ais_path!r}: {e}"

    try:
        ais_dict = D.decrypt_ais(raw_text, password)
    except D.AisDecryptError as e:
        return (
            f"ERROR: could not decrypt {Path(ais_path).name!r} for entity "
            f"{entity.key!r}: {e}"
        )

    normalized = N.normalize(ais_dict)
    report = R.reconcile_internal(normalized)

    summary_lines = [
        f"Entity: {entity.name} ({entity.key}) -- FY {year_key}",
        f"AIS-internal: {report.flagged_element_count} element(s) flagged "
        f"(of {len(report.elements)}); total TDS credit reported {report.total_tds_credit}.",
    ]

    as26_report = None
    if xlsx_path:
        try:
            as26_data = as26_engine.parse_as26_workbook(xlsx_path)
        except (OSError, KeyError, InvalidFileException) as e:
            summary_lines.append(f"26AS: workbook unreadable ({e}) -- tie-out skipped.")
        else:
            tds_sections = {}
            rules_search_dirs = [rules_dir, rules_engine.canonical_rules_dir()]
            try:
                rules = rules_engine.load_rules(rules_search_dirs, year_key)
                tds_sections = rules.common.get("tds_sections", {})
            except rules_engine.RulesError as e:
                summary_lines.append(
                    f"26AS: rules config unavailable for FY {year_key} ({e}) -- "
                    "per-category tie-out skipped, aggregate/quarter tie-out still runs."
                )
            as26_report = R26.reconcile_ais_vs_26as(normalized, as26_data, tds_sections)
            agg = "DIFFERS" if as26_report.flag_aggregate_mismatch else "matches"
            summary_lines.append(
                f"26AS: aggregate TDS {agg} (AIS {as26_report.total_ais_tds_credit} vs "
                f"26AS {as26_report.total_26as_tds_deposited}); "
                f"{len(as26_report.flagged_quarters)} quarter(s) and "
                f"{len(as26_report.flagged_categories)} categor(y/ies) flagged."
            )

    books_report = None
    fy_warning = None
    if gnucash_path:
        try:
            book = pg.parse_book(gnucash_path)
        except Exception as e:  # noqa: BLE001 -- gnucash XML parse errors vary by cause
            summary_lines.append(f"Books: GnuCash book unreadable ({e}) -- tie-out skipped.")
        else:
            if not pg.fy_transactions(book, year_key):
                fy_warning = (
                    f"WARNING: the GnuCash book has no transactions in FY {year_key} "
                    "(the AIS period). The books reconciliation is almost certainly "
                    f"comparing the wrong year -- select the FY{year_key} book."
                )

            mapping_path = Path(entities_path).parent / "mappings" / f"{entity.key}.mapping.yaml"
            if not mapping_path.is_file():
                summary_lines.append(
                    f"Books: no mapping file found at {mapping_path} for entity "
                    f"{entity.key!r} -- tie-out skipped."
                )
            else:
                try:
                    mapping = configs.load_mapping(mapping_path)
                except configs.MappingValidationError as e:
                    summary_lines.append(f"Books: mapping file invalid ({e}) -- tie-out skipped.")
                else:
                    books_report = RB.reconcile_ais_vs_books(report, book, mapping, year_key)
                    tds_flag = "DIFFERS" if books_report.flag_tds_mismatch else "matches"
                    summary_lines.append(
                        f"Books: {len(books_report.flagged_categories)} categor(y/ies) flagged, "
                        f"TDS {tds_flag} (AIS {books_report.ais_tds_credit} vs "
                        f"books {books_report.books_tds_credit})."
                    )

    suggestions = F.suggest_feedback(report, books=books_report, as26=as26_report)
    summary_lines.append(f"Feedback: {len(suggestions)} advisory suggestion(s) -- review before acting.")

    XL.write_reco_workbook(
        report, output_path,
        report_26as=as26_report, report_books=books_report, suggestions=suggestions,
    )

    if fy_warning:
        summary_lines.insert(1, fy_warning)

    return "\n".join(summary_lines)
