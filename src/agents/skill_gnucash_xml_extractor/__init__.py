"""GnuCash XML Extractor — Extract description→account mappings."""

from .agent import parse_gnucash_file, run

__all__ = ['parse_gnucash_file', 'run']
