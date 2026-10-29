"""Mask sensitive values before they reach a log, a screen or an error text.

One shared helper, used by the run logger (ui/_runner.py, ui/_runlog.py) and
by the skills that call external tools. It works on three shapes:

  * mappings and lists (a tool call's arguments), nested to any depth: the
    value of any key in REDACT_KEYS becomes "***";
  * argv-style lists: the item after "--password" (or any flag whose name is
    in REDACT_KEYS) becomes "***", and "--password=x" becomes
    "--password=***";
  * plain text: "password=x", "--password x", "--password=x" and bearer
    tokens are masked, as is every explicit secret string given.

Nothing here raises: masking must never be the thing that breaks a run.
"""
from __future__ import annotations

import re

MASK = "***"

# Lower-case names (matched after stripping leading dashes and normalising
# "-" to "_"). A key is also sensitive when it CONTAINS one of the fragments
# in _REDACT_FRAGMENTS, so pdf_password, doc_password and cas_password are
# covered without being listed one by one.
REDACT_KEYS = frozenset({
    "password", "passwd", "pwd", "pass", "pin", "secret", "token",
    "api_key", "apikey", "key", "access_token", "auth", "authorization",
    "upw", "opw",
})
_REDACT_FRAGMENTS = ("password", "passwd", "secret", "token", "api_key",
                     "apikey")


# A flag like "--password-file" or "--password-stdin" names WHERE the secret
# is read from, not the secret itself.
_SOURCE_SUFFIXES = ("_file", "_stdin", "_env")


def is_sensitive_key(name: object) -> bool:
    """True if a mapping key or command-line flag names a secret."""
    k = str(name).strip().lstrip("-").lower().replace("-", "_")
    if not k or k.endswith(_SOURCE_SUFFIXES):
        return False
    if k in REDACT_KEYS:
        return True
    return any(f in k for f in _REDACT_FRAGMENTS)


def redact_value(obj, secrets=()):
    """Return a copy of ``obj`` with sensitive values masked.

    Dict values under a sensitive key are masked whole; lists are scanned as
    argv (flag then value); strings get the text rules. Tuples come back as
    lists. Anything else is returned unchanged.
    """
    try:
        if isinstance(obj, dict):
            out = {}
            for k, v in obj.items():
                out[k] = MASK if (is_sensitive_key(k) and v not in (None, "")) \
                    else redact_value(v, secrets)
            return out
        if isinstance(obj, (list, tuple)):
            return redact_argv(list(obj), secrets)
        if isinstance(obj, str):
            return redact_text(obj, secrets)
    except Exception:
        return MASK
    return obj


def redact_argv(argv, secrets=()):
    """Mask the secret in a command line given as a list of items."""
    out = []
    mask_next = False
    for item in argv:
        if mask_next:
            out.append(MASK)
            mask_next = False
            continue
        if isinstance(item, str):
            m = re.match(r"^(-{1,2}[A-Za-z0-9_-]+)=(.*)$", item, re.S)
            if m and is_sensitive_key(m.group(1)):
                out.append(f"{m.group(1)}={MASK}")
                continue
            if item.startswith("-") and "=" not in item and is_sensitive_key(item):
                out.append(item)
                mask_next = True
                continue
        out.append(redact_value(item, secrets) if not isinstance(item, str)
                   else redact_text(item, secrets))
    return out


_BEARER_RX = re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{8,}")
# name=value / name: value / "--name value" / "--name=value", for the
# sensitive names. The value runs to whitespace, a quote, a comma or a brace.
_NAMES = (r"password|passwd|pwd|pin|secret|api[_-]?key|access[_-]?token|"
          r"token|upw|opw|\w*password\w*")
_VAL = r"(?!\*\*\*)([^\s\"',;}\]]+)"
# "--password x" / "--password=x" / "-upw x"
_FLAG_RX = re.compile(
    r"(?i)(?<![A-Za-z0-9_])(--?(?:" + _NAMES + r")(?!-file|-stdin)(?:\s+|=))[\"']?" + _VAL)
# password=x / password: x / "pdf_password": "x" (never plain prose)
_KEYVAL_RX = re.compile(
    r"(?i)(?<![A-Za-z0-9_-])((?:" + _NAMES + r")[\"']?\s*[:=]\s*[\"']?)" + _VAL)


def redact_text(text: str, secrets=()) -> str:
    """Mask explicit secrets, bearer tokens and name=value pairs in text."""
    try:
        out = text
        for s in sorted((x for x in (secrets or ()) if isinstance(x, str) and x),
                        key=len, reverse=True):
            out = out.replace(s, MASK)
        out = _BEARER_RX.sub("Bearer " + MASK, out)
        out = _FLAG_RX.sub(lambda m: m.group(1) + MASK, out)
        out = _KEYVAL_RX.sub(lambda m: m.group(1) + MASK, out)
        return out
    except Exception:
        return MASK
