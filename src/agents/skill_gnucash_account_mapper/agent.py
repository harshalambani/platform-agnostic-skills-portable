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
# Account path helpers
# ---------------------------------------------------------------------------

_ROOT_PREFIX = "Root Account:"


def _strip_root(account_path: str) -> str:
    """Remove the 'Root Account:' prefix that GnuCash XML exports include.

    GnuCash's CSV importer expects paths starting from the first real
    level (e.g. 'Income:Bank Interest'), not 'Root Account:Income:Bank Interest'.
    """
    if account_path.startswith(_ROOT_PREFIX):
        return account_path[len(_ROOT_PREFIX):]
    return account_path


_SUSPENSE_DEFAULT = "Liabilities:Suspense"


def _find_suspense_account(account_tree: List[str]) -> str:
    """Find a Suspense account in the tree, or return a sensible default.

    Searches for existing accounts named Suspense, Unclassified, or
    Imbalance in the user's GnuCash book (after stripping Root Account:).
    Falls back to 'Assets:Suspense' which GnuCash will auto-create on import.
    """
    # Try common names in priority order
    for keyword in ("Suspense", "Unclassified", "Imbalance"):
        for acct in account_tree:
            clean = _strip_root(acct)
            leaf = clean.rsplit(":", 1)[-1] if ":" in clean else clean
            if leaf.lower() == keyword.lower():
                return clean
    return _SUSPENSE_DEFAULT


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

    # 3. Service charges: "MIN BAL CHRGS", "SERVICE CHARGE", "MAINTENANCE CHRG", "SMS Charges"
    if re.search(r'MIN\s*BAL\s*CHRGS|SERVICE\s*CHARGE?|MAINT.*CHRG|ANNUAL\s*FEE|SMS\s*CH(RG|ARGE)', desc_upper):
        for acct in account_tree:
            if "Bank Service Charge" in acct or "Service Charge" in acct:
                return {"account": acct, "reason": "Bank service charge pattern"}
        return None

    # 3b. Cash withdrawal/deposit: "BY CASH", "CASH DEPOSIT", "CASH WDL"
    if re.search(r'^BY\s+CASH$|^CASH\s+(DEPOSIT|WDL|WITHDRAWAL)|^CASH\s+W/D', desc_upper):
        # Cash transactions → Expenses:Other or manual review
        return {"account": "", "reason": "Cash transaction — manual review"}

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

    # 8. Cheque paid — only match if we can identify the payee account;
    #    otherwise fall through to LLM.
    if re.search(r'CHQ\s*PAID|CHEQUE\s*PAID|CHQ\s*CLG|CHEQUE\s*CLEARING', desc_upper):
        if re.search(r'PPF|PROVIDENT\s*FUND', desc_upper):
            for acct in account_tree:
                if "PPF" in acct or "Provident Fund" in acct:
                    return {"account": acct, "reason": "Cheque to PPF account"}
        if re.search(r'TAXBOND|TAX\s*BOND|NSC|KVP|GOV.?\s*BOND', desc_upper):
            for acct in account_tree:
                if any(k in acct for k in ("Tax Bond", "Investment", "Bond", "NSC")):
                    return {"account": acct, "reason": "Cheque for tax bond / govt investment"}
        if re.search(r'ICICI\s*HOME|HDFC\s*HOME|HOME\s*FIN|HOUSING\s*LOAN|HOME\s*LOAN', desc_upper):
            for acct in account_tree:
                if any(k in acct for k in ("Home Loan", "Housing Loan", "Mortgage")):
                    return {"account": acct, "reason": "Cheque for home loan EMI"}
        # No recognisable payee — let LLM try
        # (falls through to return None)

    # 9. Cheque return / bounce — genuinely unclassifiable, skip LLM
    if re.search(r'CHQ\s*RET|CHEQUE\s*RETURN|CHQ\s*BOUNCE|INWARD\s*RETURN', desc_upper):
        return {"account": "", "reason": "Cheque return/bounce — manual review"}

    # 10-11. IMPS / UPI / FT — only match if we can identify the account
    if re.search(r'IMPS|UPI', desc_upper):
        if re.search(r'ICICI\s*HOME|HDFC\s*HOME|HOME\s*FIN|HOUSING\s*LOAN', desc_upper):
            for acct in account_tree:
                if any(k in acct for k in ("Home Loan", "Housing Loan", "Mortgage")):
                    return {"account": acct, "reason": "IMPS for home loan"}
        # No recognisable payee — let LLM try

    # 12. Loan EMI / auto-debit — match only if account found
    if re.search(r'EMI|LOAN\s*REPAY|HOME\s*LOAN|AUTO\s*DEBIT.*LOAN', desc_upper):
        for acct in account_tree:
            if any(k in acct for k in ("Home Loan", "Loan", "EMI", "Mortgage")):
                return {"account": acct, "reason": "Loan EMI / auto-debit"}

    # 13. Insurance premium — match only if account found
    if re.search(r'LIC\s*OF\s*INDIA|INSURANCE\s*PREM|LIFE\s*INSURANCE|GEN.*INSURANCE|HEALTH.*INSUR', desc_upper):
        for acct in account_tree:
            if "Insurance" in acct:
                return {"account": acct, "reason": "Insurance premium"}

    # 14. Tax payment — match only if account found
    if re.search(r'ADVANCE\s*TAX|SELF\s*ASSESS.*TAX|INCOME\s*TAX|TDS\s*PAYMENT|CHALLAN', desc_upper):
        for acct in account_tree:
            if any(k in acct for k in ("Income Tax", "Tax", "Advance Tax")):
                return {"account": acct, "reason": "Tax payment"}

    # 15. Salary / pension — match only if account found
    if re.search(r'SALARY|PENSION|PAY\s*CREDIT', desc_upper):
        for acct in account_tree:
            if any(k in acct for k in ("Salary", "Pension", "Employment")):
                return {"account": acct, "reason": "Salary/pension credit"}

    # 16. Self cheque / cash withdrawal (various formats: SELF 1579-CHQ PAID, SELF - CHQ PAID, etc.)
    if re.search(r'SELF[\s/]*(?:\d+[\s\-]*)?(?:\-\s*)?CHQ\s*PAID', desc_upper):
        for acct in account_tree:
            if acct.endswith(":Cash") or (":Cash and Bank:Cash" in acct):
                return {"account": acct, "reason": "Self cheque / cash withdrawal"}

    # 17. Cheque book charges
    if re.search(r'CH(EQUE|Q)\s*B(OO)?K\s*CH(RG|GS|ARGE)', desc_upper):
        for acct in account_tree:
            if "Bank Charges" in acct or "Bank Service" in acct:
                return {"account": acct, "reason": "Cheque book charges"}

    return None


