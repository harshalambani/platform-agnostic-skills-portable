"""No module in src/ may call sys.exit() at import time.

Why this test exists
--------------------
`src/agents/skill_bob/scripts/extract_bob_statement.py` guarded its optional
`pdfplumber` dependency with a module-level `sys.exit(2)`. Locally that was
invisible -- the dev venv has pdfplumber. On CI, which installs only
`pytest pandas numpy pyyaml`, pytest imported the module during collection,
the `sys.exit` raised SystemExit, and pytest turned that into an
INTERNALERROR that aborted the ENTIRE session. Zero tests ran.

The job carried `continue-on-error: true`, so CI reported a green check over a
suite that had collected nothing. That went unnoticed long enough for nine
dependency bumps to land on main with no test coverage at all.

A missing optional dependency should fail the one command that needs it, not
the interpreter that imported it. Check for the dependency inside `main()`.

`sys.exit()` under `if __name__ == "__main__":` is fine -- that block does not
run on import.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"

# Hand-run scripts that legitimately execute top to bottom and exit on a failed
# pre-check. Safe only because pytest never imports them -- their names do not
# match `python_files` in pyproject.toml. Add to this set with care: the moment
# such a file is named test_*.py, it takes the whole suite down.
SCRIPT_ALLOWLIST = {
    "run_4c_e2e.py",
}


def _is_main_guard(node: ast.stmt) -> bool:
    """True for `if __name__ == "__main__":` -- its body never runs on import."""
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (
        isinstance(test, ast.Compare)
        and isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.ops) == 1
        and isinstance(test.ops[0], ast.Eq)
    )


def _is_exit_call(node: ast.AST) -> bool:
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name) and func.id in {"exit", "quit"}:
        return True
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "exit"
        and isinstance(func.value, ast.Name)
        and func.value.id == "sys"
    )


def _import_time_exits(tree: ast.Module) -> list[int]:
    """Line numbers of exit calls reachable during a plain `import`.

    Walks only statements that execute at module level: function and class
    bodies are skipped (they run when called, not when imported), as is the
    `__main__` guard.
    """
    hits: list[int] = []

    def walk(body: list[ast.stmt]) -> None:
        for node in body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            if _is_main_guard(node):
                continue
            for sub in ast.walk(node):
                if _is_exit_call(sub):
                    hits.append(sub.lineno)
            # ast.walk above already covers nested bodies of try/if/with/for.
    walk(tree.body)
    return hits


def _python_files() -> list[Path]:
    return sorted(
        p
        for p in SRC.rglob("*.py")
        if "__pycache__" not in p.parts and p.name not in SCRIPT_ALLOWLIST
    )


@pytest.mark.parametrize("path", _python_files(), ids=lambda p: str(p.relative_to(SRC)))
def test_no_exit_at_import_time(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    hits = _import_time_exits(tree)
    assert not hits, (
        f"{path.relative_to(SRC)} calls sys.exit()/exit() at module level "
        f"(line(s) {', '.join(str(n) for n in hits)}).\n"
        "Importing a module must never terminate the interpreter -- pytest "
        "imports these during collection, and a SystemExit there is an "
        "INTERNALERROR that aborts the whole test session.\n"
        "Move the check into main(), or into a helper main() calls."
    )
