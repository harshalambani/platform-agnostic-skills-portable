"""
tools.py — LangChain tools for the CSV Data Analyzer skill.

Provides safe, read-only pandas operations on a CSV file.
The LLM calls these tools via the ReAct agent loop.

SECURITY NOTE (2026-06-08, fixes Tracker finding #1 / MP-01):
This module previously exposed a `query_csv(csv_path, expression)` tool that
ran an LLM-authored string through `eval(expression, {"__builtins__": {}}, ...)`,
guarded only by a regex blocklist. That is not a sandbox — regex blocklists are
trivially bypassed (string concatenation, un-blocked dunders, object-graph
traversal to `os`/`subprocess`), and a malicious CSV could steer the LLM into
emitting an escape expression (indirect prompt injection -> RCE).

`query_csv` has been removed entirely. In its place this module exposes a small
set of *parameterized, allowlisted* operations. The LLM selects an operation by
name and supplies typed arguments (column names, an aggregation enum, a filter
list); Python validates every argument against the DataFrame's actual schema
and a closed enum, then calls pandas directly. No string is ever interpreted as
code, so no expression — regardless of what a hostile CSV convinces the LLM to
emit — can execute arbitrary code or touch the filesystem.
"""
from __future__ import annotations

from io import StringIO
from pathlib import Path
from typing import Any

import pandas as pd
from langchain_core.tools import tool


# ---------------------------------------------------------------------------
# Allowlists (closed enums — never extended from user/LLM input).
# ---------------------------------------------------------------------------

# Aggregation functions permitted in `aggregate_csv`.
_ALLOWED_AGGS = frozenset({
    "sum", "mean", "median", "min", "max", "count", "nunique", "std", "var",
})

# Comparison operators permitted in filter specs.
_ALLOWED_FILTER_OPS = frozenset({
    "==", "!=", ">", ">=", "<", "<=", "in", "notin", "contains",
})

# Output row cap (matches the previous query_csv behaviour).
_MAX_ROWS = 50


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _load_csv(csv_path: str) -> pd.DataFrame:
    """Load a CSV with sensible defaults."""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    return pd.read_csv(path, encoding="utf-8", encoding_errors="replace")


def _check_columns(df: pd.DataFrame, columns: list[str]) -> str | None:
    """Return an error string if any *columns* is not a real column of *df*."""
    unknown = [c for c in columns if c not in df.columns]
    if unknown:
        available = ", ".join(map(str, df.columns))
        return (
            f"ERROR: unknown column(s) {unknown!r}. "
            f"Available columns: {available}"
        )
    return None


def _coerce_value(series: pd.Series, value: Any) -> Any:
    """
    Best-effort coercion of a filter *value* (or list of values) to the dtype
    of *series*. Coercion is purely for comparison correctness — the value is
    passed straight to pandas, never interpolated into a string or expression.
    Falls back to the original value if coercion fails (pandas will then just
    compare across dtypes, which is safe, just possibly always-false).
    """
    def _coerce_one(v: Any) -> Any:
        try:
            if pd.api.types.is_datetime64_any_dtype(series):
                return pd.to_datetime(v)
            if pd.api.types.is_bool_dtype(series):
                if isinstance(v, str):
                    return v.strip().lower() in {"true", "1", "yes", "y"}
                return bool(v)
            if pd.api.types.is_integer_dtype(series):
                return int(v)
            if pd.api.types.is_float_dtype(series):
                return float(v)
        except (TypeError, ValueError):
            return v
        return v

    if isinstance(value, (list, tuple, set)):
        return [_coerce_one(v) for v in value]
    return _coerce_one(value)


