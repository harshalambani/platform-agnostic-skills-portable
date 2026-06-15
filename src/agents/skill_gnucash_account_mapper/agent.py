#!/usr/bin/env python3
"""
GnuCash Account Mapper
Apply mapping rules to canonical CSV, populate Account column.

Public surface:
    run()            — PA Skills UI entry point. Chains xml_extractor →
                       mapping_generator → account mapper in one pass.
    map_accounts()   — apply a pre-built mapping YAML to a canonical CSV.
"""

import csv
import re
import sys
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


# ---------------------------------------------------------------------------
# Core matching helpers
# ---------------------------------------------------------------------------

def load_mapping_yaml(yaml_path: str) -> dict:
    """Load mapping rules from YAML."""
    with open(yaml_path, 'r', encoding='utf-8') as f:
        return yaml.safe_load(f)


def match_rule(
    description: str,
    rules: List[dict],
) -> Tuple[Optional[str], str, Optional[str], str]:
    """
    Try to match description against rules.
    Return: (account, confidence_level, pattern_matched, reason)
    """
    if not rules or not description:
        return None, 'none', None, 'No pattern match'

    for rule in rules:
        patterns = rule.get('patterns', [])
        for pattern in patterns:
            try:
                if re.search(pattern, description, re.IGNORECASE):
                    account = rule.get('account', '')
                    confidence = rule.get('confidence', 'medium')
                    reason = rule.get('reason', f'Pattern matched: {pattern}')
                    return account, confidence, pattern, reason
            except re.error:
                # Fallback to exact match if regex fails
                if pattern.lower() in description.lower():
                    account = rule.get('account', '')
                    confidence = rule.get('confidence', 'medium')
                    reason = rule.get('reason', f'Pattern matched: {pattern}')
                    return account, confidence, pattern, reason

    return None, 'none', None, 'No pattern match'


# ---------------------------------------------------------------------------
# Core mapping function
# ---------------------------------------------------------------------------

