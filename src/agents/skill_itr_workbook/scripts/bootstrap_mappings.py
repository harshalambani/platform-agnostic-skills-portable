"""
bootstrap_mappings.py -- LOCAL-ONLY dev helper (plan section 4.1's learning
loop, run once per real entity to seed a mapping file from scratch).

Not part of the deterministic skill run (agent.py never imports this): it's
a standalone CLI you run yourself, locally, against the real HTML+book
pair for one entity. It treats every leaf as unmapped (there is no mapping
file yet), attempts an LLM suggestion for each (scripts/suggest.py --
degrades to no suggestion with no endpoint configured), and writes a
proposed-mappings snippet you review and rename into
Data/itr/mappings/<entity>.mapping.yaml yourself.

This script NEVER writes into Data/itr/mappings/ directly and never
auto-tags anything -- every suggestion is marked suggested_by_llm and
still needs a human's REPLACE_ME-or-approve pass (plan section 4.1:
"user approves/edits the suggestion into the mapping file").

Usage (from the repo root, with the venv active):
    python src/agents/skill_itr_workbook/scripts/bootstrap_mappings.py \\
        Data/GNUCashReports/HarshalAmbani2425.html \\
        Data/GNUCashReports/HarshalAmbani2425.gnucash \\
        Data/itr/mappings/HarshalAmbani.proposed-mappings.yaml
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from configs import MappingLoadResult  # noqa: E402
from mapping import proposed_mapping_snippet, resolve_tree  # noqa: E402
from parse_eguile import parse_file  # noqa: E402
import suggest  # noqa: E402


def bootstrap(bs_html: str, config_path: str = "config.yaml", model_override: str | None = None):
    """Resolve `bs_html` against an EMPTY mapping (so every leaf is
    unmapped), attempt an LLM suggestion for each, and return the
    ResolutionResult + the snippet text. Never touches book_file directly
    -- the HTML tree alone has every leaf that needs a tag; book_file is
    only relevant for the CG-lot/dividend detail added in later batches."""
    tree = parse_file(bs_html)
    empty_mapping = MappingLoadResult(entries={}, warnings=[])
    result = resolve_tree(tree, empty_mapping)
    suggestions = suggest.suggest_for_unmapped(result.unmapped, result.resolved, config_path, model_override)
    snippet = proposed_mapping_snippet(result, suggestions)
    return result, snippet


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bs_html", help="Path to the real entity's eguile Balance Sheet HTML export")
    parser.add_argument("book_file", nargs="?", default=None, help="Path to the matching .gnucash (unused this batch, accepted for symmetry)")
    parser.add_argument("output_yaml", help="Where to write the proposed-mappings snippet")
    parser.add_argument("--config", default="config.yaml", help="config.yaml for the optional LLM endpoint")
    parser.add_argument("--model", default=None, help="Model override")
    args = parser.parse_args()

    result, snippet = bootstrap(args.bs_html, args.config, args.model)
    Path(args.output_yaml).write_text(snippet, encoding="utf-8")
    print(f"{args.bs_html}: {len(result.unmapped)} unmapped account(s) -- snippet written to {args.output_yaml}")
    print("Review it, fill in every REPLACE_ME tag, and paste the entries into "
          "Data/itr/mappings/<entity>.mapping.yaml yourself -- nothing here writes to that file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