# ---------------------------------------------------------------------------
# Historical prefix matching — deterministic fuzzy match via description prefix
# ---------------------------------------------------------------------------

def _historical_prefix_match(
    desc: str,
    historical_mappings: List[Dict],
) -> Optional[Dict]:
    """Match by stripping trailing reference numbers and comparing prefixes.

    Bank narrations like 'BAJAJ FINANCE -5150102' differ from historical
    'BAJAJ FINANCE -808693' only in the reference number.  Stripping the
    trailing digits and comparing the prefix catches these deterministically.
    Falls back to keyword matching against account leaf names.
    """
    def _norm(s: str) -> str:
        import re as _re
        s = s.strip().upper()
        s = _re.sub(r'[-\s]*\d{5,}.*$', '', s)           # trailing ref numbers
        s = _re.sub(r'[-\s]*\d{2}-\d{2}-\d{4}.*$', '', s)  # trailing dates
        return s.strip().rstrip('-').strip()

    norm_desc = _norm(desc)
    if len(norm_desc) < 6:
        return None

    best_account = None
    best_score = 0
    best_freq = 0

    for m in historical_mappings:
        hist_norm = _norm(m['description'])
        if len(hist_norm) < 6:
            continue
        freq = m.get('frequency', 1)

        if norm_desc == hist_norm:
            score = len(norm_desc) * 2
        elif len(norm_desc) >= 10 and len(hist_norm) >= 10:
            # Character-level prefix overlap
            common = 0
            for a, b in zip(norm_desc, hist_norm):
                if a == b:
                    common += 1
                else:
                    break
            if common >= 10:
                score = common
            else:
                continue
        else:
            continue

        if score > best_score or (score == best_score and freq > best_freq):
            best_score = score
            best_freq = freq
            best_account = m['account']

    if best_account:
        return {"account": best_account, "reason": f"Prefix match ({norm_desc[:30]})"}

    # Fallback: keyword match — description words vs. account leaf names
    _STOP = {'MICR', 'PAID', 'NEFT', 'IMPS', 'INCL', 'FROM', 'WITH',
             'BANK', 'TRAN', 'INWARD', 'TRANSFER', 'CLEARING', 'MUMBAI'}
    desc_words = set(re.findall(r'[A-Z]{4,}', desc.upper())) - _STOP

    seen: set = set()
    for m in historical_mappings:
        acct = m['account']
        if acct in seen:
            continue
        seen.add(acct)
        leaf = acct.rsplit(':', 1)[-1] if ':' in acct else acct
        leaf_words = set(re.findall(r'[A-Za-z]{4,}', leaf.upper()))
        common = desc_words & leaf_words
        if common and max(len(w) for w in common) >= 5:
            return {"account": acct, "reason": f"Keyword match ({', '.join(sorted(common))})"}

    return None