def map_accounts(
    canonical_csv_path: str,
    mapping_yaml_path: str,
    output_mapped_csv: str,
    output_report: str,
) -> Dict:
    """
    Apply mapping rules to canonical CSV.

    Returns a dict with keys:
        total_rows, confidence_counts, manual_review_count,
        mapped_csv, report
    """
    print(f"[mapper] Loading mapping rules: {mapping_yaml_path}")
    mapping_rules = load_mapping_yaml(mapping_yaml_path)

    print(f"[mapper] Loading canonical CSV: {canonical_csv_path}")
    with open(canonical_csv_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        canonical_rows = list(reader)
        headers = reader.fieldnames or []

    print(f"[mapper] Loaded {len(canonical_rows)} rows")

    # Flatten rules from all banks into one sorted list
    all_rules: List[dict] = []
    for bank, rules in mapping_rules.items():
        if isinstance(rules, list):
            all_rules.extend(rules)

    confidence_order = {'high': 0, 'medium': 1, 'low': 2, 'none': 3}
    all_rules.sort(key=lambda r: (
        confidence_order.get(r.get('confidence', 'low'), 99),
        -r.get('frequency', 0),
    ))

    print(f"[mapper] Loaded {len(all_rules)} rules")

    # Apply mappings
    mapped_rows = []
    confidence_counts = {'high': 0, 'medium': 0, 'low': 0, 'none': 0}
    manual_review = []

    for row_num, row in enumerate(canonical_rows, 1):
        description = row.get('Description') or row.get('Narration') or ''
        account, confidence, pattern, reason = match_rule(description, all_rules)

        mapped_row = row.copy()
        mapped_row['Account'] = account or ''
        mapped_row['Confidence'] = confidence
        mapped_row['MatchReason'] = reason

        mapped_rows.append(mapped_row)
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1

        if confidence in ('low', 'none'):
            manual_review.append({
                'row': row_num,
                'description': description[:60],
                'assigned_account': account,
                'confidence': confidence,
                'reason': reason,
            })

    print(
        f"[mapper] High: {confidence_counts['high']}  "
        f"Medium: {confidence_counts['medium']}  "
        f"Low: {confidence_counts['low']}  "
        f"No match: {confidence_counts['none']}"
    )

    # Write mapped CSV
    print(f"[mapper] Writing mapped CSV: {output_mapped_csv}")
    output_headers = list(headers) + ['Account', 'Confidence', 'MatchReason']
    Path(output_mapped_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(output_mapped_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=output_headers, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(mapped_rows)
    print(f"[mapper] Wrote {len(mapped_rows)} rows")

    # Write confidence report
    total = len(canonical_rows)
    pct = lambda n: f"{100 * n // total if total else 0}%"  # noqa: E731
    report_lines = [
        "=" * 90,
        "ACCOUNT MAPPING CONFIDENCE REPORT",
        "=" * 90,
        "",
        "CONFIDENCE DISTRIBUTION",
        "-" * 90,
        f"Total rows: {total}",
        f"  High confidence:   {confidence_counts['high']:4d}  ({pct(confidence_counts['high'])})",
        f"  Medium confidence: {confidence_counts['medium']:4d}  ({pct(confidence_counts['medium'])})",
        f"  Low confidence:    {confidence_counts['low']:4d}  ({pct(confidence_counts['low'])})",
        f"  No match:          {confidence_counts['none']:4d}  ({pct(confidence_counts['none'])})",
        "",
    ]

    if manual_review:
        report_lines += [
            "MANUAL REVIEW REQUIRED",
            "-" * 90,
            f"Items requiring review: {len(manual_review)}",
            "",
        ]
        for item in manual_review[:20]:
            report_lines += [
                f"Row {item['row']:4d}: {item['description']:60}",
                f"         Assigned to: {item['assigned_account'] or '(none)':45}",
                f"         Confidence: {item['confidence']:10} | {item['reason']}",
                "",
            ]
        if len(manual_review) > 20:
            report_lines.append(f"... and {len(manual_review) - 20} more items\n")

    report_lines += [
        "=" * 90,
        "Next: Import mapped CSV into GnuCash using File → Import → Import CSV",
        "=" * 90,
    ]
    report_text = "\n".join(report_lines)
    with open(output_report, 'w', encoding='utf-8') as f:
        f.write(report_text)

    return {
        'total_rows': total,
        'confidence_counts': confidence_counts,
        'manual_review_count': len(manual_review),
        'mapped_csv': output_mapped_csv,
        'report': output_report,
    }


# ---------------------------------------------------------------------------
# PA Skills UI entry point
# ---------------------------------------------------------------------------

def run(
    gnucash_file: str,
    canonical_csv: str,
    output_path: str,
    config_path: str = None,
    model_override: str = None,
) -> str:
    """
    Run the full account-mapping pipeline from the PA Skills UI.

    Chains:
        1. skill_gnucash_xml_extractor  — parse .gnucash → description→account history
        2. skill_gnucash_mapping_generator — build YAML rules (freq≥3, recency-weighted)
        3. map_accounts()               — apply rules to canonical CSV

    Args:
        gnucash_file:   Path to .gnucash book (gzipped XML format).
        canonical_csv:  Path to canonical 8-col CSV (from ICICI/HSBC/BoB/HDFC skills).
        output_path:    Path for the mapped output CSV.
        config_path:    Unused (no LLM required).
        model_override: Unused (no LLM required).

    Returns:
        Human-readable result string for the UI.
    """
    # Make sibling agents importable
    agents_root = Path(__file__).resolve().parent.parent
    if str(agents_root) not in sys.path:
        sys.path.insert(0, str(agents_root))

    from skill_gnucash_xml_extractor.agent import parse_gnucash_file          # noqa: E402
    from skill_gnucash_mapping_generator.agent import generate_rules, generate_yaml  # noqa: E402

    out_path = Path(output_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Step 1: Extract historical mappings from .gnucash
    print(f"[mapper] Step 1 — extracting mappings from {Path(gnucash_file).name}")
    extractor_output = parse_gnucash_file(gnucash_file)
    mapping_count = sum(
        len(v) for v in extractor_output.get('mappings', {}).values()
    )
    print(f"[mapper] Extracted {mapping_count} historical description→account pairs")

    # Step 2: Generate rules YAML from extractor output
    print(f"[mapper] Step 2 — generating rules (freq≥3, recency-weighted)")
    rules_by_bank = generate_rules(extractor_output)
    all_rules: List[dict] = []
    for bank_rules in rules_by_bank.values():
        all_rules.extend(bank_rules)
    rule_count = len(all_rules)
    print(f"[mapper] Generated {rule_count} rules")

    yaml_content = generate_yaml(all_rules)
    rules_path = out_path.with_name(out_path.stem + "_mapping_rules.yaml")
    rules_path.write_text(yaml_content, encoding='utf-8')
    print(f"[mapper] Rules saved to {rules_path.name}")

    # Step 3: Apply rules to canonical CSV
    report_path = out_path.with_name(out_path.stem + "_confidence.txt")
    print(f"[mapper] Step 3 — applying mapping to {Path(canonical_csv).name}")
    result = map_accounts(canonical_csv, str(rules_path), str(out_path), str(report_path))

    counts = result['confidence_counts']
    total = result['total_rows']
    pct = lambda n: f"{100 * n // total if total else 0}%"  # noqa: E731

    return (
        f"Mapped **{total} rows** using **{rule_count} rules** "
        f"(derived from {mapping_count} historical transactions in .gnucash).\n\n"
        f"**Confidence breakdown:**\n"
        f"- High: {counts['high']} ({pct(counts['high'])})\n"
        f"- Medium: {counts['medium']} ({pct(counts['medium'])})\n"
        f"- Low: {counts['low']} ({pct(counts['low'])})\n"
        f"- No match: {counts['none']} ({pct(counts['none'])})\n\n"
        f"**Files produced:**\n"
        f"- `{out_path.name}` — mapped CSV, ready for GnuCash import\n"
        f"- `{report_path.name}` — confidence report (review Low/No-match rows)\n"
        f"- `{rules_path.name}` — generated mapping rules (human-editable YAML)"
    )


# ---------------------------------------------------------------------------
# CLI shim
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    canonical_csv = sys.argv[1] if len(sys.argv) > 1 else None
    mapping_yaml  = sys.argv[2] if len(sys.argv) > 2 else None
    output_csv    = sys.argv[3] if len(sys.argv) > 3 else None
    output_report = sys.argv[4] if len(sys.argv) > 4 else None

    if not all([canonical_csv, mapping_yaml, output_csv, output_report]):
        print("Usage: python agent.py <canonical_csv> <mapping.yaml> <output_csv> <output_report>")
        sys.exit(1)

    map_accounts(canonical_csv, mapping_yaml, output_csv, output_report)
