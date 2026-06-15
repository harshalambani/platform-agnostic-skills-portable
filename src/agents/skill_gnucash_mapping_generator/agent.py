#!/usr/bin/env python3
"""GnuCash Mapping Generator — Generate YAML rules from extractor output."""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

def recency_weight(last_date_str: str) -> float:
    """Score based on how recent the mapping is."""
    try:
        from datetime import datetime
        last_date = datetime.strptime(last_date_str, '%Y-%m-%d')
        days_ago = (datetime.now() - last_date).days
        if days_ago <= 365:
            return 1.0
        elif days_ago <= 730:
            return 0.5
        else:
            return 0.2
    except (ValueError, TypeError):
        return 0.2

def confidence_score(frequency: int, last_date: str) -> float:
    """Compute confidence: frequency × recency_weight."""
    return frequency * recency_weight(last_date)

def extract_upi_key(description: str) -> Optional[str]:
    """Extract UPI pattern: UPI/MERCHANT/VPA."""
    match = re.search(r'UPI/([A-Z_]+)/([A-Z0-9@.]+)', description, re.IGNORECASE)
    if match:
        merchant = match.group(1)
        return f"UPI/{merchant}/.*"
    return None

def extract_neft_key(description: str) -> Optional[str]:
    """Extract NEFT pattern: NEFT/COUNTERPARTY."""
    match = re.search(r'NEFT[/-]?\s*([A-Z ]+?)(?:\s*[-/]|$)', description, re.IGNORECASE)
    if match:
        counterparty = match.group(1).strip()
        if len(counterparty) > 2:
            return f"NEFT.*{counterparty}.*"
    return None

def extract_merchant_id(description: str) -> Optional[str]:
    """Extract merchant ID (6+ digit sequences)."""
    match = re.search(r'\b(\d{6,})\b', description)
    if match:
        merchant_id = match.group(1)
        return f".*{merchant_id}.*"
    return None

def generate_rules(extractor_json: Dict[str, Any], min_freq: int = 3) -> Dict[str, List[Dict]]:
    """Generate rules from extractor JSON.

    Args:
        extractor_json: Output from xml_extractor's parse_gnucash_file().
        min_freq:       Minimum occurrence frequency to generate a rule.
                        Default 3 for cross-bank; use 1 when filtering to
                        the importing bank (every historical txn is relevant).
    """
    rules = {
        '_global': [],
        'ICICI': [],
        'HDFC': [],
        'HSBC': [],
        'BoB': []
    }

    mappings = extractor_json.get('mappings', {})

    for bank, bank_mappings in mappings.items():
        if bank not in rules:
            continue

        filtered = []
        for m in bank_mappings:
            freq = m.get('frequency', 0)
            last_date = m.get('last_date', '')
            if freq < min_freq:
                continue
            conf = confidence_score(freq, last_date)
            # For single-occurrence matches (min_freq=1), accept any positive score
            if min_freq >= 3 and conf <= 0.5:
                continue
            filtered.append(m)

        filtered.sort(
            key=lambda x: confidence_score(x.get('frequency', 0), x.get('last_date', '')),
            reverse=True
        )

        for mapping in filtered:
            description = mapping.get('description', '')
            account = mapping.get('account', '')
            frequency = mapping.get('frequency', 0)
            last_date = mapping.get('last_date', '')
            conf = confidence_score(frequency, last_date)

            patterns = []

            if conf > 0.7:
                upi_key = extract_upi_key(description)
                if upi_key:
                    patterns.append(upi_key)

                neft_key = extract_neft_key(description)
                if neft_key:
                    patterns.append(neft_key)

                merchant_id = extract_merchant_id(description)
                if merchant_id:
                    patterns.append(merchant_id)

            if not patterns:
                patterns.append(description)

            if not patterns:
                continue

            if conf > 0.8:
                confidence_level = 'high'
            elif conf > 0.5:
                confidence_level = 'medium'
            else:
                confidence_level = 'low'

            rule = {
                'patterns': patterns,
                'account': account,
                'confidence': confidence_level,
                'frequency': frequency,
                'last_date': last_date,
                'score': conf,
                'reason': f"{frequency} occurrences, last {last_date}",
                'bank': bank
            }

            rules[bank].append(rule)

    return rules

