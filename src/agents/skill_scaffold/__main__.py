"""
Dev CLI for the Skill Scaffolder.

    python -m agents.skill_scaffold --skill-dir skill_foo --display-name "Foo" \\
        --category dev --mode direct --output-type file --needs-llm no

Deterministic: no LLM, no network. Refuses (non-zero exit) rather than
overwriting an existing skill directory or accepting an invalid name. See
skill.yaml's help: block for the full field list. Source-checkout-only --
exits 2 under a frozen build, before touching the filesystem.
"""
from __future__ import annotations

import argparse
import sys

from agents import registry
from agents.skill_scaffold import scaffold as _engine


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m agents.skill_scaffold")
    p.add_argument("--skill-dir", required=True, help="new package dir name, e.g. skill_foo")
    p.add_argument("--display-name", required=True, help="UI tab / manifest display_name")
    p.add_argument("--category", default="dev", choices=list(_engine.CATEGORIES))
    p.add_argument("--mode", default="direct", choices=list(_engine.MODES))
    p.add_argument("--output-type", default="file", choices=list(_engine.OUTPUT_TYPES))
    p.add_argument("--needs-llm", default="no", choices=["no", "yes"])
    p.add_argument(
        "--skip-gen-docs",
        action="store_true",
        help="don't run scripts/gen_docs.py after scaffolding",
    )
    return p


def main(argv: list[str] | None = None) -> int:
    if _engine.is_frozen():
        print(_engine.FROZEN_MESSAGE, file=sys.stderr)
        return 2

    args = build_parser().parse_args(argv)

    result = _engine.scaffold(
        skill_dir=args.skill_dir.strip(),
        display_name=args.display_name.strip(),
        category=args.category,
        mode=args.mode,
        output_type=args.output_type,
        needs_llm=(args.needs_llm == "yes"),
        agents_root=registry._AGENTS_ROOT,
        tests_root=_engine.DEFAULT_TESTS_ROOT,
    )

    print(result.message)
    if not result.ok:
        return 1

    for f in result.created_files:
        print(f"  - {f}")

    if not args.skip_gen_docs:
        rc = _engine.run_gen_docs(_engine.PROJECT_ROOT)
        print(f"gen_docs.py exit code: {rc}")

    print(
        "The new tab will only appear after the app is restarted -- the "
        "skill registry caches its scan for the life of the process."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
