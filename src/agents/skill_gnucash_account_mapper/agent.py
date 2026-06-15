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
import json
import re
import sys
import threading
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml


# ---------------------------------------------------------------------------
# Progress helper — push events to Gradio streaming UI
# ---------------------------------------------------------------------------

def _emit_mapper_progress(message: str) -> None:
    """Push a mapper progress event to the UI streaming queue.

    The runner (ui/_runner.py) sets the queue via ``agents.base_agent``,
    so we must import from the same dotted path — otherwise Python treats
    it as a different module object with a separate threading.local().
    Falls back to the bare ``base_agent`` import for CLI / test usage.
    """
    q = None
    try:
        from agents.base_agent import get_progress_queue  # noqa: E402
        q = get_progress_queue()
    except ImportError:
        try:
            from base_agent import get_progress_queue  # noqa: E402
            q = get_progress_queue()
        except Exception:
            pass
    except Exception:
        pass
    if q is not None:
        q.put({"step": 5, "type": "pipeline", "snippet": f"mapper: {message}"})
    print(f"[mapper] {message}")


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
# LLM fallback for unmatched rows
# ---------------------------------------------------------------------------

_LLM_SYSTEM_PROMPT = """\
You are a GnuCash account-mapping assistant. Given bank transaction descriptions
and a list of GnuCash accounts, assign the most likely account to each transaction.

Rules:
- Use ONLY accounts from the provided list. Never invent accounts.
- Match based on semantic meaning, not exact text. For example:
  "Int.Pd:" or "CREDIT INTEREST" → Bank Interest
  "NACHMU-MUMBAI/ACHCR/<company>" → look up the company in the account list
  "MIN BAL CHRGS" → Bank Service Charge
  "Opening Balance" → leave blank (skip)
- If a transaction clearly maps to a dividend account, use the specific company
  dividend sub-account if one exists, otherwise use "Dividend - Other Shares".
- If truly uncertain, leave account blank rather than guess wrong.

Respond with ONLY a JSON array. Each element: {"row": <row_number>, "account": "<full_account_path>", "reason": "<brief_reason>"}
For skipped rows: {"row": <row_number>, "account": "", "reason": "skip"}
No markdown, no explanation outside the JSON."""


def _build_llm_prompt(
    unmatched: List[Dict],
    account_tree: List[str],
    example_mappings: List[Dict],
) -> str:
    """Build a compact prompt for the LLM fallback pass."""
    parts = []

    # Account tree (leaf accounts only — skip intermediate nodes)
    parts.append("=== GNUCASH ACCOUNTS ===")
    for acct in account_tree:
        parts.append(acct)

    # Example mappings from the rules pass (gives the LLM context)
    if example_mappings:
        parts.append("\n=== EXAMPLE MAPPINGS (from rules pass) ===")
        for ex in example_mappings[:15]:
            parts.append(f"  {ex['description']!r} -> {ex['account']}")

    # Unmatched rows
    parts.append("\n=== UNMATCHED TRANSACTIONS (assign accounts) ===")
    for item in unmatched:
        amt_info = ""
        if item.get('withdrawal'):
            amt_info = f" [withdrawal: {item['withdrawal']}]"
        elif item.get('deposit'):
            amt_info = f" [deposit: {item['deposit']}]"
        parts.append(f"  Row {item['row']}: {item['description']!r}{amt_info}")

    return "\n".join(parts)


_LLM_BATCH_SIZE = 5          # rows per LLM call — small batches finish faster
_LLM_TIMEOUT_SECONDS = 60    # per-batch timeout (not total)


