"""
tools.py - deterministic tools for the dev-time Parser Generator skill.

These tools never let the LLM rewrite a parser freehand. The only mutation paths
are apply_template_edit (rewrite the value of an existing module-level CONSTANT)
and create_parser_from_template (fill the same constants into the shipped
skeleton). The parser's control flow, its balance oracle, and its 0/1/2 exit
contract are therefore unreachable from here - exactly the "fill the blanks,
keep the oracle" guarantee in AGENT.md.

Every function has a plain `_impl`-style core (stdlib only, fully unit-testable)
plus a thin @tool wrapper that the LangGraph agent calls.
"""
from __future__ import annotations

import ast
import json
import re
import subprocess
import sys
from pathlib import Path

from langchain_core.tools import tool

TEMPLATE_PATH = Path(__file__).parent / "templates" / "parser_template.py"

# Decision #1: generated/edited parsers live here and nowhere else.
_LOCATION_RE = re.compile(r"agents/skill_[^/]+/scripts/parse_[^/]+\.py$")


# ---------------------------------------------------------------------------
# AST-safe constant rewriting (the only mutation primitive)
# ---------------------------------------------------------------------------

def _is_constant_name(name: str) -> bool:
    """A 'blank' is a module-level UPPER_CASE constant assignment."""
    return name.isupper() and any(c.isalpha() for c in name)


def _abs_offset(source: str, line: int, col: int) -> int:
    """Char offset into `source` for a 1-based line / utf-8 byte col (ast)."""
    lines = source.splitlines(keepends=True)
    before = "".join(lines[: line - 1])
    line_str = lines[line - 1] if line - 1 < len(lines) else ""
    col_prefix = line_str.encode("utf-8")[:col].decode("utf-8", "ignore")
    return len(before) + len(col_prefix)


def constant_spans(source: str) -> dict[str, tuple[int, int]]:
    """
    Map each module-level constant name to the (start, end) char span of its
    *value* (right-hand side) in `source`. Raises SyntaxError if `source` does
    not parse.
    """
    tree = ast.parse(source)
    spans: dict[str, tuple[int, int]] = {}
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and _is_constant_name(node.targets[0].id)
        ):
            v = node.value
            start = _abs_offset(source, v.lineno, v.col_offset)
            end = _abs_offset(source, v.end_lineno, v.end_col_offset)
            spans[node.targets[0].id] = (start, end)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.value is not None
            and _is_constant_name(node.target.id)
        ):
            v = node.value
            start = _abs_offset(source, v.lineno, v.col_offset)
            end = _abs_offset(source, v.end_lineno, v.end_col_offset)
            spans[node.target.id] = (start, end)
    return spans


# Node types a literal blank value is allowed to be built from. Anything else
# (Call, Attribute, Name, BinOp, comprehensions, imports, ...) means the "value"
# is actually code, not data - reject it so an LLM can never smuggle a
# side-effecting expression into a spliced-and-executed parser.
_LITERAL_NODE_TYPES = (
    ast.Expression,
    ast.Constant,
    ast.Tuple,
    ast.List,
    ast.Dict,
    ast.Set,
    ast.Load,
)


def _contains_non_literal_code(value_src: str) -> bool:
    """True if `value_src` parses as a Python expression containing anything
    other than literal data - a Call, Name, Attribute, import, etc. Values
    that fail to parse at all are *not* flagged here; that SyntaxError is
    surfaced later by the whole-file splice-and-parse check instead, so
    genuinely malformed edits keep raising SyntaxError as before."""
    try:
        tree = ast.parse(value_src, mode="eval")
    except SyntaxError:
        return False
    for node in ast.walk(tree):
        if isinstance(node, _LITERAL_NODE_TYPES):
            continue
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, (ast.UAdd, ast.USub)):
            continue
        return True
    return False


def rewrite_constants(source: str, new_values: dict[str, str]) -> str:
    """
    Replace the value of each named constant with new RHS source text.

    `new_values` maps CONSTANT_NAME -> replacement source (e.g. "(525, 555)").
    Raises KeyError listing any names that are not editable constants,
    ValueError if a replacement value is not a pure literal (see
    `_is_pure_literal`), and SyntaxError if the result does not parse.
    Splices from the end backwards so earlier offsets stay valid.
    """
    spans = constant_spans(source)
    unknown = [k for k in new_values if k not in spans]
    if unknown:
        raise KeyError(
            f"not editable blank constant(s): {', '.join(sorted(unknown))}; "
            f"valid blanks: {', '.join(sorted(spans))}"
        )
    non_literal = [k for k, v in new_values.items() if _contains_non_literal_code(v)]
    if non_literal:
        raise ValueError(
            f"blank value(s) are not plain literal data (constants may only be "
            f"literals - no calls, names, or attributes): {', '.join(sorted(non_literal))}"
        )
    out = source
    for name in sorted(new_values, key=lambda k: spans[k][0], reverse=True):
        start, end = spans[name]
        out = out[:start] + new_values[name].strip() + out[end:]
    ast.parse(out)  # reject any edit that breaks the file
    return out


