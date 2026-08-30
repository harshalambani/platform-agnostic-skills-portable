"""
agent.py -- Partner Compensation Reconciliation. DIRECT mode, no LLM, no
network.

Stage 1 of this skill (see AGENT.md): the computation engine, the workbook
writer, and the tests. Input arrives as a structured YAML or JSON file
describing one financial year's drivers, monthly payouts, incentive
cohorts, the Advisory's own figures, and the external (bank/26AS/return)
figures to reconcile against -- see skill.yaml's help text and
tests/fixtures for the exact shape.

Stage 2 (NOT this build) is the PDF parsers under parsers/ -- they are
guarded placeholders that raise NotImplementedError until a real specimen
of each document exists. See parsers/__init__.py.

Architecture mirrors skill_mf_cas: `_load_input` below is the ONLY
function in this package that touches the filesystem for the input path.
`engine.build_report()` is pure (no I/O); `writer.write_report_workbook()`
is the only function that touches openpyxl.
"""
from __future__ import annotations

import json
from pathlib import Path

import yaml

from .engine import build_report
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


def run(
    input_path: str = "",
    output_path: str = "",
    config_path: str | None = None,
    model_override: str | None = None,
) -> str:
    """Read the structured YAML/JSON input for one financial year, compute
    the reconciliation (engine.build_report), write the 10-sheet workbook
    (writer.write_report_workbook) to output_path, and return a text
    summary. Never raises for a user-facing problem -- returns an
    "ERROR: ..." string instead, mirroring skill_mf_cas's convention.
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
    return "\n".join(lines_out)