# ---------------------------------------------------------------------------
# LLM retry with focused prompt (fewer account groups)
# ---------------------------------------------------------------------------

def _retry_with_focused_prompt(
    desc: str,
    amt_info: str,
    historical_mappings: List[Dict],
    base_url: str,
    model: str,
) -> Optional[str]:
    """Retry LLM with a shorter prompt containing only the most relevant groups."""
    from collections import defaultdict

    desc_upper = desc.upper()
    desc_words = set(re.findall(r'[A-Z]{3,}', desc_upper))

    groups: Dict[str, list] = defaultdict(list)
    for m in historical_mappings:
        groups[m['account']].append((m['description'], m.get('frequency', 1)))

    scored = []
    for acct, descs in groups.items():
        score = 0
        for d, freq in descs:
            d_words = set(re.findall(r'[A-Z]{3,}', d.upper()))
            overlap = desc_words & d_words
            score += sum(len(w) for w in overlap) * freq
        if score > 0:
            scored.append((score, acct, descs))

    scored.sort(reverse=True)
    top = scored[:3]
    if not top:
        return None

    lines = []
    for _, acct, descs in top:
        descs.sort(key=lambda x: x[1], reverse=True)
        lines.append(f"\n{acct}:")
        for d, freq in descs[:5]:
            freq_note = f" (x{freq})" if freq > 1 else ""
            lines.append(f"  - {d}{freq_note}")

    grouped_text = "\n".join(lines)
    user_prompt = (
        f"Most likely accounts for this transaction:{grouped_text}\n\n"
        f"Transaction: {desc}{amt_info}\n"
        f"Account:"
    )

    reply = _ollama_chat(base_url, model, _LLM_SYSTEM_PROMPT, user_prompt,
                         timeout=_LLM_TIMEOUT_SECONDS)
    if reply:
        _emit_mapper_progress(f"  -> (retry matched)")
    return reply


# ---------------------------------------------------------------------------
# LLM fallback — direct Ollama /api/chat (bypasses LangChain)
# ---------------------------------------------------------------------------

_LLM_TIMEOUT_SECONDS = 60      # per-row timeout (after model is warm)
_LLM_WARMUP_TIMEOUT  = 180     # first call loads model into VRAM — needs longer

