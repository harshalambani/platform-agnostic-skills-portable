"""
Dev CLI for the (UI-hidden) Parser Generator skill.

    python -m agents.skill_parser_generator blanks <parser.py>
    python -m agents.skill_parser_generator gate   <parser.py> -- <parser args...>
    python -m agents.skill_parser_generator fix    <parser.py> --note "..." -- <args...>
    python -m agents.skill_parser_generator create <out.py>    --format NAME

`blanks` and `gate` are deterministic (no LLM): `gate` runs the quality gate
(validate + tie-out) and exits with the parser's own tie-out code, so it slots
into scripts. `fix` and `create` invoke the LLM agent (need a configured model).
"""
from __future__ import annotations

import argparse
import sys

from agents.skill_parser_generator.tools import (
    run_tieout_impl,
    validate_parser_impl,
    extract_blanks_impl,
)


def _cmd_blanks(args: argparse.Namespace) -> int:
    print(extract_blanks_impl(args.parser))
    return 0


def _cmd_gate(args: argparse.Namespace) -> int:
    print("== validate ==")
    v = validate_parser_impl(args.parser)
    print(v)
    if v.startswith(("SYNTAX ERROR", "LINT FAILED", "NOT FOUND")):
        return 1
    print("== tie-out ==")
    t = run_tieout_impl(args.parser, args.args)
    print(t)
    if "exit=0" in t:
        return 0
    if "exit=2" in t:
        return 2
    return 1


def _cmd_fix(args: argparse.Namespace) -> int:
    from agents.skill_parser_generator.agent import run

    arg_str = " ".join(args.args)
    note = f" {args.note}" if args.note else ""
    instruction = (
        f"The parser {args.parser} failed its tie-out (exit 2).{note} "
        f"Inspect its blanks, fix only the wrong blank constant(s), then re-run "
        f"the tie-out with args: {arg_str}. Report the gate result and the diff; "
        f"do not claim success unless run_tieout reports exit 0. Do not commit."
    )
    print(run(instruction, config_path=args.config, model_override=args.model))
    return 0


def _cmd_create(args: argparse.Namespace) -> int:
    from agents.skill_parser_generator.agent import run

    instruction = (
        f"Create a new parser for the {args.format} statement at {args.out} "
        f"using create_parser_from_template (fill FORMAT_NAME and any blanks you "
        f"can infer), then validate it. Remind the developer that the "
        f"extract_rows/write_output bodies still need implementing. Do not commit."
    )
    print(run(instruction, config_path=args.config, model_override=args.model))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m agents.skill_parser_generator")
    sub = p.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("blanks", help="list a parser's editable blank constants")
    b.add_argument("parser")
    b.set_defaults(func=_cmd_blanks)

    g = sub.add_parser("gate", help="run the quality gate (validate + tie-out)")
    g.add_argument("parser")
    g.add_argument("args", nargs=argparse.REMAINDER, help="parser CLI args after --")
    g.set_defaults(func=_cmd_gate)

    f = sub.add_parser("fix", help="LLM: fix a parser that failed tie-out")
    f.add_argument("parser")
    f.add_argument("--note", default="", help="extra context (symptom, target)")
    f.add_argument("--config", default="config.yaml")
    f.add_argument("--model", default=None)
    f.add_argument("args", nargs=argparse.REMAINDER, help="parser CLI args after --")
    f.set_defaults(func=_cmd_fix)

    c = sub.add_parser("create", help="LLM: create a new parser from the template")
    c.add_argument("out")
    c.add_argument("--format", required=True, help="format name, e.g. icici_savings")
    c.add_argument("--config", default="config.yaml")
    c.add_argument("--model", default=None)
    c.set_defaults(func=_cmd_create)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # argparse.REMAINDER keeps a leading "--"; drop it.
    if getattr(args, "args", None) and args.args and args.args[0] == "--":
        args.args = args.args[1:]
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