def _apply_filters(df: pd.DataFrame, filters: list[dict] | None) -> tuple[pd.DataFrame | None, str | None]:
    """
    Validate and apply a list of filter specs to *df*.

    Each filter spec must be a mapping with keys:
      - "column": str — must be a real column of *df*
      - "op": one of _ALLOWED_FILTER_OPS
      - "value": the comparison value (scalar, or list for in/notin)

    Returns (filtered_df, None) on success, or (None, error_message) on
    validation failure. Never executes a string as code; every comparison is a
    direct pandas/Series operation chosen from a closed enum.
    """
    if not filters:
        return df, None

    mask = pd.Series(True, index=df.index)
    for i, spec in enumerate(filters):
        if not isinstance(spec, dict):
            return None, f"ERROR: filter #{i} must be an object with column/op/value, got {spec!r}"

        column = spec.get("column")
        op = spec.get("op")
        value = spec.get("value")

        if not isinstance(column, str):
            return None, f"ERROR: filter #{i} is missing a string 'column'"
        err = _check_columns(df, [column])
        if err:
            return None, err

        if op not in _ALLOWED_FILTER_OPS:
            return None, (
                f"ERROR: filter #{i} has unsupported op {op!r}. "
                f"Allowed ops: {sorted(_ALLOWED_FILTER_OPS)}"
            )

        series = df[column]
        coerced = _coerce_value(series, value)

        try:
            if op == "==":
                clause = series == coerced
            elif op == "!=":
                clause = series != coerced
            elif op == ">":
                clause = series > coerced
            elif op == ">=":
                clause = series >= coerced
            elif op == "<":
                clause = series < coerced
            elif op == "<=":
                clause = series <= coerced
            elif op == "in":
                values = coerced if isinstance(coerced, list) else [coerced]
                clause = series.isin(values)
            elif op == "notin":
                values = coerced if isinstance(coerced, list) else [coerced]
                clause = ~series.isin(values)
            elif op == "contains":
                clause = series.astype(str).str.contains(str(value), case=False, na=False, regex=False)
            else:  # pragma: no cover — guarded by the enum check above
                return None, f"ERROR: filter #{i} has unsupported op {op!r}"
        except Exception as e:
            return None, f"ERROR applying filter #{i} ({column} {op} {value!r}): {e}"

        mask &= clause

    return df[mask], None


def _format_frame(result: pd.DataFrame | pd.Series, *, noun: str = "rows") -> str:
    """Render a DataFrame/Series result as text, applying the row cap."""
    if isinstance(result, pd.DataFrame):
        if len(result) > _MAX_ROWS:
            return (
                f"Result has {len(result)} {noun}. Showing first {_MAX_ROWS}:\n\n"
                + result.head(_MAX_ROWS).to_string(index=True)
            )
        return result.to_string(index=True)
    elif isinstance(result, pd.Series):
        if len(result) > _MAX_ROWS:
            return (
                f"Result has {len(result)} {noun}. Showing first {_MAX_ROWS}:\n\n"
                + result.head(_MAX_ROWS).to_string()
            )
        return result.to_string()
    return str(result)


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

@tool
def describe_csv(csv_path: str) -> str:
    """
    Describe the structure and first rows of a CSV file.
    Returns: shape, column names with dtypes, and the first 5 rows.
    Call this first to understand the data before querying it.
    """
    try:
        df = _load_csv(csv_path)
    except Exception as e:
        return f"ERROR loading CSV: {e}"

    parts: list[str] = []
    parts.append(f"Shape: {df.shape[0]} rows x {df.shape[1]} columns\n")

    # Column info.
    buf = StringIO()
    buf.write("Columns:\n")
    for col in df.columns:
        non_null = df[col].notna().sum()
        buf.write(f"  - {col} ({df[col].dtype}, {non_null}/{len(df)} non-null)\n")
    parts.append(buf.getvalue())

    # First 5 rows.
    parts.append("First 5 rows:\n" + df.head(5).to_string(index=True))

    # Basic stats for numeric columns.
    numeric = df.select_dtypes(include="number")
    if not numeric.empty:
        parts.append("\nNumeric summary:\n" + numeric.describe().to_string())

    return "\n".join(parts)


@tool
def aggregate_csv(
    csv_path: str,
    metric: str,
    agg: str,
    group_by: list[str] | None = None,
    filters: list[dict] | None = None,
) -> str:
    """
    Aggregate a numeric column, optionally grouped and filtered.

    Args:
        csv_path: Path to the CSV file.
        metric: Column to aggregate (must be a real column).
        agg: One of: sum, mean, median, min, max, count, nunique, std, var.
        group_by: Optional list of column names to group by. Omit (or pass an
            empty list) to aggregate over the whole (filtered) dataset.
        filters: Optional list of filter objects, each like
            {"column": "region", "op": "==", "value": "EMEA"}.
            Supported ops: ==, !=, >, >=, <, <=, in, notin, contains.
            For "in"/"notin", "value" should be a list.

    Examples:
        - Total revenue by region:
          aggregate_csv(csv_path, metric="revenue", agg="sum", group_by=["region"])
        - Average age of active customers:
          aggregate_csv(csv_path, metric="age", agg="mean",
                        filters=[{"column": "status", "op": "==", "value": "active"}])

    Only the aggregations listed above are permitted; anything else is rejected
    with a clean error. No code is ever executed — every argument is validated
    against the file's real columns and a closed enum before pandas runs it.
    """
    try:
        df = _load_csv(csv_path)
    except Exception as e:
        return f"ERROR loading CSV: {e}"

    columns_to_check = [metric] + list(group_by or [])
    err = _check_columns(df, columns_to_check)
    if err:
        return err

    if agg not in _ALLOWED_AGGS:
        return f"ERROR: unsupported agg {agg!r}. Allowed: {sorted(_ALLOWED_AGGS)}"

    filtered, err = _apply_filters(df, filters)
    if err:
        return err

    try:
        if group_by:
            result = filtered.groupby(list(group_by))[metric].agg(agg)
        else:
            result = getattr(filtered[metric], agg)()
    except Exception as e:
        return f"ERROR computing aggregate: {e}"

    if isinstance(result, (pd.Series, pd.DataFrame)):
        return _format_frame(result, noun="groups")
    return str(result)


