#!/usr/bin/env python3
"""GnuCash Mapping Validator — Test rules against eval set."""

import json
import csv
import re
from collections import defaultdict
from datetime import datetime

def load_rules(rules_json_path):
    """Load candidate rules."""
    with open(rules_json_path) as f:
        return json.load(f)

def load_extractor_output(json_path):
    """Load GnuCash extractor output."""
    with open(json_path) as f:
        return json.load(f)

def load_bank_statement(csv_path):
    """Load bank statement CSV."""
    rows = []
    with open(csv_path, 'r', encoding='utf-8', errors='replace') as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows

def parse_amount(amt_str):
    """Parse amount string to float."""
    if not amt_str or not isinstance(amt_str, str):
        return 0.0
    amt_str = amt_str.replace(',', '').strip()
    try:
        return float(amt_str)
    except:
        return 0.0

def build_eval_set(extractor_output, bank_statement):
    """Match bank statement rows to GnuCash accounts."""
    eval_set = []
    gnucash_mappings = []

    for bank, bank_mappings in extractor_output.get('mappings', {}).items():
        if isinstance(bank_mappings, list):
            gnucash_mappings.extend(bank_mappings)

    matched = 0
    for stmt_row in bank_statement:
        stmt_narration = stmt_row.get('Narration', '')

        best_match = None
        best_score = 0

        for gnc_mapping in gnucash_mappings:
            gnc_desc = gnc_mapping.get('description', '')
            gnc_account = gnc_mapping.get('account', '')

            desc_score = 0
            if gnc_desc.lower() in stmt_narration.lower():
                desc_score = 1.0
            elif stmt_narration.lower() in gnc_desc.lower():
                desc_score = 0.9
            else:
                stmt_words = set(stmt_narration.lower().split())
                gnc_words = set(gnc_desc.lower().split())
                if stmt_words & gnc_words:
                    overlap = len(stmt_words & gnc_words)
                    desc_score = overlap / max(len(stmt_words), len(gnc_words))

            if desc_score > best_score:
                best_score = desc_score
                best_match = (stmt_narration, gnc_account)

        if best_match and best_score > 0.3:
            eval_set.append(best_match)
            matched += 1

    return eval_set

def match_rule(description, rules_by_bank):
    """Try to match description against rules."""
    all_rules = []
    for bank, bank_rules in rules_by_bank.items():
        if isinstance(bank_rules, list):
            all_rules.extend(bank_rules)

    all_rules.sort(key=lambda r: r.get('frequency', 0), reverse=True)

    for rule in all_rules:
        patterns = rule.get('patterns', [])
        for pattern in patterns:
            try:
                if re.search(pattern, description, re.IGNORECASE):
                    return rule.get('account', ''), rule.get('confidence', 'low'), pattern
            except re.error:
                if pattern.lower() in description.lower():
                    return rule.get('account', ''), rule.get('confidence', 'low'), pattern

    return None, None, None

def validate(rules_path, extractor_path, bank_csv_path, output_report):
    """Run validation."""
    print(f"\n📂 Loading rules: {rules_path}")
    rules = load_rules(rules_path)

    print(f"📂 Loading extractor output: {extractor_path}")
    extractor_output = load_extractor_output(extractor_path)

    print(f"📂 Loading bank statement: {bank_csv_path}")
    bank_statement = load_bank_statement(bank_csv_path)

    print(f"\n🔗 Building eval set...")
    eval_set = build_eval_set(extractor_output, bank_statement)

    print(f"✓ Eval set: {len(eval_set)} matched transactions")

    # Test each eval row
    matches = 0
    mismatches = 0
    mismatch_list = []

    for bank_desc, actual_account in eval_set:
        predicted_account, confidence, pattern = match_rule(bank_desc, rules)

        if predicted_account is None:
            mismatches += 1
        else:
            pred_main = predicted_account.split(':')[0] if ':' in predicted_account else predicted_account
            actual_main = actual_account.split(':')[0] if ':' in actual_account else actual_account

            if pred_main.lower() == actual_main.lower():
                matches += 1
            else:
                mismatches += 1
                mismatch_list.append({
                    'description': bank_desc[:60],
                    'predicted': predicted_account[:50],
                    'actual': actual_account[:50],
                    'confidence': confidence
                })

    total = len(eval_set)
    accuracy = matches / total * 100 if total > 0 else 0

    report = []
    report.append("=" * 80)
    report.append("MAPPING VALIDATION REPORT")
    report.append("=" * 80)
    report.append(f"Eval set: {total} transactions")
    report.append(f"Correct: {matches} ({matches/total*100:.1f}%)")
    report.append(f"Mismatches: {mismatches} ({mismatches/total*100:.1f}%)")
    report.append(f"")
    report.append(f"ACCURACY: {accuracy:.1f}%")

    if accuracy >= 80:
        report.append(f"Status: ✓ PASSED (≥80%)")
    else:
        report.append(f"Status: ✗ BELOW TARGET (<80%)")

    report.append("=" * 80)

    report_text = "\n".join(report)
    with open(output_report, 'w') as f:
        f.write(report_text)

    print("\n" + report_text)

def run(mapping_rules_json: list, extractor_json: list, eval_statement: list, output_path: str, **kwargs) -> dict:
    """Entry point for Cowork skill runner.

    Args:
        mapping_rules_json: List of mapping rules JSON file paths
        extractor_json: List of extractor JSON file paths
        eval_statement: List of bank statement CSV file paths
        output_path: Directory to write validation reports
        **kwargs: Additional arguments from skill runner

    Returns:
        Dict with output file paths and validation results
    """
    from pathlib import Path

    if not mapping_rules_json or not extractor_json or not eval_statement:
        return {
            'success': False,
            'error': 'Missing required inputs: mapping_rules_json, extractor_json, eval_statement'
        }

    results = []

    try:
        rules_file = mapping_rules_json[0]
        extract_file = extractor_json[0]
        stmt_file = eval_statement[0]

        print(f"🔄 Validating rules: {Path(rules_file).name}")
        validate(rules_file, extract_file, stmt_file, str(Path(output_path) / 'validation_report.txt'))

        results.append({
            'status': 'success',
            'rules_file': str(rules_file),
            'extract_file': str(extract_file),
            'statement_file': str(stmt_file),
            'report': str(Path(output_path) / 'validation_report.txt')
        })
        print(f"✓ Validation complete")
    except Exception as e:
        results.append({
            'status': 'error',
            'error': str(e)
        })
        print(f"✗ Error: {e}")

    return {
        'success': all(r['status'] == 'success' for r in results),
        'results': results
    }

if __name__ == '__main__':
    import sys

    rules_path = sys.argv[1] if len(sys.argv) > 1 else None
    extractor_path = sys.argv[2] if len(sys.argv) > 2 else None
    bank_csv = sys.argv[3] if len(sys.argv) > 3 else None
    report_path = sys.argv[4] if len(sys.argv) > 4 else None

    if not all([rules_path, extractor_path, bank_csv, report_path]):
        print("Usage: python agent.py <rules.json> <extractor.json> <bank.csv> <report.txt>")
        sys.exit(1)

    validate(rules_path, extractor_path, bank_csv, report_path)