def _normalize_blanks(blanks: dict | str) -> "dict[str, str] | str":
    """Accept blanks as a dict or a JSON string; return {name: rhs_src} or err."""
    if isinstance(blanks, str):
        s = blanks.strip()
        if not s:
            return {}
        try:
            blanks = json.loads(s)
        except json.JSONDecodeError:
            return 'ERROR: blanks must be an object like {"COLUMN_X0": "{...}"}.'
    if not isinstance(blanks, dict):
        return "ERROR: blanks must be an object {CONSTANT_NAME: new_value_source}."
    out: dict[str, str] = {}
    for k, v in blanks.items():
        out[str(k)] = v if isinstance(v, str) else repr(v)
    return out


# ---------------------------------------------------------------------------
# Tool cores (stdlib only)
# ---------------------------------------------------------------------------

def validate_parser_impl(parser_path: str) -> str:
    """AST-parse then ruff-check a parser. Returns OK or the problems."""
    p = Path(parser_path)
    if not p.is_file():
        return f"NOT FOUND: {parser_path}"
    src = p.read_text(encoding="utf-8")
    try:
        ast.parse(src)
    except SyntaxError as e:
        return f"SYNTAX ERROR: line {e.lineno}: {e.msg}"
    try:
        res = subprocess.run(
            [sys.executable, "-m", "ruff", "check", str(p)],
            capture_output=True, text=True, timeout=120,
        )
    except (OSError, subprocess.SubprocessError) as e:
        return f"OK: AST valid; could not run ruff ({e})."
    if res.returncode == 0:
        return "OK: AST valid; ruff clean."
    err = res.stderr or ""
    # Distinguish real lint findings (on stdout) from ruff failing to launch
    # (no module, blocked binary, or any crash -> a Python traceback on stderr).
    launch_failed = (
        "No module named ruff" in err
        or "Traceback (most recent call last)" in err
        or "Application Control policy" in err
        or "WinError" in err
    )
    if launch_failed or not (res.stdout or "").strip():
        return "OK: AST valid; ruff unavailable (lint skipped)."
    return "LINT FAILED:\n" + res.stdout.strip()


def extract_blanks_impl(parser_path: str) -> str:
    """List the editable blank constants of a parser with their current values."""
    p = Path(parser_path)
    if not p.is_file():
        return f"NOT FOUND: {parser_path}"
    src = p.read_text(encoding="utf-8")
    try:
        spans = constant_spans(src)
    except SyntaxError as e:
        return f"SYNTAX ERROR: line {e.lineno}: {e.msg}"
    if not spans:
        return "No editable blank constants found."
    lines = [f"{len(spans)} editable blank(s):"]
    for name, (start, end) in spans.items():
        value = src[start:end].replace("\n", " ")
        if len(value) > 100:
            value = value[:97] + "..."
        lines.append(f"  {name} = {value}")
    return "\n".join(lines)


def apply_template_edit_impl(parser_path: str, blanks: dict | str) -> str:
    """Rewrite named blank constants of an existing parser, then re-validate."""
    p = Path(parser_path)
    if not p.is_file():
        return f"NOT FOUND: {parser_path}"
    norm = _normalize_blanks(blanks)
    if isinstance(norm, str):
        return norm
    if not norm:
        return "No blanks supplied; parser unchanged."
    src = p.read_text(encoding="utf-8")
    try:
        new_src = rewrite_constants(src, norm)
    except KeyError as e:
        return f"ERROR: {e.args[0]}"
    except ValueError as e:
        return f"ERROR: {e.args[0]}"
    except SyntaxError as e:
        return f"ERROR: edit produced invalid Python: line {e.lineno}: {e.msg}"
    p.write_text(new_src, encoding="utf-8")
    return (
        f"Edited {len(norm)} blank(s) in {p.name}: {', '.join(sorted(norm))}.\n"
        + validate_parser_impl(str(p))
    )