_LLM_SYSTEM_PROMPT = """\
You are a bank transaction classifier for GnuCash. Given a list of accounts with example transactions, pick the best matching account for a new transaction.

Rules:
- Use ONLY accounts from the provided list. NEVER invent or combine account paths.
- Match by similarity: shared keywords, payee names, reference numbers, transaction types.
- Bank descriptions often abbreviate or truncate — e.g. "SELF1579-CHQPAID" is similar to "SELF-CHQPAID".
- Reply with the FULL account path EXACTLY as shown, on a single line, nothing else.
- If no account is similar enough, reply SKIP.

Examples of correct replies:
  Transaction: NEFT CR-SBIN0000TBU-ITDTAX REFUND -> Expenses:Income Tax Refund
  Transaction: ACH C- MMFSL INT-0000000IW07 -> Income:Interest on FD
  Transaction: UPI-SWIGGY-Q1234@YBL -> Expenses:Food and Dining
  Transaction: SELF - CHQ PAID -> Assets:Current Assets:Cash and Bank:Cash

WRONG (never do this):
  Income:Assets:Current Assets:Cash and Bank:SELF 1579-CHQ PAID  <-- invented path with description mixed in"""


def _score_account_relevance(
    groups: Dict[str, List[Tuple[str, int]]],
    desc: str,
) -> List[Tuple[float, str, List[Tuple[str, int]]]]:
    """Score account groups by keyword overlap with the transaction description.

    Returns a sorted list of (score, account_path, descriptions) — highest first.
    """
    desc_words = set(re.findall(r'[A-Z]{2,}', desc.upper()))
    scored = []
    for acct, descs in groups.items():
        score = 0.0
        for d, freq in descs:
            d_words = set(re.findall(r'[A-Z]{2,}', d.upper()))
            overlap = desc_words & d_words
            score += sum(len(w) for w in overlap) * freq
        scored.append((score, acct, descs))
    scored.sort(reverse=True)
    return scored


def _build_historical_prompt(historical_mappings: List[Dict], desc: str, amt_info: str) -> str:
    """Build a focused prompt with only the most relevant accounts.

    Instead of dumping all 200+ examples (which overwhelms small models),
    pre-filter to the top 12 accounts by keyword overlap with the transaction.
    """
    from collections import defaultdict
    groups: Dict[str, List[Tuple[str, int]]] = defaultdict(list)
    for m in historical_mappings:
        groups[m['account']].append((m['description'], m.get('frequency', 1)))

    # Score and rank accounts by relevance to this transaction
    scored = _score_account_relevance(groups, desc)

    # Take top 12 accounts (mix of relevant + high-frequency fallbacks)
    top_relevant = scored[:10]
    # Also include top 2 by frequency that aren't already included
    top_names = {acct for _, acct, _ in top_relevant}
    freq_sorted = sorted(
        groups.items(),
        key=lambda kv: sum(f for _, f in kv[1]),
        reverse=True,
    )
    for acct, descs in freq_sorted:
        if acct not in top_names:
            top_relevant.append((0, acct, descs))
            top_names.add(acct)
        if len(top_relevant) >= 12:
            break

    lines = []
    for _, acct, descs in top_relevant:
        descs_sorted = sorted(descs, key=lambda x: x[1], reverse=True)
        lines.append(f"\n{acct}:")
        for d, freq in descs_sorted[:5]:
            freq_note = f" (x{freq})" if freq > 1 else ""
            lines.append(f"  - {d}{freq_note}")

    grouped_text = "\n".join(lines)

    return (
        f"Candidate accounts with example transactions:{grouped_text}\n\n"
        f"Transaction: {desc}{amt_info}\n"
        f"Account:"
    )


def _resolve_ollama_config(config_path: str, model_override: str = None) -> Tuple[str, str]:
    """Read config.yaml and return (base_url, model_name)."""
    cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
    ep = cfg.get("ollama") or {}
    base_url = ep.get("base_url", "http://localhost:11434").rstrip("/")
    model = model_override or ep.get("default_model") or "gemma4:12b"
    return base_url, model


