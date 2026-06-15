"""GnuCash Mapping Generator — Create YAML rules from extractor output."""

from .agent import generate_rules, generate_yaml, main

__all__ = ['generate_rules', 'generate_yaml', 'main']
