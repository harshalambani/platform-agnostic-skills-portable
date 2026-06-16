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
# Smart pattern pass — deterministic semantic matching (no LLM)
# ---------------------------------------------------------------------------

def _fuzzy_match_dividend(company_fragment: str, account_tree: List[str]) -> Optional[str]:
    """Fuzzy-match a truncated company name against dividend sub-accounts.

    Bank narrations like "NACHMU-MUMBAI/ACHCR/GANDHISPECIA" truncate
    company names. We try substring matching against dividend account
    names (e.g. "Gandhi Steel Tubes" contains "GANDHI").
    """
    fragment = company_fragment.upper().strip()
    if len(fragment) < 3:
        return None

    dividend_accounts = [
        a for a in account_tree if ":Dividend Income:" in a or ":Dividend " in a
    ]
    # "Other Shares" is the fallback — try specific accounts first
    other_shares = None
    specific = []
    for acct in dividend_accounts:
        leaf = acct.rsplit(":", 1)[-1]  # e.g. "Dividend - Gandhi Steel Tubes"
        if "Other" in leaf:
            other_shares = acct
        else:
            specific.append((acct, leaf))

    # Try matching fragment against each specific dividend account leaf name
    for acct, leaf in specific:
        # Extract company part after "Dividend - "
        company_part = leaf.replace("Dividend - ", "").replace("Dividend-", "")
        # Check if fragment is a prefix/substring of the company name
        company_upper = company_part.upper()
        if (fragment[:6] in company_upper
                or company_upper[:6] in fragment
                or any(w in fragment for w in company_upper.split() if len(w) >= 4)):
            return acct

    return other_shares  # default for unrecognized dividend companies


def smart_pattern_match(
    description: str,
    account_tree: List[str],
    withdrawal: str = "",
    deposit: str = "",
) -> Optional[Dict]:
    """
    Deterministic semantic matching for common Indian bank narration patterns.

    Returns {'account': str, 'reason': str} or None if no match.
    """
    desc_upper = description.upper().strip()

    # 1. Opening Balance — skip (no account assignment)
    if desc_upper in ("OPENING BALANCE", "OPENING BAL", "OPN BAL"):
        return {"account": "", "reason": "Opening Balance — skip"}

    # 2. Bank Interest: "Int.Pd:", "CREDIT INTEREST", "INT COLL"
    if re.search(r'Int\.Pd:|CREDIT\s*INTEREST|INT\s+COLL|INTEREST\s+PAID', description, re.IGNORECASE):
        for acct in account_tree:
            if acct.endswith(":Bank Interest") or acct.endswith(":Interest Income"):
                return {"account": acct, "reason": "Bank interest pattern"}
        return None

    # 3. Service charges: "MIN BAL CHRGS", "SERVICE CHARGE", "MAINTENANCE CHRG"
    if re.search(r'MIN\s*BAL\s*CHRGS|SERVICE\s*CHARGE?|MAINT.*CHRG|ANNUAL\s*FEE|SMS\s*CHRG', desc_upper):
        for acct in account_tree:
            if "Bank Service Charge" in acct or "Service Charge" in acct:
                return {"account": acct, "reason": "Bank service charge pattern"}
        return None

    # 4. NACH/ACH Dividends: "NACHMU-MUMBAI/ACHCR/<company>"
    nach_match = re.match(r'NACHMU[- ].*?/ACHCR/(.+)', description, re.IGNORECASE)
    if nach_match:
        company = nach_match.group(1).strip()
        # Check if it looks like a dividend (deposit, not withdrawal)
        is_deposit = bool(deposit and float(deposit or 0) > 0)
        is_withdrawal = bool(withdrawal and float(withdrawal or 0) > 0)
        if is_deposit or not is_withdrawal:
            matched_acct = _fuzzy_match_dividend(company, account_tree)
            if matched_acct:
                return {"account": matched_acct, "reason": f"NACH dividend — {company}"}

    # 5. TDS on Dividend: "NACH.*TDS", narrations with TDS
    if re.search(r'TDS\s*(ON|FOR)?\s*DIV', desc_upper):
        for acct in account_tree:
            if "TDS on Dividend" in acct or "TDS" in acct:
                return {"account": acct, "reason": "TDS on dividend pattern"}

    # 6. Self/internal transfer patterns
    if re.search(r'SELF\s*TRANSFER|AC\s*XFR\s*FROM|TRANSFER\s*TO\s*SELF|FD\s*MATURITY', desc_upper):
        # These need manual review — could be FD, loan, or drawing
        return {"account": "", "reason": "Internal transfer — manual review"}

    # 7. Self-name transfer: "SERBOM-MUMBAI/<person name>"
    # Can't determine target account without more context
    if re.search(r'SER[A-Z]{3}-.*?/', description):
        return {"account": "", "reason": "Inter-bank self transfer — manual review"}

    return None


# ---------------------------------------------------------------------------
# LLM fallback — direct Ollama /api/chat (bypasses LangChain)
# ---------------------------------------------------------------------------