def _parse_llm_json(text: str) -> Optional[list]:
    """Extract a JSON array from an LLM response, tolerating markdown fences."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("```")]
        text = "\n".join(lines)

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("[")
        end = text.rfind("]")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                pass
    return None


def _llm_batch(
    rows: List[Dict],
    account_tree: List[str],
    example_mappings: List[Dict],
    config_path: str,
    model_override: str,
    run_direct_fn,
) -> Dict[int, Dict]:
    """Send one batch of rows to the LLM and return validated results."""
    prompt = _build_llm_prompt(rows, account_tree, example_mappings)

    response = None
    error = None

    def _call():
        nonlocal response, error
        try:
            response = run_direct_fn(
                user_message=prompt,
                system_prompt=_LLM_SYSTEM_PROMPT,
                config_path=config_path,
                model_override=model_override,
            )
        except Exception as e:
            error = e

    thread = threading.Thread(target=_call, daemon=True)
    thread.start()
    thread.join(timeout=_LLM_TIMEOUT_SECONDS)

    if thread.is_alive():
        _emit_mapper_progress(f"  batch timed out after {_LLM_TIMEOUT_SECONDS}s")
        return {}
    if error:
        _emit_mapper_progress(f"  batch error: {error}")
        return {}
    if not response:
        _emit_mapper_progress("  batch: empty response")
        return {}

    mappings_list = _parse_llm_json(response)
    if mappings_list is None:
        _emit_mapper_progress("  batch: could not parse JSON")
        return {}

    account_set = set(account_tree)
    result = {}
    for item in mappings_list:
        if not isinstance(item, dict):
            continue
        row_num = item.get("row")
        account = item.get("account", "")
        reason = item.get("reason", "LLM mapping")
        if not row_num:
            continue
        if account and account not in account_set:
            _emit_mapper_progress(f"  row {row_num}: unknown account {account!r}, skipped")
            continue
        result[row_num] = {"account": account, "reason": reason}
    return result


def llm_fallback_mapping(
    unmatched_rows: List[Dict],
    account_tree: List[str],
    example_mappings: List[Dict],
    config_path: str,
    model_override: str = None,
) -> Dict[int, Dict]:
    """
    Use the LLM to map rows that the rules pass couldn't match.

    Sends rows in small batches (_LLM_BATCH_SIZE) so each LLM call is
    fast enough for local models. Reports per-batch progress to the UI.

    Returns:
        Dict mapping row_number -> {'account': str, 'reason': str}
    """
    if not unmatched_rows:
        return {}

    try:
        from agents.base_agent import run_direct  # noqa: E402
    except ImportError:
        from base_agent import run_direct  # noqa: E402

    total = len(unmatched_rows)
    batches = [
        unmatched_rows[i:i + _LLM_BATCH_SIZE]
        for i in range(0, total, _LLM_BATCH_SIZE)
    ]
    _emit_mapper_progress(
        f"LLM fallback: {total} rows in {len(batches)} batch(es) "
        f"of {_LLM_BATCH_SIZE} (timeout {_LLM_TIMEOUT_SECONDS}s each)"
    )

    all_results: Dict[int, Dict] = {}
    for batch_num, batch in enumerate(batches, 1):
        row_ids = ", ".join(str(r["row"]) for r in batch)
        _emit_mapper_progress(f"LLM batch {batch_num}/{len(batches)} — rows {row_ids}")

        batch_result = _llm_batch(
            rows=batch,
            account_tree=account_tree,
            example_mappings=example_mappings,
            config_path=config_path,
            model_override=model_override,
            run_direct_fn=run_direct,
        )
        all_results.update(batch_result)
        mapped_so_far = sum(1 for v in all_results.values() if v.get("account"))
        _emit_mapper_progress(f"  batch {batch_num} done — {mapped_so_far}/{total} mapped so far")

    matched = sum(1 for v in all_results.values() if v.get("account"))
    _emit_mapper_progress(f"LLM fallback complete: {matched}/{total} rows mapped")
    return all_results


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

    _emit_mapper_progress(
        f"rules pass: High={confidence_counts['high']} "
        f"Med={confidence_counts['medium']} "
        f"Low={confidence_counts['low']} "
        f"None={confidence_counts['none']}"
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

# Map pipeline bank labels to extractor bank keys
_BANK_KEY_MAP = {
    'Bank of Baroda': 'BoB',
    'HDFC': 'HDFC',
    'HSBC': 'HSBC',
    'ICICI': 'ICICI',
}


def run(
    gnucash_file: str,
    canonical_csv: str,
    output_path: str,
    config_path: str = None,
    model_override: str = None,
    bank_name: str = None,
) -> str:
    """
    Run the full account-mapping pipeline from the PA Skills UI.

    Chains:
        1. skill_gnucash_xml_extractor  — parse .gnucash → description→account history
        2. skill_gnucash_mapping_generator — build YAML rules from same bank's history
        3. map_accounts()               — apply rules to canonical CSV

    Args:
        gnucash_file:   Path to .gnucash book (gzipped XML format).
        canonical_csv:  Path to canonical 8-col CSV (from ICICI/HSBC/BoB/HDFC skills).
        output_path:    Path for the mapped output CSV.
        config_path:    Unused (no LLM required).
        model_override: Unused (no LLM required).
        bank_name:      Pipeline bank label (e.g. "Bank of Baroda"). When set,
                        rules are generated ONLY from that bank's historical
                        transactions — not from other banks.

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

    # Resolve bank key for filtering
    bank_key = _BANK_KEY_MAP.get(bank_name) if bank_name else None

    # Step 1: Extract historical mappings from .gnucash
    _emit_mapper_progress(f"extracting history from {Path(gnucash_file).name}")
    extractor_output = parse_gnucash_file(gnucash_file)

    # Collect ALL account paths (all banks) before filtering — needed for LLM fallback
    all_account_paths = set()
    for bank_maps in extractor_output.get('mappings', {}).values():
        for m in bank_maps:
            if m.get('account'):
                all_account_paths.add(m['account'])

    if bank_key:
        # Filter to only the importing bank's historical transactions
        all_mappings = extractor_output.get('mappings', {})
        bank_mappings = all_mappings.get(bank_key, [])
        extractor_output['mappings'] = {bank_key: bank_mappings}
        mapping_count = len(bank_mappings)
        _emit_mapper_progress(f"filtered to {bank_key}: {mapping_count} historical pairs")
    else:
        mapping_count = sum(
            len(v) for v in extractor_output.get('mappings', {}).values()
        )
        _emit_mapper_progress(f"extracted {mapping_count} pairs (all banks)")

    # Step 2: Generate rules YAML from extractor output
    _emit_mapper_progress(f"generating rules (bank={bank_key or 'all'})")
    rules_by_bank = generate_rules(extractor_output, min_freq=1 if bank_key else 3)
    all_rules: List[dict] = []
    for bank_rules in rules_by_bank.values():
        all_rules.extend(bank_rules)
    rule_count = len(all_rules)
    _emit_mapper_progress(f"generated {rule_count} rules")

    yaml_content = generate_yaml(all_rules)
    rules_path = out_path.with_name(out_path.stem + "_mapping_rules.yaml")
    rules_path.write_text(yaml_content, encoding='utf-8')

    # Step 3: Apply rules to canonical CSV
    report_path = out_path.with_name(out_path.stem + "_confidence.txt")
    _emit_mapper_progress(f"applying rules to {Path(canonical_csv).name}")
    result = map_accounts(canonical_csv, str(rules_path), str(out_path), str(report_path))

    # Step 4: LLM fallback for unmatched rows
    # Re-read the mapped CSV to find rows with no account assigned
    unmatched_count = result['confidence_counts'].get('none', 0)
    llm_mapped_count = 0

    if unmatched_count > 0 and config_path:
        _emit_mapper_progress(f"LLM fallback starting for {unmatched_count} unmatched rows")

        # Read mapped CSV to find unmatched rows + collect examples
        with open(str(out_path), 'r', encoding='utf-8', errors='replace') as f:
            mapped_rows = list(csv.DictReader(f))

        unmatched_for_llm = []
        example_mappings = []
        for i, row in enumerate(mapped_rows, 1):
            desc = row.get('Description') or row.get('Narration') or ''
            acct = row.get('Account', '')
            conf = row.get('Confidence', 'none')
            if conf == 'none' or not acct:
                unmatched_for_llm.append({
                    'row': i,
                    'description': desc,
                    'withdrawal': row.get('Withdrawal', ''),
                    'deposit': row.get('Deposit', ''),
                })
            elif conf in ('high', 'medium'):
                example_mappings.append({'description': desc, 'account': acct})

        # Use the full account tree collected before bank filtering
        account_set = set(all_account_paths)
        for ex in example_mappings:
            if ex.get('account'):
                account_set.add(ex['account'])
        account_list = sorted(account_set)

        if account_list and unmatched_for_llm:
            llm_results = llm_fallback_mapping(
                unmatched_rows=unmatched_for_llm,
                account_tree=account_list,
                example_mappings=example_mappings,
                config_path=config_path,
                model_override=model_override,
            )

            # Apply LLM results back to the mapped CSV
            if llm_results:
                for i, row in enumerate(mapped_rows):
                    row_num = i + 1
                    if row_num in llm_results:
                        llm_info = llm_results[row_num]
                        if llm_info['account']:
                            row['Account'] = llm_info['account']
                            row['Confidence'] = 'llm'
                            row['MatchReason'] = f"LLM: {llm_info['reason']}"
                            llm_mapped_count += 1

                # Rewrite the mapped CSV
                if llm_mapped_count > 0:
                    headers_out = list(mapped_rows[0].keys())
                    with open(str(out_path), 'w', newline='', encoding='utf-8') as f:
                        writer = csv.DictWriter(f, fieldnames=headers_out, extrasaction='ignore')
                        writer.writeheader()
                        writer.writerows(mapped_rows)
                    _emit_mapper_progress(f"LLM pass: {llm_mapped_count} additional rows mapped")

                    # Update confidence counts
                    result['confidence_counts']['none'] -= llm_mapped_count
                    result['confidence_counts']['llm'] = llm_mapped_count
    else:
        if unmatched_count == 0:
            _emit_mapper_progress("all rows matched by rules — no LLM needed")
        elif not config_path:
            _emit_mapper_progress("no LLM config — skipping fallback")

    counts = result['confidence_counts']
    total = result['total_rows']
    pct = lambda n: f"{100 * n // total if total else 0}%"  # noqa: E731

    bank_note = f" ({bank_key} only)" if bank_key else ""
    llm_note = f" + LLM fallback mapped {llm_mapped_count}" if llm_mapped_count else ""
    return (
        f"Mapped **{total} rows** using **{rule_count} rules** "
        f"(derived from {mapping_count} historical transactions{bank_note} in .gnucash).{llm_note}\n\n"
        f"**Confidence breakdown:**\n"
        f"- High: {counts.get('high', 0)} ({pct(counts.get('high', 0))})\n"
        f"- Medium: {counts.get('medium', 0)} ({pct(counts.get('medium', 0))})\n"
        f"- Low: {counts.get('low', 0)} ({pct(counts.get('low', 0))})\n"
        f"- LLM: {counts.get('llm', 0)} ({pct(counts.get('llm', 0))})\n"
        f"- No match: {counts.get('none', 0)} ({pct(counts.get('none', 0))})\n\n"
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