def run_tieout_impl(parser_path: str, args: list | str, timeout: int = 180) -> str:
    """Run the parser and report its exit code (0 pass / 1 error / 2 mismatch)."""
    p = Path(parser_path)
    if not p.is_file():
        return f"NOT FOUND: {parser_path}"
    if isinstance(args, str):
        arglist = args.split()
    else:
        arglist = [str(a) for a in (args or [])]
    try:
        res = subprocess.run(
            [sys.executable, str(p), *arglist],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"TIMEOUT after {timeout}s running {p.name}."
    code = res.returncode
    label = {
        0: "TIE-OUT PASS",
        1: "ERROR (bad args / file / decryption)",
        2: "TIE-OUT MISMATCH",
    }.get(code, f"exit {code}")
    out = (res.stdout or "").strip()
    err = (res.stderr or "").strip()
    balance_lines = "\n".join(ln for ln in out.splitlines() if "balance" in ln.lower())
    body = balance_lines or out or "(no stdout)"
    tail = f"\nstderr: {err}" if err else ""
    return f"[{label}] exit={code}\n{body}{tail}"


def create_parser_from_template_impl(output_path: str, blanks: dict | str) -> str:
    """Fill blanks into the shipped skeleton and write a new parser."""
    norm = _normalize_blanks(blanks)
    if isinstance(norm, str):
        return norm
    norm_path = str(output_path).replace("\\", "/")
    if not _LOCATION_RE.search(norm_path):
        return (
            "ERROR: per the locked layout, parsers must be written to "
            "src/agents/skill_<X>/scripts/parse_<format>.py - got "
            f"{output_path!r}."
        )
    tmpl = TEMPLATE_PATH.read_text(encoding="utf-8")
    try:
        new_src = rewrite_constants(tmpl, norm) if norm else tmpl
    except KeyError as e:
        return f"ERROR: {e.args[0]}"
    except ValueError as e:
        return f"ERROR: {e.args[0]}"
    except SyntaxError as e:
        return f"ERROR: filled template is invalid Python: line {e.lineno}: {e.msg}"
    out = Path(output_path)
    if out.exists():
        return f"ERROR: refusing to overwrite existing file {output_path}."
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(new_src, encoding="utf-8")
    filled = ", ".join(sorted(norm)) if norm else "(none - template defaults)"
    return (
        f"Created {out.name} from template; filled blanks: {filled}.\n"
        f"NOTE: extract_rows / write_output bodies are stubs - implement them, "
        f"then run the tie-out gate.\n" + validate_parser_impl(str(out))
    )


# ---------------------------------------------------------------------------
# @tool wrappers (what the agent sees)
# ---------------------------------------------------------------------------

@tool
def validate_parser(parser_path: str) -> str:
    """Quality gate step 1+2: AST-parse and ruff-lint a parser file. Returns OK
    or the syntax/lint problems. Run this after every edit and before any
    tie-out."""
    return validate_parser_impl(parser_path)


@tool
def extract_blanks(parser_path: str) -> str:
    """List a parser's editable 'blank' constants (column x0 ranges, regexes,
    boilerplate/transfer markers) with their current values. Call this first to
    see exactly which blanks you may change - nothing else is editable."""
    return extract_blanks_impl(parser_path)


@tool
def apply_template_edit(parser_path: str, blanks: dict | str) -> str:
    """Rewrite one or more blank constants of an existing parser, then
    re-validate. `blanks` maps CONSTANT_NAME to the new value as Python source,
    e.g. {"COLUMN_X0": "{'debit': (410, 440), ...}"}. Only existing UPPER_CASE
    module constants can be changed; the oracle and exit contract are
    untouchable."""
    return apply_template_edit_impl(parser_path, blanks)


@tool
def run_tieout(parser_path: str, args: list | str) -> str:
    """Quality gate step 3: run the parser against its sample input and report
    the exit code - 0 (tie-out PASS), 1 (error), or 2 (closing balance
    MISMATCH). `args` are the parser's CLI arguments (e.g. the input and output
    paths). Success requires exit 0."""
    return run_tieout_impl(parser_path, args)


@tool
def create_parser_from_template(output_path: str, blanks: dict | str) -> str:
    """Create a brand-new parser by filling blanks into the shared skeleton.
    `output_path` must be src/agents/skill_<X>/scripts/parse_<format>.py.
    `blanks` maps the template's CONSTANT_NAMEs (FORMAT_NAME, DATE_RE,
    COLUMN_X0, BOILERPLATE_MARKERS, INTERNAL_TRANSFER_MARKER) to Python source.
    The oracle and exit contract come pre-baked; you still implement the
    extract_rows/write_output bodies afterward."""
    return create_parser_from_template_impl(output_path, blanks)