_LLM_TIMEOUT_SECONDS = 45    # per-row timeout

_LLM_SYSTEM_PROMPT = """\
You are a GnuCash account classifier. Given ONE bank transaction and a list of accounts, reply with ONLY the full account path. If unsure, reply SKIP.
Rules:
- Use ONLY accounts from the list. Never invent accounts.
- Reply with the account path on a single line, nothing else.
- If the transaction is ambiguous, reply SKIP."""


def _resolve_ollama_config(config_path: str, model_override: str = None) -> Tuple[str, str]:
    """Read config.yaml and return (base_url, model_name)."""
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    ep = cfg.get("ollama") or {}
    base_url = ep.get("base_url", "http://localhost:11434").rstrip("/")
    model = model_override or ep.get("default_model") or "gemma4:12b"
    return base_url, model


def _ollama_chat(base_url: str, model: str, system: str, user: str, timeout: float = 45.0) -> Optional[str]:
    """Call Ollama /api/chat directly. Returns the assistant reply or None."""
    from urllib import request as _req, error as _err

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.0},
    }).encode("utf-8")

    req = _req.Request(
        f"{base_url}/api/chat",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "PA-Skills/mapper"},
    )
    try:
        with _req.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read().decode("utf-8", errors="replace"))
            return (body.get("message") or {}).get("content", "")
    except Exception as e:  # noqa: BLE001
        _emit_mapper_progress(f"  Ollama error: {e}")
        return None


