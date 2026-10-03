"""
skill_sbm/password_rule.py -- BNK-05: how the SBM statement password is derived
from the Entities record when the password box is left empty.

THE RULE (one place, change it here and nowhere else):

    first four letters of the entity's FIRST NAME, in capitals,
    followed by the date of birth as DDMM.

    e.g. name "Jane Q Example", dob 1990-03-07  ->  "JANE0703"   (synthetic)

A bank skill opts in by having a ``password_rule`` module next to it that
exposes ``RULE_SUMMARY`` (plain-English, safe to show) and
``derive_password(profile)``. A bank without that module declares no rule and
the pipeline behaves exactly as before. The derived password is NEVER logged,
written to a sidecar or output, or shown in the UI: callers pass it straight to
``parse()`` and drop it.
"""
from __future__ import annotations

import datetime as _dt
import re

RULE_SUMMARY = "first 4 letters of the first name in capitals + date of birth as DDMM"


class PasswordRuleError(ValueError):
    """The Entities record lacks what the rule needs. The message names the
    missing field only, never any value."""


def _first_name_letters(name: str) -> str:
    first = (name or "").strip().split()
    letters = re.sub(r"[^A-Za-z]", "", first[0]) if first else ""
    if len(letters) < 4:
        raise PasswordRuleError(
            "the entity's first name has fewer than 4 letters, so the statement "
            "password cannot be derived -- type it in the password box")
    return letters[:4].upper()


def _ddmm(dob) -> str:
    if isinstance(dob, (_dt.date, _dt.datetime)):
        return dob.strftime("%d%m")
    text = str(dob or "").strip()
    try:
        return _dt.date.fromisoformat(text[:10]).strftime("%d%m")
    except ValueError:
        raise PasswordRuleError(
            "the entity has no valid date of birth (YYYY-MM-DD) in the Entities "
            "record, so the statement password cannot be derived -- type it in "
            "the password box") from None


def derive_password(profile) -> str:
    """The SBM statement password for `profile` (an EntityProfile-like object
    with ``name`` and ``dob``). Raises PasswordRuleError, never returns ""."""
    return _first_name_letters(getattr(profile, "name", "")) + _ddmm(getattr(profile, "dob", None))
