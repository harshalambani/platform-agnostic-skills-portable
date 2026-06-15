"""Bank of Baroda CSV → Canonical Schema Adapter."""

from .agent import BoBAdapterAgent, adapt_bob_csv

__all__ = ['BoBAdapterAgent', 'adapt_bob_csv']
