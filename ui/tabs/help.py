"""
ui/tabs/help.py — central Help tab.

Thin wrapper over ui._help.render_help_tab, which builds the browsable guide
from each skill's help: block. Edit help text in the skill's skill.yaml.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from .. import _help

if TYPE_CHECKING:
    from agents.registry import SkillInfo


def render(skills: list["SkillInfo"]) -> None:
    _help.render_help_tab(skills)
