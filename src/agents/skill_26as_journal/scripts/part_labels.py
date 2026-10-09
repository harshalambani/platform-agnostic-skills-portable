"""TDS-16: the one spelling of the Part II label used on the 26AS screens.

Part II of a 26AS is the 15G / 15H section. Every user-facing message,
button label and journal tag that names it reads PART_II_LABEL, so the
wording cannot drift between the journal builder and the review screen.
This file has no imports, so the builder subprocess and the UI can both
load it.
"""
PART_II_LABEL = "Part II (15G/15H)"