@tool
def value_counts_csv(csv_path: str, column: str, top_n: int = 20) -> str:
    """
    Count how often each distinct value appears in a column, most frequent first.

    Args:
        csv_path: Path to the CSV file.
        column: Column to count values of (must be a real column).
        top_n: Maximum number of distinct values to return (default 20, capped at 50).

    Example: "What are the top 5 product categories by frequency?" ->
        value_counts_csv(csv_path, column="category", top_n=5)
    """
    try:
        df = _load_csv(csv_path)
    except Exception as e:
        return f"ERROR loading CSV: {e}"

    err = _check_columns(df, [column])
    if err:
        return err

    if not isinstance(top_n, int) or top_n <= 0:
        return "ERROR: top_n must be a positive integer."
    n = min(top_n, _MAX_ROWS)

    counts = df[column].value_counts()
    total = len(counts)
    shown = counts.head(n)
    header = f"{total} distinct value(s) in '{column}'. Showing top {len(shown)}:\n\n"
    return header + shown.to_string()


@tool
def filter_count_csv(csv_path: str, filters: list[dict]) -> str:
    """
    Count how many rows match a set of filter conditions.

    Args:
        csv_path: Path to the CSV file.
        filters: List of filter objects, each like
            {"column": "age", "op": ">", "value": 30}.
            Supported ops: ==, !=, >, >=, <, <=, in, notin, contains.
            For "in"/"notin", "value" should be a list. Multiple filters are
            combined with AND.

    Example: "How many customers are over 30 in the EU?" ->
        filter_count_csv(csv_path, filters=[
            {"column": "age", "op": ">", "value": 30},
            {"column": "region", "op": "==", "value": "EU"},
        ])
    """
    try:
        df = _load_csv(csv_path)
    except Exception as e:
        return f"ERROR loading CSV: {e}"

    if not filters:
        return "ERROR: filters must be a non-empty list of {column, op, value} objects."

    filtered, err = _apply_filters(df, filters)
    if err:
        return err

    return f"{len(filtered)} of {len(df)} rows match the given filter(s)."


@tool
def sort_head_csv(
    csv_path: str,
    sort_by: list[str],
    n: int = 10,
    ascending: bool = True,
    columns: list[str] | None = None,
    filters: list[dict] | None = None,
) -> str:
    """
    Sort rows by one or more columns and return the first N.

    Args:
        csv_path: Path to the CSV file.
        sort_by: Column name(s) to sort by (must be real columns).
        n: Number of rows to return (default 10, capped at 50).
        ascending: Sort ascending if True (default), descending if False.
        columns: Optional subset of columns to include in the output.
            Omit to include all columns.
        filters: Optional list of filter objects (see filter_count_csv) applied
            before sorting.

    Example: "Show the 5 highest-revenue deals in EMEA" ->
        sort_head_csv(csv_path, sort_by=["revenue"], n=5, ascending=False,
                      filters=[{"column": "region", "op": "==", "value": "EMEA"}])
    """
    try:
        df = _load_csv(csv_path)
    except Exception as e:
        return f"ERROR loading CSV: {e}"

    columns_to_check = list(sort_by) + list(columns or [])
    err = _check_columns(df, columns_to_check)
    if err:
        return err

    if not isinstance(n, int) or n <= 0:
        return "ERROR: n must be a positive integer."
    n = min(n, _MAX_ROWS)

    filtered, err = _apply_filters(df, filters)
    if err:
        return err

    try:
        result = filtered.sort_values(by=list(sort_by), ascending=ascending)
    except Exception as e:
        return f"ERROR sorting: {e}"

    if columns:
        result = result[list(columns)]

    return _format_frame(result.head(n), noun="rows")
