"""
tools.py — LangChain tools for the CSV Data Analyzer skill.

Provides safe, read-only pandas operations on a CSV file.
The LLM calls these tools via the ReAct agent loop.
"""
from __future__ import annotations

import re
from io import StringIO
from pathlib import Path

import pandas as pd
from langchain_core.tools import tool


# ---------------------------------------------------------------------------
# Safety: allowlisted pandas method calls.
# ---------------------------------------------------------------------------

# Methods that are safe to call on a DataFrame / Series.
_ALLOWED_METHODS = frozenset({
    # Inspection
    "head", "tail", "sample", "info", "describe", "dtypes", "shape",
    "columns", "index", "values", "nunique", "value_counts", "unique",
    "count", "len",
    # Selection / filtering
    "loc", "iloc", "at", "iat", "where", "mask", "query", "isin",
    "between", "nlargest", "nsmallest", "idxmax", "idxmin",
    # Aggregation
    "sum", "mean", "median", "std", "var", "min", "max", "mode",
    "quantile", "cumsum", "cumprod", "cummin", "cummax", "pct_change",
    "corr", "cov", "agg", "aggregate", "apply",
    # Grouping / pivoting
    "groupby", "pivot_table", "crosstab", "melt", "stack", "unstack",
    # Sorting / ranking
    "sort_values", "sort_index", "rank", "nlargest", "nsmallest",
    # String ops
    "str",
    # Date ops
    "dt",
    # Reshaping
    "drop_duplicates", "reset_index", "set_index", "rename",
    "fillna", "dropna", "replace", "astype",
    # Display
    "to_string", "to_markdown", "to_dict", "to_list",
})

# Patterns that are never allowed — prevent code injection.
_BLOCKED_PATTERNS = re.compile(
    r"\b(exec|eval|compile|__import__|import\s|"
    r"open\s*\(|os\.|sys\.|subprocess\.|shutil\.|"
    r"globals|locals|getattr|setattr|delattr|"
    r"__class__|__bases__|__subclasses__|"
    r"to_csv|to_excel|to_parquet|to_sql|to_hdf|to_feather|"
    r"to_clipboard|to_json\s*\(|to_pickle|"
    r"read_|from_)",
    re.IGNORECASE,
)


def _validate_expression(expr: str) -> str | None:
    """
    Return an error message if *expr* is unsafe, else None.
    """
    if _BLOCKED_PATTERNS.search(expr):
        match = _BLOCKED_PATTERNS.search(expr)
        return (
            f"Blocked: expression contains disallowed pattern '{match.group()}'. "
            "Only read-only pandas operations are permitted."
        )
    return None


def _load_csv(csv_path: str) -> pd.DataFrame:
    """Load a CSV with sensible defaults."""
    path = Path(csv_path)
    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {csv_path}")
    return pd.read_csv(path, encoding="utf-8", encoding_errors="replace")


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
def query_csv(csv_path: str, expression: str) -> str:
    """
    Run a read-only pandas expression on the CSV and return the result.

    The DataFrame is available as `df`. Write a valid Python expression
    that uses `df` — for example:
      - df.groupby('region')['revenue'].sum()
      - df[df['age'] > 30].shape[0]
      - df.describe()
      - df['status'].value_counts()

    Only read-only operations are allowed. Do not use exec, eval, import,
    open, os, sys, or any write methods (to_csv, to_excel, etc.).
    """
    # Safety check.
    err = _validate_expression(expression)
    if err:
        return err

    try:
        df = _load_csv(csv_path)
    except Exception as e:
        return f"ERROR loading CSV: {e}"

    # Execute the expression in a restricted namespace.
    namespace = {"df": df, "pd": pd}
    try:
        result = eval(expression, {"__builtins__": {}}, namespace)  # noqa: S307
    except Exception as e:
        return f"ERROR evaluating expression: {e}\nExpression was: {expression}"

    # Format the result.
    if isinstance(result, pd.DataFrame):
        if len(result) > 50:
            return (
                f"Result has {len(result)} rows. Showing first 50:\n\n"
                + result.head(50).to_string(index=True)
            )
        return result.to_string(index=True)
    elif isinstance(result, pd.Series):
        if len(result) > 50:
            return (
                f"Result has {len(result)} entries. Showing first 50:\n\n"
                + result.head(50).to_string()
            )
        return result.to_string()
    else:
        return str(result)
