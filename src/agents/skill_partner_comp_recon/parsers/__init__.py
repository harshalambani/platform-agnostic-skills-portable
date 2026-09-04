"""
agents.skill_partner_comp_recon.parsers -- Stage 2, one placeholder left.

Every module in this package exposes a single function:

    parse(path: str, password: str | None = None) -> dict

`payout_advice.py` (L1, the monthly partner payout certificate),
`advisory.py` (L3, the annual Compensation Advisory letter), and
`llp_statement.py` (L5, the LLP Statement of Account) are implemented,
each split into a PURE `parse_l1_text()`/`parse_l3_text()`/
`parse_l5_words()` core plus a thin pdfplumber-opening `parse()` shell --
see their own module docstrings. `payment_schedule.py` remains a guarded
placeholder: it raises NotImplementedError naming the real specimen this
skill needs before that parser can be written -- see its module
docstring and AGENT.md's "Stage 2" section. This is deliberate: writing
regex/layout logic against an invented, non-PII fixture would produce
code that is confidently wrong against the real document, since these
are free-form PDFs with no public schema. Until a real specimen exists
for it, agent.py's document-driven entry path (_run_from_documents)
accepts its documents but degrades the leg it would back to an explicit
"not available" note; its TEST-ONLY structured-input path
(_run_from_structured_input / input_path) accepts the same data as a
structured YAML/JSON input instead -- see skill.yaml and
engine.build_report().

Design notes that MUST survive into whichever real parser eventually gets
written here (get these wrong and the numbers are silently corrupted, not
loudly missing):

  - MAP BY LABEL, NEVER ROW POSITION. These documents are hand-formatted;
    a row's position shifts between issues of the "same" document. An
    absent label means that figure is ABSENT for this document -- never
    coerce it to zero.
  - DISPATCH ON DOCUMENT CONTENT, NEVER FILENAME. The Advisory letter and
    the monthly payout advices are frequently delivered in the same
    directory, and filenames carry no reliable convention (arbitrary/
    meaningless suffixes at some firms) -- identify which parser a given
    PDF needs by sniffing its extracted text/layout, not its name.
  - THE ROW SET CHANGES BETWEEN YEARS. A given label is not guaranteed to
    exist in every year's document -- e.g. a "TDS on Remuneration" row
    exists only from FY2025-26 onward (s.194T). Absent-this-year is a
    normal, expected state, not a parse failure.
  - ON THE PAYOUT ADVICE (L1), INTEREST ON CAPITAL HAS NO SEPARATE LABEL.
    It is delivered INSIDE the "Add. Share of Profit" row's figure -- do
    not look for (or invent) a standalone "Interest on Capital" line
    there. This is L1-specific: on the LLP Statement of Account (L5),
    Interest on Capital IS its own labelled row, printed in the CAPITAL
    ACCOUNT column (not the current account) -- the two documents are not
    consistent with each other on this point, so do not generalise
    either convention across parsers.
  - NEGATIVES ARE PARENTHESISED: "(30,000)" means -30000. Thousands
    separators are commas. Parse both conventions before coercing to a
    number.
  - "#N/A" IS A TEMPLATE ARTEFACT, NEVER A VALUE. Skip cells containing it
    outright -- do not parse it as NaN, zero, or any numeric sentinel.
  - NEVER READ "Misc Adjustments" INTO THE MODEL. Its sign is inconsistent
    between months (see payout_advice.py below) -- let engine.derive_misc()
    compute it and reconcile the computed figure against the document's
    own printed value; the printed figure is the CHECK, not an input.
  - NO RATE IS EVER HARDCODED HERE. The firm's tax rate, the capital-
    contribution rate, and every other rate/period this skill uses belong
    in the entity/tax-rules config keyed by financial year (mirroring
    tax_rules.common.filing_due_dates), because they change -- occasionally
    mid-year. A rate absent from that config must be fail-loud (an
    explicit "cannot reconcile"/ERROR naming what's missing), never a
    silently-assumed default.
"""