def _ollama_chat(base_url: str, model: str, system: str, user: str, timeout: float = 60.0) -> Optional[str]:
    """Call Ollama /api/chat directly. Returns the assistant reply or None."""
    from urllib import request as _req, error as _err

    payload = json.dumps({
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "stream": False,
        "options": {"temperature": 0.0, "num_predict": 120},
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


def _validate_llm_answer(answer: str, account_set: set) -> Optional[str]:
    """Validate an LLM answer against known accounts.

    Returns the matched account path (exact or partial) or None.
    """
    if not answer:
        return None
    # Exact match
    if answer in account_set:
        return answer
    # Partial match — LLM might omit prefix or truncate
    for acct in account_set:
        if acct.endswith(answer) or answer in acct:
            return acct
    # Strip common hallucination prefixes (e.g. "Account: Expenses:...")
    for prefix in ("Account:", "->", "account:"):
        if answer.startswith(prefix):
            cleaned = answer[len(prefix):].strip()
            if cleaned in account_set:
                return cleaned
            for acct in account_set:
                if acct.endswith(cleaned) or cleaned in acct:
                    return acct
    return None


def llm_fallback_mapping(
    unmatched_rows: List[Dict],
    account_tree: List[str],
    example_mappings: List[Dict],
    config_path: str,
    model_override: str = None,
    historical_mappings: List[Dict] = None,
) -> Dict[int, Dict]:
    """
    Use the LLM to classify rows one at a time via direct Ollama API.

    Strategy: one row per call with a structured prompt that groups
    historical GnuCash patterns by account, turning classification into
    pattern matching rather than cold reasoning.
    """
    if not unmatched_rows or not config_path:
        return {}

    try:
        base_url, model = _resolve_ollama_config(config_path, model_override)
    except Exception as e:
        _emit_mapper_progress(f"LLM config error: {e}")
        return {}

    total = len(unmatched_rows)
    hist_count = len(historical_mappings) if historical_mappings else 0
    _emit_mapper_progress(
        f"LLM fallback: {total} rows, {hist_count} historical examples, model={model}"
    )

    # ── Warm up the model (cold start loads weights into VRAM) ───────────
    _emit_mapper_progress(f"LLM warm-up: loading {model} (up to {_LLM_WARMUP_TIMEOUT}s)…")
    warmup_reply = _ollama_chat(
        base_url, model,
        "Reply OK.", "ping",
        timeout=_LLM_WARMUP_TIMEOUT,
    )
    if warmup_reply is None:
        _emit_mapper_progress("LLM warm-up failed — skipping LLM fallback")
        return {}
    _emit_mapper_progress("LLM warm-up OK — model loaded")

    # Build the set of valid accounts from historical mappings (preferred)
    # or fall back to full account tree
    if historical_mappings:
        account_set = {m['account'] for m in historical_mappings if m.get('account')}
    else:
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
            amt_info = " [deposit]"
        elif row.get("withdrawal"):
            amt_info = " [withdrawal]"

        # Build prompt — use grouped historical patterns if available,
        # otherwise fall back to flat account list + thin examples
        if historical_mappings:
            user_prompt = _build_historical_prompt(historical_mappings, desc, amt_info)
        else:
            acct_list = "\n".join(account_tree)
            example_lines = ""
            if example_mappings:
                examples = example_mappings[:5]
                example_lines = "\nExamples:\n" + "\n".join(
                    f"  {ex['description']} -> {ex['account']}" for ex in examples
                )
            user_prompt = (
                f"Accounts:\n{acct_list}\n{example_lines}\n\n"
                f"Transaction: {desc}{amt_info}\n"
                f"Account:"
            )

        _emit_mapper_progress(f"LLM row {i}/{total}: {desc[:40]}")

        reply = _ollama_chat(base_url, model, _LLM_SYSTEM_PROMPT, user_prompt, timeout=_LLM_TIMEOUT_SECONDS)

        if not reply:
            # Retry with focused prompt — top 3 account groups by keyword overlap
            if historical_mappings:
                reply = _retry_with_focused_prompt(
                    desc, amt_info, historical_mappings,
                    base_url, model,
                )
            if not reply:
                continue

        answer = reply.strip().split("\n")[0].strip()  # first line only
        if answer.upper() == "SKIP" or not answer:
            _emit_mapper_progress(f"  -> SKIP ({answer!r})")
            result[row_num] = {"account": "", "reason": "LLM: skip"}
            continue

        # Validate against known accounts
        matched_acct = _validate_llm_answer(answer, account_set)

        if not matched_acct and historical_mappings:
            # First answer was garbage — retry with focused prompt
            _emit_mapper_progress(f"  -> invalid ({answer[:40]!r}), retrying focused…")
            retry_reply = _retry_with_focused_prompt(
                desc, amt_info, historical_mappings,
                base_url, model,
            )
            if retry_reply:
                retry_answer = retry_reply.strip().split("\n")[0].strip()
                if retry_answer.upper() != "SKIP" and retry_answer:
                    matched_acct = _validate_llm_answer(retry_answer, account_set)

        if matched_acct:
            _emit_mapper_progress(f"  -> {matched_acct}")
            result[row_num] = {"account": matched_acct, "reason": "LLM: matched"}
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
        mapped_row['Account'] = _strip_root(account) if account else ''
        mapped_row['Confidence'] = confidence
        mapped_row['MatchReason'] = reason

        mapped_rows.append(mapped_row)
        confidence_counts[confidence] = confidence_counts.get(confidence, 0) + 1

        if confidence in ('low', 'none'):
            manual_review.append({
                'row': row_num,
                'description': description[:60],
                'assigned_account': _strip_root(account) if account else '',
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
    gnucash_bank_account: str = None,
) -> str:
    """
    Run the full account-mapping pipeline from the PA Skills UI.

    Chains:
        1. skill_gnucash_xml_extractor  — parse .gnucash → description→account history
        2. skill_gnucash_mapping_generator — build YAML rules from same bank's history
        3. map_accounts()               — apply rules to canonical CSV

    Args:
        gnucash_file:        Path to .gnucash book (gzipped XML format).
        canonical_csv:       Path to canonical 8-col CSV (from ICICI/HSBC/BoB/HDFC skills).
        output_path:         Path for the mapped output CSV.
        config_path:         Unused (no LLM required).
        model_override:      Unused (no LLM required).
        bank_name:           Pipeline bank label (e.g. "Bank of Baroda"). When set,
                             rules are generated ONLY from that bank's historical
                             transactions — not from other banks.
        gnucash_bank_account: Full GnuCash account path for the bank (e.g.
                             "Assets:Current Assets:Cash and Bank:HDFC Bank - ...").
                             When set, the output CSV uses GnuCash-compatible columns:
                             Account = bank account, Transfer Account = category.

    Returns:
        Human-readable result string for the UI.
    """
    # Make sibling agents importable
    agents_root = Path(__file__).resolve().parent.parent
    if str(agents_root) not in sys.path:
        sys.path.insert(0, str(agents_root))

    from skill_gnucash_xml_extractor.agent import parse_gnucash_file          # noqa: E402
    from skill_gnucash_mapping_generator.agent import generate_rules         # noqa: E402
    from skill_gnucash_account_mapper.persistent_rules import (              # noqa: E402
        merge_auto_rules, load_overrides, match_overrides,
        migrate_legacy_overrides, rules_path as persistent_rules_path,
        save_rules, load_rules,
    )

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

    # Save historical mappings for LLM few-shot context
    historical_pairs_for_llm: List[Dict] = []

    if bank_key:
        # Filter to only the importing bank's historical transactions
        all_mappings = extractor_output.get('mappings', {})
        bank_mappings = all_mappings.get(bank_key, [])
        extractor_output['mappings'] = {bank_key: bank_mappings}
        mapping_count = len(bank_mappings)
        historical_pairs_for_llm = bank_mappings
        _emit_mapper_progress(f"filtered to {bank_key}: {mapping_count} historical pairs")
    else:
        mapping_count = sum(
            len(v) for v in extractor_output.get('mappings', {}).values()
        )
        # Flatten all banks for LLM context when no specific bank
        for bank_maps in extractor_output.get('mappings', {}).values():
            historical_pairs_for_llm.extend(bank_maps)
        _emit_mapper_progress(f"extracted {mapping_count} pairs (all banks)")

    # Step 1.5: Migrate legacy _account_overrides.yaml if present
    migrated = migrate_legacy_overrides(gnucash_file, config_path)
    if migrated:
        _emit_mapper_progress(f"migrated {migrated} legacy overrides into unified rules")

    # Step 2: Generate rules from extractor output + merge into persistent YAML
    _emit_mapper_progress(f"generating rules (bank={bank_key or 'all'})")
    rules_by_bank = generate_rules(extractor_output, min_freq=1 if bank_key else 3)
    all_rules: List[dict] = []
    for bank_rules in rules_by_bank.values():
        all_rules.extend(bank_rules)
    rule_count = len(all_rules)
    _emit_mapper_progress(f"generated {rule_count} new rules")

    # Merge into single persistent YAML alongside .gnucash file
    merged = merge_auto_rules(gnucash_file, rules_by_bank, config_path)
    merged_total = sum(len(v) for v in merged.values())
    _emit_mapper_progress(f"persistent rules: {merged_total} total (in {persistent_rules_path(gnucash_file, config_path).name})")

    # Write merged rules to a temp file for map_accounts() (expects a file path)
    import tempfile
    rules_tmp = Path(tempfile.mktemp(suffix="_mapping_rules.yaml"))
    # map_accounts expects {BankKey: [rules...]} format — write merged (minus _overrides)
    rules_for_mapper = {k: v for k, v in merged.items() if k != "_overrides"}
    import yaml as _yaml
    rules_tmp.write_text(
        _yaml.dump(rules_for_mapper, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )

    # Load user overrides (if any) for this GnuCash file
    overrides = load_overrides(gnucash_file, config_path)
    if overrides:
        _emit_mapper_progress(f"loaded {len(overrides)} user overrides")

    # Step 3: Apply rules to canonical CSV
    report_path = out_path.with_name(out_path.stem + "_confidence.txt")
    _emit_mapper_progress(f"applying rules to {Path(canonical_csv).name}")
    result = map_accounts(canonical_csv, str(rules_tmp), str(out_path), str(report_path))

    # Clean up temp rules file
    try:
        rules_tmp.unlink()
    except OSError:
        pass

    # Step 3.5: User overrides pass (highest priority) ─────────────────────
    # Override pass runs on ALL rows (including matched ones) since overrides
    # are meant to correct any wrong mapping, not just fill gaps.
    override_count = 0
    if overrides:
        _emit_mapper_progress(f"applying {len(overrides)} user overrides")
        with open(str(out_path), 'r', encoding='utf-8', errors='replace') as f:
            mapped_rows = list(csv.DictReader(f))

        for i, row in enumerate(mapped_rows):
            desc = row.get('Description') or row.get('Narration') or ''
            acct, reason = match_overrides(desc, overrides)
            if acct:
                row['Account'] = _strip_root(acct) if acct.startswith('Root Account:') else acct
                row['Confidence'] = 'override'
                row['MatchReason'] = f"Override: {reason}"
                override_count += 1
                _emit_mapper_progress(f"  row {i+1}: override matched -> {acct.rsplit(':', 1)[-1] if ':' in acct else acct}")

        if override_count > 0:
            result['confidence_counts']['override'] = override_count
            _emit_mapper_progress(f"override pass: {override_count} rows matched")

            # Rewrite CSV with overrides applied
            raw_keys = list(mapped_rows[0].keys())
            with open(str(out_path), 'w', newline='', encoding='utf-8') as f:
                writer = csv.DictWriter(f, fieldnames=raw_keys)
                writer.writeheader()
                writer.writerows(mapped_rows)

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
            if match is None and historical_pairs_for_llm:
                match = _historical_prefix_match(desc, historical_pairs_for_llm)
            if match is not None:
                row['Account'] = _strip_root(match['account']) if match['account'] else ''
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
            if (conf == 'none' or not acct) and conf not in ('smart', 'override'):
                still_unmatched.append({
                    'row': i,
                    'description': desc,
                    'withdrawal': row.get('Withdrawal', ''),
                    'deposit': row.get('Deposit', ''),
                })
            elif acct and conf in ('high', 'medium', 'smart', 'override'):
                example_mappings.append({'description': desc, 'account': acct})

        if still_unmatched and config_path:
            _emit_mapper_progress(f"LLM fallback for {len(still_unmatched)} remaining rows")
            llm_results = llm_fallback_mapping(
                unmatched_rows=still_unmatched,
                account_tree=account_list,
                example_mappings=example_mappings,
                config_path=config_path,
                model_override=model_override,
                historical_mappings=historical_pairs_for_llm,
            )
            if llm_results:
                for i, row in enumerate(mapped_rows):
                    row_num = i + 1
                    if row_num in llm_results and llm_results[row_num].get('account'):
                        row['Account'] = _strip_root(llm_results[row_num]['account'])
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
            _emit_mapper_progress(
                f"pattern/LLM pass: +{smart_mapped_count} smart, +{llm_mapped_count} LLM"
            )
    else:
        _emit_mapper_progress("all rows matched by rules — no fallback needed")

    # --- Step 5: Suspense pass — assign remaining unmapped rows ---
    # Find a Suspense account in the tree, or use a sensible default.
    suspense_acct = _find_suspense_account(account_list)
    suspense_count = 0
    for row in mapped_rows:
        acct = row.get('Account', '')
        conf = row.get('Confidence', 'none')
        if not acct or conf == 'none':
            row['Account'] = suspense_acct
            row['Confidence'] = 'suspense'
            row['MatchReason'] = 'Suspense — review and reassign in GnuCash'
            suspense_count += 1
    if suspense_count > 0:
        result['confidence_counts']['none'] -= suspense_count
        result['confidence_counts']['suspense'] = suspense_count
        _emit_mapper_progress(f"suspense pass: {suspense_count} rows -> {suspense_acct}")

    # --- Restructure columns for GnuCash import ---
    # GnuCash CSV import column mapping (from the user's perspective):
    #   Account                    = the category/split account (e.g. Income:Bank Interest)
    #   Transfer Account           = the bank account (e.g. Assets:…:HDFC Bank - …)
    #   Deposit                    = deposit amount
    #   Withdrawal                 = withdrawal amount
    # "Account" already holds the category from mapping — just add the rest.
    if gnucash_bank_account:
        _emit_mapper_progress(f"restructuring columns for GnuCash (bank={gnucash_bank_account[:40]}…)")
        for row in mapped_rows:
            # Account stays as-is (category)
            row['Transfer Account'] = gnucash_bank_account

    # --- Always rewrite CSV (Root Account prefix was stripped) ---
    # Build ordered header list:
    #   - Transfer Account right after Account
    #   - Renamed deposit/withdrawal columns where originals were (before Balance)
    raw_keys = list(mapped_rows[0].keys())
    _REPOSITION = {'Transfer Account', 'Deposit', 'Withdrawal'}
    if 'Transfer Account' in raw_keys:
        headers_out = []
        for k in raw_keys:
            if k in _REPOSITION:
                continue  # will be inserted at correct position
            headers_out.append(k)
            if k == 'Account':
                headers_out.append('Transfer Account')
        if 'Deposit' in raw_keys:
            bal_idx = headers_out.index('Balance') if 'Balance' in headers_out else len(headers_out)
            headers_out.insert(bal_idx, 'Withdrawal')
            headers_out.insert(bal_idx, 'Deposit')
    else:
        headers_out = raw_keys
    with open(str(out_path), 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=headers_out, extrasaction='ignore')
        writer.writeheader()
        writer.writerows(mapped_rows)
    _emit_mapper_progress(f"CSV written: {len(mapped_rows)} rows")

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
        f"- Low: {counts.get('low', 0)} ({pct(counts.get('low', 0))})\n"
        f"- Smart: {counts.get('smart', 0)} ({pct(counts.get('smart', 0))})\n"
        f"- LLM: {counts.get('llm', 0)} ({pct(counts.get('llm', 0))})\n"
        f"- `{out_path.name}` — mapped CSV, ready for GnuCash import\n"
        f"- `{report_path.name}` — confidence report (review Low/No-match rows)\n"
        f"- `{persistent_rules_path(gnucash_file, config_path).name}` — persistent mapping rules (alongside .gnucash)"
    )