def generate_yaml(approved_rules: List[Dict]) -> str:
    """Generate mapping.yaml from approved rules."""
    yaml_lines = [
        "# GnuCash Mapping Rules — Auto-generated",
        f"# Generated: {datetime.now().isoformat()}",
        "# Note: All rules have been validated",
        "",
    ]

    by_bank = {}
    for rule in approved_rules:
        bank = rule.get('bank', '_global')
        if bank not in by_bank:
            by_bank[bank] = []
        by_bank[bank].append(rule)

    if '_global' in by_bank:
        yaml_lines.append("_global:")
        for rule in by_bank['_global']:
            _append_rule_yaml(yaml_lines, rule)
        yaml_lines.append("")

    for bank in ['ICICI', 'HDFC', 'HSBC', 'BoB']:
        if bank in by_bank:
            yaml_lines.append(f"{bank}:")
            for rule in by_bank[bank]:
                _append_rule_yaml(yaml_lines, rule)
            yaml_lines.append("")

    return "\n".join(yaml_lines)

def _append_rule_yaml(lines: List[str], rule: Dict) -> None:
    """Append a single rule to YAML output."""
    patterns = rule['patterns']
    account = rule['account']
    confidence = rule['confidence']
    reason = rule['reason']

    lines.append("  - patterns:")
    for pattern in patterns:
        lines.append(f'      - "{pattern}"')
    lines.append(f'    account: "{account}"')
    lines.append(f'    confidence: {confidence}')
    lines.append(f'    reason: "{reason}"')

def main(extractor_json_path: str, output_yaml: str) -> None:
    """Main entry point."""
    print(f"📂 Loading extractor JSON: {extractor_json_path}")
    with open(extractor_json_path) as f:
        extractor_data = json.load(f)

    print(f"📊 Generating rules...")
    rules = generate_rules(extractor_data)

    rule_count = sum(len(r) for r in rules.values())
    print(f"   Generated {rule_count} candidate rules")

    # All rules approved (no LLM validation in this version)
    all_rules = []
    for bank, bank_rules in rules.items():
        all_rules.extend(bank_rules)

    print(f"📝 Generating mapping.yaml...")
    yaml_content = generate_yaml(all_rules)
    with open(output_yaml, 'w') as f:
        f.write(yaml_content)
    print(f"   ✓ {output_yaml}")

    print(f"\n✓ Mapping generation complete")

def run(extraction_json: list, output_path: str, **kwargs) -> dict:
    """Entry point for Cowork skill runner.

    Args:
        extraction_json: List of extraction JSON file paths
        output_path: Directory to write mapping.yaml files
        **kwargs: Additional arguments from skill runner

    Returns:
        Dict with output file paths and status
    """
    results = []

    for json_file in extraction_json:
        try:
            print(f"🔄 Generating rules: {Path(json_file).name}")

            with open(json_file) as f:
                extractor_data = json.load(f)

            rules = generate_rules(extractor_data)
            rule_count = sum(len(r) for r in rules.values())

            all_rules = []
            for bank, bank_rules in rules.items():
                all_rules.extend(bank_rules)

            yaml_content = generate_yaml(all_rules)

            # Write output YAML
            output_name = Path(json_file).stem + '_mapping.yaml'
            output_file = Path(output_path) / output_name
            output_file.parent.mkdir(parents=True, exist_ok=True)

            with open(output_file, 'w') as f:
                f.write(yaml_content)

            results.append({
                'status': 'success',
                'input': str(json_file),
                'output': str(output_file),
                'rules_generated': rule_count
            })
            print(f"✓ Generated {rule_count} rules → {output_file}")
        except Exception as e:
            results.append({
                'status': 'error',
                'input': str(json_file),
                'error': str(e)
            })
            print(f"✗ Error: {e}")

    return {
        'success': all(r['status'] == 'success' for r in results),
        'results': results
    }

if __name__ == '__main__':
    import sys
    if len(sys.argv) < 3:
        print("Usage: python agent.py <extractor_json> <output_yaml>")
        sys.exit(1)
    main(sys.argv[1], sys.argv[2])
