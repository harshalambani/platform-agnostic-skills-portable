"""PIPE-12: where the local real-corpus tests find their report/book pairs.

The pairs are NOT kept in the repo. Set PASK_REAL_CORPUS_CONFIG to a small local file (keep it
outside the repo). Format, one setting per line, '#' starts a comment, blank lines are ignored:

    # report.html | book.gnucash   (relative paths are taken from the folder holding this file)
    pair = Example2425.html | Example2425.gnucash
    pair = Sample2425.html | Sample2425.gnucash
    richest = Example2425.gnucash

`pair` may repeat. `richest` (at most one) names the book used by the lot-reconstruction test.
Unset or missing file -> the real-corpus tests skip. A malformed line raises ConfigError (a failure,
never a pass).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

ENV_VAR = "PASK_REAL_CORPUS_CONFIG"


class ConfigError(ValueError):
    pass


@dataclass
class RealCorpus:
    pairs: list = field(default_factory=list)       # [(report Path, book Path)]
    richest: "Path | None" = None


def parse_text(text: str, base: Path) -> RealCorpus:
    out = RealCorpus()
    for n, raw in enumerate(text.splitlines(), 1):
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        key, sep, val = line.partition("=")
        key, val = key.strip().lower(), val.strip()
        if not sep or not val:
            raise ConfigError(f"line {n}: expected 'pair = report | book' or 'richest = book'")
        if key == "pair":
            parts = [p.strip() for p in val.split("|")]
            if len(parts) != 2 or not all(parts):
                raise ConfigError(f"line {n}: a pair needs exactly 'report | book'")
            out.pairs.append((base / parts[0], base / parts[1]))
        elif key == "richest":
            if out.richest is not None:
                raise ConfigError(f"line {n}: 'richest' given twice")
            out.richest = base / val
        else:
            raise ConfigError(f"line {n}: unknown setting {key!r}")
    return out


def load(environ=None):
    """(RealCorpus, "") or (None, skip reason). Raises ConfigError for a malformed file."""
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_VAR) or "").strip()
    if not raw:
        return None, f"{ENV_VAR} is not set -- real-corpus test skipped"
    path = Path(raw)
    if not path.is_file():
        return None, f"{ENV_VAR} points at a file that does not exist ({path.name}) -- real-corpus test skipped"
    return parse_text(path.read_text(encoding="utf-8"), path.parent), ""


def missing_files(paths) -> list:
    return [p.name for p in paths if not p.is_file()]


def need_pairs(environ=None):
    """(pairs, "") when every listed file exists, else (None, skip reason naming what is missing or empty)."""
    cfg, why = load(environ)
    if cfg is None:
        return None, why
    if not cfg.pairs:
        return None, "the real-corpus config lists no 'pair' lines -- real-corpus test skipped"
    gone = missing_files([p for pr in cfg.pairs for p in pr])
    if gone:
        return None, "listed file(s) missing: " + ", ".join(gone) + " -- real-corpus test skipped"
    return cfg.pairs, ""


def need_richest(environ=None):
    cfg, why = load(environ)
    if cfg is None:
        return None, why
    if cfg.richest is None:
        return None, "the real-corpus config has no 'richest' line -- real-corpus test skipped"
    gone = missing_files([cfg.richest])
    if gone:
        return None, "listed file(s) missing: " + ", ".join(gone) + " -- real-corpus test skipped"
    return cfg.richest, ""
