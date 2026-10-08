"""Structured hand-off of EXTRA output files from a skill to the UI (UI-24).

A skill that writes more than its main file (e.g. a GnuCash journal CSV)
declares them in skill.yaml under `output.extra_outputs` and returns its usual
reply text wrapped in ReplyWithOutputs, listing what THIS run wrote. The UI
offers only what is listed here; it never scans a folder for the latest file.
"""
from __future__ import annotations


class ReplyWithOutputs(str):
    """The skill's reply text (a plain str everywhere else) plus
    `extra_outputs`: a tuple of {"key", "path", "note"} dicts. `path` is the
    file this run wrote, or None when it wrote none (then `note` says why)."""

    extra_outputs: tuple
    withhold_primary: bool

    def __new__(cls, text: str, extra_outputs=(), withhold_primary: bool = False):
        obj = super().__new__(cls, text)
        obj.extra_outputs = tuple(extra_outputs)
        # TDS-15: True -> the UI must NOT offer the skill's main output file
        # (the run found a blocking problem, reported in `text`).
        obj.withhold_primary = bool(withhold_primary)
        return obj


def extra_output(key: str, path: str | None, note: str = "") -> dict:
    return {"key": key, "path": (str(path) if path else None), "note": note}