def llm_fallback_mapping(
    unmatched_rows: List[Dict],
    account_tree: List[str],
    example_mappings: List[Dict],
    config_path: str,
    model_override: str = None,
) -> Dict[int, Dict]:
    """
    Use the LLM to classify rows one at a time via direct Ollama API.

    Strategy: one row per call with a tiny prompt → fast local inference.
    Each call has a 45s timeout. Bypasses LangChain entirely.
    """
    if not unmatched_rows or not config_path:
        return {}

    try:
        base_url, model = _resolve_ollama_config(config_path, model_override)
    except Exception as e:
        _emit_mapper_progress(f"LLM config error: {e}")
        return {}

    total = len(unmatched_rows)
    _emit_mapper_progress(f"LLM fallback: {total} rows, one at a time ({_LLM_TIMEOUT_SECONDS}s each)")

    # Build compact account list for the prompt
    acct_list = "\n".join(account_tree)

    # Build a few example lines from rules-matched rows
    example_lines = ""
    if example_mappings:
        examples = example_mappings[:5]
        example_lines = "\nExamples:\n" + "\n".join(
            f"  {ex['description']} -> {ex['account']}" for ex in examples
        )

    account_set = set(account_tree)
    result: Dict[int, Dict] = {}

    for i, row in enumerate(unmatched_rows, 1):
        # Check for cancellation between rows
        try:
            from ui._runner import is_cancelled
            if is_cancelled():
                _emit_mapper_progress("LLM cancelled by user")
                break
        except ImportError:
            pass

        row_num = row["row"]
        desc = row["description"]
        amt_info = ""
        if row.get("deposit"):
            amt_info = f" [deposit]"
        elif row.get("withdrawal"):
            amt_info = f" [withdrawal]"

        user_prompt = (
            f"Accounts:\n{acct_list}\n{example_lines}\n\n"
            f"Transaction: {desc}{amt_info}\n"
            f"Account:"
        )

        _emit_mapper_progress(f"LLM row {i}/{total}: {desc[:40]}")

        reply = _ollama_chat(base_url, model, _LLM_SYSTEM_PROMPT, user_prompt, timeout=_LLM_TIMEOUT_SECONDS)
        if not reply:
            continue

        answer = reply.strip().split("\n")[0].strip()  # first line only
        if answer.upper() == "SKIP" or not answer:
            _emit_mapper_progress(f"  -> SKIP")
            result[row_num] = {"account": "", "reason": "LLM: skip"}
            continue

        # Validate against account tree
        if answer in account_set:
            _emit_mapper_progress(f"  -> {answer}")
            result[row_num] = {"account": answer, "reason": f"LLM: matched"}
        else:
            # Try partial match — LLM might omit "Root Account:" prefix
            matched_acct = None
            for acct in account_tree:
                if acct.endswith(answer) or answer in acct:
                    matched_acct = acct
                    break
            if matched_acct:
                _emit_mapper_progress(f"  -> {matched_acct} (partial)")
                result[row_num] = {"account": matched_acct, "reason": "LLM: partial match"}
            else:
                _emit_mapper_progress(f"  -> unknown: {answer!r}")

    matched = sum(1 for v in result.values() if v.get("account"))
    _emit_mapper_progress(f"LLM fallback complete: {matched}/{total} rows mapped")
    return result


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

    # Step 4: Smart pattern pass + LLM fallback for unmatched rows
    unmatched_count = result['confidence_counts'].get('none', 0)
    smart_mapped_count = 0
    llm_mapped_count = 0

    if unmatched_count > 0:
        # Re-read the mapped CSV
        with open(str(out_path), 'r', encoding='utf-8', errors='replace') as f:
            mapped_rows = list(csv.DictReader(f))

        # Build full account list (all banks)
        account_list = sorted(all_account_paths)

        # --- Step 4a: Smart pattern pass (deterministic, no LLM) ---
        _emit_mapper_progress(f"smart pattern pass for {unmatched_count} unmatched rows")
        for i, row in enumerate(mapped_rows):
            conf = row.get('Confidence', 'none')
            acct = row.get('Account', '')
            if conf != 'none' and acct:
                continue
            desc = row.get('Description') or row.get('Narration') or ''
            withdrawal = row.get('Withdrawal', '')
            deposit = row.get('Deposit', '')
            match = smart_pattern_match(desc, account_list, withdrawal, deposit)
            if match is not None:
                row['Account'] = match['account']
                row['Confidence'] = 'smart'
                row['MatchReason'] = f"Smart: {match['reason']}"
                if match['account']:
                    smart_mapped_count += 1
                    _emit_mapper_progress(f"  row {i+1}: {desc[:35]} -> {match['account'].rsplit(':', 1)[-1]}")
                else:
                    _emit_mapper_progress(f"  row {i+1}: {desc[:35]} -> {match['reason']}")

        if smart_mapped_count > 0:
            result['confidence_counts']['smart'] = smart_mapped_count
            result['confidence_counts']['none'] -= smart_mapped_count
            _emit_mapper_progress(f"smart pass: {smart_mapped_count} rows mapped")

        # --- Step 4b: LLM fallback for remaining unmatched ---
        still_unmatched = []
        example_mappings = []
        for i, row in enumerate(mapped_rows, 1):
            desc = row.get('Description') or row.get('Narration') or ''
            acct = row.get('Account', '')
            conf = row.get('Confidence', 'none')
            if (conf == 'none' or not acct) and conf != 'smart':
                still_unmatched.append({
                    'row': i,
                    'description': desc,
                    'withdrawal': row.get('Withdrawal', ''),
                    'deposit': row.get('Deposit', ''),
                })
            elif acct and conf in ('high', 'medium', 'smart'):
                example_mappings.append({'description': desc, 'account': acct})

        if still_unmatched and config_path:
            _emit_mapper_progress(f"LLM fallback for {len(still_unmatched)} remaining rows")
            llm_results = llm_fallback_mapping(
                unmatched_rows=still_unmatched,
                account_tree=account_list,
                example_mappings=example_mappings,
                config_path=config_path,
                model_override=model_override,
            )
            if llm_results:
                for i, row in enumerate(mapped_rows):
                    row_num = i + 1
                    if row_num in llm_results and llm_results[row_num].get('account'):
                        row['Account'] = llm_results[row_num]['account']
                        row['Confidence'] = 'llm'
                        row['MatchReason'] = f"LLM: {llm_results[row_num]['reason']}"
                        llm_mapped_count += 1
                if llm_mapped_count > 0:
                    result['confidence_counts']['none'] -= llm_mapped_count
                    result['confidence_counts']['llm'] = llm_mapped_count
        elif still_unmatched and not config_path:
            _emit_mapper_progress("no LLM config — skipping LLM fallback")
        elif not still_unmatched:
            _emit_mapper_progress("all rows resolved — no LLM needed")

        # Rewrite CSV if anything changed
        if smart_mapped_count > 0 or llm_mapped_count > 0:
            headers_out = list(mapped_rows[0].keys())
            with open(str(out_path), 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=headers_out, extrasaction='ignore')
                writer.writeheader()
                writer.writerows(mapped_rows)
            _emit_mapper_progress(
                f"CSV updated: +{smart_mapped_count} smart, +{llm_mapped_count} LLM"
            )
    else:
        _emit_mapper_progress("all rows matched by rules — no fallback needed")

    counts = result['confidence_counts']
    total = result['total_rows']
    pct = lambda n: f"{100 * n // total if total else 0}%"  # noqa: E731

    bank_note = f" ({bank_key} only)" if bank_key else ""
    extra_notes = []
    if smart_mapped_count:
        extra_notes.append(f"smart patterns mapped {smart_mapped_count}")
    if llm_mapped_count:
        extra_notes.append(f"LLM mapped {llm_mapped_count}")
    extra = (" + " + ", ".join(extra_notes)) if extra_notes else ""

    return (
        f"Mapped **{total} rows** using **{rule_count} rules** "
        f"(derived from {mapping_count} historical transactions{bank_note} in .gnucash).{extra}\n\n"
        f"**Confidence breakdown:**\n"
        f"- High: {counts.get('high', 0)} ({pct(counts.get('high', 0))})\n"
        f"- Medium: {counts.get('medium', 0)} ({pct(counts.get('medium', 0))})\n"
        f"- Low: {counts.get('low', 0)} ({pct(counts.get('low', 0))})\n"
        f"- Smart: {counts.get('smart', 0)} ({pct(counts.get('smart', 0))})\n"
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
